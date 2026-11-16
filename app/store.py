import json
import secrets
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
        CREATE TABLE IF NOT EXISTS media_links (
          id INTEGER PRIMARY KEY AUTOINCREMENT,owner TEXT NOT NULL,path TEXT NOT NULL,
          file_id TEXT NOT NULL,url TEXT NOT NULL,created REAL NOT NULL,expires_at REAL NOT NULL,
          retained_until REAL NOT NULL,hits INTEGER NOT NULL DEFAULT 0,last_used REAL NOT NULL,
          UNIQUE(owner,path));
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
            'cleanup_completed_at': 'REAL',
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
        if self.get('cloud_token') and not self.get('cloud_cache_owner'):
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                            ('cloud_cache_owner', json.dumps(secrets.token_hex(16))))
        self.db.execute('UPDATE media_links SET retained_until=expires_at WHERE retained_until!=expires_at')
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

    def save_cloud_token(self, value, new_session=False):
        # Rotate cache ownership atomically with QR login, but preserve it during token refresh.
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('cloud_token', json.dumps(value)))
            if new_session or not self.get('cloud_cache_owner'):
                self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                                ('cloud_cache_owner', json.dumps(secrets.token_hex(16))))
            if new_session:
                self.db.execute('DELETE FROM media_links')

    def media_link(self, owner, path, now):
        rows = self.rows('''SELECT id,url FROM media_links
            WHERE owner=? AND path=? AND expires_at>? AND retained_until>?''', (owner, path, now, now))
        if not rows:
            return None
        self.execute('UPDATE media_links SET hits=hits+1,last_used=? WHERE id=?', (now, rows[0]['id']))
        return rows[0]['url']

    def save_media_link(self, owner, path, file_id, url, created, expires_at):
        with self.db:
            self.db.execute('DELETE FROM media_links WHERE expires_at<=?', (created,))
            self.db.execute('''INSERT INTO media_links(owner,path,file_id,url,created,expires_at,retained_until,last_used)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(owner,path) DO UPDATE SET
                file_id=excluded.file_id,url=excluded.url,created=excluded.created,expires_at=excluded.expires_at,
                retained_until=excluded.retained_until,hits=0,last_used=excluded.last_used''',
                (owner, path, file_id, url, created, expires_at, expires_at, created))

    def media_cache_page(self, page, page_size=20):
        now, owner = time.time(), self.get('cloud_cache_owner', '')
        self.execute('DELETE FROM media_links WHERE expires_at<=?', (now,))
        stats = self.rows('''SELECT COUNT(*) total,COALESCE(SUM(expires_at>?),0) valid
            FROM media_links WHERE owner=?''', (now, owner))[0]
        pages = max(1, (stats['total'] + page_size - 1) // page_size)
        page = min(page, pages)
        # Never return the signed URL or cache ownership key through the management API.
        items = self.rows('''SELECT id,path,created,expires_at,retained_until,hits,last_used,
            expires_at>? AS usable FROM media_links WHERE owner=? ORDER BY last_used DESC,id DESC LIMIT ? OFFSET ?''',
            (now, owner, page_size, (page - 1) * page_size))
        return {**stats, 'items': items, 'page': page, 'pages': pages, 'page_size': page_size}

    def rows(self, sql, params=()):
        return [dict(x) for x in self.db.execute(sql, params).fetchall()]

    def execute(self, sql, params=()):
        result = self.db.execute(sql, params)
        self.db.commit()
        return result

    def update_job(self, job_id, **fields):
        allowed = {'status', 'attempts', 'next_at', 'remote_task', 'error', 'code', 'resource_name',
                   'completed_at', 'cleanup_enabled', 'cleanup_folder', 'cleanup_name', 'cleanup_manifest',
                   'cleanup_state', 'cleanup_task', 'cleanup_error', 'cleanup_next', 'cleanup_plan', 'cleanup_targets',
                   'cleanup_completed_at'}
        if not fields.keys() <= allowed:
            raise ValueError('Invalid job fields')
        fields['updated'] = time.time()
        self.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE id=?', (*fields.values(), job_id))

    def actionable_jobs(self):
        return self.rows("""SELECT id,status FROM jobs WHERE status IN ('failed','uncertain')
            AND cleanup_state NOT IN ('pending','deleting','submitted','uncertain') ORDER BY id""")

    def cleanup_progress(self, current_id=None):
        fields = 'id,resource_name,share_id,cleanup_state,cleanup_completed_at,completed_at,created,cleanup_next'
        # shortcut: legacy rows lack cleanup times; keep the updated fallback until they are retired.
        last = self.rows(f"""SELECT {fields} FROM jobs WHERE status='success' AND cleanup_state='done'
            ORDER BY cleanup_completed_at IS NOT NULL DESC,cleanup_completed_at DESC,updated DESC,id DESC LIMIT 1""")
        current = self.rows(f"""SELECT {fields} FROM jobs WHERE id=? AND status='success'
            AND cleanup_state IN ('pending','deleting','submitted')""", (current_id,)) if current_id is not None else []
        active = bool(current)
        if not current:
            current = self.rows(f"""SELECT {fields} FROM jobs WHERE status='success' AND cleanup_state IN ('deleting','submitted')
                ORDER BY cleanup_state='deleting' DESC,COALESCE(completed_at,created),id LIMIT 1""")
        now = time.time()
        pending = self.rows(f"""SELECT {fields},MAX(COALESCE(completed_at,created)+?,cleanup_next) AS available_at
            FROM jobs WHERE status='success' AND cleanup_enabled=1 AND cleanup_state='pending' AND id!=?
            ORDER BY MAX(available_at,?),COALESCE(completed_at,created),id LIMIT 1""",
            (self.get('cleanup_minutes',60)*60,current[0]['id'] if current else -1,now))
        if last:
            last[0]['phase'] = 'done' if last[0]['cleanup_completed_at'] is not None else 'legacy_done'
        if current:
            state = current[0]['cleanup_state']
            current[0]['phase'] = 'checking' if state=='pending' else 'polling' if active and state=='submitted' else state
        if pending:
            job = pending[0]
            job['phase'] = ('paused' if not self.get('auto_cleanup',False) else
                            'backoff' if job['cleanup_next']>now and job['cleanup_next']==job['available_at'] else
                            'waiting' if job['available_at']>now else 'queued')
        return {'last':last[0] if last else None,'current':current[0] if current else None,'next':pending[0] if pending else None}

    def discard_jobs(self, ids):
        selected = set(ids) & {job['id'] for job in self.actionable_jobs()}
        now = time.time()
        # Keep shares and receipts, and recheck eligibility immediately before updating.
        with self.db:
            self.db.executemany("UPDATE jobs SET status='discarded',updated=? WHERE id=?",
                                [(now, jid) for jid in selected])
        return len(selected)

    def retry_jobs(self, ids, confirm_uncertain=False, code=''):
        ids = set(ids)
        selected = {job['id']: job['status'] for job in self.actionable_jobs() if job['id'] in ids}
        if not confirm_uncertain and 'uncertain' in selected.values():
            raise ValueError('请先核对云盘，确认结果待核对的任务均尚未转存')
        now = time.time()
        with self.db:
            self.db.executemany("""UPDATE jobs SET status='queued',attempts=0,next_at=0,
                remote_task=NULL,error='',code=CASE WHEN ?='' THEN code ELSE ? END,updated=? WHERE id=?""",
                [(code, code, now, jid) for jid in selected])
        return len(selected)

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
