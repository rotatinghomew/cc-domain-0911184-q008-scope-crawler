"""SQLite 持久化：任务、页面、处置事件与站点节奏状态。

所有跨进程需要保留的限制都落库：
- 任务队列与状态（恢复许可后只重新推进仍有效任务，不靠重启清空）；
- 页面正文与抓取时的许可快照、保留截止时间；
- 每个页面的完整处置事件链（运维可解释停在哪条约定）；
- 站点级上次请求时间与约定间隔（重启后节奏不重置）。
"""

import json
import sqlite3
import uuid

from .clock import from_iso, now, to_iso

_UNSET = object()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks(
    task_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    not_after TEXT,
    reason TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages(
    url TEXT PRIMARY KEY,
    digest TEXT,
    body BLOB,
    status INTEGER,
    content_type TEXT,
    permit_snapshot TEXT,
    retain_until TEXT,
    redacted INTEGER NOT NULL DEFAULT 0,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
    event_id TEXT PRIMARY KEY,
    task_id TEXT,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS site_state(
    origin TEXT PRIMARY KEY,
    last_request_at TEXT,
    crawl_delay REAL
);
'''


class Store:
    def __init__(self, path, clock=None):
        self.path = path
        self.clock = clock
        # check_same_thread=False 便于调度线程与调用方共用；写操作均在事务内串行
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---- 任务 ----------------------------------------------------------
    def create_task(self, url, not_after=None):
        task_id = uuid.uuid4().hex
        ts = to_iso(now(self.clock))
        with self.db:
            self.db.execute(
                'INSERT INTO tasks(task_id,url,state,attempts,due_at,not_after,'
                'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (task_id, url, 'queued', 0, ts, to_iso(not_after), ts, ts))
        return task_id

    def get_task(self, task_id):
        row = self.db.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self, state=None):
        if state is None:
            rows = self.db.execute('SELECT * FROM tasks ORDER BY created_at').fetchall()
        else:
            rows = self.db.execute(
                'SELECT * FROM tasks WHERE state=? ORDER BY created_at', (state,)).fetchall()
        return [self._task_row(row) for row in rows]

    def workable_tasks(self, at):
        """排队、被软闸门挡住、或崩溃前停在在途状态的到期任务。

        in_flight 被纳入是为了进程崩溃恢复：正常调度中 in_flight 任务不会
        出现在这里（同一轮 tick 内会同步处置完），只有上次进程在请求期间
        退出才会残留该状态，下一轮按未完成任务重新过闸推进。
        """
        iso = to_iso(at)
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE state IN ('queued','blocked','in_flight') "
            "AND (due_at IS NULL OR due_at <= ?) ORDER BY created_at", (iso,)).fetchall()
        return [self._task_row(row) for row in rows]

    def update_task(self, task_id, state=_UNSET, attempts=_UNSET, due_at=_UNSET,
                    reason=_UNSET, detail=_UNSET):
        current = self.get_task(task_id)
        if current is None:
            raise KeyError(task_id)
        fields = {
            'state': current['state'] if state is _UNSET else state,
            'attempts': current['attempts'] if attempts is _UNSET else attempts,
            'due_at': current['due_at'] if due_at is _UNSET else to_iso(due_at),
            'reason': current['reason'] if reason is _UNSET else reason,
            'detail': current['detail'] if detail is _UNSET else detail,
            'updated_at': now(self.clock),
        }
        with self.db:
            self.db.execute(
                'UPDATE tasks SET state=:state,attempts=:attempts,due_at=:due_at,'
                'reason=:reason,detail=:detail,updated_at=:updated_at WHERE task_id=:id',
                {'state': fields['state'], 'attempts': fields['attempts'],
                 'due_at': to_iso(fields['due_at']),
                 'reason': fields['reason'],
                 'detail': json.dumps(fields['detail'], ensure_ascii=False)
                 if fields['detail'] is not None else None,
                 'updated_at': to_iso(fields['updated_at']), 'id': task_id})
        return self.get_task(task_id)

    def _task_row(self, row):
        data = dict(row)
        data['due_at'] = from_iso(data['due_at'])
        data['not_after'] = from_iso(data['not_after'])
        data['created_at'] = from_iso(data['created_at'])
        data['updated_at'] = from_iso(data['updated_at'])
        data['detail'] = json.loads(data['detail']) if data['detail'] else None
        return data

    # ---- 事件 ----------------------------------------------------------
    def add_event(self, url, kind, reason=None, detail=None, task_id=None):
        event_id = uuid.uuid4().hex
        with self.db:
            self.db.execute(
                'INSERT INTO events(event_id,task_id,url,kind,reason,detail,created_at)'
                ' VALUES(?,?,?,?,?,?,?)',
                (event_id, task_id, url, kind, reason,
                 json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                 to_iso(now(self.clock))))
        return event_id

    def events_for(self, url):
        rows = self.db.execute(
            'SELECT * FROM events WHERE url=? ORDER BY created_at', (url,)).fetchall()
        return [self._event_row(row) for row in rows]

    def _event_row(self, row):
        data = dict(row)
        data['created_at'] = from_iso(data['created_at'])
        data['detail'] = json.loads(data['detail']) if data['detail'] else None
        return data

    # ---- 页面 ----------------------------------------------------------
    def save_page(self, url, body, status, content_type, permit_snapshot,
                  retain_until, fetched_at):
        import hashlib
        digest = hashlib.sha256(body).hexdigest()
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO pages(url,digest,body,status,content_type,'
                'permit_snapshot,retain_until,redacted,fetched_at)'
                ' VALUES(?,?,?,?,?,?,?,0,?)',
                (url, digest, body, status, content_type,
                 json.dumps(permit_snapshot, ensure_ascii=False),
                 to_iso(retain_until), to_iso(fetched_at)))
        return digest

    def get_page(self, url):
        row = self.db.execute('SELECT * FROM pages WHERE url=?', (url,)).fetchone()
        return self._page_row(row) if row else None

    def list_pages(self, include_redacted=True):
        sql = 'SELECT * FROM pages'
        if not include_redacted:
            sql += ' WHERE redacted=0'
        sql += ' ORDER BY url'
        return [self._page_row(row) for row in self.db.execute(sql).fetchall()]

    def retention_due(self, at):
        iso = to_iso(at)
        rows = self.db.execute(
            'SELECT * FROM pages WHERE redacted=0 AND retain_until IS NOT NULL'
            ' AND retain_until <= ?', (iso,)).fetchall()
        return [self._page_row(row) for row in rows]

    def redact_page(self, url):
        """正文到期脱敏：保留 digest 与抓取事实，仅清空正文。"""
        with self.db:
            self.db.execute(
                'UPDATE pages SET body=NULL,redacted=1 WHERE url=?', (url,))
        return self.get_page(url)

    def _page_row(self, row):
        data = dict(row)
        data['permit_snapshot'] = json.loads(data['permit_snapshot']) \
            if data['permit_snapshot'] else None
        data['retain_until'] = from_iso(data['retain_until'])
        data['fetched_at'] = from_iso(data['fetched_at'])
        return data

    # ---- 站点节奏 ------------------------------------------------------
    def site_state(self, origin):
        row = self.db.execute(
            'SELECT * FROM site_state WHERE origin=?', (origin,)).fetchone()
        if row is None:
            return None
        return {'origin': origin,
                'last_request_at': from_iso(row['last_request_at']),
                'crawl_delay': row['crawl_delay']}

    def upsert_site_state(self, origin, last_request_at=None, crawl_delay=None):
        current = self.site_state(origin) or {'last_request_at': None, 'crawl_delay': None}
        if last_request_at is not None:
            current['last_request_at'] = last_request_at
        if crawl_delay is not None:
            current['crawl_delay'] = crawl_delay
        with self.db:
            self.db.execute(
                'INSERT OR REPLACE INTO site_state(origin,last_request_at,crawl_delay)'
                ' VALUES(?,?,?)',
                (origin, to_iso(current['last_request_at']), current['crawl_delay']))
        return self.site_state(origin)
