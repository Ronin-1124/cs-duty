"""OCR engines over client screenshots, behind a replaceable protocol."""
from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from PIL import Image

CJK = '\u3000-\u303f\u4e00-\u9fff\uff00-\uffef'


class OcrUnavailable(RuntimeError):
    """No usable OCR engine or language pack is installed."""


@dataclass(frozen=True)
class TextLine:
    text: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


def join_cjk(text: str) -> str:
    """Windows OCR separates CJK glyphs with spaces; restore readable text."""
    text = re.sub(r'[ \t]+', ' ', text)
    return re.sub(rf'(?<=[{CJK}]) (?=[{CJK}])', '', text).strip()


class OcrEngine(Protocol):
    def read_lines(self, png: bytes) -> list[TextLine]:
        """Return recognized lines with pixel boxes relative to the PNG."""


def lines_from_result(result) -> list[TextLine]:
    """Convert a RapidOCR result into text lines with pixel boxes."""
    boxes = result.boxes if getattr(result, 'boxes', None) is not None else []
    texts = result.txts if getattr(result, 'txts', None) is not None else []
    lines = []
    for box, text in zip(boxes, texts):
        text = str(text).strip()
        if not text:
            continue
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        left, top = int(min(xs)), int(min(ys))
        lines.append(TextLine(text=text, x=left, y=top,
                              width=int(max(xs)) - left, height=int(max(ys)) - top))
    return lines


class RapidOcrEngine:
    """Offline PP-OCR (RapidOCR + ONNXRuntime); stronger on small UI text."""

    def __init__(self):
        try:
            from rapidocr import RapidOCR
        except Exception as exc:
            raise OcrUnavailable('缺少 rapidocr/onnxruntime 依赖，请重新运行 setup.cmd') from exc
        logging.getLogger('RapidOCR').setLevel(logging.ERROR)
        self._engine = RapidOCR()

    def read_lines(self, png: bytes) -> list[TextLine]:
        image = np.asarray(Image.open(io.BytesIO(png)).convert('RGB'))
        return lines_from_result(self._engine(image))


class WindowsOcrEngine:
    """Offline OCR through the Windows.Media.Ocr API (no extra runtime)."""

    def __init__(self, language: str = 'zh-Hans-CN'):
        self.language = language
        self._engine = self._create_engine(language)

    @staticmethod
    def available() -> bool:
        try:
            from winrt.windows.media.ocr import OcrEngine as Engine
        except Exception:
            return False
        return Engine.available_recognizer_languages is not None

    @staticmethod
    def _create_engine(language: str):
        try:
            from winrt.windows.media.ocr import OcrEngine as Engine
        except Exception as exc:
            raise OcrUnavailable('缺少 winrt OCR 组件，请重新运行 setup.cmd 安装依赖') from exc
        try:
            from winrt.windows.globalization import Language
            engine = Engine.try_create_from_language(Language(language))
        except Exception:
            engine = None
        if engine is None:
            engine = Engine.try_create_from_user_profile_languages()
        if engine is None:
            raise OcrUnavailable('系统未安装中文 OCR 语言包，请在 Windows 设置中添加中文识别语言')
        return engine

    def read_lines(self, png: bytes) -> list[TextLine]:
        return asyncio.run(self._read(png))

    async def _read(self, payload: bytes) -> list[TextLine]:
        from winrt.windows.graphics.imaging import BitmapDecoder
        from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(payload)
        await writer.store_async()
        await writer.flush_async()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async()
        result = await self._engine.recognize_async(bitmap)
        lines = []
        for line in result.lines:
            text = join_cjk(line.text)
            if not text:
                continue
            boxes = [word.bounding_rect for word in line.words]
            left = int(min(box.x for box in boxes))
            top = int(min(box.y for box in boxes))
            right = int(max(box.x + box.width for box in boxes))
            bottom = int(max(box.y + box.height for box in boxes))
            lines.append(TextLine(text=text, x=left, y=top, width=right - left, height=bottom - top))
        return lines


ENGINES = {'windows': WindowsOcrEngine, 'rapidocr': RapidOcrEngine}


def create_ocr_engine(name: str = 'windows', language: str = 'zh-Hans-CN'):
    key = (name or 'windows').strip().lower()
    if key == 'rapidocr':
        return RapidOcrEngine()
    return WindowsOcrEngine(language)
