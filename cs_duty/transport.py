"""Transport-neutral helpers shared by live adapters."""
from __future__ import annotations

import re
import time


class TransportNotReady(RuntimeError):
    """Safe, actionable status text without login URLs or customer data."""


def comparable_text(text):
    # Input widgets may add blank lines when newlines turn into separate blocks.
    return re.sub(r'\n{2,}', '\n', text.replace('\r\n', '\n')).strip()


class ReadThrottle:
    """Skip re-reading sessions whose list preview has not changed recently."""

    interval = 60.0

    def __init__(self):
        self.read_cache = {}

    def thumbnail(self, customer):
        return (customer.get('preview', ''), customer.get('date', ''))

    def should_read(self, customer, force=False):
        if force:
            return True
        previous = self.read_cache.get(customer['customer_key'])
        return not previous or previous[0] != self.thumbnail(customer) or time.monotonic() - previous[1] >= self.interval

    def mark_read_snapshot(self, customer):
        self.read_cache[customer['customer_key']] = (self.thumbnail(customer), time.monotonic())
