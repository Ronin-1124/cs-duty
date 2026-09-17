"""Loopback management API and static pages, sharing the fixture service."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import signal
import socket
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cs_duty import VERSION
from cs_duty.database import Database, ROOT
from cs_duty.knowledge import Knowledge, RAW_CSV_SOURCES
from cs_duty.models import ModelClient, ModelError
from cs_duty.runtime import Runtime
from cs_duty.settings import Settings
from mock_dongdong.server import Handler as MockHandler
from mock_dongdong.store import Store

WEB = Path(__file__).resolve().parent / 'web'


class Application:
    def __init__(self, data_dir, base_url, env_path=ROOT / '.env', import_root=ROOT, model_factory=ModelClient, adapter_factory=None):
        self.db = Database(data_dir / 'business.sqlite3')
        self.settings = Settings(self.db, env_path)
        config = self.settings.runtime()
        if config['mock_url'] == 'http://127.0.0.1:18766/workbench':
            self.settings.save_runtime({'mock_url': base_url + '/workbench'})
        self.knowledge = Knowledge(self.db)
        self.fixture = Store(data_dir / 'mock.json')
        self.model_factory = model_factory
        kwargs = {'model_factory': model_factory}
        if adapter_factory:
            kwargs['adapter_factory'] = adapter_factory
        self.runtime = Runtime(self.db, self.settings, self.knowledge, **kwargs)
        from cs_duty.feishu import FeishuBridge
        self.feishu = FeishuBridge(self.db, self.settings)
        self.runtime.notify = self.feishu.notify
        self.import_root = import_root
        if not self.db.one('SELECT id FROM knowledge LIMIT 1'):
            self.import_project_knowledge()

    def import_project_knowledge(self):
        results = []
        if self.import_root:
            bundle = self.import_root / 'artifacts' / 'knowledge' / 'knowledge-cleaned'
            if bundle.is_dir():
                if self.runtime.status()['running']:
                    raise ValueError('请先停止接待，再同步整理后的知识包')
                from cs_duty.knowledge_bundle import import_bundle
                return [import_bundle(self.db, bundle)]
            for path in sorted((self.import_root / 'data' / 'raw').glob('*.csv')):
                try:
                    source = RAW_CSV_SOURCES.get(path.name, path.name)
                    results.append(self.knowledge.import_csv(source, path.read_text(encoding='utf-8-sig')))
                except (ValueError, UnicodeError):
                    results.append({'file': path.name, 'error': '字段或编码不受支持'})
        return results

    def state(self):
        counts = {name: self.db.one(sql)['n'] for name, sql in {
            'conversations': 'SELECT count(*) n FROM conversations',
            'knowledge': 'SELECT count(*) n FROM knowledge WHERE enabled=1',
            'drafts': "SELECT count(*) n FROM outbox WHERE status='draft'",
            'tasks': "SELECT count(*) n FROM tasks WHERE status IN ('open','ready')",
            'sent': "SELECT count(*) n FROM outbox WHERE status='sent'",
        }.items()}
        return {'version': VERSION, 'runtime': self.runtime.status(), 'counts': counts,
            'feishu': self.feishu.status(),
            'knowledge_bundle': self.db.setting('knowledge_bundle', {}),
            'config': self.settings.runtime(public=True), 'profiles': self.settings.profiles(),
            'conversations': self.db.rows('SELECT * FROM conversations ORDER BY updated DESC LIMIT 200'),
            'outbox': self.db.rows("SELECT o.*, c.name FROM outbox o JOIN conversations c ON c.id=o.conversation_id WHERE o.status NOT IN ('ignored') ORDER BY o.created DESC LIMIT 100"),
            'tasks': self.db.rows('SELECT t.*,c.name FROM tasks t JOIN conversations c ON c.id=t.conversation_id ORDER BY t.created DESC LIMIT 100'),
            'events': self.db.rows('SELECT * FROM events ORDER BY id DESC LIMIT 30')}

    def close(self):
        self.feishu.stop()
        self.runtime.close()
        self.db.close()


class Handler(MockHandler):
    app: Application

    def log_message(self, fmt, *args):
        # Management bodies, model settings and credentials never enter access logs.
        return

    def _respond(self, status, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, status, payload):
        self._respond(status, json.dumps(payload, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == '/api/manage/state':
                return self._json(200, self.app.state())
            if path == '/api/manage/history':
                cid = query.get('id', [''])[0]
                return self._json(200, {'conversation': self.app.db.conversation(cid), 'messages': self.app.db.history(cid, 500)})
            if path == '/api/manage/knowledge':
                term = query.get('q', [''])[0]
                rows = self.app.db.rows('SELECT * FROM knowledge WHERE title LIKE ? OR content LIKE ? OR product LIKE ? ORDER BY enabled DESC,updated DESC LIMIT 100', ('%' + term + '%',) * 3)
                return self._json(200, {'items': rows})
            if path == '/api/manage/knowledge/materials':
                kind, term = query.get('kind', ['review_queue'])[0], query.get('q', [''])[0]
                rows = self.app.db.rows('SELECT * FROM knowledge_materials WHERE kind=? AND payload LIKE ? ORDER BY id LIMIT 100', (kind, '%' + term + '%'))
                items = []
                for row in rows:
                    payload = json.loads(row['payload'])
                    title = next((payload[k] for k in ('title', 'topic', 'summary', 'source_path', 'source_file', 'raw')
                                  if isinstance(payload.get(k), str)), '资料追溯')
                    items.append({'id': row['id'], 'title': row['id'] + ' · ' + title,
                                  'content': json.dumps(payload, ensure_ascii=False, indent=2),
                                  'material_status': row['status'], 'material': True, 'sources': '[]'})
                return self._json(200, {'items': items})
            if path == '/api/health':
                return self._json(200, {'ok': True, 'version': 2, 'app_version': VERSION, 'offline': True})
            if path in ('/', '/manage', '/manage/') or path.startswith('/manage/'):
                name = 'index.html' if path in ('/', '/manage', '/manage/') else path.removeprefix('/manage/')
                target = (WEB / name).resolve()
                if not target.is_relative_to(WEB.resolve()) or not target.is_file():
                    return self._json(404, {'error': '页面不存在'})
                mime = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
                return self._respond(200, target.read_bytes(), mime + '; charset=utf-8')
            return super().do_GET()
        except ValueError as exc:
            return self._json(400, {'error': str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith('/api/manage/'):
            return super().do_POST()
        expected = 'http://' + self.headers.get('Host', '')
        if self.headers.get('Origin') not in (None, expected) or self.headers.get('X-CS-Duty') != '1':
            return self._json(403, {'error': '请从本机管理页面操作'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            return self._json(415, {'error': '请求必须为 JSON'})
        try:
            size = int(self.headers.get('Content-Length', 0))
            if not 0 <= size <= 4_500_000:
                raise ValueError('请求内容过大')
            data = json.loads(self.rfile.read(size) or '{}')
            if not isinstance(data, dict):
                raise ValueError('请求格式错误')
            if path == '/api/manage/data/export':
                from cs_duty.data_management import export_workspace
                payload = export_workspace(self.app, data.get('include_secrets', False))
                self.send_response(200)
                self.send_header('Content-Type', 'application/zip')
                self.send_header('Content-Disposition', 'attachment; filename="cs-duty-workspace.zip"')
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(payload)
                return
            result = self.manage(path.removeprefix('/api/manage/'), data)
            return self._json(200, {'ok': True, **(result or {})})
        except (ValueError, TypeError, KeyError) as exc:
            return self._json(400, {'error': str(exc) if isinstance(exc, ValueError) else '参数缺失或格式错误'})
        except ModelError as exc:
            return self._json(502, {'error': str(exc)})
        except Exception:
            self.app.db.event('error', '管理操作失败')
            return self._json(500, {'error': '操作失败，请检查运行记录'})

    def manage(self, path, data):
        app, db = self.app, self.app.db
        if path.startswith('feishu/'):
            action = path.split('/')[-1]
            if action not in ('start', 'stop', 'pair', 'test'):
                raise ValueError('未知飞书操作')
            if action == 'test' and data.get('confirm_send') is not True:
                raise ValueError('请确认向已配置群发送测试通知')
            result = getattr(app.feishu, action)()
            return result or app.feishu.status()
        if path == 'data/delete':
            from cs_duty.data_management import delete_conversations
            if data.get('confirmation') != '删除':
                raise ValueError('请输入“删除”确认操作')
            action = data.get('action')
            if action not in ('clear', 'delete', 'delete_all'):
                raise ValueError('未知删除操作')
            return delete_conversations(app, data.get('id'), keep_customer=action == 'clear', all_customers=action == 'delete_all')
        if path.startswith('runtime/'):
            action = path.split('/')[-1]
            if action not in ('start', 'pause', 'stop'):
                raise ValueError('未知运行操作')
            getattr(app.runtime, action)()
            return app.runtime.status()
        if path in ('settings', 'profiles/save', 'profiles/activate') and app.runtime.status()['running']:
            raise ValueError('请先停止运行，再修改配置')
        if path == 'settings':
            with app.runtime.lock, app.feishu.lock:
                if app.runtime.status()['running'] or app.feishu.status()['running']:
                    raise ValueError('请先停止接待并断开飞书连接，再修改接待设置')
                app.settings.save_runtime(data)
        elif path == 'profiles/save':
            return {'id': app.settings.save_profile(data)}
        elif path == 'profiles/activate':
            app.settings.profile(data['id'])
            db.set_setting('active_profile', data['id'])
        elif path == 'profiles/test':
            return app.model_factory(app.settings.profile(data['id'])).test()
        elif path == 'knowledge/import-project':
            return {'results': app.import_project_knowledge()}
        elif path == 'knowledge/import':
            if not isinstance(data.get('text'), str):
                raise ValueError('请上传 CSV 文本')
            return app.knowledge.import_csv(str(data.get('filename', 'data.csv')), data['text'])
        elif path == 'knowledge/toggle':
            db.execute('UPDATE knowledge SET enabled=? WHERE id=?', (1 if data.get('enabled') else 0, data['id']))
        elif path == 'knowledge/save':
            title, content = str(data.get('title', '')).strip(), str(data.get('content', '')).strip()
            if not title or not content or len(content) > 10000:
                raise ValueError('请填写标题和 1–10000 字的内容')
            kid = hashlib.sha256((title + '\n' + content).encode()).hexdigest()
            db.execute("INSERT OR REPLACE INTO knowledge VALUES(?,?,?,?,?,1,?)", (kid, title, content, str(data.get('product', '')), '[{"file":"管理页面录入","row":1}]', time.time()))
        elif path == 'conversation':
            cid, action = data['id'], data['action']
            db.conversation(cid)
            if action == 'takeover':
                db.set_state(cid, 'human')
                db.execute("UPDATE outbox SET status='draft',reason='同事已接管' WHERE conversation_id=? AND status='ready'", (cid,))
            elif action == 'resume':
                task = db.one("SELECT id FROM tasks WHERE conversation_id=? AND status IN ('open','ready')", (cid,))
                db.set_state(cid, 'waiting' if task else 'active')
            else:
                raise ValueError('未知会话操作')
        elif path == 'tasks/resolve':
            db.resolve_task(data['id'], data['result'])
            task = db.one('SELECT conversation_id FROM tasks WHERE id=?', (data['id'],))
            return {'state': db.conversation(task['conversation_id'])['state']}
        elif path == 'outbox':
            with db.lock:
                item = db.one('SELECT * FROM outbox WHERE id=?', (data['id'],))
                if not item:
                    raise ValueError('回复不存在')
                action = data['action']
                if action == 'approve':
                    if item['status'] != 'draft':
                        raise ValueError('这条回复已不在草稿状态')
                    reply = str(data.get('reply', item['reply'])).strip()
                    if not reply or len(reply) > 2000:
                        raise ValueError('回复长度必须为 1–2000 字')
                    current = db.conversation(item['conversation_id'])
                    if current['state'] == 'human':
                        raise ValueError('请先恢复该客户的自动处理')
                    if current['latest_id'] != item['source_id']:
                        db.outbox_status(item['id'], 'stale', '会话已有新消息')
                        raise ValueError('会话已有新消息，请等待新的处理方案')
                    db.execute("UPDATE outbox SET reply=?,status='ready',reason='',updated=? WHERE id=? AND status='draft'", (reply, time.time(), item['id']))
                elif action in ('cancel', 'confirmed'):
                    if item['status'] not in ('draft', 'ready', 'uncertain'):
                        raise ValueError('当前回复状态不支持此操作')
                    if action == 'confirmed' and item['status'] != 'uncertain':
                        raise ValueError('只有待核对回复可以确认已发送')
                    db.outbox_status(item['id'], 'sent' if action == 'confirmed' else 'cancelled', '同事已核对')
                else:
                    raise ValueError('未知回复操作')
        else:
            raise ValueError('未知管理操作')


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_server(host='127.0.0.1', port=18766, data_dir=None, **kwargs):
    if host not in ('127.0.0.1', 'localhost'):
        raise ValueError('管理服务目前仅允许本机访问')
    handler = type('AppHandler', (Handler,), {})
    server = LocalServer((host, port), handler)
    base = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        app = Application(Path(data_dir or ROOT / 'artifacts' / 'app'), base, **kwargs)
    except Exception:
        server.server_close()
        raise
    handler.app, handler.store = app, app.fixture
    return server, app


def serve(host='127.0.0.1', port=18766, data_dir=None):
    # Some Windows launchers pass an inherited "ignore Ctrl+C" process attribute.
    # Restore control handling for this foreground service, without changing the parent.
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
    signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        server, app = create_server(host, port, data_dir)
    except OSError:
        print(f'端口 {port} 已被占用，未启动第二个实例。请停止原服务或使用 --port 指定其他端口。', flush=True)
        return 1
    print(f'客服管理：http://127.0.0.1:{port}/manage', flush=True)
    print(f'模拟工作台：http://127.0.0.1:{port}/workbench', flush=True)
    print(f'客户控制台：http://127.0.0.1:{port}/control', flush=True)
    print('按 Ctrl+C 停止整个应用及其浏览器。', flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        print('正在停止，请等待当前模型请求结束…', flush=True)
    finally:
        app.close()
        server.server_close()
    return 0
