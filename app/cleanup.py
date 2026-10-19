"""Read-only reconciliation of legacy jobs. Candidates are never deletion authority."""
import re
import time
import json


def cleanup_snapshot_matches(original, current, multiple_targets=False):
    """Allow removed descendants only; surviving IDs retain every recorded field."""
    def compatible(old, new):
        if not isinstance(old, list) or not isinstance(new, list):
            return False
        # A standalone file fingerprint must remain entirely unchanged.
        if len(old) == 6 and isinstance(old[0], str):
            return old == new
        def indexed(rows):
            result = {}
            for row in rows:
                if (not isinstance(row, list) or len(row) != 8
                        or not isinstance(row[1], str) or not row[1]
                        or row[2] not in (1, 2) or row[1] in result):
                    raise ValueError('invalid tree snapshot')
                result[row[1]] = row
            return result
        before, after = indexed(old), indexed(new)
        return all(fid in before and row == before[fid] for fid, row in after.items())
    try:
        old, new = json.loads(original), json.loads(current)
        if multiple_targets:
            # Roots are still verified by ID/name before producing each manifest.
            return (isinstance(old, list) and isinstance(new, list) and len(old) == len(new)
                    and bool(old) and all(compatible(json.loads(a), json.loads(b)) for a, b in zip(old, new)))
        return compatible(old, new)
    except (ValueError, TypeError, KeyError):
        return False


def resume_reduced_cleanup(store):
    """One-time recheck of pre-delete mismatches, never submitted/uncertain deletes."""
    if store.get('cleanup_reduced_snapshot_v1', False):
        return
    errors = {
        '目录位置或内容变化，或核对失败；请手动检查，不会自动删除',
        '清理核对失败：当前文件清单与转存时快照不同，已停止删除；请检查新增、移走或修改的文件',
    }
    for job in store.rows("SELECT * FROM jobs WHERE status='success' AND cleanup_enabled=1 AND cleanup_state='blocked' AND cleanup_task='' AND cleanup_manifest!=''"):
        if job['cleanup_error'] not in errors:
            continue
        try:
            ids = [x['id'] for x in json.loads(job['cleanup_targets'])] if job['cleanup_targets'] else [job['cleanup_folder']]
            if not ids or any(not fid for fid in ids):
                continue
            if any(not store.rows('SELECT file_id FROM cleanup_claims WHERE file_id=? AND job_id=?', (fid, job['id']))
                   or store.rows('SELECT file_id FROM directory_clear_claims WHERE file_id=?', (fid,)) for fid in ids):
                continue
        except (ValueError, TypeError, KeyError):
            continue
        store.update_job(job['id'], cleanup_state='pending', cleanup_next=0,
                         cleanup_error='等待按仅减少文件规则重新核对原快照，尚未提交删除')
    store.set('cleanup_reduced_snapshot_v1', True)


def file_spec(row):
    return {'id': str(row.get('fileId') or row.get('id') or ''),
            'name': row.get('fileName') or row.get('name') or '', 'kind': row.get('resType'),
            'size': row.get('fileSize', row.get('size'))}


def transferred_targets(plan, after):
    """Only new IDs matching source entries; never existing/merged/renamed objects."""
    before = plan['before']
    old_ids = {x['id'] for x in before}
    old_names = {x['name'] for x in before}
    new = [file_spec(row) for row in after if file_spec(row)['id'] not in old_ids]
    targets = []
    for source in plan['source']:
        if not source['name'] or source['kind'] not in (1, 2) or source['name'] in old_names:
            raise ValueError('目标目录已有同名资源或源类型不明，停止自动清理')
        matches = [row for row in new if row['name'] == source['name'] and row['kind'] == source['kind']
                   and (source['kind'] == 2 or source['size'] is None or row['size'] == source['size'])]
        if len(matches) != 1 or not matches[0]['id'] or matches[0]['id'] in {t['id'] for t in targets}:
            raise ValueError('无法唯一定位本次新增资源，停止自动清理')
        targets.append(matches[0])
    if not targets:
        raise ValueError('没有可靠的新增目标')
    return targets


async def targets_manifest(cloud, parent, targets):
    rows = await cloud.pages('/file/get_file_list', {'parentId': parent})
    return json.dumps([await legacy_manifest(cloud, parent, row['id'], row['name'], rows) for row in targets], ensure_ascii=False)


def title_key(value):
    value = re.split(r'[（(]|\{tmdb', value or '', maxsplit=1)[0]
    return ''.join(c.lower() for c in value if c.isalnum())


async def history_preview(store, cloud, page=1):
    total = store.rows("SELECT COUNT(*) n FROM jobs WHERE status='success' AND completed_at IS NULL AND cleanup_enabled=0")[0]['n']
    pages = max(1, (total + 19) // 20)
    page = min(page, pages)
    jobs = store.rows("""SELECT id,resource_name,target_id,target_name,share_id,created FROM jobs
        WHERE status='success' AND completed_at IS NULL AND cleanup_enabled=0
        ORDER BY id LIMIT 20 OFFSET ?""", ((page - 1) * 20,))
    directories = {}
    result = []
    for job in jobs:
        parent = job['target_id']
        delay = store.get('cleanup_minutes', 60) * 60
        eligible = time.time() - job['created'] >= delay
        if eligible and parent not in directories:
            try:
                directories[parent] = await cloud.history_files(parent)
            except Exception:
                directories[parent] = None
        rows = directories.get(parent)
        key = title_key(job['resource_name'])
        candidates = []
        if rows is not None:
            for row in rows:
                name = str(row.get('fileName') or row.get('name') or '')
                other = title_key(name)
                if key and (name == job['resource_name'] or (len(key) >= 2 and key == other)):
                    candidates.append({'id': str(row.get('fileId') or row.get('id') or ''),
                                       'name': name, 'folder': row.get('resType') == 2})
        result.append({**job, 'candidates': candidates, 'eligible': eligible, 'due_at': job['created'] + delay,
                       'note': '尚未达到配置的等待时间，不请求云盘、不删除' if not eligible else
                       '无法读取保存目录，请检查登录或网络' if rows is None else
                       '已成功且达到等待时间；请选择对应的实际文件或文件夹加入清理' if candidates else
                       '未找到同名目标；可能已改名、移动或名称不同，不会自动删除'})
    return {'items': result, 'page': page, 'pages': pages, 'total': total}


async def legacy_manifest(cloud, parent, file_id, name, rows=None):
    if rows is None:
        rows = await cloud.pages('/file/get_file_list', {'parentId': parent})
    row = next((x for x in rows if str(x.get('fileId') or x.get('id')) == file_id
                and (x.get('fileName') or x.get('name')) == name), None)
    if not row or not file_id or file_id == parent:
        raise ValueError('目标已移动、改名或不存在')
    if row.get('resType') == 2:
        return await cloud.cleanup_manifest(parent, file_id, name, root_verified=True)
    if row.get('resType') != 1:
        raise ValueError('目标类型无法确认')
    return json.dumps([file_id, name, row.get('fileSize', row.get('size')), row.get('md5'),
                       row.get('updateTime'), row.get('modifyTime')], ensure_ascii=False)


async def queue_history(store, cloud, job_id, file_id, name):
    jobs = store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not jobs or jobs[0]['status'] != 'success' or jobs[0]['completed_at'] is not None:
        raise ValueError('只支持缺少成功时间的历史成功任务')
    job = jobs[0]
    if time.time() - job['created'] < store.get('cleanup_minutes', 60) * 60:
        raise ValueError('尚未达到配置的等待时间')
    if not title_key(job['resource_name']) or title_key(name) != title_key(job['resource_name']):
        raise ValueError('目标名称与历史任务不匹配')
    if store.rows('SELECT id FROM jobs WHERE cleanup_enabled=1 AND (id=? OR cleanup_folder=?)', (job_id, file_id)):
        raise ValueError('该任务或目标已加入清理，不会重复删除')
    manifest = await legacy_manifest(cloud, job['target_id'], file_id, name)
    # Recheck after network awaits; one physical target may appear in multiple jobs.
    if store.rows('SELECT id FROM jobs WHERE cleanup_enabled=1 AND (id=? OR cleanup_folder=?)', (job_id, file_id)):
        raise ValueError('该任务或目标已加入清理，不会重复删除')
    store.claim_cleanup(job_id, [file_id])
    store.update_job(job_id, cleanup_enabled=1, cleanup_folder=file_id, cleanup_name=name,
                     cleanup_manifest=manifest, cleanup_state='pending', cleanup_error='', cleanup_next=0)
