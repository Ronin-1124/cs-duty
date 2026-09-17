import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock

from cs_duty.runtime import Runtime
from cs_duty.transport import ReadThrottle
import test_service as fixtures


class RealBaselineCase(unittest.TestCase):
    setUp = fixtures.ServiceCase.setUp
    graph = fixtures.ServiceCase.graph
    value = fixtures.ServiceCase.value
    config = fixtures.ServiceCase.config

    def test_initial_history_marks_handled_without_reply_or_task(self):
        runtime = Runtime(self.db, self.settings, self.knowledge)
        self.assertTrue(runtime.baseline_history(self.cid, self.db.history(self.cid), time.time()))
        current = self.db.conversation(self.cid)
        self.assertEqual(current['latest_id'], current['handled_id'])
        self.assertFalse(self.db.rows('SELECT * FROM outbox'))
        self.assertFalse(self.db.rows('SELECT * FROM tasks'))

    def test_message_received_during_initial_scan_is_not_baselined(self):
        runtime = Runtime(self.db, self.settings, self.knowledge)
        started = time.time() - 2
        messages = [{'timestamp': datetime.now().strftime('%m-%d %H:%M:%S'), 'text': '新咨询'}]
        self.assertFalse(runtime.baseline_history(self.cid, messages, started))
        self.assertNotEqual(self.db.conversation(self.cid)['handled_id'], 'm1')

    def test_recent_leave_message_is_answered_but_old_or_agent_last_is_baselined(self):
        runtime = Runtime(self.db, self.settings, self.knowledge)
        now = datetime.now()
        answered = [{'id': 'm1', 'role': 'customer', 'text': '在吗', 'timestamp': now.strftime('%H:%M:%S')}]
        self.assertFalse(runtime.baseline_history(self.cid, answered, time.time(), True))
        old = [{'id': 'm1', 'role': 'customer', 'text': '在吗',
                'timestamp': (now - timedelta(days=2)).strftime('%m-%d %H:%M:%S')}]
        self.assertTrue(runtime.baseline_history(self.cid, old, time.time(), True))
        handled = [{'id': 'm1', 'role': 'agent', 'text': '已回复',
                    'timestamp': (now - timedelta(days=2)).strftime('%m-%d %H:%M:%S')}]
        self.assertTrue(runtime.baseline_history(self.cid, handled, time.time(), True))
        active = [{'id': 'm1', 'role': 'customer', 'text': '在吗',
                   'timestamp': (now - timedelta(minutes=1)).strftime('%m-%d %H:%M:%S')}]
        self.assertTrue(runtime.baseline_history(self.cid, active, time.time(), False))

    def test_manual_send_of_own_draft_is_marked_sent(self):
        oid = self.db.prepare_reply(self.cid, 'm1', '在的，\n有什么能帮您的吗？', 'draft')
        runtime = Runtime(self.db, self.settings, self.knowledge)
        runtime.drafted[oid] = ('m1', 'x')
        runtime._detect_manual_sends(self.cid, [
            {'id': 'm1', 'role': 'customer', 'text': '你好'},
            {'id': 'a1', 'role': 'agent', 'text': '在的，\n\n有什么能帮您的吗？'}], {'m1'})
        self.assertEqual(self.db.conversation(self.cid)['state'], 'active')
        row = self.db.one('SELECT * FROM outbox WHERE id=?', (oid,))
        self.assertEqual((row['status'], row['sent_source_id']), ('sent', 'a1'))
        self.assertNotIn(oid, runtime.drafted)

    def test_unmatched_colleague_reply_pauses_automatic_handling(self):
        self.db.prepare_reply(self.cid, 'm1', '草稿', 'draft')
        runtime = Runtime(self.db, self.settings, self.knowledge)
        runtime._detect_manual_sends(self.cid, [
            {'id': 'm1', 'role': 'customer', 'text': '你好'},
            {'id': 'a1', 'role': 'agent', 'text': '我直接回复了'}], {'m1'})
        self.assertEqual(self.db.conversation(self.cid)['state'], 'human')

    def test_reply_mode_controls_draft_or_auto(self):
        for mode in ('draft', 'auto'):
            with self.subTest(mode=mode):
                self.db.execute('DELETE FROM outbox')
                self.settings.save_runtime({'mode': mode})
                self.graph().invoke(self.value(), self.config)
                self.assertEqual(self.db.one('SELECT status FROM outbox')['status'], 'ready' if mode == 'auto' else 'draft')

    def test_draft_filled_once_and_never_sent(self):
        self.graph().invoke(self.value(), self.config)
        runtime = Runtime(self.db, self.settings, self.knowledge)
        runtime.state = 'running'
        adapter = Mock()
        adapter.fill_draft.side_effect = lambda name, source, reply, check, key='': ('filled', '已填入客户端输入框，未发送') if check() else ('draft', '已取消')
        runtime._fill_drafts(adapter)
        runtime._fill_drafts(adapter)
        self.assertEqual(adapter.fill_draft.call_count, 1)
        adapter.send.assert_not_called()
        self.assertEqual(self.db.one('SELECT status FROM outbox')['status'], 'draft')

    def test_handoff_reply_uses_same_independent_mode(self):
        for mode in ('draft', 'auto'):
            with self.subTest(mode=mode):
                self.db.execute('DELETE FROM outbox')
                self.db.execute('DELETE FROM tasks')
                self.db.set_state(self.cid, 'active')
                self.settings.save_runtime({'mode': mode})
                config = {'configurable': {'thread_id': mode}}
                self.graph(intent='after_sales').invoke(self.value(), config)
                self.assertTrue(self.db.one('SELECT id FROM tasks'))
                self.assertEqual(self.db.one('SELECT status FROM outbox')['status'], 'ready' if mode == 'auto' else 'draft')

    def test_real_monitor_skips_old_history_then_processes_new_message(self):
        self.settings.save_runtime({'mode': 'draft', 'merge_seconds': 0, 'poll_seconds': 1})
        db, calls, reads = self.db, [], []
        runtime = None

        class Adapter(ReadThrottle):
            def __init__(self, config, data_dir):
                super().__init__()

            def start(self):
                self.round = 0

            def customers(self):
                self.round += 1
                if db.one('SELECT id FROM outbox') or self.round > 8:
                    runtime.stop_event.set()
                return [{'name': '桌面流程测试', 'customer_key': 'test-desktop', 'initial_history': True,
                         'preview': '历史问题' if self.round < 3 else '新的问题'}]

            def open_customer(self, name, key=''):
                reads.append(self.round)
                messages = [{'id': 'old', 'role': 'customer', 'text': '历史问题', 'timestamp': ''}]
                if self.round >= 3:
                    messages.append({'id': 'new', 'role': 'customer', 'text': '新的问题'})
                return messages

            def close(self):
                pass

            def send(self, *args):
                raise AssertionError('草稿模式不应进入自动发送')

        class Model:
            def __init__(self, profile):
                pass

            def plan(self, persona, context):
                calls.append(context['history'][-1]['text'])
                return {'intent': 'greeting', 'reply': '测试回复', 'fields': {}}

        runtime = Runtime(db, self.settings, self.knowledge, adapter_factory=Adapter, model_factory=Model)
        runtime._run()
        self.assertEqual(calls, ['新的问题'])
        self.assertEqual(reads, [1, 3])
        self.assertEqual(runtime.baseline_count, 1)
        item = db.one('SELECT * FROM outbox')
        self.assertEqual((item['source_id'], item['status']), ('new', 'draft'))


class ReadThrottleCase(unittest.TestCase):
    def test_unchanged_preview_skips_until_changed_or_interval(self):
        throttle = ReadThrottle()
        customer = {'customer_key': 'name:test', 'preview': '你好', 'date': '今天'}
        self.assertTrue(throttle.should_read(customer))
        throttle.mark_read_snapshot(customer)
        self.assertFalse(throttle.should_read(customer))
        self.assertTrue(throttle.should_read({**customer, 'preview': '新的问题'}))
        self.assertTrue(throttle.should_read(customer, force=True))
        fingerprint, stamp = throttle.read_cache[customer['customer_key']]
        throttle.read_cache[customer['customer_key']] = (fingerprint, stamp - 61)
        self.assertTrue(throttle.should_read(customer))


if __name__ == '__main__':
    unittest.main()
