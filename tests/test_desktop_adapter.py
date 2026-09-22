import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from cs_duty.database import Database
from cs_duty.desktop.adapter import DesktopAdapter, parse_session_rows, parse_transcript, transcript_signature
from cs_duty.desktop.linkr import ScreenShot
from cs_duty.desktop.ocr import TextLine
from cs_duty.desktop.window import WindowInfo
from cs_duty.settings import Settings
from cs_duty.transport import TransportNotReady

ROOT = Path(__file__).resolve().parents[1]
SLOTS = ROOT / 'experiments' / 'dongdong_slots.json'


def line(text, x, y, width=None, height=24):
    return TextLine(text=text, x=x, y=y, width=width or max(24, len(text) * 18), height=height)


def frame(color='white', size=(1000, 600)):
    image = Image.new('RGB', size, color)
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class ParserTests(unittest.TestCase):
    def test_session_rows_group_and_trim_date(self):
        lines = [
            line('最近联系人', 10, 5, height=20),
            line('张三', 10, 40),
            line('还有货吗', 120, 40, height=20),
            line('10:24', 220, 42, width=44, height=18),
            line('李四', 10, 90),
            line('谢谢', 120, 90, height=20),
        ]
        rows = parse_session_rows(lines)
        self.assertEqual([row['name'] for row in rows], ['张三', '李四'])
        self.assertEqual(rows[0]['group'], '最近联系人')
        self.assertEqual(rows[0]['date'], '10:24')
        self.assertEqual(rows[0]['preview'], '还有货吗')

    def test_session_rows_keep_leave_group(self):
        rows = parse_session_rows([line('留言', 10, 5, height=20), line('王五', 10, 40, height=20)])
        self.assertEqual(rows[0]['group'], '留言')

    def test_qianniu_rows_merge_multiline_and_skip_headers(self):
        lines = [
            line('正在接待 1', 0, 10, height=18), line('最后一句消息', 150, 10, height=18),
            line('快乐的小布丁 123', 50, 100, height=20),
            line('托管中 ] 亲，全店可开票', 50, 130, height=18),
            line('0', 200, 300, height=18),
            line('智能助手', 100, 400, height=18),
        ]
        rows = parse_session_rows(lines)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], '快乐的小布丁123')
        self.assertIn('托管中', rows[0]['preview'])

    def test_transcript_signature_tracks_count_and_last_bubble(self):
        messages = [{'role': 'customer', 'text': '你好'}, {'role': 'agent', 'text': '在的'}]
        self.assertEqual(transcript_signature(messages), (2, 'agent', '在的'))
        self.assertEqual(transcript_signature([]), (0, '', ''))
        changed = messages + [{'role': 'customer', 'text': '型号呢'}]
        self.assertNotEqual(transcript_signature(messages), transcript_signature(changed))

    def test_transcript_uses_avatar_margins_for_roles(self):
        image = Image.new('RGB', (600, 200), 'white')
        draw = ImageDraw.Draw(image)
        draw.ellipse((2, 30, 30, 58), fill=(30, 30, 30))
        draw.ellipse((570, 120, 598, 148), fill=(240, 130, 40))
        lines = [line('你好', 40, 34, height=22), line('在的', 400, 124, height=22)]
        messages = parse_transcript(lines, 300, image=image)
        self.assertEqual([message['role'] for message in messages], ['customer', 'agent'])

    def test_transcript_assigns_roles_and_times(self):
        lines = [
            line('在吗', 20, 40, height=22),
            line('10:01', 20, 70, width=40, height=16),
            line('在的', 250, 90, height=22),
        ]
        messages = parse_transcript(lines, 150)
        self.assertEqual([message['role'] for message in messages], ['customer', 'agent'])
        self.assertEqual(messages[0]['timestamp'], '10:01')

    def test_transcript_duplicate_text_gets_distinct_ids(self):
        messages = parse_transcript([line('哦', 20, 40), line('哦', 20, 90)], 150)
        self.assertEqual(len(messages), 2)
        self.assertNotEqual(messages[0]['id'], messages[1]['id'])
        self.assertTrue(messages[1]['id'].endswith('#2'))


class FakeLinkr:
    def __init__(self):
        self.frames = []

    def snapshot(self, window_rect=None):
        png = self.frames.pop(0) if self.frames else frame()
        return ScreenShot(png=png, width=1000, height=600, left=0, top=0, frame_width=1000, frame_height=600)

    def click_image(self, shot, px, py):
        self.last_click = (px, py)
        return (px / shot.frame_width, py / shot.frame_height)

    def paste(self):
        self.pastes = getattr(self, 'pastes', 0) + 1

    def control(self, events):
        self.events = events
        return {'code': 0}

    def close(self):
        self.closed = True


class FakeOcr:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls = 0

    def read_lines(self, png):
        self.calls += 1
        return self.scripts.pop(0) if self.scripts else []


class FakeWindows:
    def __init__(self, windows=()):
        self.windows = list(windows)
        self.focused = []

    def find_windows(self, title='', process='', include_minimized=True):
        return [window for window in self.windows if not process or window.process == process]

    def focus_window(self, hwnd, **kwargs):
        self.focused.append(hwnd)
        return True


def window(process='jdm_dd_workbench', minimized=False):
    return WindowInfo(hwnd=7, title='客服工作台', class_name='Win32', process=process,
                      rect=(0, 0, 1000, 600), visible=True, minimized=minimized)


SESSION_LINES = [line('最近联系人', 5, 2, height=18), line('张三', 5, 40), line('还有货吗', 70, 42, height=20),
                 line('10:24', 150, 42, width=40, height=16)]
CHAT_LINES = [line('你好', 10, 40, height=22), line('在的，有什么能帮您的吗', 400, 90, height=22)]
REPLY = '在的，有什么能帮您的吗'


class AdapterFlowTests(unittest.TestCase):
    def adapter(self, scripts, windows=None, config=None):
        settings = {'slots_path': str(SLOTS), 'max_sessions': 100, **(config or {})}
        adapter = DesktopAdapter(settings, ROOT, linkr=FakeLinkr(), ocr=FakeOcr(scripts),
                                 windows=FakeWindows([window()] if windows is None else windows),
                                 clipboard=lambda text: None)
        adapter.stability_seconds = 0
        return adapter

    def test_start_requires_a_client_window(self):
        adapter = self.adapter([], windows=[])
        with self.assertRaisesRegex(TransportNotReady, '未找到客户端窗口'):
            adapter.start()

    def test_start_focuses_and_validates_snapshot(self):
        adapter = self.adapter([])
        adapter.start()
        self.assertEqual(adapter.window.hwnd, 7)
        self.assertTrue(adapter._windows.focused)

    def test_start_falls_back_to_env_for_linkr_credentials(self):
        from unittest.mock import patch
        adapter = self.adapter([], config={'linkr_url': '', 'linkr_token': ''})
        adapter._linkr = None
        with patch('cs_duty.desktop.adapter.LinkrClient') as linkr, \
                patch('cs_duty.desktop.adapter.load_dotenv') as loader:
            adapter.start()
        loader.assert_called_once()
        self.assertEqual(linkr.call_args.args[:2], (None, None))

    def test_customers_marks_first_scan_as_initial_history(self):
        adapter = self.adapter([SESSION_LINES, SESSION_LINES + [line('李四', 5, 90)]])
        adapter.start()
        items = adapter.customers()
        self.assertEqual([item['name'] for item in items], ['张三'])
        self.assertTrue(items[0]['initial_history'])
        items = adapter.customers()
        self.assertEqual([item['name'] for item in items], ['张三', '李四'])
        self.assertTrue(items[0]['initial_history'])
        self.assertFalse(items[1]['initial_history'])

    def test_window_title_selects_the_reception_window(self):
        workbench = WindowInfo(hwnd=1, title='千牛工作台', class_name='Qt', process='AliWorkbench',
                               rect=(0, 0, 1000, 600), visible=True, minimized=False)
        reception = WindowInfo(hwnd=2, title='千牛接待台', class_name='Qt', process='AliWorkbench',
                               rect=(0, 0, 800, 500), visible=True, minimized=False)
        with tempfile.TemporaryDirectory() as temp:
            payload = json.loads(SLOTS.read_text(encoding='utf-8'))
            payload.update(process='AliWorkbench', window_title='千牛接待台')
            path = Path(temp) / 'slots.json'
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            adapter = self.adapter([], windows=[workbench, reception], config={'slots_path': str(path)})
            adapter.start()
        self.assertEqual(adapter.window.hwnd, 2)

    def test_duplicate_display_names_are_skipped(self):
        adapter = self.adapter([[line('张三', 5, 40), line('张三', 5, 90)]])
        adapter.start()
        self.assertEqual(adapter.customers(), [])

    def test_open_customer_clicks_row_and_reads_stable_transcript(self):
        adapter = self.adapter([SESSION_LINES, CHAT_LINES, CHAT_LINES])
        adapter.start()
        messages = adapter.open_customer('张三')
        self.assertEqual(messages[-1]['text'], REPLY)
        self.assertEqual(len(messages), 2)

    def test_fill_draft_pastes_and_reads_back(self):
        adapter = self.adapter([SESSION_LINES, CHAT_LINES, CHAT_LINES, [],
                                [line(REPLY, 10, 20, height=28)], CHAT_LINES])
        adapter.start()
        source_id = parse_transcript(CHAT_LINES, 200)[-1]['id']
        status, reason = adapter.fill_draft('张三', source_id, REPLY, lambda: True)
        self.assertEqual(status, 'filled', reason)
        self.assertEqual(adapter._linkr.pastes, 1)
        self.assertEqual(adapter.owned_drafts['张三'], REPLY)

    def test_fill_draft_reports_stale_when_transcript_moved_on(self):
        adapter = self.adapter([SESSION_LINES, CHAT_LINES, CHAT_LINES])
        adapter.start()
        status, reason = adapter.fill_draft('张三', 'missing-id', REPLY, lambda: True)
        self.assertEqual(status, 'stale')
        self.assertIn('新消息', reason)

    def test_send_is_disabled_by_default(self):
        adapter = self.adapter([])
        adapter.start()
        status, reason = adapter.send('张三', 'x', REPLY, lambda: True)
        self.assertEqual(status, 'draft')
        self.assertIn('未启用', reason)

    def test_send_with_uncalibrated_slot_keeps_draft(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = json.loads(SLOTS.read_text(encoding='utf-8'))
            payload['slots']['send'] = {'x': None, 'y': None, 'label': 'uncalibrated'}
            path = Path(temp) / 'slots.json'
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            scripts = [SESSION_LINES, CHAT_LINES, CHAT_LINES, [], [line(REPLY, 10, 20, height=28)], CHAT_LINES, CHAT_LINES]
            adapter = self.adapter(scripts, config={'allow_send': True, 'slots_path': str(path)})
            adapter.start()
            source_id = parse_transcript(CHAT_LINES, 200)[-1]['id']
            status, reason = adapter.send('张三', source_id, REPLY, lambda: True)
        self.assertEqual(status, 'draft')
        self.assertIn('发送未标定', reason)


class DesktopSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / 'business.sqlite3')
        self.settings = Settings(self.db, None)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_desktop_transport_is_accepted_and_token_hidden(self):
        self.settings.save_runtime({'transport': 'desktop', 'linkr_url': 'http://linkr-usb.local',
                                    'linkr_token': 'secret-fixture', 'allow_send': False})
        config = self.settings.runtime()
        self.assertEqual(config['transport'], 'desktop')
        self.assertEqual(config['linkr_token'], 'secret-fixture')
        public = self.settings.runtime(public=True)
        self.assertNotIn('linkr_token', public)
        self.assertTrue(public['has_linkr_token'])

    def test_blank_linkr_token_keeps_the_saved_one(self):
        self.settings.save_runtime({'transport': 'desktop', 'linkr_token': 'secret-fixture'})
        self.settings.save_runtime({'transport': 'desktop', 'linkr_token': ''})
        self.assertEqual(self.settings.runtime()['linkr_token'], 'secret-fixture')

    def test_slots_path_is_saved(self):
        self.settings.save_runtime({'slots_path': 'experiments/qianniu_slots.json'})
        self.assertEqual(self.settings.runtime()['slots_path'], 'experiments/qianniu_slots.json')

    def test_ocr_engine_defaults_to_rapidocr_and_rejects_unknown(self):
        self.assertEqual(self.settings.runtime()['ocr_engine'], 'rapidocr')
        with self.assertRaisesRegex(ValueError, 'OCR 引擎'):
            self.settings.save_runtime({'ocr_engine': 'magic'})

    def test_invalid_linkr_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Linkr 地址'):
            self.settings.save_runtime({'transport': 'desktop', 'linkr_url': 'not-a-url'})

    def test_default_adapter_factory_builds_the_desktop_adapter(self):
        from cs_duty.server import default_adapter_factory
        adapter = default_adapter_factory({'transport': 'desktop'}, ROOT)
        self.assertEqual(type(adapter).__name__, 'DesktopAdapter')


if __name__ == '__main__':
    unittest.main()
