"""Library v0.4: local content, original chats, diaries and automatic indices.

No business-role ACL, automatic recall, external chat client or permanent delete.
The store is separate from Core and rejects implicit use of an older production DB.
"""
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo
import base64
import datetime as dt
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import threading
import time

from .raw_library import RawLibrary, canonical, parse_timestamp

CATEGORIES = ('事实与经历','认识与判断','兴趣','黑话与名词解释','资料')
SEARCH_CATEGORIES = CATEGORIES[1:]
SCHEMA = '''
CREATE TABLE IF NOT EXISTS v04_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v04_items(id TEXT PRIMARY KEY,category TEXT NOT NULL,title TEXT NOT NULL,
 body TEXT NOT NULL,summary TEXT NOT NULL,tags TEXT NOT NULL,aliases TEXT NOT NULL,source_refs TEXT NOT NULL,
 version INTEGER NOT NULL,status TEXT NOT NULL,needs_review INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS v04_category ON v04_items(category,status,updated,id);
CREATE TABLE IF NOT EXISTS v04_versions(id TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,
 created REAL NOT NULL,PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS v04_operations(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS v04_keywords(item_id TEXT NOT NULL,version INTEGER NOT NULL,token TEXT NOT NULL,
 PRIMARY KEY(item_id,token));
CREATE INDEX IF NOT EXISTS v04_keyword_lookup ON v04_keywords(token,item_id);
CREATE TABLE IF NOT EXISTS v04_vector_jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,
 version INTEGER NOT NULL,encoder TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
 error TEXT,updated REAL NOT NULL,UNIQUE(item_id,version,encoder));
CREATE TABLE IF NOT EXISTS v04_vectors(item_id TEXT NOT NULL,version INTEGER NOT NULL,encoder TEXT NOT NULL,
 chunk INTEGER NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,vector TEXT NOT NULL,
 PRIMARY KEY(item_id,version,encoder,chunk));
CREATE TABLE IF NOT EXISTS v04_diaries(id TEXT PRIMARY KEY,day TEXT UNIQUE NOT NULL,title TEXT NOT NULL,
 body TEXT NOT NULL,source_refs TEXT NOT NULL,version INTEGER NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS v04_diary_versions(id TEXT NOT NULL,version INTEGER NOT NULL,content TEXT NOT NULL,
 PRIMARY KEY(id,version));
CREATE TABLE IF NOT EXISTS xixi_raw_messages(record_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,
 object_id TEXT NOT NULL,direction TEXT NOT NULL,conversation TEXT NOT NULL,sent_at TEXT NOT NULL,
 received_at TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS xixi_raw_conversation_time ON xixi_raw_messages(conversation,created,record_id);
CREATE UNIQUE INDEX IF NOT EXISTS v04_raw_identity ON xixi_raw_messages(object_id,direction,conversation);
CREATE TABLE IF NOT EXISTS xixi_raw_receipts(request_id TEXT PRIMARY KEY,record_id TEXT NOT NULL,
 payload TEXT NOT NULL,fingerprint TEXT NOT NULL,created REAL NOT NULL,
 FOREIGN KEY(record_id) REFERENCES xixi_raw_messages(record_id));
'''


def text(value,name,empty=False,maximum=1000000):
    if not isinstance(value,str) or (not empty and not value.strip()) or len(value)>maximum:
        raise ValueError(name+' 必须为有效文本')
    return value


def strings(value,name):
    if not isinstance(value,list) or any(not isinstance(x,str) or not x.strip() for x in value):
        raise ValueError(name+' 必须为字符串数组')
    return list(dict.fromkeys(value))


def integer(value,name,low=0,high=1000):
    if isinstance(value,bool) or not isinstance(value,int) or not low<=value<=high:
        raise ValueError(name+' 超出允许范围')
    return value


def tokens(value):
    folded=value.casefold()
    result=set(re.findall(r'[a-z0-9_]+',folded))
    for span in re.findall(r'[\u3400-\u9fff]+',folded):
        result.update(span)
        result.update(span[i:i+2] for i in range(len(span)-1))
    return result


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class V04Store:
    def __init__(self,path):
        original=Path(path).expanduser()
        if original.is_symlink():raise ValueError('图书馆数据库不能为符号链接')
        self.path=str(original.resolve());target=Path(self.path)
        target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        # Validate an existing file without changing its journal mode, permissions,
        # schema or data. A path error must not touch the old production Library.
        if target.exists() and target.stat().st_size:
            probe=sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)
            try:
                tables={r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and 'v04_meta' not in tables:raise ValueError('目标不是v0.4数据库，旧生产库不得隐式迁移')
                if 'v04_meta' in tables:
                    version=probe.execute("SELECT value FROM v04_meta WHERE key='schema_version'").fetchone()
                    if version and json.loads(version[0])!=1:raise ValueError('不支持的图书馆数据库版本')
            finally:probe.close()
        if os.name=='posix':
            from .privacy import private_sqlite
            private_sqlite(target)
        self.lock=threading.RLock();self.closed=False
        self.db=sqlite3.connect(self.path,isolation_level=None,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL');self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('PRAGMA busy_timeout=5000');self.db.execute('PRAGMA foreign_keys=ON')
        tables={r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and 'v04_meta' not in tables:
            self.db.close();raise ValueError('目标不是v0.4数据库，旧生产库不得隐式迁移')
        self.db.executescript(SCHEMA)
        if self.get('schema_version') not in (None,1):
            self.db.close();raise ValueError('不支持的图书馆数据库版本')
        self.put('schema_version',1)
        if os.name=='posix':private_sqlite(target)

    @contextmanager
    def tx(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:yield self
            except BaseException:self.db.execute('ROLLBACK');raise
            else:self.db.execute('COMMIT')

    def execute(self,sql,args=()):
        with self.lock:return self.db.execute(sql,args)

    def all(self,sql,args=()):
        with self.lock:return [dict(x) for x in self.db.execute(sql,args).fetchall()]

    def one(self,sql,args=()):
        rows=self.all(sql,args);return rows[0] if rows else None

    def get(self,key,default=None):
        row=self.one('SELECT value FROM v04_meta WHERE key=?',(key,))
        return json.loads(row['value']) if row else default

    def put(self,key,value):
        self.execute('INSERT INTO v04_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,canonical(value)))

    def close(self):
        with self.lock:
            if not self.closed:self.db.close();self.closed=True


class LocalOnnxEncoder:
    """Run the already-installed local BGE model in its own Python ABI environment."""
    def __init__(self,python,model_dir,encoder_id=None,timeout=120):
        # Preserve the venv executable path. Resolving bin/python's symlink to
        # /usr/bin/python loses pyvenv.cfg discovery and its ONNX dependencies.
        self.python=os.path.abspath(os.fspath(python));self.model_dir=str(Path(model_dir).resolve())
        if not Path(self.python).is_file() or not Path(self.model_dir).is_dir():
            raise ValueError('本地编码器Python或模型目录不存在')
        manifest=json.loads((Path(self.model_dir)/'manifest.json').read_text(encoding='utf-8'))
        self.encoder_id=encoder_id or 'bge-small-zh-v1.5:'+manifest['revision']+':cls:normalize:prefix:tokens450overlap32'
        self.timeout=timeout

    def _run(self,operation,value):
        helper=Path(__file__).with_name('library_v04_encode.py')
        env=dict(os.environ,HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',PYTHONIOENCODING='utf-8')
        result=subprocess.run([self.python,str(helper)],input=json.dumps({'modelDir':self.model_dir,'operation':operation,'text':value},ensure_ascii=False),
            text=True,encoding='utf-8',capture_output=True,timeout=self.timeout,env=env)
        if result.returncode:raise RuntimeError('本地ONNX编码器退出，状态码 '+str(result.returncode))
        try:return json.loads(result.stdout)
        except ValueError as exc:raise RuntimeError('本地ONNX编码器未返回有效向量') from exc

    def encode_query(self,value):return self._run('query',value)
    def encode_document(self,value):return self._run('document',value)


class LibraryV04:
    mode='library-v0.4-sqlite'

    def __init__(self,path,*,encoder=None,encoder_id=None,clock=time.time,chunk_chars=400,semantic_threshold=0.3,auto_worker=False):
        self.store=V04Store(path);self.clock=clock;self.encoder=encoder
        self.encoder_id=encoder_id or getattr(encoder,'encoder_id',None) or ('unconfigured' if encoder is None else 'injected-local-encoder')
        self.chunk_chars=integer(chunk_chars,'chunk_chars',32,8000)
        self.semantic_threshold=float(semantic_threshold)
        if not math.isfinite(self.semantic_threshold) or not -1<=self.semantic_threshold<=1:raise ValueError('向量阈值无效')
        self._vector_lock=threading.Lock();self._closed=threading.Event();self._wake=threading.Event();self._worker=None
        self.store.execute("UPDATE v04_vector_jobs SET state='pending' WHERE state='running'")
        self.store.execute("INSERT OR IGNORE INTO v04_vector_jobs(item_id,version,encoder,state,updated) SELECT id,version,?,'pending',? FROM v04_items WHERE status='active'",(self.encoder_id,self.clock()))
        self.archive=V04Archive(self.store,clock=clock)
        if auto_worker and encoder is not None:self.start_vector_worker()

    def health(self):
        self.store.one('SELECT 1')
        return {'ok':True,'mode':self.mode,'productionIntegration':True,'taxonomy':'v0.4-five-categories',
                'vector':self.index_status(),'permanentCleanupEnabled':False}

    def categories(self):
        return {'items':[{'id':c,'name':c,'regularSearch':c in SEARCH_CATEGORIES} for c in CATEGORIES],
                'verified':True,'backend':self.mode,'hierarchy':False}

    def _item(self,row):
        value=dict(row)
        for source,destination in [('source_refs','sourceRefs'),('tags','tags'),('aliases','aliases')]:
            value[destination]=json.loads(value.pop(source))
        value['needsReview']=bool(value.pop('needs_review'));value['kind']='memory'
        return value

    def _prior(self,request_id,intent):
        text(request_id,'request_id',maximum=512);digest=fingerprint(intent)
        old=self.store.one('SELECT fingerprint,result FROM v04_operations WHERE request_id=?',(request_id,))
        if old:
            if old['fingerprint']!=digest:raise ValueError('幂等请求编号内容冲突')
            return json.loads(old['result']),digest
        return None,digest

    def _receipt(self,request_id,digest,result):
        self.store.execute('INSERT INTO v04_operations VALUES(?,?,?)',(request_id,digest,canonical(result)))
        return result

    def _save_item(self,item,request_id):
        if not isinstance(item,dict) or item.get('category') not in CATEGORIES:raise ValueError('必须明确选择五个并列分类之一')
        if set(item)-{'id','expectedVersion','kind','category','title','body','summary','tags','aliases','sourceRefs','needsReview','status'}:
            raise ValueError('条目含未支持字段；v0.4没有子分类')
        text(item.get('body'),'body');text(item.get('title',''),'title',True,2000)
        text(item.get('summary',''),'资料摘要' if item['category']=='资料' else 'summary',item['category']!='资料',20000)
        for field in ('tags','aliases','sourceRefs'):strings(item.get(field,[]),field)
        if 'needsReview' in item and not isinstance(item['needsReview'],bool):raise ValueError('needsReview必须为布尔值')
        previous,digest=self._prior(request_id,{'operation':'write','item':item})
        if previous is not None:return previous
        item_id=item.get('id') or 'item-'+hashlib.sha256(request_id.encode()).hexdigest()[:24]
        text(item_id,'id',maximum=200)
        old=self.store.one('SELECT * FROM v04_items WHERE id=?',(item_id,));expected=item.get('expectedVersion')
        if old and (isinstance(expected,bool) or not isinstance(expected,int) or expected!=old['version']):
            raise ValueError('版本冲突：修订必须带当前expectedVersion')
        if not old and expected not in (None,0):raise ValueError('版本冲突：条目不存在')
        now=self.clock();version=old['version']+1 if old else 1
        value={'id':item_id,'category':item['category'],'title':item.get('title','').strip() or item['body'].strip().splitlines()[0][:100],
               'body':item['body'],'summary':item.get('summary',''),'tags':strings(item.get('tags',[]),'tags'),
               'aliases':strings(item.get('aliases',[]),'aliases'),'sourceRefs':strings(item.get('sourceRefs',[]),'sourceRefs'),
               'version':version,'status':item.get('status',old['status'] if old else 'active'),
               'needsReview':item.get('needsReview',bool(old['needs_review']) if old else True),
               'created':old['created'] if old else now,'updated':now,'kind':'memory'}
        if value['status'] not in {'active','delete_marked'}:raise ValueError('status无效')
        self.store.execute('''INSERT INTO v04_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
         category=excluded.category,title=excluded.title,body=excluded.body,summary=excluded.summary,tags=excluded.tags,
         aliases=excluded.aliases,source_refs=excluded.source_refs,version=excluded.version,status=excluded.status,
         needs_review=excluded.needs_review,updated=excluded.updated''',
         (item_id,value['category'],value['title'],value['body'],value['summary'],canonical(value['tags']),canonical(value['aliases']),
          canonical(value['sourceRefs']),version,value['status'],int(value['needsReview']),value['created'],now))
        self.store.execute('INSERT INTO v04_versions VALUES(?,?,?,?)',(item_id,version,canonical(value),now))
        self.store.execute('DELETE FROM v04_keywords WHERE item_id=?',(item_id,));self.store.execute('DELETE FROM v04_vectors WHERE item_id=?',(item_id,))
        self.store.execute("UPDATE v04_vector_jobs SET state='obsolete',updated=? WHERE item_id=? AND state IN ('pending','running','failed')",(now,item_id))
        if value['status']=='active':
            for token in tokens('\n'.join([value['title'],value['body'],value['summary'],*value['tags'],*value['aliases']])):
                self.store.execute('INSERT INTO v04_keywords VALUES(?,?,?)',(item_id,version,token))
            self.store.execute('INSERT INTO v04_vector_jobs(item_id,version,encoder,state,updated) VALUES(?,?,?,?,?)',(item_id,version,self.encoder_id,'pending',now))
        self._wake.set()
        return self._receipt(request_id,digest,dict(value,indexStatus={'keyword':'ready' if value['status']=='active' else 'excluded','vector':'pending' if value['status']=='active' else 'excluded'}))

    def write(self,item,request_id):
        if isinstance(item,dict) and item.get('kind')=='diary':
            return self.write_diary(item.get('day') or item.get('id','').removeprefix('diary-'),item,request_id)
        with self.store.tx():return self._save_item(item,request_id)

    def read(self,item_id,version=None):
        row=self.store.one('SELECT * FROM v04_items WHERE id=?',(text(item_id,'id',maximum=200),))
        if not row:raise ValueError('条目不存在')
        versions=[json.loads(v['content']) for v in self.store.all('SELECT content FROM v04_versions WHERE id=? ORDER BY version DESC',(item_id,))]
        item=self._item(row) if version is None else next((v for v in versions if v['version']==version),None)
        if item is None:raise ValueError('版本不存在')
        return {'item':item,'versions':versions}

    def revise(self,item_id,expected_version,changes,request_id):
        if not isinstance(changes,dict) or set(changes)-{'category','title','body','summary','tags','aliases','sourceRefs','needsReview','summaryUnchanged'}:
            raise ValueError('不支持的修订字段')
        with self.store.tx():
            prior,digest=self._prior(request_id,{'operation':'revise','id':item_id,'expectedVersion':expected_version,'changes':changes})
            if prior is not None:return prior
            old=self.read(item_id)['item']
            if old['category']=='资料' and changes.get('body',old['body'])!=old['body'] and 'summary' not in changes and changes.get('summaryUnchanged') is not True:
                raise ValueError('修改资料正文须更新摘要或明确summaryUnchanged=true')
            data={k:old[k] for k in ('category','title','body','summary','tags','aliases','sourceRefs','needsReview')}
            data.update({k:v for k,v in changes.items() if k!='summaryUnchanged'})
            result=self._save_item(dict(data,id=item_id,expectedVersion=expected_version),request_id+':write')
            return self._receipt(request_id,digest,result)

    def mark_delete(self,item_id,expected_version,request_id,reason='',cancel=False):
        with self.store.tx():
            prior,digest=self._prior(request_id,{'operation':'cancel_delete' if cancel else 'mark_delete','id':item_id,'expectedVersion':expected_version,'reason':reason})
            if prior is not None:return prior
            old=self.read(item_id)['item'];data={k:old[k] for k in ('category','title','body','summary','tags','aliases','sourceRefs','needsReview')}
            result=self._save_item(dict(data,id=item_id,expectedVersion=expected_version,status='active' if cancel else 'delete_marked'),request_id+':write')
            return self._receipt(request_id,digest,dict(result,permanentDeletion=False))

    def _filtered(self,criteria,regular=False):
        if not isinstance(criteria,dict):raise ValueError('查询参数必须为对象')
        args=dict(criteria);nested=args.pop('filter',{})
        if not isinstance(nested,dict):raise ValueError('filter必须为对象')
        for key,value in nested.items():
            if key in args and args[key]!=value:raise ValueError('重复筛选条件冲突')
            args[key]=value
        allowed={'query','category','categories','tags','tagMode','status','needsReview','sourceRef','from','to','limit','offset','kind','includeDeleted'}
        if set(args)-allowed:raise ValueError('查询含未支持条件：'+','.join(sorted(set(args)-allowed)))
        if args.get('kind') not in (None,'memory'):raise ValueError('原话和日记使用独立接口')
        cats=args.get('categories')
        if cats is None and args.get('category') is not None:cats=[args['category']]
        if cats is not None:
            cats=strings(cats,'categories')
            if not cats or any(c not in CATEGORIES for c in cats):raise ValueError('分类无效')
        if regular and (not cats or any(c not in SEARCH_CATEGORIES for c in cats)):
            raise ValueError('常规查询必须指定分类，且不包含事实与经历')
        tags=strings(args.get('tags',[]),'tags')
        if args.get('tagMode','all') not in {'all','any'}:raise ValueError('tagMode必须为all/any')
        limit=integer(args.get('limit',10 if regular else 100),'limit',1,1000)
        offset=integer(args.get('offset',0),'offset',0,10000000)
        clauses=[];parameters=[]
        if cats:clauses.append('category IN ('+','.join('?' for _ in cats)+')');parameters.extend(cats)
        status=args.get('status','all' if args.get('includeDeleted') else 'active')
        if status not in {'active','delete_marked','all'}:raise ValueError('status无效')
        if regular and status!='active':raise ValueError('常规查询不返回删除标记内容')
        if status!='all':clauses.append('status=?');parameters.append(status)
        if 'needsReview' in args:
            if not isinstance(args['needsReview'],bool):raise ValueError('needsReview必须为布尔值')
            clauses.append('needs_review=?');parameters.append(int(args['needsReview']))
        for field,operator in [('from','>='),('to','<=')]:
            if args.get(field):clauses.append('updated'+operator+'?');parameters.append(parse_timestamp(args[field]))
        rows=[self._item(r) for r in self.store.all('SELECT * FROM v04_items'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY updated DESC,id',parameters)]
        if tags:rows=[r for r in rows if (all(t in r['tags'] for t in tags) if args.get('tagMode','all')=='all' else any(t in r['tags'] for t in tags))]
        if args.get('sourceRef'):rows=[r for r in rows if args['sourceRef'] in r['sourceRefs']]
        return args,rows,limit,offset

    def page(self,criteria=None):
        args,rows,limit,offset=self._filtered(criteria or {})
        if args.get('query'):
            query=text(args['query'],'query').casefold()
            rows=[r for r in rows if query in '\n'.join([r['title'],r['body'],r['summary'],*r['tags'],*r['aliases']]).casefold()]
        return {'items':rows[offset:offset+limit],'pagination':{'offset':offset,'limit':limit,'total':len(rows),'nextOffset':offset+limit if offset+limit<len(rows) else None}}

    def query(self,criteria):
        if isinstance(criteria,dict) and 'sql' in criteria:return self.sql(criteria['sql'],criteria.get('parameters'),limit=criteria.get('limit',100))['items']
        return self.page(criteria)['items']

    def catalog_page(self,criteria=None):
        """Model/UI directory projection; Core's internal page/query keep bodies."""
        page=self.page(criteria)
        items=[]
        for item in page['items']:
            brief={key:item[key] for key in ('id','category','title','summary','tags','aliases','version','status','needsReview','updated')}
            brief['fullTextTool']='get'
            if item['category']!='资料':
                brief['excerpt']=item['body'][:500]
                brief['excerptTruncated']=len(item['body'])>500
            items.append(brief)
        return dict(page,items=items)

    def _checked_vector(self,vector):
        if hasattr(vector,'tolist'):vector=vector.tolist()
        if not isinstance(vector,(list,tuple)) or not vector or any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in vector):
            raise ValueError('编码器向量无效')
        dimensions=self.store.get('dimension:'+self.encoder_id)
        if dimensions is not None and dimensions!=len(vector):raise ValueError('编码器维度改变，须更换encoder_id并重建')
        self.store.put('dimension:'+self.encoder_id,len(vector));return list(vector)

    def _encode_query(self,value):
        if self.encoder is None:raise RuntimeError('未配置本地编码器')
        vector=self.encoder.encode_query(value) if hasattr(self.encoder,'encode_query') else self.encoder([value])[0]
        return self._checked_vector(vector)

    def _encode_document(self,value):
        if hasattr(self.encoder,'encode_document'):
            chunks=self.encoder.encode_document(value)
        else:
            ranges=[(start,min(start+self.chunk_chars,len(value))) for start in range(0,len(value),self.chunk_chars)]
            vectors=self.encoder([value[a:b] for a,b in ranges])
            if hasattr(vectors,'tolist'):vectors=vectors.tolist()
            if len(vectors)!=len(ranges):raise ValueError('编码器返回数量不匹配')
            chunks=[(a,b,vector) for (a,b),vector in zip(ranges,vectors)]
        result=[]
        for start,end,vector in chunks:
            if isinstance(start,bool) or isinstance(end,bool) or not isinstance(start,int) or not isinstance(end,int) or not 0<=start<end<=len(value):
                raise ValueError('编码分块位置无效')
            result.append((start,end,self._checked_vector(vector)))
        if not result:raise ValueError('编码器未返回分块')
        return result

    def search_page(self,criteria):
        args,rows,limit,offset=self._filtered(criteria,True);query=text(args.get('query'),'query',maximum=20000)
        selected={r['id']:r for r in rows};terms=sorted(tokens(query));counts={}
        for start in range(0,len(terms),400):
            part=terms[start:start+400]
            for row in self.store.all('SELECT item_id,COUNT(*) n FROM v04_keywords WHERE token IN ('+','.join('?' for _ in part)+') GROUP BY item_id',part):
                if row['item_id'] in selected:counts[row['item_id']]=counts.get(row['item_id'],0)+row['n']
        keyword=sorted(counts,key=lambda key:(-(counts[key]+(2*len(terms) if query.casefold() in (selected[key]['title']+'\n'+selected[key]['body']).casefold() else 0)),key))
        semantic=[];semantic_error=None
        if self.encoder is not None and selected:
            try:
                q=self._encode_query(query);q_norm=math.sqrt(sum(v*v for v in q));scores={}
                for row in self.store.all('SELECT item_id,version,vector FROM v04_vectors WHERE encoder=?',(self.encoder_id,)):
                    item=selected.get(row['item_id'])
                    if not item or item['version']!=row['version']:continue
                    vec=json.loads(row['vector'])
                    if len(vec)!=len(q):raise ValueError('查询向量与索引维度不同')
                    denominator=q_norm*math.sqrt(sum(v*v for v in vec));score=sum(a*b for a,b in zip(q,vec))/denominator if denominator else 0
                    if score>=self.semantic_threshold:scores[item['id']]=max(scores.get(item['id'],-1),score)
                semantic=sorted(scores,key=lambda key:(-scores[key],key))
            except Exception as exc:semantic_error=type(exc).__name__+': '+str(exc)[:200]
        ranks={}
        for ranking in (keyword,semantic):
            for index,key in enumerate(ranking):ranks[key]=ranks.get(key,0)+1/(61+index)
        ordered=sorted(ranks,key=lambda key:(-ranks[key],key));items=[]
        for key in ordered[offset:offset+limit]:
            item=selected[key];value={k:item[k] for k in ('id','category','title','summary','tags','version','status','needsReview')}
            value['match']={'keyword':key in keyword,'semantic':key in semantic};value['fullTextTool']='get'
            if item['category']!='资料':value['excerpt']=item['body'][:500];value['excerptTruncated']=len(item['body'])>500
            items.append(value)
        current=self.store.all("SELECT i.id,i.version,j.state FROM v04_items i LEFT JOIN v04_vector_jobs j ON j.item_id=i.id AND j.version=i.version AND j.encoder=? WHERE i.status='active'",(self.encoder_id,))
        incomplete=sum(1 for r in current if r['id'] in selected and r['state']!='completed')
        return {'items':items,'semanticIndexComplete':self.encoder is not None and incomplete==0 and semantic_error is None,
                'keywordAvailable':True,'containsNeedsReview':any(i['needsReview'] for i in items),
                'indexStatus':{'encoderConfigured':self.encoder is not None,'encoder':self.encoder_id,'pendingOrFailed':incomplete,'semanticError':semantic_error},
                'pagination':{'offset':offset,'limit':limit,'total':len(ordered),'nextOffset':offset+limit if offset+limit<len(ordered) else None}}

    def search(self,query,categories=None,**filters):
        args=dict(query) if isinstance(query,dict) else dict(filters,query=query,categories=categories)
        return self.search_page(args)['items']

    def glossary(self,args=None):
        args=dict(args or {});query=args.pop('keywords',args.pop('query',None));args.pop('shallow',None)
        limit=integer(args.pop('limit',8),'limit',1,1000);offset=integer(args.pop('offset',0),'offset',0,10000000)
        args['category']='黑话与名词解释';_,items,_,_=self._filtered(args)
        if query is not None:
            queries=[text(query,'keywords')] if isinstance(query,str) else strings(query,'keywords')
            items=[i for i in items if any(q.casefold() in '\n'.join([i['title'],i['body'],*i['aliases']]).casefold() for q in queries)]
        return {'items':items[offset:offset+limit],'pagination':{'offset':offset,'limit':limit,'total':len(items),'nextOffset':offset+limit if offset+limit<len(items) else None}}

    def terms(self,args):return self.glossary(args)['items']

    def index_status(self):
        jobs={r['state']:r['n'] for r in self.store.all('SELECT state,COUNT(*) n FROM v04_vector_jobs GROUP BY state')}
        errors=self.store.all("SELECT item_id,version,attempts,error FROM v04_vector_jobs WHERE state='failed' ORDER BY id LIMIT 20")
        return {'encoderConfigured':self.encoder is not None,'encoder':self.encoder_id,'jobs':jobs,'failures':errors,
                'workerRunning':self._worker is not None and self._worker.is_alive(),'keyword':'ready','cleanup':'disabled-no-policy'}

    def retry_index(self,item_id=None):
        with self.store.tx():
            args=[self.clock(),self.encoder_id];statement="UPDATE v04_vector_jobs SET state='pending',error=NULL,updated=? WHERE encoder=? AND state='failed'"
            if item_id is not None:statement+=' AND item_id=?';args.append(item_id)
            count=self.store.execute(statement,args).rowcount
        self._wake.set();return {'reset':count,'indexStatus':self.index_status()}

    def rebuild_index(self,request_id):
        with self.store.tx():
            prior,digest=self._prior(request_id,{'operation':'rebuild_index','encoder':self.encoder_id})
            if prior is not None:return prior
            count=0
            for row in self.store.all("SELECT * FROM v04_items WHERE status='active'"):
                item=self._item(row);self.store.execute('DELETE FROM v04_keywords WHERE item_id=?',(item['id'],))
                for token in tokens('\n'.join([item['title'],item['body'],item['summary'],*item['tags'],*item['aliases']])):
                    self.store.execute('INSERT INTO v04_keywords VALUES(?,?,?)',(item['id'],item['version'],token))
                self.store.execute('DELETE FROM v04_vectors WHERE item_id=? AND encoder=?',(item['id'],self.encoder_id))
                self.store.execute("INSERT INTO v04_vector_jobs(item_id,version,encoder,state,updated) VALUES(?,?,?,'pending',?) ON CONFLICT(item_id,version,encoder) DO UPDATE SET state='pending',error=NULL,updated=excluded.updated",
                                   (item['id'],item['version'],self.encoder_id,self.clock()));count+=1
            result=self._receipt(request_id,digest,{'queued':count,'encoder':self.encoder_id})
        self._wake.set();return result

    def run_vector_jobs(self,limit=20):
        integer(limit,'limit',1,1000)
        if self.encoder is None:return {'processed':0,'state':'encoder_not_configured','indexStatus':self.index_status()}
        if not self._vector_lock.acquire(blocking=False):return {'processed':0,'state':'worker_busy'}
        processed=failed=obsolete=0
        try:
            jobs=self.store.all("SELECT * FROM v04_vector_jobs WHERE state='pending' AND encoder=? ORDER BY id LIMIT ?",(self.encoder_id,limit))
            for job in jobs:
                with self.store.tx():
                    item=self.store.one('SELECT * FROM v04_items WHERE id=?',(job['item_id'],))
                    if not item or item['version']!=job['version'] or item['status']!='active':
                        self.store.execute("UPDATE v04_vector_jobs SET state='obsolete',updated=? WHERE id=?",(self.clock(),job['id']));obsolete+=1;continue
                    claim=self.store.execute("UPDATE v04_vector_jobs SET state='running',attempts=attempts+1,updated=? WHERE id=? AND state='pending'",(self.clock(),job['id']))
                    if claim.rowcount!=1:continue
                    value=item['title']+'\n'+item['body']
                try:
                    chunks=self._encode_document(value)
                    with self.store.tx():
                        current=self.store.one('SELECT version,status FROM v04_items WHERE id=?',(job['item_id'],))
                        if not current or current['version']!=job['version'] or current['status']!='active':
                            self.store.execute("UPDATE v04_vector_jobs SET state='obsolete',updated=? WHERE id=?",(self.clock(),job['id']));obsolete+=1;continue
                        self.store.execute('DELETE FROM v04_vectors WHERE item_id=? AND encoder=?',(job['item_id'],self.encoder_id))
                        for index,(start,end,vector) in enumerate(chunks):
                            self.store.execute('INSERT INTO v04_vectors VALUES(?,?,?,?,?,?,?)',(job['item_id'],job['version'],self.encoder_id,index,start,end,canonical(vector)))
                        self.store.execute("UPDATE v04_vector_jobs SET state='completed',error=NULL,updated=? WHERE id=?",(self.clock(),job['id']));processed+=1
                except Exception as exc:
                    self.store.execute("UPDATE v04_vector_jobs SET state='failed',error=?,updated=? WHERE id=? AND state='running'",(type(exc).__name__+': '+str(exc)[:300],self.clock(),job['id']));failed+=1
            return {'processed':processed,'failed':failed,'obsolete':obsolete,'indexStatus':self.index_status()}
        finally:self._vector_lock.release()

    def start_vector_worker(self):
        if self.encoder is None:return False
        if self._worker and self._worker.is_alive():return True
        def loop():
            while not self._closed.is_set():
                try:self.run_vector_jobs()
                except Exception:pass
                self._wake.wait(2);self._wake.clear()
        self._worker=threading.Thread(target=loop,name='xixi-library-v04-vectors',daemon=True);self._worker.start();return True

    def close(self):
        if self.store.closed:return True
        self._closed.set();self._wake.set()
        if self._worker and self._worker is not threading.current_thread():self._worker.join(timeout=5)
        if self._worker and self._worker.is_alive():return False
        self.store.close();return True

    def _save_diary(self,day,item,request_id):
        if not isinstance(day,str) or dt.date.fromisoformat(day).isoformat()!=day:raise ValueError('日记day必须为ISO日期')
        text(item.get('body'),'日记正文');strings(item.get('sourceRefs',[]),'sourceRefs')
        prior,digest=self._prior(request_id,{'operation':'diary','day':day,'item':item})
        if prior is not None:return prior
        old=self.store.one('SELECT * FROM v04_diaries WHERE day=?',(day,));expected=item.get('expectedVersion')
        if old and (isinstance(expected,bool) or expected!=old['version']):raise ValueError('日记版本冲突，更新须带expectedVersion')
        if not old and expected not in (None,0):raise ValueError('日记版本冲突')
        value={'id':old['id'] if old else 'diary-'+day,'day':day,'title':item.get('title') or day+' 日记',
               'body':item['body'],'sourceRefs':item.get('sourceRefs',[]),'version':old['version']+1 if old else 1,'updated':self.clock(),'kind':'diary'}
        self.store.execute('INSERT INTO v04_diaries VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,body=excluded.body,source_refs=excluded.source_refs,version=excluded.version,updated=excluded.updated',
                           (value['id'],day,value['title'],value['body'],canonical(value['sourceRefs']),value['version'],value['updated']))
        self.store.execute('INSERT INTO v04_diary_versions VALUES(?,?,?)',(value['id'],value['version'],canonical(value)))
        return self._receipt(request_id,digest,value)

    def write_diary(self,day,item,request_id):
        if not isinstance(item,dict):raise ValueError('日记必须为对象')
        with self.store.tx():return self._save_diary(day,item,request_id)

    def diary(self,args=None):
        args=dict(args or {})
        if 'date' in args:
            if 'day' in args and args['day']!=args['date']:raise ValueError('日记日期冲突')
            args['day']=args.pop('date')
        if set(args)-{'id','day','from','to','offset','limit','version','includeVersions'}:raise ValueError('日记查询条件无效')
        include=args.get('includeVersions',False)
        if not isinstance(include,bool):raise ValueError('includeVersions必须为布尔值')
        version=integer(args['version'],'version',1,1000000000) if 'version' in args else None
        if (include or version is not None) and not (args.get('id') or args.get('day')):
            raise ValueError('日记历史需要明确id或day')
        rows=[];history=None;selected=None;current=None
        for row in self.store.all('SELECT * FROM v04_diaries ORDER BY day,id'):
            if args.get('id') and row['id']!=args['id'] or args.get('day') and row['day']!=args['day']:continue
            if args.get('from') and row['day']<args['from'] or args.get('to') and row['day']>args['to']:continue
            current=row['version'];selected=version or current
            if include:
                history=[]
                for previous in self.store.all('SELECT version,content FROM v04_diary_versions WHERE id=? ORDER BY version DESC LIMIT 1000',(row['id'],)):
                    value=json.loads(previous['content'])
                    history.append({'version':previous['version'],'title':value.get('title','')[:200],'updated':value.get('updated')})
            if version is not None:
                previous=self.store.one('SELECT content FROM v04_diary_versions WHERE id=? AND version=?',(row['id'],version))
                if previous is None:raise ValueError('日记历史版本不存在')
                row=json.loads(previous['content'])
            else:row['sourceRefs']=json.loads(row.pop('source_refs'))
            row['kind']='diary';rows.append(row)
        if version is not None and not rows:raise ValueError('日记历史版本不存在')
        limit=integer(args.get('limit',100),'limit',1,1000);offset=integer(args.get('offset',0),'offset',0,10000000)
        result={'items':rows[offset:offset+limit],'pagination':{'offset':offset,'limit':limit,'total':len(rows),'nextOffset':offset+limit if offset+limit<len(rows) else None}}
        if include or version is not None:
            result.update(versions=history or [],selectedVersion=selected,currentVersion=current)
        return result

    def sources(self,args):
        if isinstance(args,str):args={'id':args}
        if 'itemId' in args and 'id' not in args:args=dict(args,id=args['itemId'])
        refs=args.get('sourceRefs')
        if refs is None and args.get('sourceId'):refs=[args['sourceId']]
        if refs is None:
            try:refs=self.read(args['id'])['item']['sourceRefs']
            except ValueError:
                return {'items':[{'ref':args['id'],'type':'chat','data':self.archive.resolve_message(args['id'])}]}
        result=[]
        for ref in strings(refs,'sourceRefs'):
            try:result.append({'ref':ref,'type':'chat','data':self.archive.resolve_message(ref)});continue
            except ValueError:pass
            try:result.append({'ref':ref,'type':'item','data':self.read(ref)['item']})
            except ValueError:result.append({'ref':ref,'type':'unresolved','available':False})
        return {'items':result}

    def prepare_dream(self,batch):
        diary=self.diary({'day':batch['day']})['items']
        expected=diary[0]['version'] if diary else 0
        return {'diaryExpected':expected,'existingDiary':diary[0] if diary else None}

    @staticmethod
    def _dream_intent(batch_id,day,result):
        return {'operation':'dream','batch':batch_id,'day':day,'memories':result.get('memories',[]),'diary':result.get('diary')}

    def apply_dream(self,batch,result,*,require_memory_sources=True):
        if not isinstance(result,dict) or not isinstance(result.get('memories',[]),list) or not isinstance(result.get('diary'),dict):
            raise ValueError('做梦结果需要memories与diary')
        refs=set(batch['sourceIds']);snapshot={x['id']:x for x in batch.get('existingMemories',[])}
        with self.store.tx():
            prior,digest=self._prior('dream-v04:'+batch['id'],self._dream_intent(batch['id'],batch['day'],result))
            if prior is not None:return prior
            saved=[]
            for index,entry in enumerate(result.get('memories',[])):
                if not isinstance(entry,dict):raise ValueError('做梦条目无效')
                item=dict(entry)
                item_refs=set(strings(item.get('sourceRefs',[]),'sourceRefs'))
                if (require_memory_sources and not item_refs) or not item_refs<=refs|set(snapshot.get(item.get('id'),{}).get('sourceRefs',[])):
                    raise ValueError('做梦条目来源超出固定范围')
                if item.get('id') and (item['id'] not in snapshot or item.get('expectedVersion')!=snapshot[item['id']]['version']):
                    raise ValueError('做梦修订未引用固定版本')
                saved.append(self._save_item(item,'dream:'+batch['id']+':'+str(index)))
            diary=dict(result['diary'])
            if not set(diary.get('sourceRefs',[]))<=refs:raise ValueError('日记来源超出固定范围')
            if 'diaryExpected' in batch:diary['expectedVersion']=batch['diaryExpected']
            elif isinstance(batch.get('existingDiary'),dict):diary['expectedVersion']=batch['existingDiary']['version']
            saved.append(self._save_diary(batch['day'],diary,'dream:'+batch['id']+':diary'))
            return self._receipt('dream-v04:'+batch['id'],digest,{'items':saved})

    def dream_publication(self,batch_id,day,result):
        row=self.store.one('SELECT fingerprint,result FROM v04_operations WHERE request_id=?',('dream-v04:'+batch_id,))
        expected=len(result.get('memories',[]))+1
        if not row:return {'state':'none','expected':expected,'existing':0}
        if row['fingerprint']!=fingerprint(self._dream_intent(batch_id,day,result)):
            return {'state':'partial','expected':expected,'reason':'发布回执与固定结果内容不一致'}
        return {'state':'complete','expected':expected,'existing':len(json.loads(row['result'])['items'])}

    def schema(self):
        return {'tables':self.store.all("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"),
                'categories':list(CATEGORIES),'notes':['原话在xixi_raw_messages，日记在v04_diaries；不参加常规搜索。','SQL只读；写入统一使用工具维护版本与索引。']}

    def sql(self,query,parameters=None,*,limit=100,timeout=2,max_chars=200000):
        text(query,'SQL',maximum=50000);integer(limit,'limit',1,1000)
        if not isinstance(parameters or [],(list,tuple,dict)):raise ValueError('SQL参数无效')
        timeout=float(timeout)
        if not math.isfinite(timeout) or not 0.01<=timeout<=30:raise ValueError('SQL超时范围无效')
        deadline=time.monotonic()+timeout
        conn=sqlite3.connect(Path(self.store.path).as_uri()+'?mode=ro',uri=True);conn.row_factory=sqlite3.Row
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,max_chars*4)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH,50000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COLUMN,256)
        allowed={sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE}
        def authorize(action,a,b,database,trigger):
            if action not in allowed:return sqlite3.SQLITE_DENY
            if action==sqlite3.SQLITE_FUNCTION and (b or a or '').casefold() in {'load_extension','writefile','readfile','edit'}:return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize);conn.set_progress_handler(lambda:1 if time.monotonic()>deadline else 0,1000)
        try:
            cursor=conn.execute(query,parameters or [])
            if cursor.description is None:raise ValueError('只允许返回结果的只读查询')
            items=[];size=0;truncated=False
            for row in cursor:
                item=dict(row)
                if any(isinstance(v,bytes) for v in item.values()):raise ValueError('SQL不返回二进制字段')
                chars=len(canonical(item))
                if len(items)>=limit or size+chars>max_chars:truncated=True;break
                items.append(item);size+=chars
            return {'items':items,'truncated':truncated,'limit':limit,'readOnly':True,'reason':'结果超出上限，请缩小范围' if truncated else None}
        except sqlite3.Error as exc:raise ValueError('只读SQL被拒绝或执行失败：'+str(exc)) from exc
        finally:conn.close()

    def backup(self,destination):
        path=Path(destination).resolve()
        if path==Path(self.store.path) or path.exists():raise ValueError('备份目标必须是不同的新文件')
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700);target=sqlite3.connect(path)
        try:
            with self.store.lock:self.store.db.backup(target)
        finally:target.close()
        if os.name=='posix':os.chmod(path,0o600)
        check=sqlite3.connect(path)
        try:integrity=check.execute('PRAGMA quick_check').fetchone()[0]
        finally:check.close()
        if integrity!='ok':raise RuntimeError('备份校验失败')
        return {'path':str(path),'consistent':True,'integrity':integrity,'attachments':'外部附件文件须另行按清单备份'}

    def dispatch(self,name,args=None,request_id=None):
        name=name.removeprefix('library.');args=args or {}
        if not isinstance(args,dict):raise ValueError('工具参数必须为对象')
        writes={'record','revise','mark_delete','cancel_delete','write_diary','retry_index','rebuild_index'}
        if name in writes and not request_id:raise ValueError('写入工具需要稳定request_id')
        if name=='record':return self.write(args,request_id)
        if name=='revise':return self.revise(args['id'],args['expectedVersion'],args['changes'],request_id)
        if name=='search':return self.search_page(args)
        if name=='list':return self.catalog_page(args)
        if name=='get':return self.read(args['id'],args.get('version'))
        if name=='glossary':return self.glossary(args)
        if name=='diary':return self.diary(args)
        if name=='write_diary':return self.write_diary(args['day'],args,request_id)
        if name=='sources':return self.sources(args)
        if name=='chat_history':return self.archive.chat_history(args)
        if name=='chat_search':return self.archive.chat_search(args)
        if name=='schema':return self.schema()
        if name=='sql':return self.sql(args['sql'],args.get('parameters'),limit=args.get('limit',100),timeout=args.get('timeout',2))
        if name in {'mark_delete','cancel_delete'}:return self.mark_delete(args['id'],args['expectedVersion'],request_id,args.get('reason',''),cancel=name=='cancel_delete')
        if name=='index_status':return self.index_status()
        if name=='retry_index':return self.retry_index(args.get('id'))
        if name=='rebuild_index':return self.rebuild_index(request_id)
        raise ValueError('未知图书馆工具：'+name)


class V04Archive(RawLibrary):
    mode='library-v0.4-sqlite'

    def __init__(self,store,clock=time.time):
        self.store=store;self.clock=clock;self.owns_store=False

    def health(self):
        self.store.one('SELECT 1')
        return {'ok':True,'mode':self.mode,'productionIntegration':True,'authoritativeRawArchive':True}

    def append_message(self,payload,request_id):
        self._validate_message(payload);text(request_id,'request_id',maximum=512)
        frozen=canonical(payload)
        with self.store.lock:
            prior=self.store.one('SELECT * FROM xixi_raw_messages WHERE object_id=? AND direction=? AND conversation=?',
                                 (payload['id'],payload['direction'],payload['conversationId']))
            if prior:
                if prior['payload']!=frozen:raise ValueError('原话消息编号内容冲突')
                # Also check a reused request ID even when identity matched another row.
                by_request=self.store.one('SELECT record_id FROM xixi_raw_messages WHERE request_id=?',(request_id,))
                if by_request and by_request['record_id']!=prior['record_id']:raise ValueError('原话请求编号冲突')
                return {'recordId':prior['record_id'],'fingerprint':prior['fingerprint'],'existing':True}
            return super().append_message(payload,request_id)

    def resolve_message(self,message_id):
        row=self.store.one('SELECT record_id FROM xixi_raw_messages WHERE record_id=? OR object_id=? ORDER BY rowid LIMIT 1',(message_id,message_id))
        if row:return self.read_message(row['record_id'])
        for row in self.store.all('SELECT record_id,payload FROM xixi_raw_messages ORDER BY rowid'):
            if json.loads(row['payload']).get('channelMessageId')==message_id:return self.read_message(row['record_id'])
        raise ValueError('原话不存在')

    def query_messages(self,criteria=None):
        args=criteria or {}
        allowed={'conversationId','direction','source','query','messageId','from','to','limit','offset','throughSeq'}
        if not isinstance(args,dict) or set(args)-allowed:raise ValueError('原话查询条件无效')
        limit=integer(args.get('limit',100),'limit',1,1000);offset=integer(args.get('offset',0),'offset',0,10000000)
        start=parse_timestamp(args['from']) if args.get('from') else None;end=parse_timestamp(args['to']) if args.get('to') else None
        matched=[]
        for row in self.store.all('SELECT rowid seq,record_id,payload FROM xixi_raw_messages ORDER BY rowid'):
            if args.get('throughSeq') is not None and row['seq']>args['throughSeq']:continue
            item=json.loads(row['payload'])
            if any(args.get(k) and item.get(k)!=args[k] for k in ('conversationId','direction','source')):continue
            if args.get('messageId') and args['messageId'] not in {item['id'],row['record_id'],item.get('channelMessageId')}:continue
            if args.get('query') and str(args['query']).casefold() not in item['text'].casefold():continue
            sent=parse_timestamp(item['sentAt'])
            if start is not None and sent<start or end is not None and sent>end:continue
            matched.append((sent,row['seq'],row['record_id']))
        matched.sort();selected=matched[offset:offset+limit]
        return {'items':[self.read_message(x[2]) for x in selected],'pagination':{'offset':offset,'limit':limit,'total':len(matched),'nextOffset':offset+len(selected) if offset+len(selected)<len(matched) else None}}

    def _render(self,item):
        state=''
        if item['direction']=='outbound':
            status=item['receipts'][-1].get('status','unknown') if item.get('receipts') else 'unknown'
            state='；'+{'sending':'发送中','accepted':'通道已接受，未确认送达','delivered':'已送达','failed':'发送失败','unknown':'发送状态未知'}.get(status,status)
        lines=[item['sentAt']+' '+item['sender']+'〔'+item['id']+state+'〕',item['text']]
        reply=item.get('replyTo')
        if reply:
            quote=None;identity=reply if isinstance(reply,str) else next((reply.get(k) for k in ('id','messageId','recordId','channelMessageId') if reply.get(k)),None) if isinstance(reply,dict) else None
            if identity:
                try:quote=self.resolve_message(identity)
                except ValueError:pass
            if quote is None and isinstance(reply,dict) and isinstance(reply.get('text'),str):quote=reply
            if quote:lines+=['引用 '+str(quote.get('sentAt','时间未提供'))+' '+str(quote.get('sender',''))+'：',str(quote.get('text',''))]
            else:lines.append('引用原文暂不可用。')
        for attachment in item.get('attachments',[]):
            if isinstance(attachment,dict):
                lines.append('附件〔'+str(attachment.get('type','文件'))+'〕'+str(attachment.get('name') or attachment.get('id') or '')+('：'+str(attachment.get('description') or attachment.get('transcript')) if attachment.get('description') or attachment.get('transcript') else ''))
            else:lines.append('附件：'+str(attachment))
        return '\n'.join(lines)

    def chat_history(self,args=None):
        args=dict(args or {})
        allowed={'date','from','to','recent','around','before','after','conversationId','limit','offset','charOffset','maxChars','timezone','cursor','throughSeq'}
        if set(args)-allowed:raise ValueError('聊天读取条件无效')
        if args.get('cursor'):
            try:
                cursor=json.loads(base64.urlsafe_b64decode(args['cursor'].encode()).decode())
                if not isinstance(cursor,dict) or set(cursor)-allowed:raise ValueError()
                args={**cursor,**{k:v for k,v in args.items() if k in {'maxChars','limit'}}}
            except Exception as exc:raise ValueError('继续读取位置无效') from exc
        timezone=args.get('timezone','Etc/UTC');tz=ZoneInfo(timezone)
        args['timezone']=timezone  # Freeze the effective calendar in pagination cursors.
        if args.get('date'):
            day=dt.date.fromisoformat(args['date']);args['from']=dt.datetime.combine(day,dt.time(),tz).isoformat()
            args['to']=(dt.datetime.combine(day+dt.timedelta(days=1),dt.time(),tz)-dt.timedelta(microseconds=1)).isoformat()
        maximum=integer(args.get('maxChars',12000),'maxChars',256,100000);limit=integer(args.get('limit',100),'limit',1,1000)
        offset=integer(args.get('offset',0),'offset',0,10000000);char_offset=integer(args.get('charOffset',0),'charOffset',0,10000000)
        through=args.get('throughSeq',self.store.one('SELECT COALESCE(MAX(rowid),0) n FROM xixi_raw_messages')['n'])
        integer(through,'throughSeq',0,1000000000)
        query={k:args[k] for k in ('conversationId','from','to') if args.get(k)};query.update(limit=1000,throughSeq=through)
        items=[];position=0
        while True:
            page=self.query_messages(dict(query,offset=position));items.extend(page['items'])
            if page['pagination']['nextOffset'] is None:break
            position=page['pagination']['nextOffset']
        if args.get('around'):
            anchor=self.resolve_message(args['around']);index=next((i for i,item in enumerate(items) if item['recordId']==anchor['recordId']),None)
            if index is None:raise ValueError('中心消息不在所选范围')
            before=integer(args.get('before',10),'before',0,1000);after=integer(args.get('after',10),'after',0,1000)
            items=items[max(0,index-before):index+after+1]
        if args.get('recent') is not None:items=items[-integer(args['recent'],'recent',1,1000):]
        output='私聊记录〔时区 '+timezone+'；按发送时间排列〕\n\n';returned=[];next_offset=None;next_char=0
        for index in range(offset,min(len(items),offset+limit)):
            item=items[index];rendered=self._render(item)+'\n\n';start=char_offset if index==offset else 0
            if start>=len(rendered):continue
            part=rendered[start:start+max(0,maximum-len(output))]
            if not part:next_offset=index;next_char=start;break
            output+=part;returned.append({'id':item['id'],'recordId':item['recordId'],'sentAt':item['sentAt'],'charFrom':start,'charTo':start+len(part),'complete':start+len(part)>=len(rendered)})
            if start+len(part)<len(rendered):next_offset=index;next_char=start+len(part);break
        else:
            if offset+limit<len(items):next_offset=offset+limit
        next_cursor=None
        if next_offset is not None:
            continuation={k:v for k,v in args.items() if k not in {'cursor','maxChars','charOffset','offset'}}
            continuation.update(offset=next_offset,charOffset=next_char,throughSeq=through)
            next_cursor=base64.urlsafe_b64encode(canonical(continuation).encode()).decode()
        return {'text':output,'items':returned,'range':{'from':args.get('from'),'to':args.get('to'),'timezone':timezone},
                'pagination':{'offset':offset,'charOffset':char_offset,'totalMessages':len(items),'nextOffset':next_offset,'nextCharOffset':next_char,'nextCursor':next_cursor},'originalTextUnchanged':True}

    def chat_search(self,args):
        if not isinstance(args,dict):raise ValueError('原话搜索参数无效')
        text(args.get('query'),'query')
        allowed={'query','conversationId','from','to','limit','offset'}
        if set(args)-allowed:raise ValueError('原话搜索条件无效')
        page=self.query_messages(args)
        return {'items':[{'id':i['id'],'recordId':i['recordId'],'sentAt':i['sentAt'],'sender':i['sender'],'excerpt':i['text'][:500],
                          'excerptTruncated':len(i['text'])>500,'expand':{'tool':'chat_history','around':i['recordId']}} for i in page['items']],
                'pagination':page['pagination']}
