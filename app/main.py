import asyncio
import base64
import io
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import qrcode
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cloud import Cloud, CloudError
from .store import Store
from .telegram import Telegram
from .worker import Worker

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
    app.state.cloud_qr = {'state': 'idle'}
    app.state.cloud_login_task = None
    tasks = [asyncio.create_task(tg.run()), asyncio.create_task(Worker(store, cloud).run())]
    yield
    if app.state.cloud_login_task:
        tasks.append(app.state.cloud_login_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await tg.close()
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
        'counts': {r['status']: r['n'] for r in store.rows('SELECT status,COUNT(*) n FROM jobs GROUP BY status')},
    }


class Credentials(BaseModel):
    api_id: int = Field(gt=0)
    api_hash: str = Field(pattern=r'^[a-fA-F0-9]{32}$')


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


@app.get('/api/jobs')
async def jobs(before: int = 0):
    return app.state.store.rows('''SELECT j.*,s.title source_title FROM jobs j LEFT JOIN sources s ON s.id=j.source_id
      WHERE (?=0 OR j.id<?) ORDER BY j.id DESC LIMIT 100''', (before, before))


@app.get('/api/job-pages')
async def job_pages(page: int = Query(1, ge=1), page_size: int = Query(20),
                    max_id: int | None = Query(None, ge=0)):
    if page_size not in (10, 20, 50, 100):
        raise HTTPException(422, '每页数量只支持 10、20、50、100')
    store = app.state.store
    if max_id is None:
        max_id = store.rows('SELECT COALESCE(MAX(id),0) n FROM jobs')[0]['n']
    total = store.rows('SELECT COUNT(*) n FROM jobs WHERE id<=?', (max_id,))[0]['n']
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    items = store.rows('''SELECT j.*,s.title source_title FROM jobs j LEFT JOIN sources s ON s.id=j.source_id
      WHERE j.id<=? ORDER BY j.id DESC LIMIT ? OFFSET ?''', (max_id, page_size, (page - 1) * page_size))
    return {'items': items, 'page': page, 'page_size': page_size, 'pages': pages, 'total': total, 'max_id': max_id}


@app.post('/api/jobs/{job_id}/title')
async def fill_title(job_id: int):
    store = app.state.store
    rows = store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not rows:
        raise HTTPException(404, '记录不存在')
    job = rows[0]
    if not job['resource_name']:
        _, _, title = await app.state.cloud.prepare(job['share_id'], job['code'])
        if not title:
            raise HTTPException(400, '分享没有可用的资源名称')
        store.update_job(job_id, resource_name=title)
    return {'ok': True}


class Retry(BaseModel):
    code: str = Field(default='', max_length=32)
    confirm_uncertain: bool = False


@app.post('/api/jobs/{job_id}/retry')
async def retry(job_id: int, value: Retry):
    store = app.state.store
    rows = store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))
    if not rows or rows[0]['status'] not in ('failed', 'uncertain'):
        raise HTTPException(409, '只可重试失败或结果不明的任务')
    if rows[0]['status'] == 'uncertain' and not value.confirm_uncertain:
        raise HTTPException(409, '请先核对云盘，确认尚未转存')
    store.update_job(job_id, status='queued', attempts=0, next_at=0, remote_task=None, error='', code=value.code or rows[0]['code'])
    return {'ok': True}
