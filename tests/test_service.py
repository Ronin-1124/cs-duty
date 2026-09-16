import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
import io
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from cs_duty.database import Database
from cs_duty.knowledge import Knowledge
from cs_duty.models import ModelClient, ModelError
from cs_duty.runtime import Runtime
from cs_duty.notifications import notify_task
from cs_duty.server import create_server
from cs_duty.settings import Settings
from cs_duty.workflow import Workflow


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = Database(self.root / 'business.sqlite3')
        self.settings = Settings(self.db, None)
        self.profile = {'name': 'Test', 'protocol': 'anthropic', 'base_url': 'https://example.com/anthropic',
                        'model': 'test', 'api_key': 'test-secret'}
        self.settings.save_profile(self.profile)
        self.knowledge = Knowledge(self.db)
        self.knowledge.import_csv('test.csv', '产品型号,问题,答案\nTEST-1,TEST-1供电电压是多少,TEST-1使用5V供电\n')
        self.cid, _ = self.db.ingest('mock', 'shop', 'customer-1', '客户甲',
                                    [{'id': 'm1', 'role': 'customer', 'text': 'TEST-1供电电压是多少'}])
        self.checkpoints = sqlite3.connect(self.root / 'checkpoints.sqlite3', check_same_thread=False)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.db.close)
        self.addCleanup(self.checkpoints.close)

    def graph(self, intent='consult', fields=None, callback=None):
        class Model:
            def __init__(self, profile):
                pass

            def plan(model, persona, context):
                if callback:
                    return callback(context)
                evidence = context['knowledge']
                return {'intent': intent, 'reply': context['colleague_result'] or '这款使用5V供电。',
                        'fields': fields or {}, 'evidence_ids': [evidence[0]['id']] if evidence else []}

        return Workflow(self.db, self.settings, self.knowledge, SqliteSaver(self.checkpoints), Model).graph

    def value(self):
        c = self.db.conversation(self.cid)
        return {'conversation_id': self.cid, 'source_id': c['latest_id'], 'messages': self.db.history(self.cid),
                'fields': c['fields'], 'employee_result': '', 'task_id': ''}

    @property
    def config(self):
        return {'configurable': {'thread_id': self.cid}}

    def test_deduplication_staleness_and_customer_isolation(self):
        oid = self.db.prepare_reply(self.cid, 'm1', '回复', 'ready')
        self.assertEqual(oid, self.db.prepare_reply(self.cid, 'm1', '重复', 'ready'))
        same, changed = self.db.ingest('mock', 'shop', 'customer-1', '客户甲',
                                       [{'id': 'm1', 'role': 'customer', 'text': 'TEST-1供电电压是多少'}])
        self.assertFalse(changed)
        self.assertEqual(same, self.cid)
        other, _ = self.db.ingest('mock', 'other-shop', 'customer-1', '客户甲',
                                  [{'id': 'm1', 'role': 'customer', 'text': '另外的问题'}])
        self.assertNotEqual(other, self.cid)
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [{'id': 'm2', 'role': 'customer', 'text': '补充问题'}])
        self.assertEqual(self.db.one('SELECT status FROM outbox WHERE id=?', (oid,))['status'], 'stale')
        self.assertIsNone(self.db.prepare_reply(self.cid, 'm1', '过期回复', 'ready'))

    def test_restart_marks_inflight_uncertain(self):
        oid = self.db.prepare_reply(self.cid, 'm1', '回复', 'ready')
        self.db.outbox_status(oid, 'sending')
        reopened = Database(self.db.path)
        try:
            self.assertEqual(reopened.one('SELECT status FROM outbox')['status'], 'uncertain')
        finally:
            reopened.close()

    def test_secret_redaction_and_preservation(self):
        public = json.dumps(self.settings.profiles()) + json.dumps(self.settings.runtime(public=True))
        self.assertNotIn('test-secret', public)
        profile = self.settings.profile()
        self.settings.save_profile({**profile, 'api_key': '', 'name': 'Renamed'})
        self.assertEqual(self.settings.profile()['api_key'], 'test-secret')
        with self.assertRaises(ValueError):
            self.settings.save_profile({**self.profile, 'base_url': 'http://remote.example.com'})

    def test_three_csv_formats_and_preserved_disabled_entry(self):
        for filename, text in [('a.csv', '话术标题（选填）,话术内容（必填）\n供电,使用5V供电\n'),
                               ('b.csv', '快捷短语内容（必填）\n请提供您的订单号\n')]:
            self.assertEqual(self.knowledge.import_csv(filename, text)['inserted'], 1)
            self.assertEqual(self.knowledge.import_csv(filename, text)['duplicates'], 1)
        item = self.knowledge.search('TEST-1供电电压')[0]
        self.db.execute('UPDATE knowledge SET enabled=0 WHERE id=?', (item['id'],))
        self.knowledge.import_csv('test.csv', '产品型号,问题,答案\nTEST-1,TEST-1供电电压是多少,TEST-1使用5V供电\n')
        self.assertFalse(self.db.one('SELECT enabled FROM knowledge WHERE id=?', (item['id'],))['enabled'])

    def test_grounded_reply_and_idempotent_replay(self):
        graph = self.graph()
        graph.invoke(self.value(), self.config)
        graph.invoke(self.value(), self.config)
        rows = self.db.rows('SELECT * FROM outbox')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'draft')
        self.assertIn('5V', rows[0]['reply'])
        self.assertEqual(self.db.conversation(self.cid)['handled_id'], 'm1')

    def test_model_completion_cannot_override_takeover_or_new_message(self):
        def takeover(context):
            self.db.set_state(self.cid, 'human')
            return {'intent': 'consult', 'reply': '不应发送', 'evidence_ids': [context['knowledge'][0]['id']]}
        self.graph(callback=takeover).invoke(self.value(), self.config)
        self.assertEqual(self.db.conversation(self.cid)['state'], 'human')
        self.assertEqual(self.db.rows('SELECT * FROM outbox'), [])

    def test_greetings_and_nudges_do_not_require_model_or_knowledge(self):
        self.cid, _ = self.db.ingest('mock', 'shop', 'greeting', 'a', [
            {'id': 'g1', 'role': 'customer', 'text': '你好'},
            {'id': 'g2', 'role': 'customer', 'text': '客服在吗'},
            {'id': 'g3', 'role': 'customer', 'text': '？？？'},
            {'id': 'g4', 'role': 'customer', 'text': '工作日没人吗'},
        ])
        self.db.execute('UPDATE knowledge SET enabled=0')
        graph = self.graph(callback=lambda context: self.fail('问候不应调用模型'))
        graph.invoke(self.value(), self.config)
        reply = self.db.one('SELECT * FROM outbox')
        self.assertEqual(reply['source_id'], 'g4')
        self.assertEqual(reply['status'], 'draft')
        self.assertIn('在的', reply['reply'])
        self.assertEqual(self.db.conversation(self.cid)['state'], 'active')
        self.assertFalse(self.db.rows('SELECT * FROM tasks'))
        self.assertEqual(graph.get_state(self.config).next, ())

    def test_greeting_keeps_custom_collection_state(self):
        self.db.set_state(self.cid, 'collecting', {'product': 'TEST-1'})
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'a1', 'role': 'agent', 'text': '请提供定制数量'},
            {'id': 'm2', 'role': 'customer', 'text': '还在吗？'},
        ])
        self.graph().invoke(self.value(), self.config)
        current = self.db.conversation(self.cid)
        self.assertEqual(current['state'], 'collecting')
        self.assertEqual(current['fields'], {'product': 'TEST-1'})

    def test_nudge_does_not_hide_unanswered_product_question(self):
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'm2', 'role': 'customer', 'text': '客服在吗？'},
        ])
        self.graph().invoke(self.value(), self.config)
        self.assertIn('5V', self.db.one('SELECT reply FROM outbox')['reply'])

    def test_ungrounded_consult_still_hands_off_and_can_restart_after_cancellation(self):
        self.db.execute('UPDATE knowledge SET enabled=0')
        graph = self.graph()
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.conversation(self.cid)['state'], 'waiting')
        self.assertEqual(graph.get_state(self.config).next, ('wait_colleague',))
        self.db.execute("UPDATE tasks SET status='cancelled'")
        self.db.set_state(self.cid, 'active')
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'm2', 'role': 'customer', 'text': '想问声好'},
        ])
        # Fresh input replaces the cancelled interrupt without deleting checkpoints.
        graph = self.graph('greeting')
        graph.invoke(self.value(), self.config)
        self.assertEqual(graph.get_state(self.config).next, ())
        self.assertEqual(self.db.conversation(self.cid)['state'], 'active')
        reply = self.db.one("SELECT * FROM outbox WHERE source_id='m2'")
        self.assertIn('在的', reply['reply'])
        self.assertNotIn('5V', reply['reply'])
        self.assertFalse(self.db.rows("SELECT * FROM tasks WHERE status='open'"))

    def test_custom_collection_then_persistent_colleague_resume(self):
        self.graph('custom', {'product': 'TEST-1'}).invoke(self.value(), self.config)
        self.assertEqual(self.db.conversation(self.cid)['state'], 'collecting')
        self.assertEqual(self.db.rows('SELECT * FROM tasks'), [])
        fields = {'product': 'TEST-1', 'requirements': '定制外壳', 'quantity': '100台',
                  'deadline': '下月', 'contact': '电话123'}
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [{'id': 'm2', 'role': 'customer', 'text': '请同事确认'}])
        graph = self.graph('custom', fields)
        graph.invoke(self.value(), self.config)
        task = self.db.one('SELECT * FROM tasks')
        self.assertEqual(self.db.conversation(self.cid)['state'], 'waiting')
        self.assertEqual(graph.get_state(self.config).next, ('wait_colleague',))
        # Reopen SQLite and rebuild the graph to exercise process-restart persistence.
        self.checkpoints.close()
        self.checkpoints = sqlite3.connect(self.root / 'checkpoints.sqlite3', check_same_thread=False)
        self.addCleanup(self.checkpoints.close)
        graph = self.graph('consult')
        self.db.resolve_task(task['id'], '定制方案已确认，需要提供图纸。')
        graph.invoke(Command(resume={'result': '定制方案已确认，需要提供图纸。',
                                      'messages': self.db.history(self.cid), 'source_id': 'm2'}), self.config)
        reply = self.db.one("SELECT * FROM outbox WHERE source_id='m2'")
        self.assertIn('图纸', reply['reply'])
        self.assertEqual(self.db.conversation(self.cid)['state'], 'active')
        self.assertEqual(graph.get_state(self.config).next, ())

    def test_repeated_offtopic_and_closing_do_not_loop(self):
        graph = self.graph('offtopic')
        graph.invoke(self.value(), self.config)
        reply = self.db.one('SELECT reply FROM outbox')['reply']
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'a1', 'role': 'agent', 'text': reply}, {'id': 'm2', 'role': 'customer', 'text': '给我写篇作文'}])
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.one("SELECT status FROM outbox WHERE source_id='m2'")['status'], 'ignored')

    def test_invalid_model_plan_produces_no_reply(self):
        with self.assertRaises(ModelError):
            self.graph('invalid').invoke(self.value(), self.config)
        self.assertFalse(self.db.rows('SELECT * FROM outbox'))

    def test_first_thanks_replies_once_and_new_question_is_not_swallowed(self):
        courtesy = '客气了，有需要随时联系我。'
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'a1', 'role': 'agent', 'text': '这款使用5V供电。'},
            {'id': 'm2', 'role': 'customer', 'text': '好的，谢谢客服'},
        ])
        graph = self.graph(callback=lambda context: self.fail('简单致谢不调用模型'))
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.one("SELECT reply FROM outbox WHERE source_id='m2'")['reply'], courtesy)
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'a2', 'role': 'agent', 'text': courtesy},
            {'id': 'm3', 'role': 'customer', 'text': '谢谢您！'},
        ])
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.one("SELECT status FROM outbox WHERE source_id='m3'")['status'], 'ignored')
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'm4', 'role': 'customer', 'text': '谢谢，TEST-1供电电压是多少？'},
        ])
        self.graph().invoke(self.value(), self.config)
        self.assertIn('5V', self.db.one("SELECT reply FROM outbox WHERE source_id='m4'")['reply'])
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'a4', 'role': 'agent', 'text': '使用5V供电。'},
            {'id': 'm5', 'role': 'customer', 'text': '谢谢'},
        ])
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.one("SELECT reply FROM outbox WHERE source_id='m5'")['reply'], courtesy)

    def test_quote_collects_then_hands_off_without_reasking_known_fields(self):
        graph = self.graph('quote', {'product': 'TEST-1'})
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.conversation(self.cid)['state'], 'collecting')
        self.assertFalse(self.db.rows('SELECT * FROM tasks'))
        reply = self.db.one('SELECT reply FROM outbox')['reply']
        self.assertIn('数量', reply)
        self.assertNotIn('产品型号', reply)
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'a1', 'role': 'agent', 'text': reply},
            {'id': 'm2', 'role': 'customer', 'text': '谢谢'},
        ])
        graph.invoke(self.value(), self.config)
        self.assertEqual(self.db.conversation(self.cid)['state'], 'collecting')
        self.assertEqual(graph.get_state(self.config).values['collection_intent'], 'quote')
        self.db.ingest('mock', 'shop', 'customer-1', '客户甲', [
            {'id': 'm3', 'role': 'customer', 'text': '100台，在这里联系'},
        ])
        def complete(context):
            self.assertEqual(context['collection_intent'], 'quote')
            return {'intent': 'quote', 'fields': {'quantity': '100台', 'contact': '当前会话'}, 'need_colleague': True}
        self.graph(callback=complete).invoke(self.value(), self.config)
        self.assertEqual(self.db.conversation(self.cid)['state'], 'waiting')
        task = self.db.one('SELECT * FROM tasks')
        self.assertEqual(json.loads(task['fields'])['product'], 'TEST-1')
        self.assertEqual(self.db.one("SELECT reply FROM outbox WHERE source_id='m3'")['reply'], '我帮您看看，稍等。')

    def test_confirmed_results_use_customer_service_voice_and_keep_qualifiers(self):
        for original, expected in [
            ('同事提到我们这边有一款 TEST-1，库存还需要核实。', '这边帮您确认到，有一款 TEST-1，库存还需要核实。'),
            ('同事帮您确认了，TEST-1 暂时没货。', '这边帮您确认到，TEST-1 暂时没货。'),
            ('这边暂时还无法确认 TEST-1 的库存。', '这边暂时还无法确认 TEST-1 的库存。'),
        ]:
            with self.subTest(original=original):
                workflow = Workflow(self.db, self.settings, self.knowledge, SqliteSaver(self.checkpoints))
                state = {**self.value(), 'employee_result': '有 TEST-1 这个型号；库存尚未确认。',
                         'plan': {'intent': 'consult', 'reply': original, 'fields': {}}}
                result = workflow.validate(state)
                self.assertEqual(result['reply'], expected)
                self.assertEqual(result['action'], 'reply')

    def test_cancel_during_browser_preparation_is_not_restored_to_draft(self):
        oid = self.db.prepare_reply(self.cid, 'm1', '回复', 'ready')
        runtime = Runtime(self.db, self.settings, self.knowledge)
        runtime.state = 'running'
        db = self.db

        class Adapter:
            def send(self, name, source_id, reply, before_click):
                db.outbox_status(oid, 'cancelled')
                self.clicked = before_click()
                return 'draft', '发送被取消'

        adapter = Adapter()
        runtime._deliver(adapter)
        self.assertFalse(adapter.clicked)
        self.assertEqual(self.db.one('SELECT status FROM outbox')['status'], 'cancelled')

    def test_notifications_require_enablement_and_are_not_duplicated(self):
        tid = self.db.create_task(self.cid, 'm1', '测试需求', {})
        task = self.db.one('SELECT * FROM tasks WHERE id=?', (tid,))
        with patch('cs_duty.notifications.urllib.request.build_opener') as opener:
            notify_task(self.db, self.settings, task)
            opener.assert_not_called()
            self.settings.save_runtime({'feishu_enabled': True, 'feishu_webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/test', 'feishu_secret': 'fixture'})
            opener.return_value.open.return_value = io.BytesIO(b'{"code":0}')
            notify_task(self.db, self.settings, task)
            notify_task(self.db, self.settings, task)
            self.assertEqual(opener.return_value.open.call_count, 1)
            self.assertEqual(self.db.one('SELECT notification FROM tasks')['notification'], 'sent')

    def test_both_model_protocols_extract_final_text_and_hide_http_errors(self):
        profile = self.settings.profile()
        for protocol, response, suffix, header in [
            ('anthropic', {'content': [{'type': 'thinking', 'thinking': 'private'}, {'type': 'text', 'text': 'OK'}]}, '/v1/messages', 'X-api-key'),
            ('openai', {'choices': [{'message': {'content': '<think>private</think>OK', 'reasoning_content': 'private'}}]}, '/chat/completions', 'Authorization'),
        ]:
            with self.subTest(protocol=protocol), patch('cs_duty.models.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value = io.BytesIO(json.dumps(response).encode())
                self.assertEqual(ModelClient({**profile, 'protocol': protocol}).complete('system', 'user'), 'OK')
                request = opener.return_value.open.call_args.args[0]
                self.assertTrue(request.full_url.endswith(suffix))
                self.assertTrue(request.get_header(header))
                opener.return_value.open.side_effect = HTTPError(request.full_url, 401, 'test-secret', {}, None)
                with self.assertRaises(ModelError) as error:
                    ModelClient({**profile, 'protocol': protocol}).complete('system', 'user')
                self.assertNotIn('test-secret', str(error.exception))


class ManagementCase(unittest.TestCase):
    def test_http_state_origin_checks_and_exclusive_port(self):
        with tempfile.TemporaryDirectory() as directory:
            server, app = create_server(port=0, data_dir=Path(directory), env_path=None, import_root=None)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            try:
                with urlopen(base + '/api/manage/state') as response:
                    state = json.load(response)
                self.assertEqual(state['runtime']['state'], 'stopped')
                self.assertNotIn('feishu_secret', state['config'])
                with self.assertRaises(HTTPError) as denied:
                    urlopen(Request(base + '/api/manage/settings', data=b'{}', headers={'Content-Type': 'application/json'}))
                self.assertEqual(denied.exception.code, 403)
                with self.assertRaises(OSError):
                    create_server(port=server.server_address[1], data_dir=Path(directory), env_path=None, import_root=None)
                with urlopen(Request(base + '/api/manage/settings', data=b'{"mode":"auto"}',
                                     headers={'Content-Type': 'application/json', 'X-CS-Duty': '1'})) as response:
                    self.assertTrue(json.load(response)['ok'])
                self.assertEqual(app.settings.runtime()['mode'], 'auto')
            finally:
                server.shutdown()
                thread.join()
                app.close()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
