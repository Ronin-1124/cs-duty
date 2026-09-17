import io
import unittest

from PIL import Image, ImageDraw, ImageFont

from cs_duty.desktop.ocr import OcrUnavailable, WindowsOcrEngine, join_cjk


class JoinCjkTests(unittest.TestCase):
    def test_spaces_between_han_characters_are_removed(self):
        self.assertEqual(join_cjk('在 吗 ？ 有 货 吗'), '在吗？有货吗')

    def test_latin_words_keep_single_spaces(self):
        self.assertEqual(join_cjk('云米   净水器  1000G'), '云米净水器 1000G')

    def test_empty_text_stays_empty(self):
        self.assertEqual(join_cjk('   '), '')


@unittest.skipUnless(WindowsOcrEngine.available(), 'Windows OCR is not available')
class WindowsOcrTests(unittest.TestCase):
    def engine(self):
        try:
            return WindowsOcrEngine()
        except OcrUnavailable as exc:
            self.skipTest(str(exc))

    def test_reads_rendered_chinese_text(self):
        engine = self.engine()
        try:
            font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 36)
        except OSError:
            self.skipTest('Microsoft YaHei font is not installed')
        image = Image.new('RGB', (760, 120), 'white')
        ImageDraw.Draw(image).text((20, 30), '请帮我查一下这个型号', font=font, fill='black')
        buffer = io.BytesIO()
        image.save(buffer, 'PNG')
        lines = engine.read_lines(buffer.getvalue())
        text = ''.join(line.text for line in lines)
        self.assertIn('型号', text)
        for line in lines:
            self.assertGreater(line.width, 0)
            self.assertGreater(line.height, 0)


if __name__ == '__main__':
    unittest.main()
