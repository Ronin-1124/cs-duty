from __future__ import annotations

import ctypes
import re
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from pathlib import Path

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4

user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetForegroundWindow.restype = wt.HWND
user32.SwitchToThisWindow.argtypes = [wt.HWND, wt.BOOL]
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_void_p]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wt.HANDLE,
    wt.DWORD,
    wt.LPWSTR,
    ctypes.POINTER(wt.DWORD),
]
kernel32.GetCurrentThreadId.restype = wt.DWORD


def enable_dpi_awareness() -> None:
    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)):
            return
    except (AttributeError, OSError):
        pass


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    process: str
    rect: tuple[int, int, int, int]
    visible: bool
    minimized: bool

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]


def _window_text(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _process_name(hwnd: int) -> str:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def list_windows(include_invisible: bool = False) -> list[WindowInfo]:
    enable_dpi_awareness()
    results: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def callback(hwnd: int, lparam: int) -> bool:
        visible = bool(user32.IsWindowVisible(hwnd))
        if not visible and not include_invisible:
            return True
        title = _window_text(hwnd)
        if not title and not include_invisible:
            return True
        rect = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        results.append(
            WindowInfo(
                hwnd=hwnd,
                title=title,
                class_name=_class_name(hwnd),
                process=_process_name(hwnd),
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                visible=visible,
                minimized=bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    user32.EnumWindows(callback, 0)
    return results


def find_windows(
    title: str = "",
    process: str = "",
    include_minimized: bool = True,
) -> list[WindowInfo]:
    title_re = re.compile(title, re.IGNORECASE) if title else None
    process_re = re.compile(process, re.IGNORECASE) if process else None
    matches = []
    for window in list_windows():
        if window.minimized and not include_minimized:
            continue
        if title_re and not title_re.search(window.title):
            continue
        if process_re and not process_re.search(window.process):
            continue
        matches.append(window)
    return matches


def focus_window(hwnd: int, timeout: float = 2.0, attempts: int = 4) -> bool:
    enable_dpi_awareness()
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.25)
    current = kernel32.GetCurrentThreadId()
    deadline = time.time() + timeout
    for _ in range(attempts):
        if user32.GetForegroundWindow() == hwnd:
            return True
        target_pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
        foreground = user32.GetForegroundWindow()
        fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = False
        if fg_thread and fg_thread != current:
            attached = bool(user32.AttachThreadInput(current, fg_thread, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(current, fg_thread, False)
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.SwitchToThisWindow(hwnd, True)
        if user32.GetForegroundWindow() == hwnd:
            return True
        user32.keybd_event(VK_MENU, 0, 0, None)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, None)
        user32.SetForegroundWindow(hwnd)
        if user32.GetForegroundWindow() == hwnd:
            return True
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(0.2, remaining))
    return user32.GetForegroundWindow() == hwnd
