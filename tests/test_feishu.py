import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cs_duty.database import Database
from cs_duty.feishu import FeishuAPI, FeishuBridge, FeishuError
from cs_duty.settings import Settings


class FeishuCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / 'business.sqlite3')
        self.addCleanup(self.db.close)
        self.settings = Settings(self.db, None)
        self.settings.save_runtime({'feishu_mode': 'app', 'feishu_app_id': 'cli_fixture',
            'feishu_app_secret': 'secret-feishu-fixture', 'feishu_enabled': True,
            'feishu_chat_id': 'oc_group', 'feishu_allowed_users': ['ou_owner'],
            'feishu_allowed_chats': ['oc_group', 'oc_private', 'oc_other']})
        self.api = Mock()
        self.api.bot_id.return_value = 'ou_bot'
        self.api.send.return_value = 'om_task'
        self.bridge = FeishuBridge(self.db, self.settings, lambda config: self.api)
        self.addCleanup(self.bridge.stop)
        self.bridge.api, self.bridge.bot, self.bridge.state = self.api, 'ou_bot', 'connected'
        cid, _ = self.db.ingest('mock', 'shop', 'fixture', '测试客户',
            [{'id': 'm1', 'role': 'customer', 'text': '请确认交期'}])
        self.tid = self.db.create_task(cid, 'm1', '确认交期', {'product': 'TEST'})

    def task(self):
        return self.db.one('SELECT * FROM tasks WHERE id=?', (self.tid,))

    def event(self, **changes):
        return {'kind': 'message', 'message_id': 'om_result', 'sender_id': 'ou_owner',
            'sender_type': 'user', 'chat_id': 'oc_group', 'chat_type': 'group',
            'message_type': 'text', 'content': json.dumps({'text': '确认下周交货'}),
            'parent_id': 'om_task', 'mentions': [], **changes}

    def test_notification_and_reply_are_durable_and_deduplicated(self):
        self.bridge.notify(self.task())
        self.bridge.notify(self.task())
        self.api.send.assert_called_once()
        self.assertEqual(self.task()['notification'], 'sent')
        self.assertTrue(self.bridge.handle(self.event()))
        self.assertIsNone(self.bridge.handle(self.event(content=json.dumps({'text': '覆盖结果'}))))
        self.assertEqual(self.task()['status'], 'ready')
        self.assertEqual(self.task()['result'], '确认下周交货')
        self.assertEqual(len(self.db.rows('SELECT * FROM feishu_receipts')), 1)
        reopened = Database(self.db.path)
        try:
            bridge = FeishuBridge(reopened, Settings(reopened, None))
            self.assertIsNone(bridge.handle(self.event()))
            self.assertEqual(reopened.one('SELECT result FROM tasks')['result'], '确认下周交货')
        finally:
            reopened.close()

    def test_filters_before_accepting_results(self):
        self.bridge.notify(self.task())
        for changes in [
            {'sender_id': 'ou_stranger'}, {'sender_type': 'app'},
            {'chat_id': 'oc_unlisted'}, {'chat_id': 'oc_other'},
            {'message_type': 'image'}, {'content': '{broken'},
            {'content': '{"text":42}'}, {'content': json.dumps({'text': 'x' * 5001})},
            {'parent_id': ''}, {'chat_type': 'p2p'},
        ]:
            with self.subTest(changes=list(changes)):
                self.assertIsNone(self.bridge.handle(self.event(**changes)))
                self.assertEqual(self.task()['status'], 'open')
        self.settings.save_runtime({'feishu_enabled': False})
        self.assertIsNone(self.bridge.handle(self.event()))
        self.settings.save_runtime({'feishu_enabled': True, 'feishu_app_id': 'cli_changed'})
        self.assertIsNone(self.bridge.handle(self.event()))
        self.assertFalse(self.db.rows('SELECT * FROM feishu_receipts'))

    def test_group_command_requires_bot_mention_and_matching_chat(self):
        self.bridge.notify(self.task())
        event = self.event(parent_id='', content=json.dumps({'text': '@_user_1 处理 ' + self.tid[:8] + ' 本周发货'}))
        self.assertIsNone(self.bridge.handle(event))
        event['mentions'] = [{'key': '@_user_1', 'id': 'ou_bot'}]
        self.assertIsNone(self.bridge.handle({**event, 'chat_id': 'oc_other'}))
        self.assertTrue(self.bridge.handle(event))
        self.assertEqual(self.task()['result'], '本周发货')

    def test_private_requires_explicit_enablement_and_both_allowlists(self):
        self.bridge.notify(self.task())
        event = self.event(chat_type='p2p', chat_id='oc_private', parent_id='',
            content=json.dumps({'text': '处理 ' + self.tid[:8] + ' 下周交货'}))
        self.assertIsNone(self.bridge.handle(event))
        self.settings.save_runtime({'feishu_allow_private': True})
        self.assertIsNone(self.bridge.handle({**event, 'sender_id': 'ou_other'}))
        self.assertIsNone(self.bridge.handle({**event, 'chat_id': 'oc_unlisted'}))
        self.assertTrue(self.bridge.handle(event))

    def test_pair_code_only_discovers_and_is_single_use_and_expires(self):
        command = self.bridge.pair()['command']
        event = self.event(sender_id='ou_new', content=json.dumps({'text': command}))
        self.bridge.handle(self.event(content=json.dumps({'text': '绑定 不正确'})))
        self.assertEqual(self.bridge.status()['discovered'], [])
        self.bridge.handle(event)
        self.assertEqual(self.bridge.status()['discovered'][0]['user_id'], 'ou_new')
        self.assertEqual(self.settings.runtime()['feishu_allowed_users'], ['ou_owner'])
        self.bridge.handle({**event, 'sender_id': 'ou_intruder'})
        self.assertEqual(self.bridge.status()['discovered'][0]['user_id'], 'ou_new')
        command = self.bridge.pair()['command']
        self.bridge.pair_expires = time.time() - 1
        self.bridge.handle(self.event(content=json.dumps({'text': command})))
        self.assertEqual(self.bridge.status()['discovered'], [])
        self.assertEqual(self.task()['status'], 'open')

    def test_unknown_send_result_is_not_retried(self):
        self.api.send.side_effect = FeishuError('未确认')
        self.bridge.notify(self.task())
        self.bridge.notify(self.task())
        self.api.send.assert_called_once()
        self.assertEqual(self.task()['notification'], 'uncertain')

    def test_empty_allowlist_is_not_public_and_secret_is_not_returned(self):
        with self.assertRaises(ValueError):
            self.settings.save_runtime({'feishu_allowed_users': []})
        self.settings.save_runtime({'feishu_app_secret': ''})
        self.assertEqual(self.settings.runtime()['feishu_app_secret'], 'secret-feishu-fixture')
        self.assertNotIn('secret-feishu-fixture', json.dumps(self.settings.runtime(public=True)))
        self.settings.save_runtime({'feishu_enabled': False, 'feishu_allowed_users': []})
        self.bridge.notify(self.task())
        self.api.send.assert_not_called()

    def test_owned_receiver_acknowledges_after_persist_and_stops(self):
        self.bridge.notify(self.task())
        original_popen = subprocess.Popen
        program = '''import json,sys,sqlite3
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
config=json.loads(sys.stdin.readline())
print(json.dumps({'kind':'status','state':'connected'}),flush=True)
print(sys.argv[2],flush=True)
ack=sys.stdin.readline().strip()
conn=sqlite3.connect(sys.argv[1])
ok=ack=='ok' and conn.execute("SELECT status FROM tasks").fetchone()[0]=='ready'
conn.close()
print(json.dumps({'kind':'status','state':'persisted' if ok else 'bad_ack'}),flush=True)
sys.stdin.readline()
'''
        def spawn(args, **kwargs):
            return original_popen([sys.executable, '-u', '-c', program, str(self.db.path), json.dumps(self.event())], **kwargs)
        with patch('cs_duty.feishu.subprocess.Popen', side_effect=spawn):
            self.bridge.start()
        deadline = time.monotonic() + 5
        while self.bridge.status()['state'] not in ('persisted', 'bad_ack', 'error') and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.bridge.status()['state'], 'persisted')
        self.bridge.stop()
        self.assertFalse(self.bridge.status()['running'])
        self.assertIsNotNone(self.bridge.process.poll())


class FeishuAPICase(unittest.TestCase):
    def test_token_is_cached_and_errors_hide_response_body(self):
        api = FeishuAPI({'feishu_app_id': 'cli_fixture', 'feishu_app_secret': 'secret-fixture'})
        with patch('cs_duty.feishu.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = [
                io.BytesIO(b'{"code":0,"tenant_access_token":"fixture-token","expire":7200}'),
                io.BytesIO(b'{"code":0,"bot":{"open_id":"ou_bot"}}'),
                io.BytesIO(b'{"code":0,"data":{"message_id":"om_sent"}}'),
                io.BytesIO(b'{"code":999,"msg":"private-response-secret"}'),
            ]
            self.assertEqual(api.bot_id(), 'ou_bot')
            self.assertEqual(api.send('oc_test', '测试', 'task-id'), 'om_sent')
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer fixture-token')
            with self.assertRaises(FeishuError) as error:
                api.send('oc_test', '测试', 'other')
            self.assertNotIn('private-response-secret', str(error.exception))
            self.assertEqual(opener.return_value.open.call_count, 4)
