"""Inspect one failed cleanup without changing jobs, tokens, or cloud files.

Run inside the existing container: python scripts/diagnose_cleanup.py
Also supports stdin execution through docker exec -i sbtg2cloud python -.
"""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, os.getcwd())
from app.cloud import Cloud, CloudError
from app.cleanup import targets_manifest, legacy_manifest


class ReadOnlyStore:
    def __init__(self):
        path = Path(os.environ.get('DATA_DIR', '/data')) / 'state.sqlite3'
        self.db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        self.db.row_factory = sqlite3.Row

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, *args):
        raise RuntimeError('diagnostic cannot write settings')


def unpack(value):
    if isinstance(value, str) and value.startswith('['):
        try:
            return unpack(json.loads(value))
        except ValueError:
            pass
    return [unpack(x) for x in value] if isinstance(value, list) else value


def differences(old, new, path='$'):
    if isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            yield f'{path}: item count {len(old)} -> {len(new)}'
        for index, (left, right) in enumerate(zip(old, new)):
            yield from differences(left, right, f'{path}[{index}]')
    elif old != new or type(old) is not type(new):
        # No names, IDs, token values, provider bodies, or signed URLs in output.
        yield f'{path}: value changed ({type(old).__name__} -> {type(new).__name__})'


async def main():
    store = ReadOnlyStore()
    cloud = Cloud(store)
    requests = 0
    original_raw = cloud.raw
    async def readonly_raw(url, payload, token=''):
        nonlocal requests
        if not url.endswith('/file/get_file_list'):
            raise RuntimeError('token refresh or non-list request blocked; retry after WebUI login refresh')
        requests += 1
        if requests > 20:
            raise RuntimeError('read request limit reached')
        return await original_raw(url, payload, token)
    cloud.raw = readonly_raw
    try:
        row = store.db.execute("""SELECT * FROM jobs WHERE status='success'
            AND cleanup_state='blocked' AND cleanup_manifest!='' AND cleanup_task=''
            ORDER BY id DESC LIMIT 1""").fetchone()
        if row is None:
            print('No blocked job with an original snapshot. No cloud requests made.')
            return
        job = dict(row)
        print('job_id:', job['id'], flush=True)
        print('Checking original snapshot; no deletion and no database writes.', flush=True)
        if job['cleanup_targets']:
            current = await targets_manifest(cloud, job['target_id'], json.loads(job['cleanup_targets']))
        elif job['completed_at'] is None:
            current = await legacy_manifest(cloud, job['target_id'], job['cleanup_folder'], job['cleanup_name'])
        else:
            current = await cloud.cleanup_manifest(job['target_id'], job['cleanup_folder'], job['cleanup_name'])
        print('exact_snapshot_match:', current == job['cleanup_manifest'])
        old, new = unpack(job['cleanup_manifest']), unpack(current)
        print('decoded_snapshot_match:', old == new)
        delta = list(differences(old, new))
        print('difference_count:', len(delta))
        print('Tree row fields [0..7]: parent_id,id,type,name,size,md5,updateTime,modifyTime')
        print('Standalone file fields [0..5]: id,name,size,md5,updateTime,modifyTime')
        for line in delta[:12]:
            print(line)
    except Exception as error:
        print('check_error_type:', type(error).__name__)
        # CloudError messages are sanitized by the adapter. Never dump httpx exceptions.
        if isinstance(error, CloudError):
            print('check_error:', str(error))
        elif isinstance(error, ValueError):
            print('check_error: target missing/renamed/moved, or invalid stored snapshot')
        elif isinstance(error, RuntimeError):
            print('check_error: read-only request guard stopped the check (login refresh or request cap)')
    finally:
        print('directory_read_requests:', requests)
        await cloud.http.aclose()
        store.db.close()


if __name__ == '__main__':
    asyncio.run(main())
