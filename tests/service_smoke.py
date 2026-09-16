"""Opt-in browser integration: python -m tests.service_smoke [--live-model]."""
import argparse
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import Mock

from playwright.sync_api import expect, sync_playwright

from cs_duty.database import ROOT
from cs_duty.server import create_server
from cs_duty.browser import BrowserAdapter


def check_virtual_contacts(playwright):
    """Exercise the real adapter against a routed page, without contacting JD."""
    browser = playwright.chromium.launch(channel='msedge', headless=True)
    context = browser.new_context()
    html = '''<!doctype html><meta charset="utf-8">
    <div class="c_tabs-tabpane">其他插件面板</div>
    <div id="t-alluser-wrap"><div class="c_tabs">
    <div class="c_tabs-nav-container">
      <div class="c_tabs-tab" title="正在咨询">正在咨询</div>
      <div class="c_tabs-tab c_tabs-tab_check" title="历史咨询">历史咨询</div>
    </div><div class="c_tabs-content">
    <div class="c_tabs-tabpane c_tabs-tab_inactive" id="consulting">正在咨询(23)
      <div id="scroller" style="height:120px;width:300px;overflow-y:auto;position:relative">
        <div id="spacer" style="height:690px;position:relative"></div>
      </div>
    </div><div class="c_tabs-tabpane" id="history">最近联系人(99)</div>
    </div></div></div>
    <div class="chat-head-name"><span></span></div>
    <div id="t-chat-scroll"></div><div class="EditorContent" contenteditable="true" style="min-height:30px"></div>
    <div class="SendButtonGroup"><span class="send-button">发送</span></div>
    <script>
    document.querySelector('.send-button').onclick=()=>{
      const editor=document.querySelector('.EditorContent');
      const message=document.createElement('div');message.className='message';
      message.innerHTML='<div class="message_right" id="sent-test"><span class="message__content"></span></div>';
      message.querySelector('.message__content').textContent=editor.innerText;
      document.querySelector('#t-chat-scroll').append(message);editor.innerText='';
    };
    document.querySelector('[title="正在咨询"]').onclick=()=>{
      document.querySelector('[title="正在咨询"]').classList.add('c_tabs-tab_check');
      document.querySelector('[title="历史咨询"]').classList.remove('c_tabs-tab_check');
      document.querySelector('#consulting').classList.remove('c_tabs-tab_inactive');
      document.querySelector('#history').classList.add('c_tabs-tab_inactive');
    };
    const scroller=document.querySelector('#scroller'),spacer=document.querySelector('#spacer');
    function render(){
      const first=Math.floor(scroller.scrollTop/30);spacer.innerHTML='';
      for(let i=first;i<Math.min(23,first+5);i++){
        const row=document.createElement('div');row.className='alluser-item';
        row.style.cssText=`position:absolute;top:${i*30}px;height:30px;width:290px`;
        row.innerHTML=`<span class="alluser-item-name">客户${i+1}</span><span class="alluser-item-breifdesc">问供电</span>`;
        row.onclick=()=>{
          document.querySelector('.chat-head-name span').textContent=`客户${i+1}`;
          setTimeout(()=>document.querySelector('#t-chat-scroll').innerHTML=
            `<div class="message"><div class="message_left" id="s_${i+1}"><span class="message__content">客户${i+1}的问题</span></div></div>`,200);
        };spacer.appendChild(row);
      }
    }
    scroller.addEventListener('scroll',render);render();
    </script>'''
    context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
    page = context.new_page()
    page.goto('https://dongdong.jd.com/')
    adapter = BrowserAdapter({'transport': 'jingmai', 'max_sessions': 100}, ROOT / 'artifacts')
    adapter.context, adapter.page = context, page
    try:
        contacts = adapter.customers()
        expect(page.locator('[title="正在咨询"]')).to_have_class('c_tabs-tab c_tabs-tab_check')
        assert len(contacts) == 23 and len({c['customer_key'] for c in contacts}) == 23
        assert all(c['initial_history'] for c in contacts)
        assert adapter.open_customer('客户1')[-1]['id'] == 's_1'
        assert adapter.open_customer('客户23')[-1]['id'] == 's_23'
        status, _ = adapter.fill_draft('客户23', 's_23', '待确认的草稿', lambda: True)
        assert status == 'filled'
        expect(page.locator('.EditorContent')).to_have_text('待确认的草稿')
        assert not page.locator('.message_right').count()
        status, _ = adapter.send('客户23', 's_23', '待确认的草稿', lambda: True)
        assert status == 'sent' and adapter.confirmed_message['text'] == '待确认的草稿'
        assert adapter.send('客户23', 's_23', '过期回复', lambda: True)[0] == 'stale'
        adapter.open_customer('客户1')
        page.locator('.EditorContent').fill('客服手写内容')
        assert adapter.fill_draft('客户1', 's_1', '新草稿', lambda: True)[0] == 'draft'
        expect(page.locator('.EditorContent')).to_have_text('客服手写内容')
        adapter.mark_read_snapshot(contacts[0])
        assert not adapter.should_read(contacts[0])
        page.evaluate("document.querySelector('#consulting').innerHTML='正在咨询(0)'")
        assert adapter.customers() == []
        return len(contacts)
    finally:
        adapter.close()
        browser.close()


class FixtureModel:
    def __init__(self, profile):
        pass

    def plan(self, persona, context):
        last = next(m['text'] for m in reversed(context['history']) if m['role'] == 'customer')
        if context['colleague_result']:
            return {'intent': 'consult', 'reply': context['colleague_result'], 'fields': {}}
        if '定制' in last:
            return {'intent': 'custom', 'reply': '我先整理需求。', 'reason': '定制外壳需求',
                    'fields': {'product': 'TEST-1', 'requirements': '外壳', 'quantity': '100台',
                               'deadline': '下月', 'contact': '测试联系人'}}
        return {'intent': 'consult', 'reply': '这款 TEST-1 使用 5V 供电。',
                'evidence_ids': [k['id'] for k in context['knowledge']]}

    def test(self):
        return {'ok': True, 'model': 'fixture', 'seconds': 0}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live-model', action='store_true')
    args = parser.parse_args()
    output = ROOT / 'artifacts' / ('service-live-smoke' if args.live_model else 'service-smoke')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT / 'artifacts') as directory:
        kwargs = {} if args.live_model else {'model_factory': FixtureModel, 'env_path': None}
        server, app = create_server(port=0, data_dir=Path(directory), import_root=None, **kwargs)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        errors = []
        try:
            with app.fixture._lock:
                app.fixture._write({'rev': 0, 'sessions': []})
            if not args.live_model:
                app.settings.save_profile({'name': '测试模型', 'protocol': 'anthropic', 'base_url': 'https://example.com',
                                           'api_key': 'fixture-key', 'model': 'fixture'})
            app.settings.save_runtime({'poll_seconds': 1, 'merge_seconds': 0})
            with sync_playwright() as playwright:
                virtual_contacts = check_virtual_contacts(playwright)
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/manage')
                expect(page.locator('#runtime-title')).to_have_text('服务已就绪')
                page.locator('.nav-item[data-view=settings]').click()
                for transport in ('jingmai', 'mock'):
                    for mode in ('auto', 'draft'):
                        page.locator('[name=transport]').select_option(transport)
                        page.locator('[name=mode]').select_option(mode)
                        expect(page.locator('#mock-address')).to_be_visible() if transport == 'mock' else expect(page.locator('#real-address')).to_be_visible()
                        expect(page.locator('#real-address')).not_to_be_visible() if transport == 'mock' else expect(page.locator('#mock-address')).not_to_be_visible()
                        page.locator('#settings-form button[type=submit]').click()
                        expect(page.locator('#runtime-target')).to_have_text('模拟页面' if transport == 'mock' else '真实页面 · 京东京麦')
                        expect(page.locator('#runtime-mode')).to_have_text('自动发送' if mode == 'auto' else '填写草稿')
                        expect(page.locator('#settings-form button[type=submit]')).to_be_enabled()
                page.evaluate('window.scrollTo(0,0)')
                page.screenshot(path=str(output / 'reception-settings.png'), full_page=True)
                page.locator('[name=feishu_mode]').select_option('app')
                expect(page.locator('#feishu-app-fields')).to_be_visible()
                expect(page.locator('#feishu-webhook-fields')).not_to_be_visible()
                page.locator('[name=feishu_app_id]').fill('cli_fixture')
                page.locator('[name=feishu_app_secret]').fill('secret-feishu-fixture')
                page.locator('[name=feishu_allowed_users]').fill('ou_owner\nou_owner')
                page.locator('[name=feishu_allowed_chats]').fill('oc_group')
                page.locator('[name=feishu_chat_id]').fill('oc_group')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('[name=feishu_app_secret]')).to_have_value('')
                assert app.settings.runtime()['feishu_allowed_users'] == ['ou_owner']
                page.screenshot(path=str(output / 'feishu-settings.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Feishu settings overflow mobile'
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.locator('[name=feishu_mode]').select_option('webhook')
                page.locator('#settings-form button[type=submit]').click()
                expect(page.locator('#settings-form button[type=submit]')).to_be_enabled()
                page.locator('[data-view="knowledge"]').click()
                page.locator('#add-knowledge').click()
                page.locator('#knowledge-form [name=title]').fill('TEST-1供电电压是多少')
                page.locator('#knowledge-form [name=content]').fill('TEST-1是一款测试开发板，使用5V供电。')
                page.locator('#knowledge-form [name=product]').fill('TEST-1')
                page.locator('#knowledge-form button[type=submit]').click()
                expect(page.locator('#knowledge-dialog')).not_to_be_visible()
                control = browser.new_page()
                control.goto(base + '/control')
                control.locator('#create [name=name]').fill('测试客户甲')
                control.locator('#create button').click()
                expect(control.locator('#customer-title')).to_have_text('测试客户甲')
                control.locator('#inbound [name=text]').fill('请问TEST-1供电电压是多少？')
                control.locator('#inbound button[type=submit]').click()
                page.locator('[data-view="overview"]').click()
                page.locator('#start').click()
                expect(page.locator('#outbox textarea')).to_be_visible(timeout=120000)
                draft = page.locator('#outbox textarea')
                assert '5V' in draft.input_value(), draft.input_value()
                expect(page.locator('#outbox .reason').first).to_have_text('已填入网页输入框，未发送', timeout=15000)
                assert not app.db.rows("SELECT * FROM outbox WHERE status='sent'")
                draft.fill(draft.input_value() + ' 请使用匹配的电源。')
                draft.focus()
                page.wait_for_timeout(2500)
                assert draft.evaluate('(el)=>document.activeElement===el'), 'Polling stole editor focus'
                draft.blur()
                page.locator('[data-outbox=approve]').click()
                expect(page.locator('#outbox .badge').first).to_have_text('已发送', timeout=30000)
                expect(control.locator('#history')).to_contain_text('匹配的电源', timeout=15000)
                page.locator('#pause').click()
                expect(page.locator('#runtime-title')).to_have_text('接待已暂停')
                control.locator('#create [name=name]').fill('测试客户乙')
                control.locator('#create button').click()
                expect(control.locator('#customer-title')).to_have_text('测试客户乙')
                control.locator('#inbound [name=text]').fill('我要定制TEST-1外壳，100台，下月交货，联系人是测试联系人。')
                control.locator('#inbound button[type=submit]').click()
                page.wait_for_timeout(1600)
                assert not app.db.rows('SELECT * FROM tasks'), 'Pause still processed new messages'
                page.locator('#start').click()
                page.locator('[data-view=tasks]').click()
                expect(page.locator('[data-result]')).to_be_visible(timeout=120000)
                page.screenshot(path=str(output / 'tasks.png'), full_page=True)
                if args.live_model:
                    page.locator('[data-result]').fill('同事已确认可以定制外壳。\n\n请您提供尺寸图纸。')
                    page.locator('[data-resolve]').click()
                else:
                    # Exercise the same RPA resume path with a scoped Feishu result;
                    # only the external Feishu transport is replaced here.
                    app.feishu.api = Mock()
                    app.feishu.api.send.return_value = 'om_smoke_task'
                    app.feishu.bot, app.feishu.state = 'ou_bot', 'connected'
                    app.settings.save_runtime({'feishu_mode': 'app', 'feishu_enabled': True})
                    expect(page.locator('#task-list')).to_contain_text('飞书通知：已发送', timeout=15000)
                    event = {'message_id': 'om_smoke_reply', 'sender_id': 'ou_owner', 'sender_type': 'user',
                        'chat_id': 'oc_group', 'chat_type': 'group', 'message_type': 'text',
                        'parent_id': 'om_smoke_task', 'mentions': [],
                        'content': json.dumps({'text': '同事已确认可以定制外壳。\n\n请您提供尺寸图纸。'})}
                    assert app.feishu.handle(event)
                    assert app.feishu.handle(event) is None
                expect(page.locator('#task-list .badge').first).to_have_text('已完成', timeout=120000)
                page.locator('[data-view=overview]').click()
                expect(page.locator('#outbox textarea').first).to_contain_text('图纸', timeout=15000)
                page.screenshot(path=str(output / 'overview.png'), full_page=True)
                page.locator('[data-outbox=approve]').click()
                expect(page.locator('#outbox .badge').first).to_have_text('已发送', timeout=30000)
                page.locator('#stop').click()
                expect(page.locator('#runtime-title')).to_have_text('服务已就绪', timeout=30000)
                assert not app.runtime.status()['running']
                if not args.live_model:
                    app.settings.save_runtime({'mode': 'auto', 'feishu_mode': 'webhook', 'feishu_enabled': False})
                    app.feishu.state = 'stopped'
                    control.locator('#inbound [name=text]').fill('TEST-1使用什么供电电压？')
                    control.locator('#inbound button[type=submit]').click()
                    page.locator('#start').click()
                    expect(page.locator('#stat-sent')).to_have_text('3', timeout=30000)
                    page.locator('#stop').click()
                    expect(page.locator('#runtime-title')).to_have_text('服务已就绪', timeout=30000)
                for name in ['conversations', 'knowledge', 'models', 'settings']:
                    page.locator(f'.nav-item[data-view={name}]').click()
                    page.wait_for_timeout(350)
                    page.screenshot(path=str(output / f'{name}.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                for name in ['overview', 'models', 'settings', 'conversations']:
                    page.locator(f'.nav-item[data-view={name}]').click()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'{name} overflows mobile'
                page.locator('[data-view=overview]').click()
                page.screenshot(path=str(output / 'mobile.png'), full_page=True)
                control.set_viewport_size({'width': 1366, 'height': 900})
                control.screenshot(path=str(output / 'control.png'), full_page=True)
                control.goto(base + '/workbench')
                expect(control.locator('.c_tabs-tab_check')).to_have_attribute('title', '正在咨询')
                expect(control.locator('.message__content').first).to_be_visible()
                control.screenshot(path=str(output / 'workbench.png'), full_page=True)
                page.set_viewport_size({'width': 1366, 'height': 960})
                page.locator('[data-view=data]').click()
                expect(page.locator('#view-data')).to_be_visible()
                page.screenshot(path=str(output / 'data-management.png'), full_page=True)
                with page.expect_download() as download_info:
                    page.locator('#export-data').click()
                download = download_info.value
                migration_path = output / 'workspace.zip'
                download.save_as(migration_path)
                from cs_duty.data_management import restore_workspace
                restored = restore_workspace(migration_path, Path(directory) / 'restored')
                assert Path(restored['directory'], 'business.sqlite3').exists()
                page.locator('.nav-item[data-view=conversations]').click()
                expect(page.locator('[data-delete=clear]')).to_be_visible()
                page.locator('[data-delete=clear]').click()
                page.locator('#delete-form input').fill('删除')
                page.locator('#delete-form button[type=submit]').click()
                expect(page.locator('#delete-dialog')).not_to_be_visible()
                expect(page.locator('.chat-history .chat-turn')).to_have_count(0)
                page.locator('[data-delete=delete]').click()
                page.locator('#delete-form input').fill('删除')
                page.locator('#delete-form button[type=submit]').click()
                expect(page.locator('#conversation-list .customer-row')).to_have_count(1)
                page.locator('[data-view=data]').click()
                page.locator('[data-delete=delete_all]').click()
                page.locator('#delete-form input').fill('删除')
                page.locator('#delete-form button[type=submit]').click()
                expect(page.locator('#stat-conversations')).to_have_text('0')
                assert not app.fixture.list_users()
                assert not app.db.rows('SELECT * FROM tasks')
                assert app.db.rows('SELECT * FROM knowledge')
                assert not errors, errors
                report = {'ok': True, 'live_model': args.live_model, 'virtual_contacts': virtual_contacts, 'counts': app.state()['counts'],
                          'browser_errors': errors, 'runtime': app.runtime.status()}
                (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(report, ensure_ascii=False), flush=True)
                browser.close()
        except Exception:
            (output / 'failure.json').write_text(json.dumps({'state': app.state(),
                'chats': [app.fixture.get_chat(u['id']) for u in app.fixture.list_users()]},
                ensure_ascii=False, indent=2), encoding='utf-8')
            raise
        finally:
            server.shutdown()
            thread.join()
            app.close()
            server.server_close()


if __name__ == '__main__':
    main()
