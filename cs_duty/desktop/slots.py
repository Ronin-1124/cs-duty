"""Calibrated slot table for the desktop client window."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


class SlotError(ValueError):
    """Slot table is missing, malformed, or not yet calibrated."""


@dataclass(frozen=True)
class Panel:
    x1: float
    y1: float
    x2: float
    y2: float

    def pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        return (round(self.x1 * width), round(self.y1 * height),
                round(self.x2 * width), round(self.y2 * height))


@dataclass(frozen=True)
class SlotTable:
    process: str
    layout: str
    slots: dict
    panels: dict
    path: Path

    def point(self, name: str) -> tuple[float, float]:
        slot = self.slots.get(name)
        if slot is None:
            raise SlotError(f'槽位表缺少 {name}')
        x, y = slot.get('x'), slot.get('y')
        if x is None or y is None:
            raise SlotError(f'槽位 {name}（{slot.get("label", "")}）尚未标定；请在目标客户端上完成标定')
        return float(x), float(y)

    def point_pixels(self, name: str, width: int, height: int) -> tuple[int, int]:
        x, y = self.point(name)
        return round(x * width), round(y * height)

    def panel(self, name: str) -> Panel:
        panel = self.panels.get(name)
        if panel is None:
            raise SlotError(f'槽位表缺少面板 {name}')
        return panel

    def panel_pixels(self, name: str, width: int, height: int) -> tuple[int, int, int, int]:
        return self.panel(name).pixels(width, height)


def load_slots(path) -> SlotTable:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise SlotError(f'槽位文件不存在：{path}') from None
    except ValueError:
        raise SlotError(f'槽位文件不是有效 JSON：{path}') from None
    if not isinstance(raw, dict):
        raise SlotError('槽位文件格式错误：顶层必须是对象')
    process, layout = raw.get('process'), raw.get('layout')
    if not isinstance(process, str) or not process.strip():
        raise SlotError('槽位文件缺少客户端进程名')
    if not isinstance(layout, str) or not layout.strip():
        raise SlotError('槽位文件缺少截图布局说明')
    slots, panels = raw.get('slots'), raw.get('panels')
    if not isinstance(slots, dict) or not isinstance(panels, dict):
        raise SlotError('槽位文件格式错误：slots 与 panels 必须是对象')
    checked = {}
    for name, slot in slots.items():
        if not isinstance(slot, dict):
            raise SlotError(f'槽位 {name} 格式错误')
        x, y = slot.get('x'), slot.get('y')
        if (x is None) != (y is None):
            raise SlotError(f'槽位 {name} 的 x/y 必须同时标定或同时为空')
        for value in (x, y):
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1):
                raise SlotError(f'槽位 {name} 的坐标必须在 0–1 之间')
        checked[name] = dict(slot)
    checked_panels = {}
    for name, panel in panels.items():
        if (not isinstance(panel, list) or len(panel) != 4
                or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in panel)):
            raise SlotError(f'面板 {name} 必须是 [x1,y1,x2,y2]')
        x1, y1, x2, y2 = panel
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise SlotError(f'面板 {name} 的坐标必须在 0–1 之间且左上小于右下')
        checked_panels[name] = Panel(*map(float, panel))
    return SlotTable(process=process.strip(), layout=layout.strip(), slots=checked, panels=checked_panels, path=path)
