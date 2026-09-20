import json
import tempfile
import unittest
from pathlib import Path

from cs_duty.desktop.slots import Panel, SlotError, load_slots

ROOT = Path(__file__).resolve().parents[1]
SLOTS = ROOT / 'experiments' / 'dongdong_slots.json'
QIANNIU = ROOT / 'experiments' / 'qianniu_slots.json'


class SlotTableTests(unittest.TestCase):
    def test_calibrated_points_load(self):
        table = load_slots(SLOTS)
        self.assertEqual(table.process, 'jdm_dd_workbench')
        self.assertIn('letterbox', table.layout)
        self.assertEqual(table.point('compose'), (0.42, 0.665))
        self.assertEqual(table.point_pixels('compose', 1000, 500), (420, 332))
        self.assertEqual(table.point('send'), (0.537, 0.977))

    def test_uncalibrated_point_is_reported(self):
        table = load_slots(SLOTS)
        with self.assertRaisesRegex(SlotError, '尚未标定'):
            table.point('session_recent')

    def test_panel_pixels_scale(self):
        table = load_slots(SLOTS)
        self.assertEqual(table.panel('session_list'), Panel(0.040, 0.210, 0.205, 0.950))
        self.assertEqual(table.panel_pixels('session_list', 1000, 500), (40, 105, 205, 475))

    def test_qianniu_table_targets_the_reception_window(self):
        table = load_slots(QIANNIU)
        self.assertEqual(table.process, 'AliWorkbench')
        self.assertEqual(table.window_title, '千牛接待台')
        self.assertEqual(table.point('send'), (0.9575, 0.939))
        self.assertEqual(table.panel('chat'), Panel(0.237, 0.225, 0.990, 0.670))

    def test_missing_panel_is_reported(self):
        table = load_slots(SLOTS)
        with self.assertRaisesRegex(SlotError, '缺少面板'):
            table.panel('missing')

    def test_missing_file_is_reported(self):
        with self.assertRaisesRegex(SlotError, '不存在'):
            load_slots(Path('no-such-slots.json'))

    def test_malformed_values_are_rejected(self):
        cases = [
            ({'process': 'x', 'layout': 'y', 'slots': {'a': {'x': 1.2, 'y': 0.1}}, 'panels': {}}, '0–1'),
            ({'process': 'x', 'layout': 'y', 'slots': {'a': {'x': 0.1, 'y': None}}, 'panels': {}}, '同时'),
            ({'process': 'x', 'layout': 'y', 'slots': {}, 'panels': {'p': [0.5, 0.1, 0.2, 0.9]}}, '左上小于右下'),
            ({'process': '', 'layout': 'y', 'slots': {}, 'panels': {}}, '进程名'),
            ('not-a-dict', '对象'),
        ]
        for payload, hint in cases:
            with self.subTest(hint=hint), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / 'slots.json'
                path.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaisesRegex(SlotError, hint):
                    load_slots(path)


if __name__ == '__main__':
    unittest.main()
