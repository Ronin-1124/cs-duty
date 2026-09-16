"""Business records are separate from LangGraph checkpoints and browser fixtures."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from cs_duty.knowledge import terms

ROOT = Path(__file__).resolve().parents[1]


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function('knowledge_terms', 1, lambda value: ' '.join(sorted(terms(value or ''))))
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA recursive_triggers=ON;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, name TEXT, protocol TEXT,
                base_url TEXT, api_key TEXT, model TEXT, timeout INTEGER, max_tokens INTEGER);
            CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY, platform TEXT, shop TEXT,
                customer_key TEXT, name TEXT, latest_id TEXT DEFAULT '', handled_id TEXT DEFAULT '',
                state TEXT DEFAULT 'active', fields TEXT DEFAULT '{}', updated REAL,
                failures INTEGER DEFAULT 0, retry_after REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT REFERENCES conversations(id), source_id TEXT, role TEXT,
                text TEXT, timestamp TEXT, UNIQUE(conversation_id, source_id));
            CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,
                conversation_id TEXT REFERENCES conversations(id), source_id TEXT, reply TEXT,
                status TEXT, reason TEXT, evidence TEXT, created REAL, updated REAL,
                UNIQUE(conversation_id, source_id));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,
                conversation_id TEXT REFERENCES conversations(id), source_id TEXT,
                summary TEXT, fields TEXT, status TEXT, result TEXT DEFAULT '',
                notification TEXT DEFAULT 'pending', created REAL, updated REAL,
                UNIQUE(conversation_id, source_id));
            CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY, title TEXT, content TEXT,
                product TEXT, sources TEXT, enabled INTEGER DEFAULT 1, updated REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT, text TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS deleted_messages(token TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS feishu_task_messages(task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                message_id TEXT UNIQUE,chat_id TEXT,app_id TEXT);
            CREATE TABLE IF NOT EXISTS feishu_receipts(message_id TEXT PRIMARY KEY,task_id TEXT,
                sender_id TEXT,chat_id TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS knowledge_materials(id TEXT PRIMARY KEY,
                kind TEXT, status TEXT, payload TEXT, batch TEXT);
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(title,body);
            CREATE TRIGGER IF NOT EXISTS knowledge_insert AFTER INSERT ON knowledge BEGIN
                INSERT INTO knowledge_fts(rowid,title,body) VALUES(new.rowid,
                    knowledge_terms(new.title || ' ' || new.product),knowledge_terms(new.content));
            END;
            CREATE TRIGGER IF NOT EXISTS knowledge_delete AFTER DELETE ON knowledge BEGIN
                DELETE FROM knowledge_fts WHERE rowid=old.rowid;
            END;
            CREATE TRIGGER IF NOT EXISTS knowledge_update AFTER UPDATE ON knowledge BEGIN
                DELETE FROM knowledge_fts WHERE rowid=old.rowid;
                INSERT INTO knowledge_fts(rowid,title,body) VALUES(new.rowid,
                    knowledge_terms(new.title || ' ' || new.product),knowledge_terms(new.content));
            END;
        """)
        if not self.conn.execute("SELECT 1 FROM settings WHERE key='knowledge_fts_v1'").fetchone():
            self.conn.execute('DELETE FROM knowledge_fts')
            self.conn.execute("INSERT INTO knowledge_fts(rowid,title,body) SELECT rowid,knowledge_terms(title || ' ' || product),knowledge_terms(content) FROM knowledge")
            self.conn.execute("INSERT INTO settings VALUES('knowledge_fts_v1','true')")
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(outbox)')}
        if 'sent_source_id' not in columns:
            self.conn.execute("ALTER TABLE outbox ADD COLUMN sent_source_id TEXT DEFAULT ''")
        self.conn.execute("UPDATE outbox SET status='uncertain', reason='上次运行在发送确认前中断，请核对网页记录' WHERE status='sending'")
        self.conn.execute("UPDATE tasks SET notification='uncertain' WHERE notification='sending'")
        self.conn.commit()

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock, self.conn:
            return self.conn.execute(sql, args).rowcount

    def setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def event(self, kind, text):
        self.execute("INSERT INTO events(kind,text,created) VALUES(?,?,?)", (kind, str(text)[:1000], time.time()))

    def ingest(self, platform, shop, customer_key, name, messages):
        cid = hashlib.sha256(json.dumps([platform, shop, customer_key]).encode()).hexdigest()[:32]
        now = time.time()
        with self.lock, self.conn:
            messages = self.filter_deleted(cid, messages)
            if not messages and not self.one('SELECT id FROM conversations WHERE id=?', (cid,)):
                return cid, False
            self.conn.execute("INSERT OR IGNORE INTO conversations(id,platform,shop,customer_key,name,updated) VALUES(?,?,?,?,?,?)",
                              (cid, platform, shop, customer_key, name, now))
            self.conn.execute("UPDATE conversations SET name=? WHERE id=?", (name, cid))
            new = 0
            for message in messages:
                if not message.get('id') or message.get('role') not in ('customer', 'agent', 'system'):
                    continue
                new += self.conn.execute("INSERT OR IGNORE INTO messages(conversation_id,source_id,role,text,timestamp) VALUES(?,?,?,?,?)",
                    (cid, message['id'], message['role'], message.get('text', ''), message.get('timestamp', ''))).rowcount
            if new:
                latest = self.conn.execute("SELECT source_id FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT 1", (cid,)).fetchone()[0]
                self.conn.execute("UPDATE conversations SET latest_id=?,updated=?,failures=0,retry_after=0 WHERE id=?", (latest, now, cid))
                self.conn.execute("UPDATE outbox SET status='stale',reason='会话已有新消息',updated=? WHERE conversation_id=? AND source_id<>? AND status IN ('draft','ready')", (now, cid, latest))
        return cid, bool(new)

    @staticmethod
    def deletion_token(cid, source_id):
        return hashlib.sha256((cid + ':' + str(source_id or '')).encode()).hexdigest()

    def filter_deleted(self, cid, messages):
        return [m for m in messages if not self.one('SELECT 1 FROM deleted_messages WHERE token=?',
                (self.deletion_token(cid, m.get('id', '')),))]

    def conversation(self, cid):
        row = self.one("SELECT * FROM conversations WHERE id=?", (cid,))
        if not row:
            raise ValueError('会话不存在')
        row['fields'] = json.loads(row['fields'])
        return row

    def history(self, cid, limit=80):
        return list(reversed(self.rows("SELECT source_id AS id,role,text,timestamp FROM messages WHERE conversation_id=? ORDER BY seq DESC LIMIT ?", (cid, limit))))

    def set_state(self, cid, state, fields=None):
        if state not in ('active', 'collecting', 'waiting', 'human', 'error'):
            raise ValueError('无效会话状态')
        with self.lock, self.conn:
            self.conn.execute("UPDATE conversations SET state=?, updated=?,failures=0,retry_after=0 WHERE id=?", (state, time.time(), cid))
            if fields is not None:
                self.conn.execute("UPDATE conversations SET fields=? WHERE id=?", (json.dumps(fields, ensure_ascii=False), cid))

    def prepare_reply(self, cid, source_id, reply, status, reason='', evidence=None, replace_draft=False):
        if status not in ('draft', 'ready', 'ignored'):
            raise ValueError('无效回复状态')
        with self.lock, self.conn:
            conversation = self.conversation(cid)
            if conversation['latest_id'] != source_id or conversation['state'] == 'human':
                return None
            existing = self.one("SELECT * FROM outbox WHERE conversation_id=? AND source_id=?", (cid, source_id))
            if existing:
                if replace_draft and existing['status'] in ('draft', 'ready'):
                    self.conn.execute('UPDATE outbox SET reply=?,status=?,reason=?,evidence=?,updated=? WHERE id=?',
                                      (reply, status, reason, json.dumps(evidence or []), time.time(), existing['id']))
                return existing['id']
            oid = uuid.uuid4().hex
            now = time.time()
            self.conn.execute("INSERT INTO outbox(id,conversation_id,source_id,reply,status,reason,evidence,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (oid, cid, source_id, reply, status, reason, json.dumps(evidence or [], ensure_ascii=False), now, now))
            self.conn.execute("UPDATE conversations SET handled_id=? WHERE id=?", (source_id, cid))
            return oid

    def outbox_status(self, oid, status, reason=''):
        self.execute("UPDATE outbox SET status=?,reason=?,updated=? WHERE id=?", (status, reason, time.time(), oid))

    def create_task(self, cid, source_id, summary, fields):
        tid = hashlib.sha256(f'{cid}:{source_id}'.encode()).hexdigest()[:32]
        now = time.time()
        self.execute("INSERT OR IGNORE INTO tasks(id,conversation_id,source_id,summary,fields,status,created,updated) VALUES(?,?,?,?,?,'open',?,?)",
                     (tid, cid, source_id, summary, json.dumps(fields, ensure_ascii=False), now, now))
        return tid

    def resolve_task(self, tid, result):
        if not isinstance(result, str) or not result.strip() or len(result) > 5000:
            raise ValueError('请填写 1–5000 字的处理结果')
        changed = self.execute("UPDATE tasks SET status='ready',result=?,updated=? WHERE id=? AND status='open'", (result.strip(), time.time(), tid))
        if not changed:
            raise ValueError('待办不存在或已经处理')

    def close(self):
        with self.lock:
            self.conn.close()
