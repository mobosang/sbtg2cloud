"""Optional Emby playback proxy. Only original files under the configured mount use 302."""
import asyncio
import logging
import os
import re
import socket
import time
from contextlib import nullcontext
from urllib.parse import urlsplit

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .cloud import CloudError

# Request URLs contain Emby tokens and temporary download signatures.
for name in ('httpx', 'httpcore'):
    logging.getLogger(name).setLevel(logging.WARNING)
WS_LOG = logging.getLogger('sbtg.media.websocket')
WS_LOG.disabled = True
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}
STREAM = re.compile(r'^/(?:emby/)?videos/([a-z0-9_-]{1,200})/(stream|original)(?:\.([a-z0-9]{1,10}))?$', re.I)
DEFAULTS = {'enabled': False, 'upstream_url': '', 'emby_prefix': '', 'cloud_prefix': '/'}


def file_path(value):
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//')
        or len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value)
        or '\\' in value or any(p in ('.', '..') for p in value.split('/'))
        or '//' in value):
        raise ValueError('目录必须是绝对路径，不可包含重复斜线、控制字符或 . / ..')
    return value.rstrip('/') or '/'


def media_settings(value):
    result = {**DEFAULTS, **value}
    url = result['upstream_url'].strip()
    if url:
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username
                     and not parsed.password and not parsed.query and not parsed.fragment
                     and (parsed.port is None or 1 <= parsed.port <= 65535)
                     and not any(c.isspace() or ord(c) < 32 for c in url) and '\\' not in url)
            file_path(parsed.path or '/')
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('Emby 上游地址必须是有效的 HTTP/HTTPS 地址，不可带认证信息、查询参数或片段')
    result['upstream_url'] = url.rstrip('/')
    for key in ('emby_prefix', 'cloud_prefix'):
        if result[key]:
            result[key] = file_path(result[key])
    if result['emby_prefix'] == '/':
        raise ValueError('请选择具体的光鸭挂载目录，不可把 Emby 根目录映射到光鸭')
    if result['enabled'] and not all(result[k] for k in ('upstream_url', 'emby_prefix', 'cloud_prefix')):
        raise ValueError('启用 302 前请填写上游地址、Emby 光鸭挂载路径和光鸭内对应目录')
    return result


def mapped_path(path, settings):
    prefix = settings['emby_prefix']
    if not prefix or not isinstance(path, str) or not path.startswith(prefix + '/'):
        return None
    path = file_path(path)
    return file_path(settings['cloud_prefix'].rstrip('/') + path[len(prefix):])


def safe_headers(headers, extra=()):
    excluded = HOP_HEADERS | {x.strip().lower() for x in headers.get('connection', '').split(',')} | set(extra)
    items = headers.multi_items() if isinstance(headers, httpx.Headers) else headers.items()
    return [(k, v) for k, v in items if k.lower() not in excluded]


class ProxyServer(uvicorn.Server):
    def capture_signals(self):
        return nullcontext()  # The main WebUI server owns process signals.


class MediaProxy:
    def __init__(self, store, cloud, secondary=False, cache_lock=None):
        self.store, self.cloud = store, cloud
        self.setting_key = 'media_proxy_secondary' if secondary else 'media_proxy'
        self.env_prefix = 'EMBY_PROXY_SECONDARY' if secondary else 'EMBY_PROXY'
        self.default_port = '12360' if secondary else '12359'
        self.http = httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                      timeout=httpx.Timeout(30, read=120))
        self.app = Starlette(routes=[
            Route('/{path:path}', self.handle, methods=['GET', 'HEAD', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS']),
            WebSocketRoute('/{path:path}', self.websocket)])
        self.cache_lock = cache_lock if cache_lock is not None else asyncio.Lock()
        self.directories = {}
        self.cache_owner = None
        self.server = self.server_task = self.listener = None
        self.error, self.last, self.redirects = '', None, 0

    def settings(self):
        return media_settings(self.store.get(self.setting_key, {}))

    def status(self):
        return {**self.settings(), 'listening': bool(self.server and self.server.started),
                'public_port': os.environ.get(self.env_prefix + '_PORT', self.default_port),
                'redirects': self.redirects, 'last': self.last, 'error': self.error}

    async def start(self):
        port = os.environ.get(self.env_prefix + '_LISTEN_PORT', '')
        if not port:
            return
        try:
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(('0.0.0.0', port))
            self.listener.listen(128)
            self.listener.setblocking(False)
        except (OSError, ValueError):
            if self.listener:
                self.listener.close()
            self.error = f'播放入口监听失败，请检查 {self.env_prefix}_LISTEN_PORT 和端口占用'
            return
        self.server = ProxyServer(uvicorn.Config(self.app, access_log=False, log_config=None,
                                                log_level='warning', lifespan='off'))
        self.server_task = asyncio.create_task(self.server.serve(sockets=[self.listener]))

    async def close(self):
        if self.server_task:
            self.server.should_exit = True
            try:
                await asyncio.wait_for(self.server_task, 10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self.server_task.cancel()
                await asyncio.gather(self.server_task, return_exceptions=True)
        if self.listener:
            self.listener.close()
        await self.http.aclose()

    def upstream_url(self, request, settings, path=None):
        base = httpx.URL(settings['upstream_url'])
        if path is None:
            path = request.scope.get('raw_path', request.scope['path'].encode('utf-8'))
        query = request.scope.get('query_string', b'')
        return base.copy_with(raw_path=base.raw_path.rstrip(b'/') + path + (b'?' + query if query else b''))

    def request_headers(self, request):
        headers = safe_headers(request.headers, {'host', 'x-forwarded-for', 'x-forwarded-host',
                                                 'x-forwarded-proto', 'forwarded', 'x-sbtg-client'})
        if request.client:
            headers.append(('x-forwarded-for', request.client.host))
        headers.append(('x-forwarded-proto', 'https' if request.url.scheme in ('https', 'wss') else 'http'))
        return headers

    async def children(self, parent, refresh=False):
        cached = self.directories.get(parent)
        if not refresh and cached and cached[0] > time.monotonic():
            return cached[1], True
        rows = await self.cloud.pages('/file/get_file_list', {'parentId': parent})
        if len(self.directories) >= 128:
            self.directories.pop(next(iter(self.directories)))
        # shortcut: renamed/replaced paths may be stale for five minutes; add invalidation if needed.
        self.directories[parent] = (time.monotonic() + 300, rows)
        return rows, False

    async def resolve(self, path):
        parts = file_path(path).strip('/').split('/')
        if not parts or len(parts) > 32:
            raise CloudError('光鸭视频路径过深或为空')
        parent = ''
        for index, name in enumerate(parts):
            rows, cached = await self.children(parent)
            matches = [r for r in rows if r.get('fileName', r.get('name')) == name]
            if not matches and cached:
                rows, _ = await self.children(parent, refresh=True)
                matches = [r for r in rows if r.get('fileName', r.get('name')) == name]
            if len(matches) != 1:
                raise CloudError('光鸭目录中没有唯一匹配的视频，请检查路径映射或文件是否已移动、删除')
            entry = matches[0]
            folder = entry.get('resType') == 2
            if folder != (index < len(parts) - 1) or entry.get('resType') not in (1, 2):
                raise CloudError('光鸭路径类型不符，请确认映射的是视频文件所在目录')
            parent = str(entry.get('fileId', entry.get('id', '')))
            if not parent:
                raise CloudError('光鸭文件编号缺失')
        return parent

    async def signed_url(self, path):
        # Coalesce concurrent seeks/clients; directory reads retain the shared Guangya pacing.
        async with self.cache_lock:
            await self.cloud.token()
            owner = self.store.get('cloud_cache_owner')
            if not owner:
                self.store.save_cloud_token(self.store.get('cloud_token'))
                owner = self.store.get('cloud_cache_owner')
            if owner != self.cache_owner:
                self.directories.clear()
                self.cache_owner = owner
            cached = self.store.media_link(owner, path, time.time())
            if cached:
                return cached
            fid = await self.resolve(path)
            started = time.time()
            url, duration = await self.cloud.download_url(fid)
            if owner != self.store.get('cloud_cache_owner'):
                raise CloudError('光鸭登录会话已切换，请重新播放')
            created = time.time()
            self.store.save_media_link(owner, path, fid, url, created,
                                       started + max(0, duration - 5))
            return url

    async def redirect(self, request, settings, match):
        query = {k.lower(): v for k, v in request.query_params.multi_items()}
        if ((match[3] or '').lower() in ('m3u8', 'm3u') or query.get('static', '').lower() == 'false'
            or (match[2].lower() == 'stream' and query.get('static', '').lower() != 'true')
            or query.get('subtitlemethod', '').lower() == 'encode'):
            return None
        authenticated = (any(request.headers.get(k) for k in ('x-emby-token', 'x-mediabrowser-token',
                           'authorization', 'x-emby-authorization'))
                         or any(query.get(k) for k in ('api_key', 'x-emby-token')))
        if not authenticated:
            return JSONResponse({'detail': '请先登录 Emby'}, status_code=401)
        info_url = self.upstream_url(request, settings, f'/Items/{match[1]}/PlaybackInfo'.encode())
        # Use the caller's credentials, never an administrator API key or cached authorization.
        headers = [(k, v) for k, v in self.request_headers(request) if k.lower() not in
                   {'range', 'if-range', 'if-none-match', 'if-modified-since', 'if-match', 'if-unmodified-since',
                    'content-length', 'content-type'}]
        info = await self.http.get(info_url, headers=headers)
        if info.status_code in (401, 403, 404):
            return JSONResponse({'detail': 'Emby 拒绝播放此资源'}, status_code=info.status_code)
        if info.status_code != 200:
            raise CloudError('读取 Emby 播放信息失败')
        try:
            body = info.json()
            sources = body['MediaSources']
            if body.get('ErrorCode') or not isinstance(sources, list):
                return None
            selected = query.get('mediasourceid')
            source = next((s for s in sources if s.get('Id') == selected), None) if selected else sources[0] if len(sources) == 1 else None
            path = mapped_path(source.get('Path'), settings) if source else None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise CloudError('Emby 播放信息格式无法识别') from None
        if not path:
            return None
        try:
            url = await asyncio.wait_for(self.signed_url(path), 45)
        except asyncio.TimeoutError:
            raise CloudError('光鸭取链等待超时，请稍后重试；首次读取多层目录可能较慢') from None
        self.redirects += 1
        self.last = {'item_id': match[1], 'name': path.rsplit('/', 1)[-1], 'at': time.time()}
        self.error = ''
        return RedirectResponse(url, status_code=302,
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    async def forward(self, request, settings):
        outgoing = self.http.build_request(request.method, self.upstream_url(request, settings),
            headers=self.request_headers(request), content=None if request.method in ('GET', 'HEAD') else request.stream())
        upstream = await self.http.send(outgoing, stream=True)
        headers = safe_headers(upstream.headers)
        for i, (key, value) in enumerate(headers):
            if key.lower() == 'location' and value.startswith(settings['upstream_url'] + '/'):
                headers[i] = (key, value[len(settings['upstream_url']):])
        async def content():
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()
        if request.method == 'HEAD':
            await upstream.aclose()
            response = Response(status_code=upstream.status_code)
        else:
            response = StreamingResponse(content(), status_code=upstream.status_code,
                                         background=BackgroundTask(upstream.aclose))
        response.raw_headers = [(k.lower().encode('latin-1'), v.encode('latin-1')) for k, v in headers]
        return response

    async def handle(self, request):
        settings = self.settings()
        if not settings['enabled']:
            return JSONResponse({'detail': '请在 sbtg2cloud 管理页配置并启用 Emby 302'}, status_code=503)
        try:
            match = STREAM.fullmatch(request.url.path)
            if request.method in ('GET', 'HEAD') and match:
                response = await self.redirect(request, settings, match)
                if response is not None:
                    return response
            return await self.forward(request, settings)
        except (CloudError, httpx.HTTPError, ValueError) as error:
            self.error = str(error) if isinstance(error, CloudError) else '播放服务连接失败或路径无效，请检查上游地址和目录映射'
            return JSONResponse({'detail': self.error}, status_code=502,
                                headers={'Cache-Control': 'no-store'})

    async def websocket(self, client):
        settings = self.settings()
        if not settings['enabled']:
            await client.close(code=1008)
            return
        url = self.upstream_url(client, settings)
        url = url.copy_with(scheme='wss' if url.scheme == 'https' else 'ws')
        headers = self.request_headers(client)
        headers = [(k, v) for k, v in headers if not k.lower().startswith('sec-websocket-')]
        try:
            async with connect(str(url), additional_headers=headers, proxy=None, compression=None,
                               subprotocols=client.scope.get('subprotocols') or None,
                               logger=WS_LOG, open_timeout=10) as upstream:
                await client.accept(subprotocol=upstream.subprotocol)
                async def to_upstream():
                    while True:
                        message = await client.receive()
                        if message['type'] == 'websocket.disconnect':
                            return
                        await upstream.send(message.get('text') if 'text' in message else message['bytes'])
                async def to_client():
                    async for message in upstream:
                        if isinstance(message, str):
                            await client.send_text(message)
                        else:
                            await client.send_bytes(message)
                tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_client())]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except (ConnectionClosed, InvalidHandshake, OSError, TimeoutError, WebSocketDisconnect):
            pass
        finally:
            try:
                await client.close(code=1001)
            except (RuntimeError, OSError):
                pass
