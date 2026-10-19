"""One-off, explicitly approved directory batch. Run with the main container stopped.

Compatible with the existing Cloud adapter; no login secrets are accepted on CLI.
The approval manifest and durable journal must remain in /data, never in Git.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.cwd()))
from app.cloud import API, Cloud, CloudError, TaskFailed, check
from app.store import Store


class ConfirmedCleanup:
    def __init__(self, cloud, journal, manifest):
        self.cloud = cloud
        self.targets = manifest.get('targets', [])
        if manifest.get('version') != 1 or not 1 <= len(self.targets) <= 100:
            raise ValueError('确认清单格式或数量无效')
        ids = []
        for row in self.targets:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not row['id']
                or not isinstance(row.get('name'), str) or not row['name']
                or not isinstance(row.get('parent_id'), str) or row['id'] == row['parent_id']):
                raise ValueError('确认清单缺少目标 ID、名称或父目录')
            ids.append(row['id'])
        if len(ids) != len(set(ids)):
            raise ValueError('确认清单有重复目录 ID')
        # Stable even when the same manifest is reordered or copied to a different path.
        self.digest = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()
        self.db = sqlite3.connect(journal)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS batches (digest TEXT PRIMARY KEY,state TEXT,task TEXT,next_at REAL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS claimed (file_id TEXT PRIMARY KEY,digest TEXT NOT NULL)')

    def save(self, state, task='', next_at=0):
        self.db.execute('INSERT OR REPLACE INTO batches VALUES (?,?,?,?)', (self.digest, state, task, next_at))
        self.db.commit()

    async def run(self, execute=False):
        previous = self.db.execute('SELECT state,task,next_at FROM batches WHERE digest=?', (self.digest,)).fetchone()
        if previous:
            state, task, next_at = previous
            if state != 'submitted' or not execute:
                return 'uncertain' if state == 'submitting' else state
            if time.time() < next_at:
                return 'submitted'
            try:
                if await self.cloud.task(task):
                    self.save('done', task)
                    return 'done'
            except TaskFailed:
                self.save('blocked', task)
                return 'blocked'
            except Exception:
                pass
            self.save('submitted', task, time.time()+60)
            return 'submitted'
        if any(self.db.execute('SELECT 1 FROM claimed WHERE file_id=?', (row['id'],)).fetchone() for row in self.targets):
            return 'blocked'
        # Fresh normal listings only; never query or delete anything in the recycle bin.
        parents = {}
        for row in self.targets:
            parent = row['parent_id']
            if parent not in parents:
                await asyncio.sleep(2)
                parents[parent] = await self.cloud.folders(parent)
            if not any(x['id'] == row['id'] and x['name'] == row['name'] for x in parents[parent]):
                raise ValueError('有目录已移动、改名或不存在，整批停止。请重新核对清单')
        if not execute:
            return 'preview'
        token = await self.cloud.token()
        await asyncio.sleep(2)
        with self.db:
            self.db.executemany('INSERT INTO claimed VALUES (?,?)', [(row['id'], self.digest) for row in self.targets])
            self.db.execute('INSERT INTO batches VALUES (?,?,?,?)', (self.digest, 'submitting', '', 0))
        try:
            # Exactly one destructive POST; never the call() helper that retries 401.
            data = check(await self.cloud.raw(API+'/file/delete_file',
                {'fileIds': [row['id'] for row in self.targets]}, token)).get('data')
            if not isinstance(data, dict) or not data.get('taskId'):
                raise CloudError('missing receipt')
        except Exception:
            self.save('uncertain')
            return 'uncertain'
        self.save('submitted', str(data['taskId']), time.time()+30)
        return 'submitted'


async def main(args):
    directory = Path(os.environ.get('DATA_DIR', '/data'))
    # Linux/Unraid advisory lock protects against two copies of this utility.
    import fcntl
    with (directory / 'confirmed-cleanup.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Store(directory)
        cloud = Cloud(store)
        cleaner = None
        try:
            manifest = json.loads(Path(args.manifest).read_text(encoding='utf-8'))
            cleaner = ConfirmedCleanup(cloud, directory / 'confirmed-cleanup.sqlite3', manifest)
            result = await cleaner.run(args.execute)
            messages = {
                'preview': '只读核对通过；未删除。添加 --execute 才会执行已确认清单。',
                'submitted': '已提交原删除任务。至少 60 秒后重跑相同命令查询；不会再次提交删除。',
                'done': '云盘确认删除任务完成，目录已移入回收站。',
                'blocked': '原删除任务失败；请手动核对，不会重新提交。',
                'uncertain': '删除结果不明或提交曾中断；请手动核对，不会再次删除。',
            }
            print(messages[result])
            return 0 if result in ('preview', 'done', 'submitted') else 2
        finally:
            if cleaner:
                cleaner.db.close()
            await cloud.http.aclose()
            store.db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args)))
    except (ValueError, FileNotFoundError) as error:
        # JSON decode errors may include file snippets; do not echo arbitrary exceptions.
        print('清单无法读取或目录核对不通过，未继续操作。请检查文件与目录状态。')
        sys.exit(2)
    except Exception:
        print('连接或执行异常，已停止。保留 /data/confirmed-cleanup.sqlite3 后再检查，勿删除日志后重试。')
        sys.exit(2)
