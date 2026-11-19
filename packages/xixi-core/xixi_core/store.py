"""SQLite is the durable coordinator; a single Core process owns each database."""
import json
import sqlite3
import threading
from contextlib import contextmanager, closing
from pathlib import Path
from .privacy import private_sqlite, private_directory

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,current_session TEXT,pending TEXT,last_owner REAL NOT NULL, dream_cursor INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,kind TEXT NOT NULL,created REAL NOT NULL,config_version INTEGER NOT NULL,bootstrap TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inputs(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,conversation TEXT NOT NULL,channel TEXT NOT NULL,channel_message_id TEXT NOT NULL,source TEXT NOT NULL,sender TEXT NOT NULL,sent_at TEXT NOT NULL,received REAL NOT NULL,text TEXT NOT NULL,reply_to TEXT,attachments TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',session_id TEXT,error TEXT,UNIQUE(channel,channel_message_id));
CREATE TABLE IF NOT EXISTS maintenance(id TEXT PRIMARY KEY,kind TEXT NOT NULL,conversation TEXT NOT NULL,old_session TEXT NOT NULL,from_seq INTEGER NOT NULL,to_seq INTEGER NOT NULL,day TEXT NOT NULL,phase TEXT NOT NULL,result TEXT,new_session TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,conversation TEXT NOT NULL,title TEXT NOT NULL,objective TEXT NOT NULL,context_refs TEXT NOT NULL,executor TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS outbound(id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,conversation TEXT NOT NULL,session_id TEXT NOT NULL,text TEXT NOT NULL,state TEXT NOT NULL,receipt TEXT,created REAL NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tool_calls(request_id TEXT PRIMARY KEY,tool TEXT NOT NULL,state TEXT NOT NULL,result TEXT,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS library(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,source_refs TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS library_versions(id TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS library_operations(request_id TEXT PRIMARY KEY,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plugins(id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,manifest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbound_links(outbound_id TEXT PRIMARY KEY,input_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dream_coverage(timezone TEXT NOT NULL,day TEXT NOT NULL,fingerprint TEXT NOT NULL,batch_id TEXT NOT NULL,PRIMARY KEY(timezone,day));
'''


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def decode(value, default=None):
    return json.loads(value) if value is not None else default


class Store:
    def __init__(self, path):
        private_sqlite(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        current_version = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if current_version and int(current_version[0]) > 4:
            self.db.close()
            raise ValueError('数据库版本高于当前 Core，拒绝降级写入')
        if current_version and int(current_version[0])<4 and str(path)!=':memory:':
            import uuid,os
            backup_path=Path(path).resolve().parent/'backups'/('core-before-v4-'+uuid.uuid4().hex+'.sqlite3')
            private_directory(backup_path.parent)
            descriptor=os.open(backup_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            os.close(descriptor)
            with closing(sqlite3.connect(backup_path)) as target:self.db.backup(target)
            backup_path.chmod(0o600)
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO meta VALUES('schema_version','4') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        private_sqlite(path)

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield self
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            else:
                self.db.execute('COMMIT')

    def execute(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params)

    def one(self, sql, params=()):
        with self.lock:
            row = self.db.execute(sql, params).fetchone()
            return dict(row) if row else None

    def all(self, sql, params=()):
        with self.lock:
            return [dict(x) for x in self.db.execute(sql, params).fetchall()]

    def get(self, key, default=None):
        row = self.one('SELECT value FROM meta WHERE key=?', (key,))
        return decode(row['value']) if row else default

    def put(self, key, value):
        self.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encode(value)))

    def event(self, kind, payload, now):
        self.execute('INSERT INTO events(kind,payload,created) VALUES(?,?,?)', (kind, encode(payload), now))

    def close(self):
        self.db.close()
