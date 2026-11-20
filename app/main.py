import asyncio
import base64
import io
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from typing import Annotated

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cloud import Cloud, CloudError
from .store import BYTES_PER_GB, Store
from .extract import extract
from .telegram import Telegram
from .worker import Worker
from .cleanup import history_preview, queue_history
from .directory_clear import DirectoryClear
from .media import MediaProxy, media_settings

DATA = Path(os.environ.get('DATA_DIR', './data'))
STATIC = Path(__file__).parent / 'static'


def qr_image(url):
    if not url:
        return None
    buffer = io.BytesIO()
    qrcode.make(url).save(buffer, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()


@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)
    store = Store(DATA)
    cloud = Cloud(store)
    tg = Telegram(store, DATA)
    app.state.store, app.state.cloud, app.state.tg = store, cloud, tg
    app.state.directory_clear = DirectoryClear(store, cloud)
    app.state.cloud_qr = {'state': 'idle'}
    app.state.cloud_login_task = None
    app.state.worker = Worker(store, cloud)
    app.state.media = MediaProxy(store, cloud)
    app.state.media_secondary = MediaProxy(store, cloud, secondary=True, cache_lock=app.state.media.cache_lock)
    await app.state.media.start()
    await app.state.media_secondary.start()
    tasks = [asyncio.create_task(tg.run()), asyncio.create_task(app.state.worker.run())]
    yield
    if app.state.cloud_login_task:
        tasks.append(app.state.cloud_login_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await tg.close()
    await asyncio.gather(app.state.media.close(), app.state.media_secondary.close())
    await cloud.http.aclose()
    store.db.close()


app = FastAPI(title='TG → 光鸭', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/static', StaticFiles(directory=STATIC), name='static')


@app.middleware('http')
async def local_api_guard(request: Request, call_next):
    if request.url.path.startswith('/api/'):
        origin = request.headers.get('origin')
        if (request.headers.get('x-sbtg-client') != 'web'
            or request.headers.get('sec-fetch-site') == 'cross-site'
            or (origin and urlsplit(origin).netloc != request.headers.get('host'))):
            return JSONResponse({'detail': '仅允许从本管理页面访问 API'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.exception_handler(CloudError)
async def cloud_exception(request, error):
    return JSONResponse({'detail': str(error)}, status_code=400)


@app.exception_handler(ValueError)
async def value_exception(request, error):
    return JSONResponse({'detail': str(error)}, status_code=400)


@app.exception_handler(httpx.HTTPError)
async def http_exception(request, error):
    if isinstance(error, httpx.ProxyError):
        return JSONResponse({'detail': 'HTTP 代理连接光鸭失败，请检查代理认证和 CONNECT 支持'}, status_code=502)
    return JSONResponse({'detail': '连接光鸭服务失败，请稍后重试'}, status_code=502)


@app.get('/')
async def index():
    return FileResponse(STATIC / 'index.html')


@app.get('/healthz')
async def health():
    return {'status': 'ok'}


@app.get('/api/state')
async def state():
    store, tg, cloud = app.state.store, app.state.tg, app.state.cloud
    return {
        'telegram': {'state': tg.state, 'error': tg.error, 'configured': bool(store.get('telegram')),
                     'qr': qr_image(tg.qr_url)},
        'cloud': {'configured': bool(store.get('cloud_token')), 'error': cloud.login_error, **app.state.cloud_qr},
        'target': store.get('target', {'id': '', 'name': '/'}),
        'share_size_limit_gb': store.get('share_size_limit_gb', 200),
        'daily_transfer_quota': store.transfer_quota_status(),
        'auto_cleanup': bool(store.get('auto_cleanup', False)),
        'cleanup_minutes': store.get('cleanup_minutes', 60),
        'cleanup_interval_seconds': store.get('cleanup_interval_seconds', 30),
        'cleanup_queue': {r['cleanup_state']: r['n'] for r in store.rows("""SELECT cleanup_state,COUNT(*) n
            FROM jobs WHERE status='success' AND cleanup_enabled=1 GROUP BY cleanup_state""")},
        'cleanup_progress': store.cleanup_progress(app.state.worker.current_cleanup_id),
        'media_proxy': app.state.media.status(),
        'media_proxy_secondary': app.state.media_secondary.status(),
        'directory_clear': app.state.directory_clear.latest(),
        'counts': {r['status']: r['n'] for r in store.rows('SELECT status,COUNT(*) n FROM jobs GROUP BY status')},
    }


class Credentials(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(pattern=r'^[a-fA-F0-9]{32}$')


class MediaSettings(BaseModel):
    enabled: bool = Field(strict=True)
    upstream_url: str = Field(default='', max_length=2048)
    emby_prefix: str = Field(default='', max_length=4096)
    cloud_prefix: str = Field(default='/', max_length=4096)


def set_media_settings(value, request, proxy):
    settings = media_settings(value.model_dump())
    parsed = urlsplit(settings['upstream_url'])
    if (settings['enabled'] and parsed.hostname in (request.url.hostname, 'localhost', '127.0.0.1', '::1', 'sbtg2cloud')
        and str(parsed.port or (443 if parsed.scheme == 'https' else 80)) in (
            str(request.url.port or 80), os.environ.get('EMBY_PROXY_PORT', '12359'),
            os.environ.get('EMBY_PROXY_LISTEN_PORT', ''), os.environ.get('EMBY_PROXY_SECONDARY_PORT', '12360'),
            os.environ.get('EMBY_PROXY_SECONDARY_LISTEN_PORT', ''), '8080')):
        raise HTTPException(400, '上游地址不可指向 sbtg2cloud 自身的管理或播放端口')
    app.state.store.set(proxy.setting_key, settings)
    proxy.error = ''
    return {'ok': True}


@app.post('/api/media/settings')
async def save_media_settings(value: MediaSettings, request: Request):
    return set_media_settings(value, request, app.state.media)


@app.post('/api/media/secondary/settings')
async def save_secondary_media_settings(value: MediaSettings, request: Request):
    return set_media_settings(value, request, app.state.media_secondary)


@app.get('/api/media/cache')
async def media_cache(page: int = Query(1, ge=1)):
    return app.state.store.media_cache_page(page)


@app.post('/api/media/cache/clear')
async def clear_media_cache():
    async with app.state.media.cache_lock:
        count = app.state.store.execute('DELETE FROM media_links').rowcount
        return {'cleared': count}


class ShareLimit(BaseModel):
    gb: int = Field(ge=1, strict=True)


@app.post('/api/shares/limit')
async def share_limit(value: ShareLimit):
    app.state.store.set('share_size_limit_gb', value.gb)
    return {'ok': True}


@app.post('/api/shares/daily-limit')
async def daily_share_limit(value: ShareLimit):
    if value.gb > (2**63 - 1) // BYTES_PER_GB:
        raise HTTPException(422, '每日转存上限超出支持范围')
    app.state.store.set_daily_transfer_limit(value.gb)
    return {'ok': True}


class CleanupSettings(BaseModel):
    enabled: bool


class CleanupDelay(BaseModel):
    minutes: int = Field(ge=5, le=600, strict=True)


class CleanupInterval(BaseModel):
    seconds: int = Field(ge=5, le=600, strict=True)


@app.post('/api/cleanup/interval')
async def cleanup_interval(value: CleanupInterval):
    app.state.store.set('cleanup_interval_seconds', value.seconds)
    return {'ok': True}


@app.post('/api/cleanup/delay')
async def cleanup_delay(value: CleanupDelay):
    app.state.store.set('cleanup_minutes', value.minutes)
    return {'ok': True}


@app.get('/api/clear/targets')
async def clear_targets():
    return app.state.directory_clear.targets()


class ClearTarget(BaseModel):
    id: str = Field(min_length=1, max_length=200)


@app.post('/api/clear/preview')
async def clear_preview(value: ClearTarget):
    return await app.state.directory_clear.preview(value.id)


@app.post('/api/clear/{token}/confirm')
async def clear_confirm(token: str):
    app.state.directory_clear.confirm(token)
    return {'ok': True}


@app.post('/api/cleanup/settings')
async def cleanup_settings(value: CleanupSettings):
    app.state.store.set('auto_cleanup', value.enabled)
    return {'ok': True}


@app.get('/api/cleanup/history')
async def cleanup_history(page: int = Query(1, ge=1)):
    return await history_preview(app.state.store, app.state.cloud, page)


class HistoryTarget(BaseModel):
    file_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=2048)


@app.post('/api/cleanup/history/{job_id}')
async def cleanup_history_target(job_id: int, value: HistoryTarget):
    await queue_history(app.state.store, app.state.cloud, job_id, value.file_id, value.name)
    return {'ok': True}


@app.post('/api/telegram/config')
async def telegram_config(value: Credentials):
    tg = app.state.tg
    if tg.state == 'connected' or tg.state in ('scan_qr', 'password_required'):
        raise HTTPException(409, '已登录或正在登录，请先退出账号再修改应用凭据')
    async with tg.lock:
        await tg.close()
        tg.client = None
        app.state.store.set('telegram', value.model_dump())
    return {'ok': True}


@app.post('/api/telegram/qr')
async def telegram_qr():
    try:
        await app.state.tg.start_qr()
    except (ValueError, CloudError):
        raise
    except Exception:
        raise HTTPException(400, 'Telegram 登录请求失败，请核对应用凭据与网络') from None
    return {'ok': True}


class Password(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@app.post('/api/telegram/password')
async def telegram_password(value: Password):
    await app.state.tg.password(value.password)
    return {'ok': True}


@app.post('/api/telegram/logout')
async def telegram_logout():
    tg = app.state.tg
    async with tg.lock:
        tg.ready_at = None
        await tg.close()
        # Reconnect only to revoke this project's Telegram session, not other devices.
        if tg.client:
            await tg.client.connect()
            await tg.client.log_out()
        tg.client = None
        tg.ready_at = None
        tg.state = 'login_required'
        app.state.store.execute('UPDATE sources SET enabled=0')
    return {'ok': True}


async def poll_cloud(device, expires, interval):
    try:
        deadline = time.time() + expires
        while time.time() < deadline:
            await asyncio.sleep(interval)
            try:
                result = await app.state.cloud.poll_qr(device)
            except httpx.HTTPError:
                continue
            if result == 'authorized':
                app.state.cloud_qr = {'state': 'authorized'}
                return
            if result == 'slow_down':
                interval += 5
        app.state.cloud_qr = {'state': 'expired'}
    except CloudError as error:
        app.state.cloud_qr = {'state': 'failed', 'error': str(error)}
    except Exception:
        app.state.cloud_qr = {'state': 'failed', 'error': '扫码登录异常，请重新生成二维码'}


@app.post('/api/cloud/qr')
async def cloud_qr():
    previous = app.state.cloud_login_task
    if previous:
        previous.cancel()
        await asyncio.gather(previous, return_exceptions=True)
    data = await app.state.cloud.qr()
    if not data.get('verification_uri_complete') or not data.get('device_code'):
        raise CloudError('二维码返回格式异常')
    app.state.cloud_qr = {'state': 'scan_qr', 'qr': qr_image(data['verification_uri_complete'])}
    app.state.cloud_login_task = asyncio.create_task(poll_cloud(data['device_code'], min(int(data.get('expires_in', 300)), 900), max(int(data.get('interval', 5)), 2)))
    return {'ok': True}


@app.get('/api/folders')
async def folders(parent: str = ''):
    return await app.state.cloud.folders(parent)


class Target(BaseModel):
    id: str = Field(max_length=200)
    name: str = Field(min_length=1, max_length=2048)


@app.post('/api/target')
async def target(value: Target):
    app.state.store.set('target', value.model_dump())
    return {'ok': True}


@app.post('/api/sources/refresh')
async def refresh_sources():
    try:
        await app.state.tg.dialogs()
    except ValueError:
        raise
    except Exception:
        raise HTTPException(400, '获取聊天列表失败，请稍后重试') from None
    return {'ok': True}


@app.get('/api/sources')
async def sources():
    return app.state.store.rows('SELECT * FROM sources ORDER BY enabled DESC,title')


class SourceUpdate(BaseModel):
    enabled: bool
    target: Target | None = None


class SourceSelection(SourceUpdate):
    id: int


class SourceBatch(BaseModel):
    sources: list[SourceSelection] = Field(max_length=10000)


@app.post('/api/sources/save')
async def save_sources(value: SourceBatch):
    app.state.store.save_sources([source.model_dump() for source in value.sources])
    return {'ok': True}


@app.post('/api/sources/{source_id}')
async def source(source_id: int, value: SourceUpdate):
    store = app.state.store
    rows = store.rows('SELECT * FROM sources WHERE id=?', (source_id,))
    if not rows:
        raise HTTPException(404, '来源不存在')
    enabled_at = int(time.time()) if value.enabled and not rows[0]['enabled'] else rows[0]['enabled_at']
    store.execute('UPDATE sources SET enabled=?,target_id=?,target_name=?,enabled_at=? WHERE id=?',
                  (value.enabled, value.target.id if value.target else None, value.target.name if value.target else None, enabled_at, source_id))
    return {'ok': True}


class ManualShare(BaseModel):
    text: str = Field(min_length=1, max_length=4096)


@app.post('/api/jobs/manual')
async def manual_share(value: ManualShare):
    links = extract(value.text)
    if len(links) != 1:
        raise HTTPException(400, '请输入一个有效的光鸭云盘分享链接，可附提取码；不支持中转站链接')
    store = app.state.store
    job_id = store.enqueue(links[0], {'id': 0, 'target_id': None, 'target_name': None}, 0)
    if job_id is None:
        job = store.rows('SELECT id,status FROM jobs WHERE share_id=?', (links[0]['share_id'],))[0]
        return {'created': False, 'job_id': job['id'], 'status': job['status']}
    return {'created': True, 'job_id': job_id, 'status': 'queued'}


@app.get('/api/jobs')
async def jobs(before: int = 0):
    items = app.state.store.rows('''SELECT j.*,CASE WHEN j.source_id=0 THEN '手动提交' ELSE s.title END source_title
      FROM jobs j LEFT JOIN sources s ON s.id=j.source_id
      WHERE (?=0 OR j.id<?) ORDER BY j.id DESC LIMIT 100''', (before, before))
    for item in items:
        item.pop('cleanup_manifest', None)
        item.pop('cleanup_plan', None)
    return items


@app.get('/api/job-pages')
async def job_pages(page: int = Query(1, ge=1), page_size: int = Query(20),
                    max_id: int | None = Query(None, ge=0), status_filter: str = 'all'):
    if page_size not in (10, 20, 50, 100):
        raise HTTPException(422, '每页数量只支持 10、20、50、100')
    store = app.state.store
    filters = {'all': (), 'queued': ('queued',), 'attention': ('failed', 'uncertain'), 'discarded': ('discarded',),
               'cleanup': ('success',)}
    if status_filter not in filters:
        raise HTTPException(422, '不支持的记录筛选条件')
    statuses = filters[status_filter]
    condition = ' AND j.status IN (' + ','.join('?' for _ in statuses) + ')' if statuses else ''
    if status_filter == 'cleanup':
        condition += " AND j.cleanup_enabled=1 AND j.cleanup_state IN ('pending','deleting','submitted')"
    if max_id is None:
        max_id = store.rows('SELECT COALESCE(MAX(id),0) n FROM jobs')[0]['n']
    total = store.rows('SELECT COUNT(*) n FROM jobs j WHERE j.id<=?' + condition, (max_id, *statuses))[0]['n']
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    items = store.rows('''SELECT j.*,CASE WHEN j.source_id=0 THEN '手动提交' ELSE s.title END source_title
      FROM jobs j LEFT JOIN sources s ON s.id=j.source_id
      WHERE j.id<=?''' + condition + ' ORDER BY j.id DESC LIMIT ? OFFSET ?', (max_id, *statuses, page_size, (page - 1) * page_size))
    for item in items:
        item.pop('cleanup_manifest', None)
        item.pop('cleanup_plan', None)
    return {'items': items, 'page': page, 'page_size': page_size, 'pages': pages, 'total': total, 'max_id': max_id,
            'status_filter': status_filter,
            'duplicates': store.rows("""SELECT d.job_id,d.created,j.status,d.source_id=0 AS manual,
                COALESCE(NULLIF(d.title,''),NULLIF(j.resource_name,''),j.share_id) title,
                CASE WHEN d.source_id=0 THEN '手动提交' ELSE s.title END source_title
                FROM duplicate_shares d JOIN jobs j ON j.id=d.job_id
                LEFT JOIN sources s ON s.id=d.source_id ORDER BY d.id DESC LIMIT 20""")}


@app.post('/api/jobs/{job_id}/title')
async def fill_title(job_id: int):
    store = app.state.store
    rows = store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not rows:
        raise HTTPException(404, '记录不存在')
    job = rows[0]
    if job['status'] == 'discarded':
        raise HTTPException(409, '已丢弃的任务不再读取分享')
    if not job['resource_name']:
        _, _, title = await app.state.cloud.prepare(job['share_id'], job['code'])
        if not title:
            raise HTTPException(400, '分享没有可用的资源名称')
        store.update_job(job_id, resource_name=title)
    return {'ok': True}


class Retry(BaseModel):
    code: str = Field(default='', max_length=32)
    confirm_uncertain: bool = False


class JobBatch(BaseModel):
    ids: list[Annotated[int, Field(strict=True, ge=1)]] = Field(min_length=1, max_length=100000)


class BulkRetry(JobBatch):
    confirm_uncertain: bool = Field(default=False, strict=True)


@app.get('/api/jobs/retry-preview')
@app.get('/api/jobs/discard-preview')
async def job_action_preview():
    jobs = app.state.store.actionable_jobs()
    return {'ids': [job['id'] for job in jobs], 'count': len(jobs),
            'uncertain': sum(job['status'] == 'uncertain' for job in jobs)}


@app.post('/api/jobs/discard-batch')
async def discard_batch(value: JobBatch):
    count = app.state.store.discard_jobs(value.ids)
    return {'discarded': count, 'skipped': len(set(value.ids)) - count}


@app.post('/api/jobs/retry-batch')
async def retry_batch(value: BulkRetry):
    try:
        count = app.state.store.retry_jobs(value.ids, value.confirm_uncertain)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    return {'retried': count, 'skipped': len(set(value.ids)) - count}


@app.post('/api/jobs/{job_id}/discard')
async def discard(job_id: int):
    store = app.state.store
    rows = store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not rows:
        raise HTTPException(404, '记录不存在')
    job = rows[0]
    if job['status'] == 'discarded':
        return {'ok': True}
    # Preserve share_id and all receipts: discarded links stay globally deduplicated.
    if not store.discard_jobs([job_id]):
        raise HTTPException(409, '只可丢弃失败或待核对且没有进行中清理操作的任务')
    return {'ok': True}


@app.post('/api/jobs/{job_id}/retry')
async def retry(job_id: int, value: Retry):
    try:
        count = app.state.store.retry_jobs([job_id], value.confirm_uncertain, value.code)
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    if not count:
        raise HTTPException(409, '只可重试失败或待核对且没有进行中清理操作的任务')
    return {'ok': True}
