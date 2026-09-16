import unittest
from pathlib import Path
from unittest.mock import Mock

from cs_duty.browser import BrowserAdapter, BrowserNotReady, CONSULTING_TAB


class BrowserLoginCase(unittest.TestCase):
    def page(self, url, ready=False, closed=False):
        page = Mock()
        page.url = url
        page.is_closed.return_value = closed
        page.locator.return_value.count.return_value = int(ready)
        return page

    def test_login_popup_workbench_replaces_original_login_tab(self):
        adapter = BrowserAdapter({'transport': 'jingmai'}, Path('.'))
        login = self.page('https://passport.jd.com/login')
        workbench = self.page('https://dongdong.jd.com/', ready=True)
        adapter.page = login
        adapter.context = Mock(pages=[login, workbench])
        adapter.select_workbench()
        self.assertIs(adapter.page, workbench)
        workbench.locator.assert_called_with(CONSULTING_TAB)

    def test_similar_domain_and_closed_tabs_do_not_pass_login_detection(self):
        adapter = BrowserAdapter({'transport': 'jingmai'}, Path('.'))
        other = self.page('https://dongdong.jd.com.example.invalid/', ready=True)
        closed = self.page('https://dongdong.jd.com/', ready=True, closed=True)
        adapter.context = Mock(pages=[other, closed])
        with self.assertRaisesRegex(BrowserNotReady, '没有咚咚工作台标签页'):
            adapter.select_workbench()

    def test_unrecognized_page_is_distinguished_from_missing_workbench(self):
        adapter = BrowserAdapter({'transport': 'jingmai'}, Path('.'))
        adapter.context = Mock(pages=[self.page('https://dongdong.jd.com/')])
        with self.assertRaisesRegex(BrowserNotReady, '尚未识别到客服列表'):
            adapter.select_workbench()


if __name__ == '__main__':
    unittest.main()
