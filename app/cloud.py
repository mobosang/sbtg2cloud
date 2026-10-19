"""Guangya protocol adapter. See docs/INTEGRATION.md for evidence and limits."""
import asyncio
import json
import time
import httpx
from .proxy import validate_http_environment, ProxyConfigError

ACCOUNT = 'https://account.guangyapan.com/v1'
API = 'https://api.guangyapan.com/userres/v1'
CLIENT_ID = 'aMe-8VSlkrbQXpUR'


class CloudError(Exception):
    def __init__(self, message, retryable=False, login=False):
        super().__init__(message)
        self.retryable = retryable
        self.login = login


class TaskFailed(CloudError):
    """The provider explicitly confirmed a terminal failed transfer."""


def check(body):
    if not isinstance(body, dict):
        raise CloudError('云盘返回格式异常')
    code = body.get('code', 0)
    if body.get('error') or code not in (0, '0', None) or body.get('msg', 'success') not in ('success', '', None, '成功'):
        # Never persist the full provider response: it may contain credentials.
        safe_code = str(body.get('error') or code)[:64]
        if not all(c.isalnum() or c in '_-' for c in safe_code):
            safe_code = 'unknown'
        raise CloudError(f'光鸭接口拒绝请求（{safe_code}），请检查提取码、分享有效性、空间或登录状态')
    return body


class Cloud:
    def __init__(self, store):
        self.store = store
        self.proxy_error = ''
        try:
            validate_http_environment()
            self.http = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=True)
        except (ProxyConfigError, ValueError) as error:
            self.proxy_error = str(error) if isinstance(error, ProxyConfigError) else 'HTTP 环境代理配置错误，请检查代理 URL 和 NO_PROXY'
            # Keep WebUI available, but never silently send direct requests.
            self.http = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        self.lock = asyncio.Lock()
        self.request_lock = asyncio.Lock()
        self.operation_lock = asyncio.Lock()
        self.next_request_at = 0.0
        self.history_lock = asyncio.Lock()
        self.history_cache = {}
        self.login_error = self.proxy_error

    async def raw(self, url, payload, token=''):
        # All Guangya traffic in this process shares the same pacing, including UI reads.
        async with self.request_lock:
            delay = self.next_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await self._raw(url, payload, token)
            finally:
                self.next_request_at = max(self.next_request_at, time.monotonic() + 2)

    async def _raw(self, url, payload, token=''):
        if self.proxy_error:
            raise CloudError(self.proxy_error)
        headers = {'Authorization': f'Bearer {token}'} if token else {}
        response = await self.http.post(url, json=payload, headers=headers)
        if response.status_code == 401:
            raise CloudError('光鸭登录已过期，请重新扫码', login=True)
        if response.status_code == 429 or response.status_code >= 500:
            retry_after = response.headers.get('Retry-After', '')
            cooldown = max(60, min(int(retry_after), 900)) if retry_after.isdigit() else 60
            self.next_request_at = max(self.next_request_at, time.monotonic() + cooldown)
            raise CloudError('光鸭服务繁忙，请稍后重试', retryable=True)
        if response.status_code >= 400:
            # OAuth uses HTTP 400 for authorization_pending; retain only parsed body internally.
            try:
                body = response.json()
                if url.startswith(ACCOUNT) and body.get('error'):
                    return body
            except (ValueError, AttributeError):
                pass
            raise CloudError(f'光鸭请求失败（HTTP {response.status_code}）')
        try:
            return response.json()
        except ValueError:
            raise CloudError('光鸭返回非 JSON 响应，接口可能已变化') from None

    def save_token(self, data):
        if not data.get('access_token') or not data.get('refresh_token'):
            raise CloudError('扫码授权返回缺少令牌')
        self.store.set('cloud_token', {**data, 'expires_at': time.time() + int(data.get('expires_in', 3600))})
        self.login_error = ''

    async def token(self, force=False):
        async with self.lock:
            token = self.store.get('cloud_token')
            if not token:
                raise CloudError('请先扫码登录光鸭云盘', login=True)
            if force or token['expires_at'] < time.time() + 60:
                data = await self.raw(ACCOUNT + '/auth/token', {
                    'client_id': CLIENT_ID, 'grant_type': 'refresh_token', 'refresh_token': token['refresh_token']})
                if data.get('error'):
                    self.login_error = '光鸭登录失效，请重新扫码'
                    raise CloudError(self.login_error, login=True)
                self.save_token(data)
                token = self.store.get('cloud_token')
            return token['access_token']

    async def call(self, path, payload):
        token = await self.token()
        try:
            body = await self.raw(API + path, payload, token)
        except CloudError as error:
            if not error.login:
                raise
            token = await self.token(force=True)
            body = await self.raw(API + path, payload, token)
        return check(body).get('data')

    async def qr(self):
        return check(await self.raw(ACCOUNT + '/auth/device/code', {'client_id': CLIENT_ID, 'scope': 'user'}))

    async def poll_qr(self, device_code):
        body = await self.raw(ACCOUNT + '/auth/token', {
            'client_id': CLIENT_ID, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device_code})
        if body.get('error') in ('authorization_pending', 'slow_down'):
            return body['error']
        check(body)
        self.save_token(body)
        return 'authorized'

    async def pages(self, path, payload, cursor_mode=False):
        items = []
        seen = set()
        cursor = None
        cursors = set()
        for page in range(10000):
            paging = ({'cursor': cursor} if cursor else {}) if cursor_mode else {'page': page}
            data = await self.call(path, {**payload, **paging, 'pageSize': 100})
            if not isinstance(data, dict):
                raise CloudError('文件列表格式异常')
            rows = data.get('files', data.get('list'))
            # Observed userres empty directory response: {"msg":"success","data":{}}.
            # Also accept explicit zero/null arrays, only for normal directory lists.
            # Unknown non-empty shapes and share/cursor responses still fail closed.
            if rows is None and path == '/file/get_file_list' and not cursor_mode:
                empty_array = ('list' in data and data['list'] is None) or ('files' in data and data['files'] is None)
                total = data.get('total')
                if not data or (not data.get('hasMore') and (total in (0, '0') or (total is None and empty_array))):
                    rows = []
            if not isinstance(rows, list):
                raise CloudError('文件列表字段无法识别，需核对接口')
            ids = [str(x.get('id', x.get('fileId', ''))) for x in rows]
            if any(not x or x in seen for x in ids):
                raise CloudError('文件分页返回重复或缺少 ID，已停止以避免漏转存')
            seen.update(ids)
            items.extend(rows)
            if cursor_mode:
                if not data.get('hasMore'):
                    return items
                cursor = data.get('cursor')
                if not cursor or cursor in cursors or not rows:
                    raise CloudError('分页游标异常，已停止以避免漏转存')
                cursors.add(cursor)
            elif len(rows) < 100:
                return items
        raise CloudError('文件列表超过分页上限')

    async def folders(self, parent=''):
        rows = await self.pages('/file/get_file_list', {'parentId': parent})
        return [{'id': str(x.get('id', x.get('fileId'))), 'name': x.get('name', x.get('fileName', '未命名'))}
                for x in rows if x.get('resType') == 2 or x.get('isDir') in (True, 1) or x.get('kind') == 'drive#folder']

    async def history_files(self, parent):
        # Only previews may use stale listings. Deletion preflight always reads fresh data.
        async with self.history_lock:
            cached = self.history_cache.get(parent)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            rows = await self.pages('/file/get_file_list', {'parentId': parent})
            if len(self.history_cache) >= 8:
                self.history_cache.pop(next(iter(self.history_cache)))
            self.history_cache[parent] = (time.monotonic(), rows)
            return rows

    async def prepare(self, share_id, code, details=False):
        access = await self.call('/get_share_access_token', {'shareId': share_id, 'code': code})
        if not isinstance(access, dict) or not access.get('accessToken'):
            raise CloudError('分享访问令牌缺失')
        token = access['accessToken']
        rows = await self.pages('/get_share_page_files_list', {'accessToken': token, 'parentId': ''}, cursor_mode=True)
        if not rows:
            raise CloudError('分享中没有可转存的文件')
        names = [str(x.get('fileName') or x.get('name') or '') for x in rows]
        names = [name for name in names if name]
        title = '、'.join(names[:3])[:240]
        if len(names) > 3:
            title += f' 等 {len(names)} 项'
        result = (token, [str(x.get('id', x.get('fileId'))) for x in rows], title)
        return (*result, rows) if details else result

    async def submit(self, access, ids, parent):
        data = await self.call('/restore_share', {'accessToken': access, 'fileIds': ids, 'parentId': parent})
        if not isinstance(data, dict) or not data.get('taskId'):
            raise CloudError('转存响应缺少任务 ID，请核对云盘后再重试')
        return str(data['taskId'])

    async def create_cleanup_folder(self, parent, name):
        data = await self.call('/file/create_dir', {
            'parentId': parent, 'dirName': name, 'failIfNameExist': True})
        if not isinstance(data, dict) or not data.get('fileId'):
            raise CloudError('临时目录创建结果不明，请核对云盘')
        folder = str(data['fileId'])
        if folder == parent:
            raise CloudError('临时目录 ID 异常')
        return folder

    async def cleanup_manifest(self, parent, folder, name, root_verified=False):
        """Read only the normal directory tree; never enumerate the recycle bin."""
        roots = [] if root_verified else await self.folders(parent)
        if not folder or folder == parent or (not root_verified and not any(x['id'] == folder and x['name'] == name for x in roots)):
            raise CloudError('临时目录已移动、改名或不存在，停止自动删除')
        pending, seen, manifest = [folder], {folder}, []
        while pending:
            current = pending.pop()
            rows = await self.pages('/file/get_file_list', {'parentId': current})
            for row in rows:
                fid = str(row.get('fileId') or row.get('id') or '')
                kind = row.get('resType')
                if not fid or fid in seen or kind not in (1, 2) or len(seen) >= 10000:
                    raise CloudError('临时目录内容无法完整核对，停止自动删除')
                seen.add(fid)
                # Only stable identity/content fields, never access tokens or signed URLs.
                manifest.append([current, fid, kind, row.get('fileName', row.get('name')),
                                 row.get('fileSize', row.get('size')), row.get('md5'),
                                 row.get('updateTime'), row.get('modifyTime')])
                if kind == 2:
                    pending.append(fid)
        return json.dumps(sorted(manifest, key=lambda x: x[1]), ensure_ascii=False)

    async def delete_cleanup_folder(self, folder):
        if not folder:
            raise CloudError('缺少待删除目录 ID')
        return await self.delete_cleanup_targets([folder])

    async def delete_cleanup_targets(self, ids):
        if not ids or any(not isinstance(x, str) or not x for x in ids):
            raise CloudError('缺少待删除目标 ID')
        # Deliberately do not use call(): no automatic retry of this destructive request,
        # including after HTTP 401. A second delete can permanently erase recycled files.
        token = await self.token()
        data = check(await self.raw(API + '/file/delete_file', {'fileIds': ids}, token)).get('data')
        if not isinstance(data, dict) or not data.get('taskId'):
            raise CloudError('删除提交结果不明，请手动核对')
        return str(data['taskId'])

    async def task(self, task_id):
        data = await self.call('/get_task_status', {'taskId': task_id})
        if not isinstance(data, dict) or 'status' not in data:
            raise CloudError('转存进度格式异常')
        status = data['status']
        detail = data.get('detail') or {}
        if status == 3 or (status == 2 and detail.get('code', 0) not in (0, '0', None)):
            raise TaskFailed('云盘转存任务失败，请检查空间、分享状态和同名文件')
        return status == 2
