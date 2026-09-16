import unittest
from pathlib import Path
from unittest.mock import Mock

from playwright._impl._errors import TargetClosedError
from playwright.sync_api import Error

from cs_duty.browser import BrowserAdapter


class BrowserCleanupCase(unittest.TestCase):
    def adapter(self):
        adapter = BrowserAdapter({}, Path('.'))
        adapter.context = Mock()
        adapter.playwright = Mock()
        adapter.page = Mock()
        return adapter

    def test_already_closed_targets_are_cleaned_up_once(self):
        adapter = self.adapter()
        context, playwright = adapter.context, adapter.playwright
        context.close.side_effect = TargetClosedError()
        playwright.stop.side_effect = TargetClosedError()
        adapter.close()
        adapter.close()
        context.close.assert_called_once()
        playwright.stop.assert_called_once()
        self.assertIsNone(adapter.context)
        self.assertIsNone(adapter.page)
        self.assertIsNone(adapter.playwright)

    def test_other_close_errors_remain_visible_and_driver_is_stopped(self):
        adapter = self.adapter()
        playwright = adapter.playwright
        adapter.context.close.side_effect = Error('Unexpected cleanup failure')
        with self.assertRaisesRegex(Error, 'Unexpected cleanup failure'):
            adapter.close()
        playwright.stop.assert_called_once()
        self.assertIsNone(adapter.playwright)

    def test_driver_already_disconnected_during_ctrl_c(self):
        adapter = self.adapter()
        playwright = adapter.playwright
        adapter.context.close.side_effect = Exception(': Connection closed while reading from the driver')
        adapter.close()
        playwright.stop.assert_called_once()
        self.assertIsNone(adapter.context)

    def test_other_driver_errors_remain_visible(self):
        adapter = self.adapter()
        adapter.playwright.stop.side_effect = Error('Unexpected driver failure')
        with self.assertRaisesRegex(Error, 'Unexpected driver failure'):
            adapter.close()
        self.assertIsNone(adapter.playwright)


if __name__ == '__main__':
    unittest.main()
