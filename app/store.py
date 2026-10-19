import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, directory):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(Path(directory) / 'state.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS directory_clears (
          id TEXT PRIMARY KEY,parent_id TEXT NOT NULL,parent_name TEXT NOT NULL,
          items TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS directory_clear_parts (
          clear_id TEXT NOT NULL,part INTEGER NOT NULL,ids TEXT NOT NULL,state TEXT NOT NULL,
          task TEXT NOT NULL DEFAULT '',next_at REAL NOT NULL DEFAULT 0,PRIMARY KEY(clear_id,part));
        CREATE TABLE IF NOT EXISTS directory_clear_claims (file_id TEXT PRIMARY KEY,clear_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY, title TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
          target_id TEXT, target_name TEXT, enabled_at REAL NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, share_id TEXT UNIQUE NOT NULL,
          url TEXT NOT NULL, code TEXT NOT NULL DEFAULT '', source_id INTEGER,
          message_id INTEGER, target_id TEXT NOT NULL, target_name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0, remote_task TEXT, error TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL);
        ''')
        if 'resource_name' not in {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN resource_name TEXT NOT NULL DEFAULT ''")
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        for name, definition in {
            'completed_at': 'REAL', 'cleanup_enabled': 'INTEGER NOT NULL DEFAULT 0',
            'cleanup_folder': "TEXT NOT NULL DEFAULT ''", 'cleanup_name': "TEXT NOT NULL DEFAULT ''",
            'cleanup_manifest': "TEXT NOT NULL DEFAULT ''", 'cleanup_state': "TEXT NOT NULL DEFAULT ''",
            'cleanup_task': "TEXT NOT NULL DEFAULT ''", 'cleanup_error': "TEXT NOT NULL DEFAULT ''",
            'cleanup_next': 'REAL NOT NULL DEFAULT 0',
            'cleanup_plan': "TEXT NOT NULL DEFAULT ''", 'cleanup_targets': "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        self.db.execute("UPDATE jobs SET cleanup_state='uncertain', cleanup_error='删除提交期间服务中断，请手动核对；不会再次删除' WHERE cleanup_state='deleting'")
        self.db.execute("UPDATE jobs SET cleanup_state='blocked', cleanup_error='未记录完整目录快照，不会自动删除' WHERE status='success' AND cleanup_enabled=1 AND cleanup_state=''")
        self.db.execute('CREATE TABLE IF NOT EXISTS cleanup_claims (file_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL)')
        for job in self.rows("SELECT id,cleanup_folder,cleanup_targets FROM jobs WHERE cleanup_enabled=1"):
            ids = [job['cleanup_folder']] if job['cleanup_folder'] else [x['id'] for x in json.loads(job['cleanup_targets'] or '[]')]
            for fid in ids:
                self.db.execute('INSERT OR IGNORE INTO cleanup_claims VALUES (?,?)', (fid, job['id']))
        # A request in flight may already have changed the cloud: never blindly resubmit.
        self.db.execute("UPDATE jobs SET status='uncertain', error='上次提交期间服务中断，请先核对云盘，再决定是否重试' WHERE status='submitting'")
        self.db.execute("UPDATE jobs SET status='queued' WHERE status='preparing'")
        self.db.execute("UPDATE directory_clears SET state='uncertain',error='删除提交中断，请核对回收站；不会重发' WHERE id IN (SELECT clear_id FROM directory_clear_parts WHERE state='deleting')")
        self.db.execute("UPDATE directory_clear_parts SET state='uncertain' WHERE state='deleting'")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def claim_cleanup(self, job_id, ids):
        with self.db:
            for fid in ids:
                old = self.db.execute('SELECT job_id FROM cleanup_claims WHERE file_id=?', (fid,)).fetchone()
                if old and old[0] != job_id:
                    raise ValueError('目标已被其他任务登记清理')
                self.db.execute('INSERT OR IGNORE INTO cleanup_claims VALUES (?,?)', (fid, job_id))

    def set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))
        self.db.commit()

    def rows(self, sql, params=()):
        return [dict(x) for x in self.db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        result = self.db.execute(sql, params)
        self.db.commit()
        return result

    def update_job(self, job_id, **fields):
        allowed = {'status', 'attempts', 'next_at', 'remote_task', 'error', 'code', 'resource_name',
                   'completed_at', 'cleanup_enabled', 'cleanup_folder', 'cleanup_name', 'cleanup_manifest',
                   'cleanup_state', 'cleanup_task', 'cleanup_error', 'cleanup_next', 'cleanup_plan', 'cleanup_targets'}
        if not fields.keys() <= allowed:
            raise ValueError('Invalid job fields')
        fields['updated'] = time.time()
        self.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), job_id))

    def enqueue(self, link, source, message_id, resource_name=''):
        default = self.get('target', {'id': '', 'name': '/'})
        target_id = source['target_id'] if source['target_id'] is not None else default['id']
        target_name = source['target_name'] if source['target_id'] is not None else default['name']
        now = time.time()
        self.execute('''INSERT OR IGNORE INTO jobs
          (share_id,url,code,source_id,message_id,target_id,target_name,created,updated,resource_name,cleanup_enabled)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
          (link['share_id'], link['url'], link['code'], source['id'], message_id, target_id, target_name, now, now, resource_name,
           bool(self.get('auto_cleanup', False))))

    def save_sources(self, sources):
        ids = [source['id'] for source in sources]
        if len(ids) != len(set(ids)):
            raise ValueError('监听来源重复')
        known = {row['id'] for row in self.rows('SELECT id FROM sources')}
        if not set(ids) <= known:
            raise ValueError('聊天列表已变化，请重新获取')
        now = int(time.time())
        with self.db:
            for source in sources:
                target = source.get('target')
                self.db.execute('''UPDATE sources SET
                  enabled_at=CASE WHEN enabled=0 AND ?=1 THEN ? ELSE enabled_at END,
                  enabled=?,target_id=?,target_name=? WHERE id=?''',
                  (source['enabled'], now, source['enabled'], target['id'] if target else None,
                   target['name'] if target else None, source['id']))
