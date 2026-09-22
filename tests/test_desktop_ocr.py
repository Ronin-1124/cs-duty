import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from cs_duty.desktop.ocr import (ENGINES, OcrUnavailable, RapidOcrEngine, WindowsOcrEngine,
                                 create_ocr_engine, join_cjk, lines_from_result)


class JoinCjkTests(unittest.TestCase):
    def test_spaces_between_han_characters_are_removed(self):
        self.assertEqual(join_cjk('在 吗 ？ 有 货 吗'), '在吗？有货吗')

    def test_latin_words_keep_single_spaces(self):
        self.assertEqual(join_cjk('云米   净水器  1000G'), '云米净水器 1000G')

    def test_empty_text_stays_empty(self):
        self.assertEqual(join_cjk('   '), '')


def rendered_text(text='请帮我查一下这个型号', size=36):
    try:
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', size)
    except OSError:
        return None
    image = Image.new('RGB', (760, 120), 'white')
    ImageDraw.Draw(image).text((20, 30), text, font=font, fill='black')
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    return buffer.getvalue()


class RapidResultTests(unittest.TestCase):
    def test_boxes_become_text_lines(self):
        result = SimpleNamespace(
            boxes=np.array([[[10, 20], [110, 20], [110, 44], [10, 44]],
                            [[200, 60], [260, 60], [260, 80], [200, 80]]]),
            txts=['你好开专票', '2026-09-20'])
        lines = lines_from_result(result)
        self.assertEqual([line.text for line in lines], ['你好开专票', '2026-09-20'])
        self.assertEqual((lines[0].x, lines[0].y, lines[0].width, lines[0].height), (10, 20, 100, 24))

    def test_empty_result_is_safe(self):
        self.assertEqual(lines_from_result(SimpleNamespace(boxes=None, txts=None)), [])
        self.assertEqual(lines_from_result(SimpleNamespace(boxes=[], txts=[])), [])


class EngineFactoryTests(unittest.TestCase):
    def test_factory_selects_engine(self):
        with patch.dict(ENGINES, {'windows': unittest.mock.MagicMock(), 'rapidocr': unittest.mock.MagicMock()}):
            with patch('cs_duty.desktop.ocr.WindowsOcrEngine') as windows, \
                    patch('cs_duty.desktop.ocr.RapidOcrEngine') as rapid:
                create_ocr_engine('rapidocr')
                rapid.assert_called_once_with()
                create_ocr_engine('windows', 'zh-Hans-CN')
                windows.assert_called_once_with('zh-Hans-CN')

    def test_unknown_engine_falls_back_to_windows(self):
        with patch('cs_duty.desktop.ocr.RapidOcrEngine') as rapid:
            create_ocr_engine('nonsense')
            rapid.assert_not_called()


@unittest.skipUnless(WindowsOcrEngine.available(), 'Windows OCR is not available')
class WindowsOcrTests(unittest.TestCase):
    def engine(self):
        try:
            return WindowsOcrEngine()
        except OcrUnavailable as exc:
            self.skipTest(str(exc))

    def test_reads_rendered_chinese_text(self):
        payload = rendered_text()
        if payload is None:
            self.skipTest('Microsoft YaHei font is not installed')
        lines = self.engine().read_lines(payload)
        text = ''.join(line.text for line in lines)
        self.assertIn('型号', text)
        for line in lines:
            self.assertGreater(line.width, 0)
            self.assertGreater(line.height, 0)


class RapidOcrTests(unittest.TestCase):
    def engine(self):
        try:
            return RapidOcrEngine()
        except OcrUnavailable as exc:
            self.skipTest(str(exc))
        except Exception as exc:
            self.skipTest(f'rapidocr unavailable: {exc}')

    def test_reads_rendered_chinese_text(self):
        payload = rendered_text()
        if payload is None:
            self.skipTest('Microsoft YaHei font is not installed')
        lines = self.engine().read_lines(payload)
        text = ''.join(line.text for line in lines)
        self.assertIn('型号', text)
        for line in lines:
            self.assertGreater(line.width, 0)
            self.assertGreater(line.height, 0)


if __name__ == '__main__':
    unittest.main()
