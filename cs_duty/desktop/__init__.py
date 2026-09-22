"""Desktop client I/O via Radxa Linkr (HDMI snapshot + USB HID). No web DOM."""
from cs_duty.desktop.clipboard import set_clipboard_text
from cs_duty.desktop.linkr import LinkrClient, ScreenShot
from cs_duty.desktop.window import (WindowInfo, enable_dpi_awareness, find_windows, focus_window,
                                    list_windows, raise_window)

__all__ = ['LinkrClient', 'ScreenShot', 'WindowInfo', 'enable_dpi_awareness', 'find_windows',
           'focus_window', 'list_windows', 'raise_window', 'set_clipboard_text']
