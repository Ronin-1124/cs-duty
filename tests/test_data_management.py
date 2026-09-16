import io
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from cs_duty.data_management import delete_conversations, export_workspace, restore_workspace
from cs_duty.server import Application
from cs_duty.workflow import Workflow


class DataManagementCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = Application(self.root / 'source', 'http://127.0.0.1:18766', env_path=None, import_root=None)
        self.addCleanup(self.app.close)
        self.db = self.app.db
        self.old = {'id': 'old', 'role': 'customer', 'text': 'private-old-text'}
        self.cid, _ = self.db.ingest('jingmai', 'test', 'buyer', '测试客户', [self.old])
        self.other, _ = self.db.ingest('jingmai', 'test', 'other', '保留客户', [{'id': 'other-old', 'role': 'customer', 'text': '保留消息'}])
        self.db.prepare_reply(self.cid, 'old', '草稿', 'draft')
        tid = self.db.create_task(self.cid, 'old', '待确认', {'product': 'TEST'})
        self.db.execute('INSERT INTO feishu_task_messages VALUES(?,?,?,?)', (tid, 'om_task', 'oc_group', 'cli_fixture'))
        self.db.execute('INSERT INTO feishu_receipts VALUES(?,?,?,?,?)', ('om_result', tid, 'ou_owner', 'oc_group', 1))
        self.app.knowledge.import_csv('test.csv', '问题,答案\n供电,5V\n')
        self.pid = self.app.settings.save_profile({'name': 'fixture', 'protocol': 'openai', 'model': 'fixture', 'base_url': 'https://example.com/v1', 'api_key': 'secret-test-unique-key'})
        self.app.settings.save_runtime({'feishu_webhook': 'https://open.feishu.cn/open-apis/bot/v2/hook/secret-hook', 'feishu_secret': 'secret-signature', 'feishu_enabled': True, 'feishu_app_secret': 'secret-feishu-app'})
        with closing(sqlite3.connect(self.root / 'source' / 'checkpoints.sqlite3', check_same_thread=False)) as conn, conn:
            SqliteSaver(conn).setup()
            for cid in (self.cid, self.other):
                conn.execute("INSERT INTO checkpoints(thread_id,checkpoint_id,checkpoint,type,metadata) VALUES(?, '1', ?, 'json', '{}')", (cid, b'private-checkpoint'))
                conn.execute("INSERT INTO writes(thread_id,checkpoint_id,task_id,idx,channel,type,value) VALUES(?, '1', 'task',0,'channel','json',?)", (cid, b'private-write'))

    def test_clear_removes_associated_state_and_suppresses_old_transcript(self):
        delete_conversations(self.app, self.cid, keep_customer=True)
        self.assertEqual(self.db.conversation(self.cid)['fields'], {})
        self.assertEqual(self.db.history(self.cid), [])
        self.assertFalse(self.db.rows('SELECT * FROM tasks'))
        self.assertFalse(self.db.rows('SELECT * FROM outbox'))
        self.assertFalse(self.db.rows('SELECT * FROM feishu_task_messages'))
        self.assertFalse(self.db.rows('SELECT * FROM feishu_receipts'))
        with closing(sqlite3.connect(self.root / 'source' / 'checkpoints.sqlite3', check_same_thread=False)) as conn, conn:
            self.assertEqual(conn.execute('SELECT thread_id FROM checkpoints').fetchall(), [(self.other,)])
            self.assertEqual(conn.execute('SELECT thread_id FROM writes').fetchall(), [(self.other,)])
        self.db.ingest('jingmai', 'test', 'buyer', '测试客户', [self.old])
        self.assertEqual(self.db.history(self.cid), [])
        self.db.ingest('jingmai', 'test', 'buyer', '测试客户', [self.old, {'id': 'new', 'role': 'customer', 'text': '新问题'}])
        self.assertEqual([m['id'] for m in self.db.history(self.cid)], ['new'])
        self.assertTrue(self.db.history(self.other))
        self.assertTrue(self.app.knowledge.search('5V供电'))
        self.assertEqual(self.app.settings.profile(self.pid)['api_key'], 'secret-test-unique-key')

    def test_delete_customer_not_recreated_by_old_messages(self):
        delete_conversations(self.app, self.cid)
        self.db.ingest('jingmai', 'test', 'buyer', '测试客户', [self.old])
        self.assertIsNone(self.db.one('SELECT id FROM conversations WHERE id=?', (self.cid,)))
        self.assertTrue(self.db.history(self.other))

    def test_mock_clear_and_delete_all(self):
        user = self.app.fixture.create_user('模拟删除测试')
        msg = self.app.fixture.send_customer(user['id'], '待清理')
        cid, _ = self.db.ingest('mock', 'local-shop', 'name:模拟删除测试', user['name'], [self.old])
        delete_conversations(self.app, cid, keep_customer=True)
        self.assertEqual(self.app.fixture.get_chat(user['id'])['messages'], [])
        delete_conversations(self.app, all_customers=True)
        self.assertFalse(self.app.fixture.list_users())
        self.assertFalse(self.db.rows('SELECT * FROM conversations'))
        self.assertTrue(self.db.rows('SELECT * FROM knowledge'))

    def test_active_or_paused_runtime_blocks_changes_and_export(self):
        with patch.object(self.app.runtime, 'status', return_value={'running': True, 'state': 'paused'}):
            with self.assertRaisesRegex(ValueError, '停止接待'):
                delete_conversations(self.app, self.cid)
            with self.assertRaisesRegex(ValueError, '停止接待'):
                export_workspace(self.app)
        self.assertTrue(self.db.history(self.cid))

    def test_feishu_receiver_blocks_cleanup_and_export(self):
        with patch.object(self.app.feishu, 'status', return_value={'running': True}):
            with self.assertRaisesRegex(ValueError, '断开飞书'):
                delete_conversations(self.app, self.cid)
            with self.assertRaisesRegex(ValueError, '断开飞书'):
                export_workspace(self.app)

    def test_export_restore_roundtrip_and_secret_exclusion(self):
        self.app.settings.save_runtime({'mock_url': 'http://127.0.0.1:19999/workbench'})
        data = export_workspace(self.app)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in archive.namelist():
                for secret in (b'secret-test-unique-key', b'secret-hook', b'secret-signature', b'secret-feishu-app'):
                    self.assertNotIn(secret, archive.read(name))
        package = self.root / 'export.zip'
        package.write_bytes(data)
        result = restore_workspace(package, self.root / 'restored')
        restored = Application(Path(result['directory']), 'http://127.0.0.1:19000', env_path=None, import_root=None)
        try:
            self.assertEqual(restored.db.history(self.cid), self.db.history(self.cid))
            self.assertEqual(len(restored.db.rows('SELECT * FROM tasks')), 1)
            self.assertEqual(restored.db.one('SELECT api_key FROM profiles')['api_key'], '')
            self.assertFalse(restored.settings.runtime()['feishu_enabled'])
            self.assertEqual(restored.settings.runtime()['mock_url'], 'http://127.0.0.1:19000/workbench')
            self.assertTrue(restored.knowledge.search('5V供电'))
            self.assertFalse(restored.runtime.status()['running'])
        finally:
            restored.close()
        with self.assertRaisesRegex(ValueError, '已存在'):
            restore_workspace(package, self.root / 'restored')
        self.assertEqual(self.app.settings.profile(self.pid)['api_key'], 'secret-test-unique-key')

    def test_include_secrets_and_corrupt_archive(self):
        data = export_workspace(self.app, True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            self.assertIn(b'secret-test-unique-key', archive.read('business.sqlite3'))
            contents = {name: archive.read(name) for name in archive.namelist()}
        package = self.root / 'broken.zip'
        contents['mock.json'] = b'{}'
        with zipfile.ZipFile(package, 'w') as archive:
            for name, value in contents.items():
                archive.writestr(name, value)
        with self.assertRaisesRegex(ValueError, '校验失败'):
            restore_workspace(package, self.root / 'broken-target')
        self.assertFalse((self.root / 'broken-target').exists())
        with zipfile.ZipFile(package, 'w') as archive:
            archive.writestr('../escaped', b'x')
        with self.assertRaisesRegex(ValueError, '未知文件'):
            restore_workspace(package, self.root / 'broken-target')
        self.assertFalse((self.root / 'escaped').exists())

    def test_waiting_workflow_resumes_after_migration(self):
        cid, _ = self.db.ingest('jingmai', 'test', 'waiting', '售后测试', [{'id': 'w1', 'role': 'customer', 'text': '订单发货进度需要核实'}])
        config = {'configurable': {'thread_id': cid}}
        class Model:
            def __init__(self, profile):
                pass
            def plan(self, persona, context):
                return {'intent': 'after_sales', 'reply': context['colleague_result'] or '我帮您确认一下。', 'fields': {}}
        with closing(sqlite3.connect(self.root / 'source' / 'checkpoints.sqlite3', check_same_thread=False)) as conn:
            graph = Workflow(self.db, self.app.settings, self.app.knowledge, SqliteSaver(conn), Model).graph
            graph.invoke(self.app.runtime._input(self.db.conversation(cid)), config)
            self.assertIn('wait_colleague', graph.get_state(config).next)
        package = self.root / 'waiting.zip'
        package.write_bytes(export_workspace(self.app, True))
        restore_workspace(package, self.root / 'waiting-restored')
        restored = Application(self.root / 'waiting-restored', 'http://127.0.0.1:18766', env_path=None, import_root=None)
        try:
            with closing(sqlite3.connect(self.root / 'waiting-restored' / 'checkpoints.sqlite3', check_same_thread=False)) as conn:
                graph = Workflow(restored.db, restored.settings, restored.knowledge, SqliteSaver(conn), Model).graph
                self.assertIn('wait_colleague', graph.get_state(config).next)
                graph.invoke(Command(resume={'result': '已核实，订单预计明天发出。', 'messages': restored.db.history(cid), 'source_id': 'w1'}), config)
                self.assertFalse(graph.get_state(config).next)
                self.assertIn('订单预计明天发出', restored.db.one('SELECT reply FROM outbox WHERE conversation_id=?', (cid,))['reply'])
        finally:
            restored.close()
