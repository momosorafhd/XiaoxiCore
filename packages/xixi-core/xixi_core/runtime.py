"""Development mock and the Core-owned HTTP mapping to XixiBridge."""
import json
import sqlite3
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from .store import encode
from .privacy import private_sqlite


class BridgeRuntime:
    mode = 'dsh'

    def __init__(self, url, token='', timeout=30):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('首版 XixiBridge 仅允许本机 HTTP 地址')
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout

    def request(self, path, body=None, timeout=None):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
            headers['X-Xixi-Token'] = self.token
        req = urllib.request.Request(self.url + path, data=encode(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(req, timeout=self.timeout if timeout is None else timeout) as response:
            return json.load(response)

    def create(self, session):
        return self.request('/sessions', session,timeout=30)

    def resume(self, session_id):
        return self.state(session_id)

    def deliver(self, session_id, message):
        # Only immutable contract fields belong in the remote idempotence key.
        # Core queue state/error/session routing changes during crash recovery.
        keys = ('inputId', 'text', 'source', 'sentAt', 'receivedAt', 'replyTo',
                'channel', 'sender', 'attachments', 'channelMessageId', 'conversationId', 'contextFragments', 'promptSections', 'presentationText')
        envelope = {key: message[key] for key in keys if key in message}
        return self.request(f'/sessions/{session_id}/deliver', envelope,timeout=30)

    def input_status(self, session_id, input_id):
        return self.request(f'/sessions/{session_id}/inputs/{input_id}',timeout=10)

    def state(self, session_id):
        return self.request(f'/sessions/{session_id}/state',timeout=10)

    def usage(self, session_id):
        return self.request(f'/sessions/{session_id}/usage',timeout=10)

    def maintain(self, batch):
        # Maintenance may be slow; transport timeout never means safe to replay.
        return self.request('/maintenance', batch)

    def run_task(self, task):
        keys = ('id', 'title', 'objective', 'contextRefs', 'executor', 'originConversationId', 'model', 'modelOptions', 'contextFragments', 'toolScopes', 'promptSections', 'presentationText', 'nativeCompaction', 'libraryMode', 'promptPolicy', 'resultDestination', 'pluginTask', 'agentPreset')
        return self.request('/tasks/run', {key: task[key] for key in keys if key in task})

    def task_status(self, task_id):
        return self.request(f'/tasks/{task_id}',timeout=10)

    def task_cancel(self, task_id):
        return self.request(f'/tasks/{task_id}/cancel', {},timeout=15)

    def cancel(self, session_id, keep_inbox=True):
        return self.request(f'/sessions/{session_id}/cancel', {'keepInbox': keep_inbox})

    def quarantine_input(self, session_id, input_id, stop=False):
        return self.request(f'/sessions/{session_id}/inputs/{input_id}/quarantine',{'stop':stop},timeout=15)

    def maintenance_state(self, batch_id, attempt=0):
        return self.request(f'/maintenance/{batch_id}?attempt={attempt}',timeout=10)

    def stop_maintenance(self, batch_id, attempt=0):
        return self.request(f'/maintenance/{batch_id}/stop',{'attempt':attempt},timeout=15)

    def release_unused(self, session_id):
        return self.request(f'/sessions/{session_id}/release',{'reason':'unused_recovery'},timeout=15)

    def plugins(self):
        return self.request('/plugins',timeout=5)

    def set_plugin(self, plugin_id, patch):
        from urllib.parse import quote
        if not isinstance(plugin_id,str):raise ValueError('plugin id 无效')
        return self.request('/plugins/'+quote(plugin_id,safe=''),patch,timeout=10)

    def prompt_policy_capabilities(self):
        return self.request('/prompt-policy/capabilities',timeout=10)

    def model_catalog(self):
        return self.request('/models',timeout=10)

    def context(self,session_id):
        return self.request(f'/sessions/{session_id}/context',timeout=5)

    def health(self):
        return self.request('/health', timeout=5)


class MockRuntime:
    """No LLM, no DSH: deterministic durable smoke-test adapter."""
    mode = 'mock'

    def __init__(self, path):
        private_sqlite(path)
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript('''PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,result TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,result TEXT NOT NULL);''')
        private_sqlite(path)

    def model_catalog(self):
        return {'source':'mock','metadataOnly':True,'default':None,'groups':[],'routableProviders':[],
                'agentPresets':[{'id':'standard','available':True},{'id':'minimal','available':True}],'failures':[]}

    def create(self, session):
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO sessions VALUES(?,?)', (session['id'], encode(session)))
        return {'id': session['id'], 'state': 'idle'}

    def resume(self, session_id):
        return self.state(session_id)

    def state(self, session_id):
        with self.lock:
            row = self.db.execute('SELECT data FROM sessions WHERE id=?', (session_id,)).fetchone()
        if not row:
            raise ValueError('mock session not found')
        config = json.loads(row['data'])
        return {'id': session_id, 'state': 'idle', 'busy': False, 'kind': config.get('kind', 'daily'),
                'agentPreset': config.get('agentPreset', 'standard')}

    def deliver(self, session_id, message):
        with self.lock:
            row = self.db.execute('SELECT result FROM inputs WHERE id=?', (message['inputId'],)).fetchone()
            if row:
                return json.loads(row['result'])
            self.state(session_id)
            result = {'status': 'completed', 'text': '[mock 演示] 已接收：' + message['text'], 'usage': self.usage(session_id)}
            self.db.execute('INSERT INTO inputs VALUES(?,?,?)', (message['inputId'], session_id, encode(result)))
            return result

    def input_status(self, session_id, input_id):
        with self.lock:
            row = self.db.execute('SELECT result FROM inputs WHERE id=? AND session_id=?', (input_id, session_id)).fetchone()
        return json.loads(row['result']) if row else {'status': 'not_found'}

    def usage(self, session_id):
        with self.lock:
            count = self.db.execute('SELECT COUNT(*) FROM inputs WHERE session_id=?', (session_id,)).fetchone()[0]
        return {'usedTokens': count * 128, 'contextWindow': 8192, 'source': 'estimate', 'method': 'mock deterministic counter; not model tokens', 'measuredAt': time.time()}

    def maintain(self, batch):
        with self.lock:
            row = self.db.execute('SELECT result FROM jobs WHERE id=?', (batch['id'],)).fetchone()
            if row:
                return json.loads(row['result'])
            source = batch['sourceMessages']
            refs = [x['id'] for x in source]
            text = '\n'.join(f"{x['sentAt']} {x['source']}: {x['text']}" for x in source)
            result = {'handoff': ('[mock 固定范围原文摘录，非模型摘要]\n' + text)[:batch.get('handoffMaxChars', 12000)],
                      'selectedOriginals': refs[-batch.get('selectedOriginalLimit', 12):] if batch.get('selectedOriginalLimit', 12) else [],
                      'memories': [], 'diary': {'title': batch['day'] + ' mock 日记', 'body': '[mock 原文回顾，未提炼长期记忆]\n' + (text or '本日无对话'), 'sourceRefs': refs}}
            self.db.execute('INSERT INTO jobs VALUES(?,?)', (batch['id'], encode(result)))
            return result

    def run_task(self, task):
        with self.lock:
            row = self.db.execute('SELECT result FROM jobs WHERE id=?', (task['id'],)).fetchone()
            if row:
                return json.loads(row['result'])
            result = {'status': 'completed', 'summary': '[mock 独立任务演示] ' + task['objective'], 'artifactRefs': []}
            self.db.execute('INSERT INTO jobs VALUES(?,?)', (task['id'], encode(result)))
            return result

    def task_status(self, task_id):
        with self.lock:
            row = self.db.execute('SELECT result FROM jobs WHERE id=?', (task_id,)).fetchone()
        return json.loads(row['result']) if row else {'status': 'not_found'}

    def task_cancel(self, task_id):
        with self.lock:
            row=self.db.execute('SELECT result FROM jobs WHERE id=?',(task_id,)).fetchone()
            if row:return json.loads(row['result'])
            result={'status':'stopped','summary':'','artifactRefs':[],'safeToRelease':True}
            self.db.execute('INSERT INTO jobs VALUES(?,?)',(task_id,encode(result)))
            return result

    def cancel(self, session_id, keep_inbox=True):
        return {'status': 'idle', 'keepInbox': keep_inbox}

    def quarantine_input(self, session_id, input_id, stop=False):
        result=self.input_status(session_id,input_id)
        if result['status'] in {'completed','failed','stopped','not_found'}:return result
        result={'status':'quarantined','safeToRelease':True,'safeToContinue':True,'evidence':{'mock':True,'idle':True}}
        with self.lock:self.db.execute('UPDATE inputs SET result=? WHERE id=?',(encode(result),input_id))
        return result

    def maintenance_state(self,batch_id,attempt=0):
        return {'status':'idle','safeToRelease':True,'evidence':{'mock':True,'noRemoteExecutor':True}}

    def stop_maintenance(self,batch_id,attempt=0):
        return self.maintenance_state(batch_id,attempt)

    def release_unused(self,session_id):
        with self.lock:
            used=self.db.execute('SELECT id FROM inputs WHERE session_id=?',(session_id,)).fetchone()
        return {'status':'blocked' if used else 'released','safeToRelease':not bool(used),'evidence':{'mock':True}}

    def plugins(self):
        return {'host':'dsh','plugins':[],'failures':[],'mock':True}

    def set_plugin(self,plugin_id,patch):
        raise ValueError('mock 不加载 DSH 技能')

    def health(self):
        return {'ok': True, 'mode': 'mock', 'llm': False}


class ConsoleChannel:
    """Delivered means stored in the console feed; it never means human-read."""
    def send(self, outbound):
        return {'status': 'delivered', 'channel': 'console', 'messageId': outbound['id'], 'meaning': 'available in local console; not a read receipt'}

    def receipt(self, outbound_id):
        return {'status': 'unknown', 'messageId': outbound_id}
