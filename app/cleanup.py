"""Read-only reconciliation of legacy jobs. Candidates are never deletion authority."""
import re


def title_key(value):
    value = re.split(r'[（(]|\{tmdb', value or '', maxsplit=1)[0]
    return ''.join(c.lower() for c in value if c.isalnum())


async def history_preview(store, cloud, page=1):
    total = store.rows("SELECT COUNT(*) n FROM jobs WHERE status='success' AND completed_at IS NULL AND cleanup_enabled=0")[0]['n']
    pages = max(1, (total + 19) // 20)
    page = min(page, pages)
    jobs = store.rows("""SELECT id,resource_name,target_id,target_name,share_id FROM jobs
        WHERE status='success' AND completed_at IS NULL AND cleanup_enabled=0
        ORDER BY id LIMIT 20 OFFSET ?""", ((page - 1) * 20,))
    directories = {}
    result = []
    for job in jobs:
        parent = job['target_id']
        if parent not in directories:
            try:
                directories[parent] = await cloud.history_files(parent)
            except Exception:
                directories[parent] = None
        rows = directories[parent]
        key = title_key(job['resource_name'])
        candidates = []
        if rows is not None:
            for row in rows:
                name = str(row.get('fileName') or row.get('name') or '')
                other = title_key(name)
                if key and (name == job['resource_name'] or (len(key) >= 2 and key == other)):
                    candidates.append({'id': str(row.get('fileId') or row.get('id') or ''),
                                       'name': name, 'folder': row.get('resType') == 2})
        result.append({**job, 'candidates': candidates,
                       'note': '无法读取保存目录，请检查登录或网络' if rows is None else
                       '仅按名称匹配，不能证明来自本次转存；成功时间未知，需人工确认' if candidates else
                       '未找到同名目标；可能已改名、移动或名称不同，不会自动删除'})
    return {'items': result, 'page': page, 'pages': pages, 'total': total}
