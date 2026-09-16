"""One owned browser worker, bounded model workers, and durable per-customer jobs."""
from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from cs_duty.browser import BrowserAdapter, BrowserNotReady, comparable_text
from cs_duty.models import ModelClient, ModelError
from cs_duty.notifications import notify_task
from cs_duty.workflow import Workflow


class Runtime:
    def __init__(self, db, settings, knowledge, adapter_factory=BrowserAdapter, model_factory=ModelClient):
        self.db, self.settings, self.knowledge = db, settings, knowledge
        self.adapter_factory, self.model_factory = adapter_factory, model_factory
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.state, self.detail = 'stopped', '尚未启动'
        self.processed = 0
        self.generation = 0
        self.baseline_count = 0
        self.drafted = {}
        self.notify = lambda task: notify_task(self.db, self.settings, task)

    def status(self):
        with self.lock:
            return {'state': self.state, 'detail': self.detail, 'processed': self.processed,
                    'running': bool(self.thread and self.thread.is_alive()), 'baseline_count': self.baseline_count}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                if self.state == 'paused':
                    self.state, self.detail = 'running', '继续处理会话'
                    return
                raise ValueError('已有运行实例，请先停止或继续当前实例')
            self.settings.profile()
            self.stop_event.clear()
            self.generation += 1
            self.state, self.detail = 'starting', '正在启动浏览器'
            self.db.event('runtime', '开始接待，正在连接网页')
            self.thread = threading.Thread(target=self._run, name='cs-duty-browser', daemon=False)
            self.thread.start()

    def pause(self):
        with self.lock:
            if self.state not in ('running', 'waiting_login'):
                raise ValueError('当前没有可暂停的任务')
            self.state, self.detail = 'paused', '已暂停，正在生成的方案会保存，暂停期间不发送'
            self.db.event('runtime', '接待已暂停')

    def stop(self, wait=False):
        with self.lock:
            self.stop_event.set()
            self.generation += 1
            if self.thread and self.thread.is_alive():
                self.state, self.detail = 'stopping', '正在结束请求并关闭浏览器'
            thread = self.thread
        if wait and thread:
            thread.join()

    def _run(self):
        config = self.settings.runtime()
        adapter = self.adapter_factory(config, self.db.path.parent)
        adapter.cancelled = self.stop_event.is_set
        started_at = time.time()
        baselined = set()
        self.baseline_count = 0
        self.drafted = {}
        checkpoint_conn = sqlite3.connect(self.db.path.parent / 'checkpoints.sqlite3', check_same_thread=False)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='cs-duty-model')
        graph = Workflow(self.db, self.settings, self.knowledge, SqliteSaver(checkpoint_conn), self.model_factory).graph
        pending, arrivals = {}, {}
        fatal_error = False
        try:
            adapter.start()
            with self.lock:
                if not self.stop_event.is_set():
                    self.state, self.detail = 'running', '正在通过网页读取消息'
            while not self.stop_event.is_set():
                for cid, job in list(pending.items()):
                    future, task_id = job
                    if not future.done():
                        continue
                    del pending[cid]
                    try:
                        future.result()
                        self.processed += 1
                        if task_id:
                            self.db.execute("UPDATE tasks SET status='resolved',updated=? WHERE id=?", (time.time(), task_id))
                            # If newer customer input invalidated the resumed reply, schedule it again.
                            with self.db.lock:
                                current = self.db.conversation(cid)
                                if current['state'] == 'waiting' and current['latest_id'] != current['handled_id']:
                                    self.db.set_state(cid, 'active')
                    except Exception as exc:
                        # Exception payloads never include prompts, credentials, or raw HTTP bodies.
                        message = str(exc) if isinstance(exc, ModelError) else '流程执行失败，请检查模型配置或会话状态'
                        self.db.event('error', message)
                        failures = self.db.conversation(cid)['failures'] + 1
                        self.db.execute('UPDATE conversations SET failures=?,retry_after=? WHERE id=?', (failures, time.time() + min(300, 15 * 2 ** min(failures, 4)), cid))
                        with self.lock:
                            self.detail = message
                if self.state == 'paused':
                    self.stop_event.wait(.25)
                    continue
                try:
                    customers = adapter.customers()
                except Exception as exc:
                    with self.lock:
                        if self.state != 'paused':
                            state = 'waiting_login' if isinstance(exc, BrowserNotReady) else 'error'
                            detail = str(exc) if isinstance(exc, BrowserNotReady) else f'客服列表读取失败（{type(exc).__name__}），请检查页面遮挡或页面结构变化'
                            if (self.state, self.detail) != (state, detail):
                                self.db.event('browser', detail)
                            self.state, self.detail = state, detail
                    self.stop_event.wait(2)
                    continue
                with self.lock:
                    if self.state in ('waiting_login', 'error'):
                        self.state, self.detail = 'running', '客服页面已连接'
                for customer in customers:
                    if self.stop_event.is_set() or self.state == 'paused':
                        break
                    try:
                        prior = self.db.one('SELECT * FROM conversations WHERE platform=? AND shop=? AND customer_key=?',
                            (config['transport'], config['shop'], customer['customer_key']))
                        initial = config['transport'] != 'mock' and customer.get('initial_history', False) and customer['customer_key'] not in baselined
                        ready_task = prior and self.db.one("SELECT id FROM tasks WHERE conversation_id=? AND status='ready' LIMIT 1", (prior['id'],))
                        needs_plan = prior and prior['latest_id'] != prior['handled_id'] and prior['state'] in ('active', 'collecting') and prior['retry_after'] <= time.time() and prior['id'] not in pending
                        if hasattr(adapter, 'should_read') and not adapter.should_read(customer, force=bool(initial or ready_task or needs_plan)):
                            continue
                        messages = adapter.open_customer(customer['name'], customer['customer_key'])
                        old_ids = {m['id'] for m in self.db.history(prior['id'], 500)} if prior else set()
                        cid, changed = self.db.ingest(config['transport'], config['shop'], customer['customer_key'], customer['name'], messages)
                        messages = self.db.filter_deleted(cid, messages)
                        if not self.db.one('SELECT id FROM conversations WHERE id=?', (cid,)):
                            continue
                        if hasattr(adapter, 'mark_read_snapshot'):
                            adapter.mark_read_snapshot(customer)
                        if initial:
                            baselined.add(customer['customer_key'])
                            if self.baseline_history(cid, messages, started_at, '留言' in customer.get('group', '')):
                                with self.lock:
                                    self.baseline_count += 1
                                    self.detail = f'已初始化 {self.baseline_count} 个历史会话，仅处理新消息；无变化会话每 60 秒复核'
                                continue
                        if prior and not initial:
                            self._detect_manual_sends(cid, messages, old_ids)
                        current = self.db.conversation(cid)
                        if changed or cid not in arrivals:
                            arrivals[cid] = time.monotonic()
                        if cid in pending or current['state'] == 'human' or current['retry_after'] > time.time():
                            continue
                        task = self.db.one("SELECT * FROM tasks WHERE conversation_id=? AND status='ready' ORDER BY created LIMIT 1", (cid,))
                        thread_config = {'configurable': {'thread_id': cid}}
                        leave_message = '留言' in customer.get('group', '')
                        if task:
                            snapshot = graph.get_state(thread_config)
                            if snapshot.next:
                                value = Command(resume={'result': task['result'], 'messages': self.db.history(cid), 'source_id': current['latest_id']}) if 'wait_colleague' in snapshot.next else None
                                pending[cid] = (pool.submit(graph.invoke, value, thread_config), task['id'])
                            else:
                                value = self._input(current, employee_result=task['result'], leave_message=leave_message)
                                pending[cid] = (pool.submit(graph.invoke, value, thread_config), task['id'])
                            continue
                        if current['state'] == 'waiting':
                            continue
                        if not messages or messages[-1]['role'] != 'customer' or current['latest_id'] == current['handled_id']:
                            continue
                        if time.monotonic() - arrivals[cid] < config['merge_seconds']:
                            continue
                        snapshot = graph.get_state(thread_config)
                        if snapshot.next and 'wait_colleague' not in snapshot.next:
                            # A faulted run is only resumed while its input is still current.
                            value = None if snapshot.values.get('source_id') == current['latest_id'] else self._input(current, leave_message=leave_message)
                        else:
                            value = self._input(current, leave_message=leave_message)
                        pending[cid] = (pool.submit(graph.invoke, value, thread_config), '')
                    except Exception:
                        self.db.event('browser', '一个会话读取未完成，将在下一轮重新检查')
                if self.state == 'running' and not self.stop_event.is_set():
                    if config['mode'] == 'draft':
                        self._fill_drafts(adapter)
                    self._deliver(adapter)
                    for task in self.db.rows("SELECT * FROM tasks WHERE status='open' AND notification='pending'"):
                        if self.stop_event.is_set():
                            break
                        self.notify(task)
                self.stop_event.wait(config['poll_seconds'])
        except Exception:
            fatal_error = True
            with self.lock:
                self.state, self.detail = 'error', '浏览器启动失败；请检查浏览器安装、配置目录或端口'
            self.db.event('error', self.detail)
        finally:
            try:
                adapter.close()
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
                checkpoint_conn.close()
                with self.lock:
                    if not fatal_error:
                        self.state, self.detail = 'stopped', '已停止，浏览器已关闭'
                        self.db.event('runtime', self.detail)

    def _input(self, conversation, employee_result='', leave_message=False):
        return {'conversation_id': conversation['id'], 'source_id': conversation['latest_id'],
                'messages': self.db.history(conversation['id']), 'fields': conversation['fields'],
                'employee_result': employee_result, 'task_id': '', 'plan': {}, 'evidence': [], 'reply': '',
                'action': '', 'leave_message': leave_message}

    def _detect_manual_sends(self, cid, messages, old_ids):
        """A new agent bubble is our own draft sent by hand unless it matches nothing we wrote."""
        for msg in messages:
            if msg['role'] != 'agent' or msg['id'] in old_ids:
                continue
            own = self.db.one("SELECT id FROM outbox WHERE conversation_id=? AND status='sent' AND (sent_source_id=? OR (sent_source_id='' AND reply=?))",
                              (cid, msg['id'], msg['text']))
            if own:
                continue
            pending = self.db.rows("SELECT id,reply FROM outbox WHERE conversation_id=? AND status IN ('draft','ready')", (cid,))
            match = next((row for row in pending if comparable_text(row['reply']) == comparable_text(msg['text'])), None)
            if match:
                self.db.execute("UPDATE outbox SET status='sent',sent_source_id=?,reason='同事在网页手动发送',updated=? WHERE id=?",
                                (msg['id'], time.time(), match['id']))
                self.drafted.pop(match['id'], None)
                continue
            self.db.set_state(cid, 'human')
            self.db.execute("UPDATE outbox SET status='draft',reason='检测到同事直接回复，已暂停自动处理' WHERE conversation_id=? AND status='ready'", (cid,))
            return

    @staticmethod
    def message_time(raw, started_at):
        text = ' '.join(str(raw or '').split())
        if not text:
            return None
        now = datetime.fromtimestamp(started_at)
        for fmt, day_offset in (('%Y-%m-%d %H:%M:%S', 0), ('%m-%d %H:%M:%S', 0), ('%m-%d %H:%M', 0),
                                ('昨天 %H:%M:%S', -1), ('昨天 %H:%M', -1), ('%H:%M:%S', 0), ('%H:%M', 0)):
            try:
                stamp = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if fmt.startswith('%Y'):
                return stamp.timestamp()
            if fmt.startswith('%m'):
                stamp = stamp.replace(year=now.year)
                if stamp.timestamp() > started_at + 86400:
                    stamp = stamp.replace(year=now.year - 1)
                return stamp.timestamp()
            stamp = stamp.replace(year=now.year, month=now.month, day=now.day)
            if day_offset:
                stamp += timedelta(days=day_offset)
            elif stamp.timestamp() > started_at + 300:
                stamp -= timedelta(days=1)
            return stamp.timestamp()
        return None

    def baseline_history(self, cid, messages, started_at, reply_recent_leave=False):
        """Do not reply to old transcripts, except a leave message awaiting an answer within a day."""
        if messages:
            stamp = self.message_time(messages[-1].get('timestamp', ''), started_at)
            if stamp is not None:
                if stamp >= int(started_at):
                    return False
                if reply_recent_leave and messages[-1]['role'] == 'customer' and time.time() - stamp <= 86400:
                    return False
        self.db.execute('UPDATE conversations SET handled_id=latest_id WHERE id=?', (cid,))
        return True

    def _fill_drafts(self, adapter):
        for item in self.db.rows("SELECT o.*,c.name,c.state,c.latest_id,c.customer_key FROM outbox o JOIN conversations c ON c.id=o.conversation_id WHERE o.status='draft' ORDER BY o.created"):
            if self.stop_event.is_set() or self.state != 'running':
                break
            fingerprint = (item['source_id'], item['reply'])
            if self.drafted.get(item['id']) == fingerprint or item['state'] == 'human' or item['latest_id'] != item['source_id']:
                continue
            def before_fill():
                current = self.db.conversation(item['conversation_id'])
                reply = self.db.one('SELECT status,reply FROM outbox WHERE id=?', (item['id'],))
                return (not self.stop_event.is_set() and self.state == 'running' and current['state'] != 'human'
                        and current['latest_id'] == item['source_id'] and reply and reply['status'] == 'draft' and reply['reply'] == item['reply'])
            try:
                status, reason = adapter.fill_draft(item['name'], item['source_id'], item['reply'], before_fill, item['customer_key'])
            except Exception:
                status, reason = 'draft', '网页草稿填入未完成，请检查页面；重新开始接待可重试'
            with self.db.lock:
                current = self.db.one('SELECT status,reply FROM outbox WHERE id=?', (item['id'],))
                if current and current['status'] == 'draft' and current['reply'] == item['reply']:
                    self.db.outbox_status(item['id'], 'stale' if status == 'stale' else 'draft', reason)
            self.drafted[item['id']] = fingerprint

    def _deliver(self, adapter):
        for item in self.db.rows("SELECT * FROM outbox WHERE status='ready' ORDER BY created LIMIT 20"):
            if self.stop_event.is_set() or self.state != 'running':
                return
            cid = item['conversation_id']
            current = self.db.conversation(cid)
            if current['state'] == 'human':
                self.db.outbox_status(item['id'], 'draft', '同事已接管')
                continue
            if current['latest_id'] != item['source_id']:
                self.db.outbox_status(item['id'], 'stale', '会话已有新消息')
                continue

            def before_click():
                with self.lock, self.db.lock:
                    latest = self.db.conversation(cid)
                    if self.stop_event.is_set() or self.state != 'running' or latest['state'] == 'human' or latest['latest_id'] != item['source_id']:
                        return False
                    return bool(self.db.execute("UPDATE outbox SET status='sending',updated=? WHERE id=? AND status='ready'", (time.time(), item['id'])))
            try:
                status, reason = adapter.send(current['name'], item['source_id'], item['reply'], before_click, current['customer_key'])
            except Exception:
                old = self.db.one('SELECT status FROM outbox WHERE id=?', (item['id'],))
                status = 'uncertain' if old['status'] == 'sending' else 'draft'
                reason = '网页操作中断，请核对会话后处理'
            with self.db.lock:
                existing = self.db.one('SELECT status FROM outbox WHERE id=?', (item['id'],))
                if existing['status'] not in ('ready', 'sending'):
                    continue
                self.db.outbox_status(item['id'], status, reason)
            if status == 'sent' and getattr(adapter, 'confirmed_message', None):
                confirmed = adapter.confirmed_message
                self.db.execute('UPDATE outbox SET sent_source_id=?,reply=? WHERE id=?',
                                (confirmed['id'], confirmed['text'], item['id']))
            if status == 'uncertain':
                self.db.set_state(cid, 'human')

    def close(self):
        self.stop(wait=True)
