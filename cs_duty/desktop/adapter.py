"""Desktop client adapter: Linkr screenshots, Windows OCR, and HID input."""
from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path

from PIL import Image
from dotenv import load_dotenv

from cs_duty.database import ROOT
from cs_duty.desktop.clipboard import set_clipboard_text
from cs_duty.desktop.linkr import LinkrClient
from cs_duty.desktop.ocr import WindowsOcrEngine
from cs_duty.desktop.slots import SlotError, load_slots
from cs_duty.desktop.window import find_windows, focus_window
from cs_duty.transport import ReadThrottle, TransportNotReady, comparable_text

TIME_RE = re.compile(r'(\d{1,2}[:：]\d{2})')
GROUP_RE = re.compile(r'^(在线咨询|正在咨询|最近联系人|全部|留言|其他)$')
COMPOSE_MARGIN = (340, 70)


def _median_height(lines):
    heights = sorted(line.height for line in lines)
    return heights[len(heights) // 2] if heights else 20


def _clusters(lines, tolerance):
    groups = []
    for line in sorted(lines, key=lambda item: item.center_y):
        if groups and line.center_y - groups[-1][-1].center_y <= tolerance:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _bounds(cluster):
    left = min(line.x for line in cluster)
    top = min(line.y for line in cluster)
    right = max(line.x + line.width for line in cluster)
    bottom = max(line.y + line.height for line in cluster)
    return left, top, right, bottom


def parse_session_rows(lines):
    """Group OCR lines into session rows: name, preview, date, group, box."""
    rows, group, tolerance = [], '', max(6.0, _median_height(lines) * 0.9)
    for cluster in _clusters(lines, tolerance):
        cluster.sort(key=lambda line: line.x)
        if len(cluster) == 1 and GROUP_RE.match(cluster[0].text.strip()):
            group = cluster[0].text.strip()
            continue
        name = cluster[0].text.strip()
        body = cluster[1:]
        date = ''
        if body and len(body[-1].text.strip()) <= 12 and TIME_RE.search(body[-1].text):
            date = body[-1].text.strip()
            body = body[:-1]
        left, top, right, bottom = _bounds(cluster)
        rows.append({'name': name, 'preview': ' '.join(line.text for line in body).strip(), 'date': date,
                     'group': group, 'x': left, 'y': top, 'width': right - left, 'height': bottom - top})
    return rows


def parse_transcript(lines, midpoint):
    """Group OCR lines into chat bubbles, assigning role by horizontal position."""
    if not lines:
        return []
    tolerance = max(6.0, _median_height(lines) * 0.8)
    messages = []
    for cluster in _clusters(lines, tolerance):
        cluster.sort(key=lambda line: (line.y, line.x))
        left, top, right, bottom = _bounds(cluster)
        text = '\n'.join(line.text for line in cluster).strip()
        if not text:
            continue
        if TIME_RE.fullmatch(text) and messages:
            messages[-1]['timestamp'] = text
            continue
        messages.append({'role': 'agent' if (left + right) / 2 > midpoint else 'customer',
                         'text': text, 'timestamp': ''})
    counts = {}
    for message in messages:
        digest = hashlib.sha1(f"{message['role']}|{message['text']}|{message['timestamp']}".encode()).hexdigest()[:16]
        counts[digest] = counts.get(digest, 0) + 1
        message['id'] = digest if counts[digest] == 1 else f'{digest}#{counts[digest]}'
    return messages


class DesktopAdapter(ReadThrottle):
    """Runtime adapter over the desktop client window. All I/O goes through Linkr."""

    stability_seconds = 0.45
    read_interval = 0.15
    transcript_deadline = 8.0

    def __init__(self, config, data_dir: Path, *, linkr=None, ocr=None, windows=None, clipboard=None):
        super().__init__()
        self.config, self.data_dir = config, data_dir
        self._linkr, self._ocr = linkr, ocr
        self._windows = windows or _WindowApi()
        self._clipboard = clipboard or set_clipboard_text
        self._owns_linkr = linkr is None
        self.window, self.slots = None, None
        self.cursor = 0
        self.confirmed_message = None
        self.initial_keys = None
        self.owned_drafts = {}
        self.cancelled = lambda: False

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        self.slots = load_slots(self.config.get('slots_path') or ROOT / 'experiments' / 'dongdong_slots.json')
        if self._linkr is None:
            # The project .env is the documented fallback for LINKR_BASE_URL/LINKR_TOKEN.
            load_dotenv(ROOT / '.env')
            self._linkr = LinkrClient(self.config.get('linkr_url') or None, self.config.get('linkr_token') or None)
        if self._ocr is None:
            self._ocr = WindowsOcrEngine(self.config.get('ocr_language') or 'zh-Hans-CN')
        self.window = self._locate_window()
        self._capture()

    def close(self):
        linkr, self._linkr = self._linkr, None
        if linkr and self._owns_linkr:
            linkr.close()

    # -- runtime interface -------------------------------------------------
    def customers(self):
        shot = self._capture()
        rows = self._scan_sessions(shot)
        counts = {}
        for row in rows:
            counts[row['name']] = counts.get(row['name'], 0) + 1
        kept = [row for row in rows if counts[row['name']] == 1]
        if self.initial_keys is None:
            self.initial_keys = {row['name'] for row in kept}
        items = [{'customer_key': row['name'], 'name': row['name'], 'preview': row['preview'],
                  'date': row['date'], 'group': row['group'], 'initial_history': row['name'] in self.initial_keys}
                 for row in kept]
        if not items:
            return []
        start = self.cursor % len(items)
        limit = max(1, int(self.config.get('max_sessions', 100)))
        ordered = items[start:] + items[:start]
        self.cursor = (start + limit) % len(items)
        return ordered[:limit]

    def open_customer(self, name, key=''):
        shot = self._capture()
        box, lines = self._read_panel(shot, 'session_list')
        rows = [row for row in parse_session_rows(lines) if row['name'] == name]
        if not rows:
            raise TransportNotReady('会话列表中找不到目标会话，请确认客户端已加载')
        if len(rows) > 1:
            raise TransportNotReady('存在无法区分的同名会话，已跳过以避免上下文混淆')
        row = rows[0]
        self._linkr.click_image(shot, box[0] + row['x'] + row['width'] / 2, box[1] + row['y'] + row['height'] / 2)
        return self._stable_transcript()

    def fill_draft(self, name, source_id, reply, before_fill, key=''):
        messages = self.open_customer(name, key)
        if not messages or messages[-1]['id'] != source_id:
            return 'stale', '客户端已有新消息，本条回复已取消'
        existing = self._compose_text()
        owned = self.owned_drafts.get(name)
        if existing and comparable_text(existing) not in (comparable_text(owned or ''), comparable_text(reply)):
            return 'draft', '输入框存在未发送内容，请先处理草稿'
        if not before_fill():
            return 'draft', '运行已暂停或会话被同事接管'
        self._clipboard(reply)
        shot = self._capture()
        try:
            px, py = self.slots.point_pixels('compose', shot.width, shot.height)
        except SlotError as exc:
            return 'draft', str(exc)
        self._linkr.click_image(shot, px, py)
        self._linkr.paste()
        self.owned_drafts[name] = comparable_text(reply)
        if comparable_text(self._compose_text()) != comparable_text(reply):
            self._clear_compose()
            return 'draft', '输入框内容与审核回复不一致，请检查客户端'
        latest = self._transcript()
        if not latest or latest[-1]['id'] != source_id:
            self._clear_compose()
            return 'stale', '发送前会话发生变化'
        return 'filled', '已填入客户端输入框，未发送'

    def send(self, name, source_id, reply, before_click, key=''):
        self.confirmed_message = None
        if not self.config.get('allow_send'):
            return 'draft', '桌面发送未启用，已保留草稿'
        status, reason = self.fill_draft(name, source_id, reply, lambda: True, key)
        if status != 'filled':
            return status, reason
        if not before_click():
            self._clear_compose()
            return 'draft', '运行已暂停或会话被同事接管'
        before = {message['id'] for message in self._transcript()}
        shot = self._capture()
        try:
            px, py = self.slots.point_pixels('send', shot.width, shot.height)
        except SlotError as exc:
            return 'draft', f'发送未标定：{exc}'
        try:
            self._linkr.click_image(shot, px, py)
        except Exception:
            return 'uncertain', '发送操作未完成，请核对客户端后处理；不会自动重发'
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            for message in self._transcript():
                if message['id'] not in before and message['role'] == 'agent' and comparable_text(message['text']) == comparable_text(reply):
                    self.confirmed_message = message
                    self.owned_drafts.pop(name, None)
                    return 'sent', ''
            time.sleep(self.read_interval)
        return 'uncertain', '发送结果未确认，请核对客户端后处理；不会自动重发'

    # -- capture helpers ---------------------------------------------------
    def _locate_window(self):
        matches = self._windows.find_windows(process=self.slots.process)
        if not matches:
            raise TransportNotReady(f'未找到客户端窗口（{self.slots.process}）；请打开客户端并保持登录')
        visible = [window for window in matches if not window.minimized] or matches
        window = max(visible, key=lambda item: item.width * item.height)
        if not self._windows.focus_window(window.hwnd):
            raise TransportNotReady('无法将客户端窗口置于前台，请关闭遮挡窗口后重试')
        return window

    def _refresh_window(self):
        matches = self._windows.find_windows(process=self.slots.process)
        match = next((window for window in matches if window.hwnd == self.window.hwnd), None)
        if match is None and matches:
            match = max(matches, key=lambda item: item.width * item.height)
        if match is None:
            raise TransportNotReady('客户端窗口已关闭；请重新打开并登录后再开始接待')
        self.window = match
        return match.rect

    def _capture(self):
        rect = self._refresh_window()
        shot = self._linkr.snapshot(window_rect=rect)
        if not shot.width or not shot.height:
            raise TransportNotReady('客户端截图失败，请检查 Linkr 连接')
        return shot

    def _read_panel(self, shot, panel):
        image = Image.open(io.BytesIO(shot.png))
        x1, y1, x2, y2 = self.slots.panel_pixels(panel, image.width, image.height)
        crop = image.crop((x1, y1, x2, y2))
        buffer = io.BytesIO()
        crop.save(buffer, 'PNG')
        return (x1, y1, x2, y2), self._ocr.read_lines(buffer.getvalue())

    def _scan_sessions(self, shot):
        _, lines = self._read_panel(shot, 'session_list')
        return parse_session_rows(lines)

    def _transcript(self):
        shot = self._capture()
        box, lines = self._read_panel(shot, 'chat')
        return parse_transcript(lines, (box[2] - box[0]) / 2)

    def _stable_transcript(self):
        previous, stable_since = None, None
        deadline = time.monotonic() + self.transcript_deadline
        while time.monotonic() < deadline:
            if self.cancelled():
                raise TransportNotReady('正在停止会话读取')
            messages = self._transcript()
            signature = [message['id'] for message in messages]
            if signature != previous:
                previous, stable_since = signature, time.monotonic()
            elif time.monotonic() - stable_since >= self.stability_seconds:
                return messages
            time.sleep(self.read_interval)
        raise TransportNotReady('客户端聊天记录未稳定加载，请检查窗口是否被遮挡')

    def _compose_pixels(self, shot):
        center_x, center_y = self.slots.point_pixels('compose', shot.width, shot.height)
        left = max(0, center_x - COMPOSE_MARGIN[0])
        top = max(0, center_y - COMPOSE_MARGIN[1])
        right = min(shot.width, center_x + COMPOSE_MARGIN[0])
        bottom = min(shot.height, center_y + COMPOSE_MARGIN[1])
        return left, top, right, bottom

    def _compose_text(self):
        shot = self._capture()
        image = Image.open(io.BytesIO(shot.png))
        crop = image.crop(self._compose_pixels(shot))
        buffer = io.BytesIO()
        crop.save(buffer, 'PNG')
        return '\n'.join(line.text for line in self._ocr.read_lines(buffer.getvalue()))

    def _clear_compose(self):
        try:
            self._linkr.control([
                ['keyboard', 'ControlLeft', True],
                ['delay', 30],
                ['keyboard', 'KeyA', True],
                ['delay', 40],
                ['keyboard', 'KeyA', False],
                ['delay', 30],
                ['keyboard', 'ControlLeft', False],
                ['delay', 30],
                ['keyboard', 'Backspace', True],
                ['delay', 40],
                ['keyboard', 'Backspace', False],
            ])
        except Exception:
            pass


class _WindowApi:
    find_windows = staticmethod(find_windows)
    focus_window = staticmethod(focus_window)
