"""Feishu application bot: scoped task notifications and durable employee replies."""
from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
import re

from cs_duty.database import ROOT
from cs_duty.models import NoRedirect
from cs_duty.notifications import notify_task as notify_webhook
from cs_duty.settings import FIELD_LABELS


class FeishuError(ValueError):
    pass


class FeishuAPI:
    def __init__(self, config):
        self.config = config
        self.token, self.expires = '', 0
        self.lock = threading.RLock()

    def request(self, path, body=None, *, auth=True):
        headers = {'Content-Type': 'application/json'}
        if auth:
            headers['Authorization'] = 'Bearer ' + self.access_token()
        try:
            request = urllib.request.Request('https://open.feishu.cn/open-apis/' + path,
                data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None, headers=headers)
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=10) as response:
                result = json.load(response)
        except Exception as exc:
            raise FeishuError('飞书请求未确认，请检查网络或应用配置') from exc
        if not isinstance(result, dict) or result.get('code', -1) != 0:
            code = result.get('code') if isinstance(result, dict) else 'invalid'
            raise FeishuError(f'飞书接口拒绝请求（代码 {code}），请检查凭据、权限和应用发布状态')
        return result

    def access_token(self):
        with self.lock:
            if time.time() >= self.expires:
                result = self.request('auth/v3/tenant_access_token/internal',
                    {'app_id': self.config['feishu_app_id'], 'app_secret': self.config['feishu_app_secret']}, auth=False)
                self.token = result['tenant_access_token']
                self.expires = time.time() + max(0, result.get('expire', 7200) - 120)
            return self.token

    def bot_id(self):
        # Feishu returns bot at the top level for most tenants and under data for some;
        # the official channel SDK accepts both shapes.
        result = self.request('bot/v3/info')
        data = result.get('data') if isinstance(result.get('data'), dict) else {}
        bot = result.get('bot') or data.get('bot') or {}
        open_id = bot.get('open_id') if isinstance(bot, dict) else ''
        if not open_id:
            raise FeishuError('飞书未返回机器人信息，请确认应用已开通机器人能力')
        return open_id

    def send(self, chat_id, text, key):
        result = self.request('im/v1/messages?receive_id_type=chat_id', {'receive_id': chat_id,
            'msg_type': 'text', 'content': json.dumps({'text': text}, ensure_ascii=False),
            'uuid': hashlib.sha256(key.encode()).hexdigest()[:32]})
        return result['data']['message_id']


class FeishuBridge:
    def __init__(self, db, settings, api_factory=FeishuAPI):
        self.db, self.settings, self.api_factory = db, settings, api_factory
        self.lock = threading.RLock()
        self.process = self.thread = self.api = None
        self.state, self.detail = 'stopped', '飞书协作未连接'
        self.bot = ''
        self.pair_code, self.pair_expires, self.discovered = '', 0, []
        self.stopping = threading.Event()

    def status(self):
        with self.lock:
            return {'state': self.state, 'detail': self.detail,
                    'running': bool(self.thread and self.thread.is_alive()), 'discovered': list(self.discovered)}

    def pair(self):
        with self.lock:
            self.pair_code, self.pair_expires = secrets.token_hex(4), time.time() + 300
            self.discovered = []
            return {'command': '绑定 ' + self.pair_code, 'expires_in': 300}

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError('飞书连接已经运行')
            config = self.settings.runtime()
            if config['feishu_mode'] != 'app' or not config['feishu_app_id'] or not config['feishu_app_secret']:
                raise ValueError('请先保存应用机器人 App ID 和 App Secret')
            self.api = self.api_factory(config)
            self.bot = self.api.bot_id()
            self.stopping.clear()
            self.state, self.detail = 'starting', '正在建立飞书长连接'
            self.process = subprocess.Popen([sys.executable, '-u', '-m', 'cs_duty.feishu_worker'], cwd=ROOT,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding='utf-8', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                self.process.stdin.write(json.dumps({'app_id': config['feishu_app_id'], 'app_secret': config['feishu_app_secret']}) + '\n')
                self.process.stdin.flush()
            except Exception:
                self.process.terminate()
                self.process.wait()
                self.process.stdin.close()
                self.process.stdout.close()
                self.state, self.detail = 'error', '飞书接收进程启动失败'
                raise FeishuError(self.detail) from None
            self.thread = threading.Thread(target=self._read, args=(self.process,), name='cs-duty-feishu', daemon=False)
            self.thread.start()

    def _read(self, process):
        try:
            for line in process.stdout:
                if self.stopping.is_set():
                    break
                item = json.loads(line)
                if item.get('kind') == 'status':
                    with self.lock:
                        self.state = item['state']
                        self.detail = {'connected': '飞书长连接已建立', 'reconnecting': '飞书连接中断，正在重连', 'error': '飞书连接失败，请检查凭据及事件订阅'}.get(self.state, '正在连接飞书')
                elif item.get('kind') == 'message':
                    try:
                        reply = self.handle(item)
                    except Exception:
                        process.stdin.write('error\n')
                        process.stdin.flush()
                        continue
                    process.stdin.write('ok\n')
                    process.stdin.flush()
                    if reply:
                        try:
                            self.api.send(item['chat_id'], reply, 'receipt:' + item['message_id'])
                        except FeishuError:
                            self.db.event('feishu', '处理结果已保存，飞书确认消息未确认送达')
        except Exception:
            if not self.stopping.is_set():
                with self.lock:
                    self.state, self.detail = 'error', '飞书接收进程已中断，请重新连接'
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait()
            for pipe in (process.stdin, process.stdout):
                pipe.close()
            with self.lock:
                if not self.stopping.is_set():
                    self.state = 'error'

    def stop(self):
        with self.lock:
            self.stopping.set()
            self.state, self.detail = 'stopping', '正在断开飞书连接'
            process, thread = self.process, self.thread
        if process and process.poll() is None:
            process.terminate()
        if thread:
            thread.join(timeout=15)
        with self.lock:
            self.state, self.detail = 'stopped', '飞书协作已断开'

    def handle(self, event):
        if event.get('sender_type') != 'user' or event.get('message_type') != 'text':
            return None
        try:
            text = json.loads(event['content'])['text']
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(text, str):
            return None
        text = text.strip()
        for mention in event.get('mentions', []):
            text = text.replace(mention['key'], '').strip()
        user, chat = event.get('sender_id', ''), event.get('chat_id', '')
        config = self.settings.runtime()
        if not user.startswith('ou_') or not chat.startswith('oc_'):
            return None
        with self.lock:
            if self.pair_code and time.time() < self.pair_expires and secrets.compare_digest(text.encode(), ('绑定 ' + self.pair_code).encode()):
                self.discovered = [{'user_id': user, 'chat_id': chat, 'chat_type': event.get('chat_type')}]
                self.pair_code = ''
                return None
        if user not in config['feishu_allowed_users'] or chat not in config['feishu_allowed_chats']:
            return None
        private = event.get('chat_type') == 'p2p'
        if private:
            if not config['feishu_allow_private']:
                return None
        elif event.get('chat_type') != 'group' or chat not in config['feishu_allowed_chats']:
            return None
        if not config['feishu_enabled'] or config['feishu_mode'] != 'app':
            return None
        parent = event.get('parent_id', '')
        with self.db.lock, self.db.conn:
            if self.db.one('SELECT 1 FROM feishu_receipts WHERE message_id=?', (event['message_id'],)):
                return None
            binding = self.db.one('SELECT * FROM feishu_task_messages WHERE message_id=? AND app_id=?', (parent, config['feishu_app_id'])) if parent else None
            mentioned = any(m.get('id') == self.bot for m in event.get('mentions', []))
            if not private and not mentioned and not (binding and binding['chat_id'] == chat):
                return None
            command = re.fullmatch(r'处理\s+([a-f0-9]{8,32})\s+([\s\S]+)', text)
            if command:
                candidates = self.db.rows('SELECT * FROM feishu_task_messages WHERE app_id=? AND task_id LIKE ?', (config['feishu_app_id'], command[1] + '%'))
                candidates = [c for c in candidates if private or c['chat_id'] == chat]
                if len(candidates) != 1:
                    return None
                binding, result = candidates[0], command[2].strip()
            elif binding and (private or binding['chat_id'] == chat):
                result = text
            else:
                return None
            if not 1 <= len(result) <= 5000:
                return None
            task = self.db.one("SELECT id FROM tasks WHERE id=? AND status='open'", (binding['task_id'],))
            if not task:
                return None
            self.db.conn.execute("UPDATE tasks SET status='ready',result=?,updated=? WHERE id=? AND status='open'", (result, time.time(), task['id']))
            self.db.conn.execute('INSERT INTO feishu_receipts VALUES(?,?,?,?,?)', (event['message_id'], task['id'], user, chat, time.time()))
            return '处理结果已保存，客服将在接待运行时继续处理。待办 ' + task['id'][:8]

    def notify(self, task):
        config = self.settings.runtime()
        if config['feishu_mode'] == 'webhook':
            return notify_webhook(self.db, self.settings, task)
        if not config['feishu_enabled'] or not self.api or self.state != 'connected':
            return
        chat = config['feishu_chat_id']
        if chat not in config['feishu_allowed_chats'] or not config['feishu_allowed_users']:
            return
        if not self.db.execute("UPDATE tasks SET notification='sending' WHERE id=? AND status='open' AND notification='pending'", (task['id'],)):
            return
        customer = self.db.conversation(task['conversation_id'])
        fields = json.loads(task['fields'])
        content = '\n'.join(['客服协同待办 ' + task['id'][:8], '客户：' + customer['name'], '店铺：' + customer['shop'],
            '问题：' + task['summary'], *[FIELD_LABELS.get(k, k) + '：' + str(v) for k, v in fields.items()],
            '请回复这条消息并 @机器人 填写结果，或发送：', '@机器人 处理 ' + task['id'][:8] + ' 已确认的处理结果'])
        try:
            message_id = self.api.send(chat, content, 'task:' + task['id'])
        except FeishuError:
            self.db.execute("UPDATE tasks SET notification='uncertain' WHERE id=?", (task['id'],))
            return
        with self.db.lock, self.db.conn:
            self.db.conn.execute('INSERT OR REPLACE INTO feishu_task_messages VALUES(?,?,?,?)', (task['id'], message_id, chat, config['feishu_app_id']))
            self.db.conn.execute("UPDATE tasks SET notification='sent' WHERE id=?", (task['id'],))

    def test(self):
        config = self.settings.runtime()
        chat = config['feishu_chat_id']
        if chat not in config['feishu_allowed_chats'] or not config['feishu_allowed_users']:
            raise ValueError('请先配置通知会话和允许用户')
        api = self.api_factory(config)
        api.send(chat, 'CS Duty 飞书连接测试：这是一条测试通知，不包含客户资料。', secrets.token_hex(16))
        return {'detail': '测试通知已发送'}


def check(config, *, send=False, api_factory=None):
    """Validate saved app credentials against the live tenant, optionally sending a data-free test."""
    if not str(config.get('feishu_app_id', '')).startswith('cli_') or not config.get('feishu_app_secret'):
        raise FeishuError('请先在管理页保存应用机器人 App ID 和 App Secret')
    api = (api_factory or FeishuAPI)(config)
    bot = api.bot_id()
    result = {'bot': bot, 'chat': '', 'sent': False}
    if send:
        chat = str(config.get('feishu_chat_id', ''))
        if not chat.startswith('oc_'):
            raise FeishuError('请先在管理页配置通知会话')
        api.send(chat, 'CS Duty 飞书连接自检：这是一条测试消息，不包含客户资料。', secrets.token_hex(16))
        result.update(chat=chat, sent=True)
    return result
