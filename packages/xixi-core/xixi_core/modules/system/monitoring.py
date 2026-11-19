"""Minute-level Linux telemetry history and aggregation."""
from __future__ import annotations

from contextlib import contextmanager
import math
from pathlib import Path
import sqlite3
import threading
import time

from .files import no_links

DAY = 86400


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


class History:
    def __init__(self, state_dir, now=time.time):
        self.path, self.now = Path(state_dir)/'system-monitor.sqlite3', now
        no_links(self.path,must_exist=False)
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS samples (at INTEGER PRIMARY KEY, cpu REAL, memory REAL, temperature REAL);
                CREATE TABLE IF NOT EXISTS disks (at INTEGER, id TEXT, mount TEXT, percent REAL, PRIMARY KEY(at,id));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        no_links(self.path,must_exist=False)
        connection=sqlite3.connect(self.path,timeout=3)
        connection.row_factory=sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, data, at):
        minute=int(at//60)*60
        cpu=data.get('cpu',{}).get('percent')
        memory=data.get('memory',{}).get('percent')
        temperatures=[s['celsius'] for s in data.get('temperatures',[]) if s.get('status')=='ok' and finite(s.get('celsius')) and s.get('component') in {'coretemp','k10temp','zenpower','cpu_thermal','x86_pkg_temp'}]
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?)',(minute,cpu if finite(cpu) else None,memory if finite(memory) else None,max(temperatures,default=None)))
            db.execute('DELETE FROM disks WHERE at=?',(minute,))
            db.executemany('INSERT INTO disks VALUES (?,?,?,?)',[(minute,d['id'],d['mount'],d['percent']) for d in data.get('disks',[]) if d.get('status')=='ok' and finite(d.get('percent'))])
            cutoff=minute-30*DAY
            db.execute('DELETE FROM samples WHERE at<?',(cutoff,))
            db.execute('DELETE FROM disks WHERE at<?',(cutoff,))

    def query(self, interval=60):
        if type(interval) is not int or interval not in (60,600,1800):
            raise ValueError('invalid_interval')
        now=self.now()
        start=int((now-min(30*DAY,1440*interval))//interval)*interval
        end=int(now//interval)*interval
        with self.db() as db:
            rows=db.execute('SELECT CAST(at / ? AS INTEGER)*? AS bucket, AVG(cpu) AS cpu, AVG(memory) AS memory, MAX(temperature) AS temperature, COUNT(*) AS count FROM samples WHERE at>=? AND at<=? GROUP BY bucket ORDER BY bucket',(interval,interval,start,now)).fetchall()
            disks=db.execute('SELECT CAST(at / ? AS INTEGER)*? AS bucket,id,mount,AVG(percent) AS percent,MAX(percent) AS peak FROM disks WHERE at>=? AND at<=? GROUP BY bucket,id,mount ORDER BY bucket,id',(interval,interval,start,now)).fetchall()
        indexed={row['bucket']:dict(row) for row in rows}
        points=[{'at':at,**{k:indexed.get(at,{}).get(k) for k in ('cpu','memory','temperature','count')}} for at in range(start,end+1,interval)]
        return {'interval_seconds':interval,'retention_days':30,'sample_seconds':60,'start':start,'end':end,'points':points,'disks':[dict(d) for d in disks]}


class ResourceMonitor:
    """The existing minute worker, limited to history and ordinary shutdown."""

    def __init__(self, sampler, state_dir, *, now=time.time, logger=None):
        self.sampler, self.state_dir, self.now = sampler, Path(state_dir), now
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.history = History(self.state_dir, now)
        self.logger = logger
        self.stop_event = threading.Event()
        self.thread = None
        self.last_error = None
        with self.history.db() as db:
            self.last_sample = db.execute("SELECT MAX(at) FROM samples").fetchone()[0]

    def status(self):
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "sample_seconds": 60,
            "retention_days": 30,
            "last_sample": self.last_sample,
            "last_error": self.last_error,
        }

    def sample_once(self):
        at = self.now()
        # The shared sampler already waits briefly for an in-flight snapshot.
        # A longer failure ends this minute's attempt; _run records it and waits.
        data = self.sampler.collect()
        # Windows or an unavailable Linux sample must not create fabricated data.
        if data.get("status") != "ok":
            self.last_error = data.get("reason", "metrics_unavailable")
            return data
        self.history.record(data, at)
        self.last_sample = at
        self.last_error = None
        return data

    def _run(self):
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.sample_once()
            except Exception as exc:
                self.last_error = "sample_failed"
                if self.logger is not None:
                    self.logger.error("分钟资源采样失败：%s", type(exc).__name__)
            self.stop_event.wait(max(1, 60 - (time.monotonic() - started)))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if getattr(self.sampler, "enabled", True) is not True:
            self.last_error = "linux_required"
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="resource-history", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=8)
            if self.thread.is_alive():
                raise RuntimeError("资源历史采样线程未退出，请重启 Core")
