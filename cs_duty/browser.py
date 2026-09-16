"""DOM-only transport. No fixture APIs are used to read or send messages."""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright
from playwright._impl._errors import TargetClosedError

ACTIVE_PANE = '#t-alluser-wrap > .c_tabs > .c_tabs-content > .c_tabs-tabpane:not(.c_tabs-tab_inactive)'
ROWS = ACTIVE_PANE + ' .alluser-item:visible'
EDITOR = '.EditorContent[contenteditable=true]'
HEADER = '.chat-head-name > span'
CONSULTING_TAB = '#t-alluser-wrap .c_tabs-nav-container > .c_tabs-tab[title="正在咨询"]'
STABLE_KEY_ATTRS = ('data-user-id', 'data-uid', 'data-userid', 'data-customer-id', 'data-key')


class BrowserNotReady(RuntimeError):
    """Safe, actionable status text without login URLs or customer data."""


def comparable_text(text):
    # Contenteditable may add blank lines when Chromium turns newlines into divs.
    return re.sub(r'\n{2,}', '\n', text.replace('\r\n', '\n')).strip()


class BrowserAdapter:
    def __init__(self, config, data_dir: Path):
        self.config, self.data_dir = config, data_dir
        self.playwright = self.context = self.page = None
        self.cursor = 0
        self.confirmed_message = None
        self.initial_keys = None
        self.read_cache = {}
        self.owned_drafts = {}
        self.key_attrs = {}
        self.cancelled = lambda: False

    def should_read(self, customer, force=False):
        if self.config['transport'] == 'mock' or force:
            return True
        previous = self.read_cache.get(customer['customer_key'])
        fingerprint = (customer.get('preview', ''), customer.get('date', ''))
        return not previous or previous[0] != fingerprint or time.monotonic() - previous[1] >= 60

    def mark_read_snapshot(self, customer):
        self.read_cache[customer['customer_key']] = ((customer.get('preview', ''), customer.get('date', '')), time.monotonic())

    def scroll_contacts(self, top=False):
        # Locate the actual scrollable ancestor, including virtualized list wrappers.
        return self.page.locator(ACTIVE_PANE).evaluate("""(pane, top) => {
            let node = pane.querySelector('.alluser-item');
            while (node && pane.contains(node)) {
                if (node.clientHeight > 0 && node.scrollHeight > node.clientHeight + 1 &&
                    /auto|scroll/.test(getComputedStyle(node).overflowY)) {
                    const before = node.scrollTop;
                    node.scrollTop = top ? 0 : Math.min(node.scrollHeight, before + node.clientHeight * .8);
                    return {moved: node.scrollTop !== before, bottom: node.scrollTop + node.clientHeight >= node.scrollHeight - 2};
                }
                node = node.parentElement;
            }
            return {moved: false, bottom: true};
        }""", top)

    def contact_rows(self):
        return self.page.locator(ACTIVE_PANE).first.evaluate("""pane => {
            const rows = [];
            let group = '';
            const walk = node => {
                for (const child of node.children) {
                    if (child.classList?.contains('c_cas-head')) group = child.textContent.trim();
                    else if (child.classList?.contains('alluser-item')) {
                        if (!child.getClientRects().length) continue;
                        const identity = {};
                        for (const attr of child.attributes || []) {
                            if (attr.name.indexOf('data-') === 0 && attr.value) identity[attr.name] = attr.value;
                        }
                        const row = {
                            name: child.querySelector('.alluser-item-name')?.textContent?.trim() || '',
                            identity,
                            group,
                            preview: child.querySelector('.alluser-item-breifdesc')?.textContent || '',
                            date: child.querySelector('.alluser-item-date-w')?.textContent || ''
                        };
                        if (row.name) rows.push(row);
                    } else walk(child);
                }
            };
            walk(pane);
            return rows;
        }""")

    @staticmethod
    def stable_key(item):
        identity = item.get('identity') or {}
        for attr in STABLE_KEY_ATTRS:
            value = identity.get(attr)
            if value:
                return value, attr
        return 'name:' + item['name'], ''

    def collect_contacts(self):
        found, dropped = {}, set()
        real = self.config['transport'] != 'mock'
        if real:
            self.scroll_contacts(top=True)
            self.page.wait_for_timeout(150)
        bottom_checks = 0
        for _ in range(100 if real else 1):
            if self.cancelled():
                raise BrowserNotReady('正在停止联系人扫描')
            rows = [(*self.stable_key(item), item) for item in self.contact_rows()]
            counts = {}
            for key, _, _ in rows:
                counts[key] = counts.get(key, 0) + 1
            before = len(found)
            for key, attr, item in rows:
                item['customer_key'], item['key_attr'] = key, attr
                if counts[key] > 1:
                    dropped.add(key)
                found[key] = item
                if attr:
                    self.key_attrs[key] = attr
            if not real:
                break
            movement = self.scroll_contacts()
            bottom_checks = bottom_checks + 1 if movement['bottom'] and not movement['moved'] and len(found) == before else 0
            if bottom_checks >= 2:
                break
            self.page.wait_for_timeout(200)
        else:
            raise BrowserNotReady('联系人列表尚未完整扫描，未启动历史初始化；请检查列表加载')
        kept = [item for key, item in found.items() if key not in dropped]
        if real and not dropped:
            text = self.page.locator(ACTIVE_PANE).inner_text()
            count = re.search(r'最近联系人\s*[（(](\d+)[）)]', text)
            if count and len(kept) < int(count[1]):
                raise BrowserNotReady(f'联系人尚未完整加载：已读取 {len(kept)} / {count[1]}，正在重试')
        return kept

    def start(self):
        self.playwright = sync_playwright().start()
        try:
            channel = self.config['channel']
            transport = self.config['transport']
            profile = self.data_dir / 'browser' / hashlib.sha256(f"{transport}:{self.config['shop']}".encode()).hexdigest()[:16]
            options = {'channel': channel, 'headless': transport == 'mock', 'timeout': 20000}
            if transport == 'mock':
                options['viewport'] = {'width': 1366, 'height': 960}
            else:
                # Headed real browser: let the page fill whatever window size the user keeps.
                options['no_viewport'] = True
            self.context = self.playwright.chromium.launch_persistent_context(str(profile), **options)
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.page.set_default_timeout(6000)
            self.page.goto(self.config['mock_url'] if transport == 'mock' else self.config['jd_url'], wait_until='domcontentloaded', timeout=20000)
        except Exception:
            self.close()
            raise

    def guard(self):
        url = urlparse(self.page.url)
        if self.config['transport'] == 'mock':
            if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise RuntimeError('模拟适配器拒绝操作非本地页面')
        elif url.scheme != 'https' or url.hostname != 'dongdong.jd.com':
            raise BrowserNotReady('请在 RPA 打开的浏览器中登录并进入咚咚工作台')

    def select_workbench(self):
        if self.config['transport'] == 'mock':
            return
        trusted_pages = []
        for page in reversed(self.context.pages):
            if page.is_closed():
                continue
            url = urlparse(page.url)
            if url.scheme == 'https' and url.hostname == 'dongdong.jd.com':
                trusted_pages.append(page)
                if page.locator(CONSULTING_TAB).count():
                    self.page = page
                    self.page.set_default_timeout(6000)
                    return
        if trusted_pages:
            raise BrowserNotReady('已打开咚咚网站，但尚未识别到客服列表；请确认已进入聊天工作台')
        raise BrowserNotReady('RPA 浏览器中没有咚咚工作台标签页；请登录后进入 https://dongdong.jd.com/')

    def select_consulting(self):
        tab = self.page.locator(CONSULTING_TAB)
        if not tab.count():
            raise BrowserNotReady('尚未识别到客服列表，请等待工作台加载')
        if 'c_tabs-tab_check' not in (tab.get_attribute('class') or '').split():
            tab.click()
        expect(self.page.locator(ACTIVE_PANE)).to_be_visible()

    def customers(self):
        self.select_workbench()
        self.guard()
        self.select_consulting()
        items = self.collect_contacts()
        if self.initial_keys is None:
            self.initial_keys = {item['customer_key'] for item in items}
        for item in items:
            item['initial_history'] = self.config['transport'] != 'mock' and item['customer_key'] in self.initial_keys
        if not items:
            return []
        start = self.cursor % len(items)
        ordered = items[start:] + items[:start]
        self.cursor = (start + self.config['max_sessions']) % len(items)
        return ordered[:self.config['max_sessions']]

    def row_locator(self, name, key):
        attr = self.key_attrs.get(key) if key and not key.startswith('name:') else ''
        if attr:
            value = key.replace('\\', '\\\\').replace('"', '\\"')
            return self.page.locator(f'{ROWS}[{attr}="{value}"]')
        return self.page.locator(ROWS).filter(has=self.page.get_by_text(name, exact=True))

    def stable_messages(self, previous_ids=None):
        """Wait until the transcript left the previously open conversation and stopped changing."""
        previous, stable_since = previous_ids, time.monotonic()
        switched = previous_ids is None
        switch_deadline = time.monotonic() + 2
        deadline = time.monotonic() + 7
        while time.monotonic() < deadline:
            messages = self.read_messages()
            ids = [m['id'] for m in messages]
            if not switched:
                if ids != previous_ids:
                    switched = True
                elif time.monotonic() >= switch_deadline:
                    switched = True
                else:
                    self.page.wait_for_timeout(100)
                    continue
            if ids != previous:
                previous, stable_since = ids, time.monotonic()
            if time.monotonic() - stable_since >= .4:
                return messages
            self.page.wait_for_timeout(100)
        raise RuntimeError('聊天记录未稳定加载')

    def open_customer(self, name, key=''):
        self.guard()
        self.select_consulting()
        header = self.page.locator(HEADER).first
        current = header.inner_text() if header.count() else ''
        previous_ids = None
        if current != name:
            row = self.row_locator(name, key)
            if self.config['transport'] != 'mock':
                self.scroll_contacts(top=True)
                for _ in range(100):
                    if self.cancelled():
                        raise BrowserNotReady('正在停止会话读取')
                    if row.count():
                        break
                    self.page.wait_for_timeout(150)
                    if not self.scroll_contacts()['moved']:
                        break
            count = row.count()
            if count == 0:
                raise BrowserNotReady('列表中找不到目标会话，请确认列表已加载')
            if count > 1:
                raise BrowserNotReady('存在无法区分的同名会话，已跳过以避免上下文混淆')
            previous_ids = [m['id'] for m in self.read_messages()]
            row.click()
            expect(header).to_have_text(name)
        expect(self.page.locator(EDITOR)).to_be_visible()
        return self.stable_messages(previous_ids)

    def read_messages(self):
        return self.page.locator('#t-chat-scroll .message').evaluate_all("""nodes => nodes.flatMap(node => {
            const side = node.querySelector('.message_left, .message_right');
            const body = side?.querySelector('.message__content');
            if (!side?.id || !body) return [];
            return [{id: side.id, role: side.classList.contains('message_left') ? 'customer' : 'agent',
                text: body.textContent, timestamp: side.querySelector('.message__time_str')?.textContent || ''}];
        })""")

    def fill_draft(self, name, source_id, reply, before_fill, key=''):
        messages = self.open_customer(name, key)
        if not messages or messages[-1]['id'] != source_id:
            return 'stale', '网页已有新消息，本条回复已取消'
        editor = self.page.locator(EDITOR)
        existing = comparable_text(editor.inner_text())
        if existing and existing != self.owned_drafts.get(name) and existing != comparable_text(reply):
            return 'draft', '输入框存在未发送内容，请先处理草稿'
        if not before_fill():
            return 'draft', '运行已暂停或会话被同事接管'
        editor.fill(reply)
        self.owned_drafts[name] = comparable_text(reply)
        if comparable_text(editor.inner_text()) != comparable_text(reply):
            editor.fill('')
            return 'draft', '输入框内容与审核回复不一致，请检查页面'
        latest = self.read_messages()
        if self.page.locator(HEADER).first.inner_text() != name or not latest or latest[-1]['id'] != source_id:
            editor.fill('')
            return 'stale', '发送前会话发生变化'
        return 'filled', '已填入网页输入框，未发送'

    def send(self, name, source_id, reply, before_click, key=''):
        self.confirmed_message = None
        status, reason = self.fill_draft(name, source_id, reply, lambda: True, key)
        if status != 'filled':
            return status, reason
        editor = self.page.locator(EDITOR)
        if not before_click():
            editor.fill('')
            return 'draft', '运行已暂停或会话被同事接管'
        latest = self.read_messages()
        if self.page.locator(HEADER).first.inner_text() != name or not latest or latest[-1]['id'] != source_id:
            editor.fill('')
            return 'stale', '发送前会话发生变化'
        before = {m['id'] for m in latest}
        try:
            self.page.locator('.SendButtonGroup > .send-button').click(timeout=5000)
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                for message in self.read_messages():
                    if message['id'] not in before and message['role'] == 'agent' and comparable_text(message['text']) == comparable_text(reply):
                        self.confirmed_message = message
                        self.owned_drafts.pop(name, None)
                        return 'sent', ''
                self.page.wait_for_timeout(150)
        except Exception:
            pass
        return 'uncertain', '发送结果未确认，请核对网页后处理；不会自动重发'

    def close(self):
        def release(callback):
            try:
                callback()
            except Exception as exc:
                # Ctrl+C may terminate either Chromium or its Playwright driver first.
                if not isinstance(exc, TargetClosedError) and not str(exc).endswith(
                    'Connection closed while reading from the driver'
                ):
                    raise

        context, playwright = self.context, self.playwright
        self.context = self.page = self.playwright = None
        try:
            if context:
                release(context.close)
        finally:
            if playwright:
                release(playwright.stop)
