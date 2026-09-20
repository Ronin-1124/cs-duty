from __future__ import annotations

import ctypes
import io
import os
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np
from PIL import Image

user32 = ctypes.windll.user32


@dataclass
class ScreenShot:
    png: bytes
    width: int
    height: int
    left: int
    top: int
    frame_width: int = 0
    frame_height: int = 0
    content_left: int = 0
    content_top: int = 0
    content_width: int = 0
    content_height: int = 0


def _desktop_size() -> tuple[int, int]:
    from cs_duty.desktop.window import enable_dpi_awareness
    enable_dpi_awareness()
    return (
        int(user32.GetSystemMetrics(78) or 2560),
        int(user32.GetSystemMetrics(79) or 1600),
    )


def _content_box(image: Image.Image, desk_size: tuple[int, int] | None = None,
                 thr: float = 12.0) -> tuple[int, int, int, int]:
    """Locate the desktop inside the captured frame.

    With a known desktop size the letterbox is pure geometry, which stays correct
    for dark interfaces. Without it, uniform dark edge bands are trimmed instead,
    and a mostly dark frame is never collapsed.
    """
    frame_w, frame_h = image.size
    if desk_size:
        desk_w, desk_h = desk_size
        if desk_w > 0 and desk_h > 0:
            scale = min(frame_w / desk_w, frame_h / desk_h)
            cw, ch = max(1, round(desk_w * scale)), max(1, round(desk_h * scale))
            if cw <= frame_w and ch <= frame_h:
                x1, y1 = (frame_w - cw) // 2, (frame_h - ch) // 2
                return x1, y1, x1 + cw, y1 + ch
    luma = np.asarray(image, dtype=np.float32).mean(axis=2)
    height, width = luma.shape
    col = luma.mean(axis=0)
    row = luma.mean(axis=1)

    def span(values, size):
        start, end = 0, size
        while start < end - 1 and values[start] <= thr:
            start += 1
        while end > start + 1 and values[end - 1] <= thr:
            end -= 1
        return start, end

    x1, x2 = span(col, width)
    y1, y2 = span(row, height)
    if x2 - x1 < width * .6 or y2 - y1 < height * .6:
        return 0, 0, width, height
    return x1, y1, x2, y2


class LinkrClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 20.0,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("LINKR_BASE_URL") or "http://linkr-usb.local"
        ).rstrip("/")
        self.token = token or os.getenv("LINKR_TOKEN", "")
        if not self.token:
            raise RuntimeError("LINKR_TOKEN is not set; put it in .env")
        self.http = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.http.close()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"token {self.token}"}

    def snapshot(self, window_rect: tuple[int, int, int, int] | None = None) -> ScreenShot:
        response = self.http.get(
            f"{self.base_url}/api/public/snapshot",
            headers=self._headers(),
        )
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        frame_w, frame_h = image.size
        cx1, cy1, cx2, cy2 = _content_box(image, _desktop_size())
        left, top, right, bottom = 0, 0, frame_w, frame_h
        if window_rect:
            desk_w, desk_h = _desktop_size()
            content_w = max(1, cx2 - cx1)
            content_h = max(1, cy2 - cy1)
            wl, wt, wr, wb = window_rect
            left = int(round(cx1 + wl * content_w / desk_w))
            top = int(round(cy1 + wt * content_h / desk_h))
            right = int(round(cx1 + wr * content_w / desk_w))
            bottom = int(round(cy1 + wb * content_h / desk_h))
            left = max(cx1, min(left, cx2 - 1))
            top = max(cy1, min(top, cy2 - 1))
            right = max(left + 1, min(right, cx2))
            bottom = max(top + 1, min(bottom, cy2))
            image = image.crop((left, top, right, bottom))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return ScreenShot(
            png=buffer.getvalue(),
            width=image.width,
            height=image.height,
            left=left,
            top=top,
            frame_width=frame_w,
            frame_height=frame_h,
            content_left=cx1,
            content_top=cy1,
            content_width=cx2 - cx1,
            content_height=cy2 - cy1,
        )

    def control(self, events: list[list[Any]]) -> dict[str, Any]:
        response = self.http.post(
            f"{self.base_url}/api/public/control",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"events": events},
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") not in (0, None):
            raise RuntimeError(payload.get("message") or f"linkr control failed: {payload}")
        return payload

    def click(self, x: float, y: float, press_ms: int = 40) -> None:
        nx = min(1.0, max(0.0, float(x)))
        ny = min(1.0, max(0.0, float(y)))
        self.control(
            [
                ["mouse_abs", 0, nx, ny, 0, 0],
                ["delay", press_ms],
                ["mouse_abs", 1, nx, ny, 0, 0],
                ["delay", press_ms],
                ["mouse_abs", 0, nx, ny, 0, 0],
            ]
        )

    def click_image(self, shot: ScreenShot, px: float, py: float) -> tuple[float, float]:
        # Linkr maps normalized coordinates onto the desktop, not the letterboxed frame.
        cw = shot.content_width or shot.frame_width or shot.width
        ch = shot.content_height or shot.frame_height or shot.height
        nx = (shot.left + px - shot.content_left) / cw
        ny = (shot.top + py - shot.content_top) / ch
        self.click(nx, ny)
        return nx, ny

    def paste(self) -> None:
        self.control(
            [
                ["keyboard", "ControlLeft", True],
                ["delay", 30],
                ["keyboard", "KeyV", True],
                ["delay", 40],
                ["keyboard", "KeyV", False],
                ["delay", 30],
                ["keyboard", "ControlLeft", False],
            ]
        )
