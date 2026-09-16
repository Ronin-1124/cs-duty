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


def _desktop_size() -> tuple[int, int]:
    return (
        int(user32.GetSystemMetrics(78) or 2560),
        int(user32.GetSystemMetrics(79) or 1600),
    )


def _content_box(image: Image.Image, thr: float = 12.0) -> tuple[int, int, int, int]:
    arr = np.asarray(image, dtype=np.float32)
    luma = arr.mean(axis=2)
    col = luma.mean(axis=0)
    row = luma.mean(axis=1)
    xs = np.where(col > thr)[0]
    ys = np.where(row > thr)[0]
    w, h = image.size
    if len(xs) == 0 or len(ys) == 0:
        return 0, 0, w, h
    return int(xs[0]), int(ys[0]), int(xs[-1] + 1), int(ys[-1] + 1)


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
        cx1, cy1, cx2, cy2 = _content_box(image)
        left, top, right, bottom = cx1, cy1, cx2, cy2
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
        fw = shot.frame_width or shot.width
        fh = shot.frame_height or shot.height
        nx = (shot.left + px) / fw
        ny = (shot.top + py) / fh
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
