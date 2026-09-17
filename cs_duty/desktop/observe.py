"""Read-only client capture for slot calibration. Artifacts contain client text."""
from __future__ import annotations

import io
import json
import time
from pathlib import Path

from PIL import Image, ImageDraw

from cs_duty.desktop.adapter import DesktopAdapter
from cs_duty.desktop.slots import SlotError


def _annotate(adapter, shot, lines):
    image = Image.open(io.BytesIO(shot.png)).convert('RGB')
    draw = ImageDraw.Draw(image)
    for name in adapter.slots.panels:
        draw.rectangle(adapter.slots.panel_pixels(name, image.width, image.height), outline='#e11d48', width=2)
    for name in adapter.slots.slots:
        try:
            x, y = adapter.slots.point_pixels(name, image.width, image.height)
        except SlotError:
            continue
        draw.line((x - 8, y, x + 8, y), fill='#2563eb', width=2)
        draw.line((x, y - 8, x, y + 8), fill='#2563eb', width=2)
    for line in lines:
        draw.rectangle((line.x, line.y, line.x + line.width, line.y + line.height), outline='#059669', width=1)
    return image


def capture(config, data_dir, output=None, wait=True):
    adapter = DesktopAdapter(config, data_dir)
    adapter.start()
    try:
        if wait:
            try:
                input('请在桌面客户端打开一个包含消息的测试会话，然后回到本窗口按回车开始采集…')
            except EOFError:
                pass
        shot = adapter._capture()
        lines = adapter._ocr.read_lines(shot.png)
        image = _annotate(adapter, shot, lines)
        directory = Path(output) if output else data_dir / 'observations'
        directory.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        png_path = directory / f'desktop-{stamp}.png'
        image.save(png_path, 'PNG')
        payload = {'app': 'cs-duty', 'kind': 'desktop-panel', 'captured': time.time(),
                   'process': adapter.slots.process, 'layout': adapter.slots.layout,
                   'window': list(adapter.window.rect), 'image': png_path.name,
                   'lines': [{'text': line.text, 'x': line.x, 'y': line.y,
                              'width': line.width, 'height': line.height} for line in lines]}
        json_path = directory / f'desktop-{stamp}.json'
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
        return str(json_path)
    finally:
        adapter.close()
