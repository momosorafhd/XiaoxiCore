"""SQLite library and archive helpers."""
from contextlib import contextmanager
import hashlib
import datetime as dt
import json
import sqlite3
import threading
import time
from .privacy import private_sqlite
from .store import encode, decode


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def parse_timestamp(value):
    if not isinstance(value,str):raise ValueError('时间必须为含时区 ISO8601 字符串')
    parsed=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('时间必须包含时区')
    return parsed.timestamp()


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


class ArchiveUnavailable(RuntimeError):
    pass


LIBRARY_SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS library(id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,source_refs TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS library_versions(id TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS library_operations(request_id TEXT PRIMARY KEY,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS xixi_raw_messages(record_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,object_id TEXT NOT NULL,direction TEXT NOT NULL,conversation TEXT NOT NULL,sent_at TEXT NOT NULL,received_at TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS xixi_raw_conversation_time ON xixi_raw_messages(conversation,created,record_id);
CREATE TABLE IF NOT EXISTS xixi_raw_receipts(request_id TEXT PRIMARY KEY,record_id TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,created REAL NOT NULL,FOREIGN KEY(record_id) REFERENCES xixi_raw_messages(record_id));
'''


class LibraryStore:
    """Minimal Store-compatible Library database, without Core queues/sessions."""
    def __init__(self, path):
        self.path = str(path)
        private_sqlite(path)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(LIBRARY_SCHEMA)
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
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def get(self, key, default=None):
        row = self.one('SELECT value FROM meta WHERE key=?', (key,))
        return decode(row['value']) if row else default

    def put(self, key, value):
        self.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, encode(value)))

    def close(self):
        self.db.close()

    def migrate_legacy(self, core_store):
        """Copy exact legacy Library rows, verify, then remove their Core copies.

        Run before workers start and after the caller's consistent backup. A crash
        between databases leaves source rows intact; replay verifies the existing
        destination rows. A conflicting destination aborts without source loss.
        """
        keys = {'library': ('id',), 'library_versions': ('id', 'version'), 'library_operations': ('request_id',)}
        with core_store.lock:
            snapshots = {table: core_store.all('SELECT * FROM ' + table) for table in keys}
            with self.tx():
                for table, rows in snapshots.items():
                    for row in rows:
                        where = ' AND '.join(key + '=?' for key in keys[table])
                        params = tuple(row[key] for key in keys[table])
                        prior = self.one('SELECT * FROM ' + table + ' WHERE ' + where, params)
                        if prior is not None and prior != row:
                            raise ValueError('Library 迁移冲突，原数据保留：' + table)
                        if prior is None:
                            columns = list(row)
                            self.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(row.values()))
                for table, rows in snapshots.items():
                    for row in rows:
                        where = ' AND '.join(key + '=?' for key in keys[table])
                        copied = self.one('SELECT * FROM ' + table + ' WHERE ' + where, tuple(row[key] for key in keys[table]))
                        if copied != row:
                            raise RuntimeError('Library 迁移核对失败，原数据保留')
            # No Core writer can change the source while its lock is held.
            with core_store.tx():
                for table, rows in snapshots.items():
                    for row in rows:
                        where = ' AND '.join(key + '=?' for key in keys[table])
                        core_store.execute('DELETE FROM ' + table + ' WHERE ' + where, tuple(row[key] for key in keys[table]))
                core_store.put('library_owned_storage_migrated', True)
        return {table: len(rows) for table, rows in snapshots.items()}


class RawLibrary:
    mode = 'local-development-library'

    def __init__(self, path_or_store, clock=time.time):
        self.store = path_or_store if isinstance(path_or_store, LibraryStore) else LibraryStore(path_or_store)
        self.owns_store = not isinstance(path_or_store, LibraryStore)
        self.clock = clock

    @staticmethod
    def _validate_message(payload):
        if not isinstance(payload, dict):
            raise ValueError('原始消息必须为对象')
        for key in ('id', 'direction', 'conversationId', 'source', 'sender', 'channel', 'text', 'sentAt', 'receivedAt'):
            if not isinstance(payload.get(key), str) or (key != 'text' and not payload[key]):
                raise ValueError('原始消息缺少 ' + key)
        if payload['direction'] not in {'input', 'outbound'}:
            raise ValueError('direction 无效')
        if not isinstance(payload.get('attachments', []), list):
            raise ValueError('attachments 必须为数组')
        parse_timestamp(payload['sentAt']);parse_timestamp(payload['receivedAt'])
        canonical(payload)

    def append_message(self, payload, request_id):
        self._validate_message(payload)
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('原话归档需要稳定 request_id')
        frozen = canonical(payload)
        fingerprint = hashlib.sha256(frozen.encode()).hexdigest()
        record_id = 'chat-' + hashlib.sha256(request_id.encode()).hexdigest()[:32]
        with self.store.tx():
            prior = self.store.one('SELECT * FROM xixi_raw_messages WHERE request_id=?', (request_id,))
            if prior:
                if prior['fingerprint'] != fingerprint or prior['payload'] != frozen:
                    raise ValueError('原话归档幂等标识内容冲突')
                return {'recordId': prior['record_id'], 'fingerprint': fingerprint, 'existing': True}
            self.store.execute('INSERT INTO xixi_raw_messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                (record_id, request_id, payload['id'], payload['direction'], payload['conversationId'], payload['sentAt'], payload['receivedAt'], frozen, fingerprint, self.clock()))
        return {'recordId': record_id, 'fingerprint': fingerprint, 'existing': False}

    def append_receipt(self, record_id, receipt, request_id):
        if not isinstance(receipt, dict) or receipt.get('status') not in {'sending', 'accepted', 'delivered', 'failed', 'unknown'}:
            raise ValueError('发送回执状态无效；accepted 不等于 delivered')
        if not isinstance(request_id, str) or not request_id:
            raise ValueError('发送回执需要稳定 request_id')
        frozen = canonical(receipt)
        fingerprint = hashlib.sha256(frozen.encode()).hexdigest()
        with self.store.tx():
            if not self.store.one('SELECT record_id FROM xixi_raw_messages WHERE record_id=?', (record_id,)):
                raise ValueError('回执关联原话不存在')
            prior = self.store.one('SELECT * FROM xixi_raw_receipts WHERE request_id=?', (request_id,))
            if prior:
                if prior['record_id'] != record_id or prior['fingerprint'] != fingerprint:
                    raise ValueError('回执幂等标识内容冲突')
                return {'recordId': record_id, 'requestId': request_id, 'existing': True}
            self.store.execute('INSERT INTO xixi_raw_receipts VALUES(?,?,?,?,?)', (request_id, record_id, frozen, fingerprint, self.clock()))
        return {'recordId': record_id, 'requestId': request_id, 'existing': False}

    def read_message(self, record_id):
        row = self.store.one('SELECT * FROM xixi_raw_messages WHERE record_id=?', (record_id,))
        if not row:
            raise ValueError('图书馆原话不存在')
        receipts = [dict(decode(r['payload']), recordedAt=r['created'], requestId=r['request_id'])
                    for r in self.store.all('SELECT * FROM xixi_raw_receipts WHERE record_id=? ORDER BY created,rowid', (record_id,))]
        return dict(decode(row['payload']), recordId=record_id, archiveFingerprint=row['fingerprint'], receipts=receipts)

    def query_messages(self, criteria=None):
        criteria = criteria or {}
        allowed = {'conversationId', 'direction', 'source', 'query', 'messageId', 'from', 'to', 'limit', 'offset'}
        if not isinstance(criteria, dict) or set(criteria) - allowed:
            raise ValueError('原话查询条件无效')
        limit, offset = criteria.get('limit', 100), criteria.get('offset', 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError('原话查询分页无效')
        start = parse_timestamp(criteria['from']) if criteria.get('from') else None
        end = parse_timestamp(criteria['to']) if criteria.get('to') else None
        rows = self.store.all('SELECT record_id,payload FROM xixi_raw_messages ORDER BY rowid')
        matches = []
        for row in rows:
            item = decode(row['payload'])
            if any(criteria.get(key) and item.get(key) != criteria[key] for key in ('conversationId', 'direction', 'source')):
                continue
            if criteria.get('messageId') and criteria['messageId'] not in {item['id'], row['record_id']}:
                continue
            if criteria.get('query') and str(criteria['query']).casefold() not in item['text'].casefold():
                continue
            if start is not None and parse_timestamp(item['sentAt']) < start or end is not None and parse_timestamp(item['sentAt']) > end:
                continue
            matches.append(row['record_id'])
        selected = matches[offset:offset + limit]
        return {'items': [self.read_message(key) for key in selected], 'pagination': {'offset': offset, 'limit': limit, 'total': len(matches), 'nextOffset': offset + len(selected) if offset + len(selected) < len(matches) else None}}

    def health(self):
        self.store.one('SELECT 1')
        return {'ok': True, 'mode': self.mode, 'productionIntegration': False, 'taxonomy': 'existing classifications unchanged'}

    def close(self):
        if self.owns_store:
            self.store.close()
