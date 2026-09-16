"""Execute the owned SDK worker against a stub lark_oapi to verify the real IPC contract."""
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STUB_LOG = '''\
import logging
import sys

logger = logging.getLogger('StubLark')
logger.addHandler(logging.StreamHandler(sys.stderr))
logger.setLevel(logging.WARNING)
'''

STUB_INIT = '''\
import json as _json
import os
from types import SimpleNamespace

from lark_oapi.core.log import logger


class LogLevel:
    INFO = 20


class _Builder:
    def __init__(self):
        self.callback = None

    def register_p2_im_message_receive_v1(self, callback):
        self.callback = callback
        return self

    def build(self):
        return self


class EventDispatcherHandler:
    @staticmethod
    def builder(encrypt_key, verification_token):
        return _Builder()


def _capture(**values):
    path = os.environ.get('FAKE_LARK_CAPTURE')
    if not path:
        return
    data = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as handle:
            data = _json.load(handle)
    data.update(values)
    with open(path, 'w', encoding='utf-8') as handle:
        _json.dump(data, handle)


def _event():
    text = 'x' * 120000 if os.environ.get('FAKE_LARK_BIG') else '确认下周交货'
    content = _json.dumps({'text': text}, ensure_ascii=False)
    if os.environ.get('FAKE_LARK_MINIMAL'):
        sender = SimpleNamespace(sender_type='user', sender_id=None)
        message = SimpleNamespace(message_id='om_result', chat_id='oc_group', chat_type='group',
            message_type='text', content=content, parent_id=None, mentions=None)
    else:
        sender = SimpleNamespace(sender_type='user', sender_id=SimpleNamespace(open_id='ou_owner'))
        message = SimpleNamespace(message_id='om_result', chat_id='oc_group', chat_type='group',
            message_type='text', content=content, parent_id='om_task',
            mentions=[SimpleNamespace(key='@_user_1', id=SimpleNamespace(open_id='ou_bot'))])
    return SimpleNamespace(header=SimpleNamespace(event_id='evt_fixture'),
        event=SimpleNamespace(sender=sender, message=message))


class _Client:
    def __init__(self, app_id, app_secret, event_handler=None, log_level=None):
        _capture(app_id=app_id, app_secret=app_secret)
        if log_level is not None:
            logger.setLevel(getattr(log_level, 'value', log_level))
        self.handler = event_handler
        self.on_reconnecting = lambda: None

    def start(self):
        if os.environ.get('FAKE_LARK_FAIL'):
            raise RuntimeError('stub connect failure')
        logger.info('connected to wss://stub.feishu/ [conn_id=fixture]')
        self.handler.callback(_event())
        logger.info('disconnected to wss://stub.feishu/ [conn_id=fixture]')


class ws:
    Client = _Client
'''


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        package = self.root / 'stub' / 'lark_oapi'
        (package / 'core').mkdir(parents=True)
        (package / '__init__.py').write_text(STUB_INIT, encoding='utf-8')
        (package / 'core' / '__init__.py').write_text('', encoding='utf-8')
        (package / 'core' / 'log.py').write_text(STUB_LOG, encoding='utf-8')
        self.stub = self.root / 'stub'

    def start(self, **overrides):
        env = {**os.environ, 'PYTHONPATH': str(self.stub),
               'FAKE_LARK_CAPTURE': str(self.root / 'capture.json'), **overrides}
        proc = subprocess.Popen([sys.executable, '-u', '-m', 'cs_duty.feishu_worker'], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', env=env)
        self.addCleanup(self._close, proc)
        proc.stdin.write(json.dumps({'app_id': 'cli_fixture', 'app_secret': 'secret-fixture'}) + '\n')
        proc.stdin.flush()
        lines = queue.Queue()
        threading.Thread(target=self._pump, args=(proc.stdout, lines), daemon=True).start()
        return proc, lines

    @staticmethod
    def _close(proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for pipe in (proc.stdin, proc.stdout):
            pipe.close()

    @staticmethod
    def _pump(stream, lines):
        try:
            for line in stream:
                lines.put(line)
        except ValueError:
            pass

    def read(self, lines, timeout=10):
        try:
            return json.loads(lines.get(timeout=timeout))
        except queue.Empty:
            self.fail('worker 未按预期输出 IPC 消息')

    def test_event_is_acknowledged_only_after_parent_handles_it(self):
        proc, lines = self.start()
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'connected'})
        self.assertEqual(self.read(lines), {
            'kind': 'message', 'event_id': 'evt_fixture', 'message_id': 'om_result',
            'chat_id': 'oc_group', 'chat_type': 'group', 'message_type': 'text',
            'content': json.dumps({'text': '确认下周交货'}, ensure_ascii=False),
            'parent_id': 'om_task', 'sender_id': 'ou_owner', 'sender_type': 'user',
            'mentions': [{'key': '@_user_1', 'id': 'ou_bot'}]})
        with self.assertRaises(queue.Empty):
            lines.get(timeout=0.5)
        proc.stdin.write('ok\n')
        proc.stdin.flush()
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'reconnecting'})
        self.assertEqual(proc.wait(timeout=10), 0)
        captured = json.loads((self.root / 'capture.json').read_text(encoding='utf-8'))
        self.assertEqual(captured, {'app_id': 'cli_fixture', 'app_secret': 'secret-fixture'})

    def test_connect_failure_is_reported(self):
        proc, lines = self.start(FAKE_LARK_FAIL='1')
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'error'})
        self.assertEqual(proc.wait(timeout=10), 1)

    def test_oversized_event_is_dropped_without_waiting_for_ack(self):
        proc, lines = self.start(FAKE_LARK_BIG='1')
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'connected'})
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'reconnecting'})
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_missing_optional_fields_do_not_crash_the_worker(self):
        proc, lines = self.start(FAKE_LARK_MINIMAL='1')
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'connected'})
        message = self.read(lines)
        self.assertEqual(message['sender_id'], '')
        self.assertEqual(message['parent_id'], '')
        self.assertEqual(message['mentions'], [])
        proc.stdin.write('ok\n')
        proc.stdin.flush()
        self.assertEqual(self.read(lines), {'kind': 'status', 'state': 'reconnecting'})
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_pinned_sdk_matches_worker_contract(self):
        program = '''\
import json
import lark_oapi as lark
from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1
from lark_oapi.core.json import JSON

handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(lambda data: None).build()
client = lark.ws.Client('cli_fixture', 'secret-fixture', event_handler=handler, log_level=lark.LogLevel.INFO)
assert callable(client.on_reconnecting)
payload = {'header': {'event_id': 'evt_1'},
    'event': {'sender': {'sender_id': {'open_id': 'ou_owner'}, 'sender_type': 'user'},
        'message': {'message_id': 'om_1', 'chat_id': 'oc_1', 'chat_type': 'group', 'message_type': 'text',
            'content': json.dumps({'text': 'hi'}), 'parent_id': None,
            'mentions': [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}}]}}}
data = JSON.unmarshal(json.dumps(payload), P2ImMessageReceiveV1)
assert data.header.event_id == 'evt_1'
assert data.event.sender.sender_id.open_id == 'ou_owner'
assert data.event.message.parent_id is None
assert data.event.message.mentions[0].id.open_id == 'ou_bot'
print('sdk-contract-ok')
'''
        result = subprocess.run([sys.executable, '-c', program], cwd=ROOT, capture_output=True,
            text=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('sdk-contract-ok', result.stdout)
