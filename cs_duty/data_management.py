"""Local conversation cleanup and portable, consistent workspace snapshots."""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cs_duty.knowledge import terms

FILES = {'business.sqlite3', 'checkpoints.sqlite3'}
LEGACY_FILES = {'mock.json'}
MAX_BYTES = 512 * 1024 * 1024


def require_stopped(app):
    if app.runtime.status()['running']:
        raise ValueError('请先停止接待，并等待客户端操作及模型请求结束；暂停不等于停止')
    if getattr(app, 'feishu', None) and app.feishu.status()['running']:
        raise ValueError('请先断开飞书连接，再清理或导出数据')


def delete_conversations(app, cid=None, *, keep_customer=False, all_customers=False):
    if not all_customers and not isinstance(cid, str):
        raise ValueError('请选择客户')
    db = app.db
    # Prevent start racing the stopped check.
    with app.runtime.lock, app.feishu.lock, db.lock:
        require_stopped(app)
        customers = db.rows('SELECT * FROM conversations') if all_customers else [db.conversation(cid)]
        checkpoint = db.path.parent / 'checkpoints.sqlite3'
        attached = checkpoint.exists()
        if attached:
            db.conn.execute('ATTACH DATABASE ? AS cleanup', (str(checkpoint),))
        try:
            with db.conn:
                checkpoint_tables = {r[0] for r in db.conn.execute("SELECT name FROM cleanup.sqlite_master WHERE type='table'")} if attached else set()
                for customer in customers:
                    key = customer['id']
                    for row in db.rows('SELECT source_id FROM messages WHERE conversation_id=?', (key,)):
                        db.conn.execute('INSERT OR IGNORE INTO deleted_messages VALUES(?)', (db.deletion_token(key, row['source_id']),))
                    for table in ('feishu_task_messages', 'feishu_receipts'):
                        db.conn.execute(f'DELETE FROM {table} WHERE task_id IN (SELECT id FROM tasks WHERE conversation_id=?)', (key,))
                    for table in ('messages', 'outbox', 'tasks'):
                        db.conn.execute(f'DELETE FROM {table} WHERE conversation_id=?', (key,))
                    for table in ('checkpoints', 'writes'):
                        if table in checkpoint_tables:
                            db.conn.execute(f'DELETE FROM cleanup.{table} WHERE thread_id=?', (key,))
                    if keep_customer:
                        db.conn.execute("UPDATE conversations SET latest_id='',handled_id='',fields='{}',state='active',failures=0,retry_after=0 WHERE id=?", (key,))
                    else:
                        db.conn.execute('DELETE FROM conversations WHERE id=?', (key,))
                if all_customers:
                    db.conn.execute('DELETE FROM events')
                    # A full cleanup is also a fresh start: allow the next scan to read these
                    # conversations again instead of suppressing them as previously deleted.
                    db.conn.execute('DELETE FROM deleted_messages')
                    for table in checkpoint_tables & {'checkpoints', 'writes'}:
                        db.conn.execute(f'DELETE FROM cleanup.{table}')
        finally:
            if attached:
                db.conn.execute('DETACH DATABASE cleanup')
        return {'deleted': len(customers)}


def export_workspace(app, include_secrets=False):
    if not isinstance(include_secrets, bool):
        raise ValueError('密钥选项必须为布尔值')
    with app.runtime.lock, app.feishu.lock, app.db.lock, tempfile.TemporaryDirectory() as temp:
        require_stopped(app)
        root = Path(temp)
        business = root / 'business.sqlite3'
        conn = sqlite3.connect(business)
        try:
            app.db.conn.backup(conn)
            conn.create_function('knowledge_terms', 1, lambda v: ' '.join(sorted(terms(v or ''))))
            # Remove credentials from logical rows and freed pages, not only the manifest.
            conn.execute('PRAGMA secure_delete=ON')
            if not include_secrets:
                conn.execute("UPDATE profiles SET api_key=''")
                row = conn.execute("SELECT value FROM settings WHERE key='runtime'").fetchone()
                if row:
                    settings = json.loads(row[0])
                    settings.update(feishu_webhook='', feishu_secret='', feishu_app_secret='', feishu_enabled=False, linkr_token='')
                    conn.execute("UPDATE settings SET value=? WHERE key='runtime'", (json.dumps(settings, ensure_ascii=False),))
            conn.commit()
            conn.execute('VACUUM')
        finally:
            conn.close()
        payload = {'business.sqlite3': business.read_bytes()}
        checkpoint = app.db.path.parent / 'checkpoints.sqlite3'
        if checkpoint.exists():
            source = sqlite3.connect(checkpoint.resolve().as_uri() + '?mode=ro', uri=True)
            target = sqlite3.connect(root / 'checkpoints.sqlite3')
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            payload['checkpoints.sqlite3'] = (root / 'checkpoints.sqlite3').read_bytes()
        manifest = {'format': 'cs-duty-workspace', 'version': 2,
                    'created': datetime.now(timezone.utc).isoformat(), 'includes_secrets': include_secrets,
                    'files': {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
        if sum(map(len, payload.values())) > MAX_BYTES - 4096:
            raise ValueError('数据超过 512 MB，请停止服务后通过文件复制迁移')
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in payload.items():
                archive.writestr(name, data)
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        return output.getvalue()


def restore_workspace(archive_path, target_dir):
    """Validate in staging and publish to a new directory; never overwrite a workspace."""
    target_dir = Path(target_dir).resolve()
    if target_dir.exists():
        raise ValueError('目标目录已存在，请指定一个尚不存在的新数据目录')
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target_dir.parent) as temp:
        stage = Path(temp) / 'workspace'
        stage.mkdir()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or not set(names) <= FILES | LEGACY_FILES | {'manifest.json'}:
                    raise ValueError('迁移包包含重复或未知文件')
                if sum(f.file_size for f in archive.infolist()) > MAX_BYTES:
                    raise ValueError('迁移包解压后不能超过 512 MB')
                manifest = json.loads(archive.read('manifest.json'))
                if not isinstance(manifest, dict) or not isinstance(manifest.get('files'), dict) or not isinstance(manifest.get('includes_secrets'), bool):
                    raise ValueError('迁移包清单格式错误')
                version = manifest.get('version')
                if manifest.get('format') not in ('cs-duty-workspace', 'cs-rpa-workspace') or version not in (1, 2):
                    raise ValueError('不支持的迁移包版本')
                required = {'business.sqlite3', 'mock.json'} if version == 1 else {'business.sqlite3'}
                if set(manifest['files']) != set(names) - {'manifest.json'} or not required <= set(names):
                    raise ValueError('迁移包文件清单不完整')
                for name, digest in manifest['files'].items():
                    data = archive.read(name)
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError('迁移包校验失败，文件可能已损坏')
                    (stage / name).write_bytes(data)
        except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError('迁移包格式错误') from exc
        for path in stage.glob('*.sqlite3'):
            conn = sqlite3.connect(path)
            try:
                if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('迁移数据库完整性检查失败')
                if path.name == 'business.sqlite3':
                    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {'conversations', 'messages', 'profiles', 'settings', 'knowledge', 'outbox', 'tasks'} <= tables:
                        raise ValueError('迁移数据库缺少业务表')
                    row = conn.execute("SELECT value FROM settings WHERE key='runtime'").fetchone()
                    if row:
                        settings = json.loads(row[0])
                        for key in ('transport', 'mock_url', 'jd_url', 'channel'):
                            settings.pop(key, None)
                        conn.execute("UPDATE settings SET value=? WHERE key='runtime'", (json.dumps(settings, ensure_ascii=False),))
                        conn.commit()
            finally:
                conn.close()
        stage.rename(target_dir)
    return {'directory': str(target_dir), 'includes_secrets': manifest['includes_secrets']}
