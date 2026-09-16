"""Read-only workbench structure capture for adapter design. Customer text is never stored."""
from __future__ import annotations

import json
import time
from pathlib import Path

from cs_duty.browser import BrowserAdapter

CAPTURE = """() => {
    const clean = value => String(value || '').replace(/\\s+/g, ' ').trim()
        .replace(/\\d{4,}/g, '####').slice(0, 40);
    let budget = 4000;
    const visible = el => el.getClientRects().length > 0;
    const walk = (el, depth) => {
        if (budget-- <= 0 || depth > 14) return null;
        const box = el.getBoundingClientRect();
        const node = {
            tag: el.tagName.toLowerCase(),
            id: el.id || undefined,
            cls: (typeof el.className === 'string' ? el.className : '').split(/\\s+/).filter(Boolean).slice(0, 6),
            role: el.getAttribute('role') || undefined,
            title: clean(el.getAttribute('title')) || undefined,
            aria: clean(el.getAttribute('aria-label')) || undefined,
            rect: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
            text: undefined,
            children: []
        };
        const interactive = el.matches('button, a, [role=button], [class*=btn], [class*=Btn], [class*=button], [class*=Button], [class*=send], [class*=Send], [class*=apply], [class*=Apply], [class*=order], [class*=Order], [class*=coupon], [class*=Coupon]');
        if (interactive) node.text = clean(el.textContent) || undefined;
        for (const child of el.children) {
            const item = walk(child, depth + 1);
            if (item) node.children.push(item);
        }
        return node;
    };
    const root = document.querySelector('.panel-content') || document.body;
    return {
        url: location.origin + location.pathname,
        title: document.title,
        captured: Date.now(),
        layout: [...document.querySelectorAll('.panel-content > *, .plugin-wrap, .root')].map(el => {
            const box = el.getBoundingClientRect();
            return {
                tag: el.tagName.toLowerCase(),
                id: el.id || undefined,
                cls: (typeof el.className === 'string' ? el.className : '').split(/\\s+/).filter(Boolean).slice(0, 8),
                rect: [Math.round(box.x), Math.round(box.y), Math.round(box.width), Math.round(box.height)],
                visible: visible(el)
            };
        }),
        tree: walk(root, 0)
    };
}"""


def capture(config, data_dir, output=None, wait=True):
    if config.get('transport') != 'jingmai':
        raise ValueError('请先在接待设置中选择“真实页面 · 京东京麦”并保存')
    adapter = BrowserAdapter(config, data_dir)
    adapter.start()
    try:
        adapter.select_workbench()
        adapter.guard()
        adapter.select_consulting()
        if wait:
            try:
                input('请在 RPA 浏览器中打开一个包含订单的测试会话，然后回到本窗口按回车开始采集…')
            except EOFError:
                pass
        frames = []
        for frame in adapter.page.frames:
            try:
                item = frame.evaluate(CAPTURE)
            except Exception:
                continue
            item['main'] = frame == adapter.page.main_frame
            frames.append(item)
        snapshot = {'app': 'cs-duty', 'kind': 'jingmai-panel', 'frames': frames}
    finally:
        adapter.close()
    target = Path(output) if output else data_dir / 'observations' / time.strftime('jingmai-panel-%Y%m%d-%H%M%S.json')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding='utf-8')
    return str(target)
