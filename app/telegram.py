import asyncio
import time
import os
import logging
from collections import OrderedDict
from datetime import datetime, timezone
import socks
from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError, PasswordHashInvalidError, FloodWaitError
from .extract import extract, resource_title
from .proxy import http_proxy, ProxyConfigError


# Telethon may log raw network exceptions and protocol request objects. Keep
# those out of application logs; expose only our fixed, sanitized status text.
TRANSPORT_LOG = logging.getLogger('sbtg2cloud.telegram_transport')
TRANSPORT_LOG.setLevel(logging.CRITICAL + 1)
TRANSPORT_LOG.propagate = False
LISTENER_LOG = logging.getLogger('uvicorn.error.sbtg2cloud.telegram')
LISTENER_LOG.setLevel(logging.INFO)  # The playback servers set uvicorn.error to WARNING.
CHECK_INTERVAL = 60
CHECK_WINDOW = 300
CHECK_LIMIT = 100


def connection_error(error, using_proxy):
    if isinstance(error, ProxyConfigError):
        return str(error)
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return ('HTTP 代理连接 Telegram 超时' if using_proxy else 'Telegram 直连超时') + '，10 秒后重试'
    if isinstance(error, socks.HTTPError):
        return 'HTTP 代理拒绝 CONNECT 请求，请检查认证及代理访问规则；10 秒后重试'
    if using_proxy:
        return '通过 HTTP 代理连接 Telegram 失败，请检查代理可达性、CONNECT 支持及认证；10 秒后重试'
    return 'Telegram 直连失败，请检查网络或配置 TELEGRAM_PROXY_URL；10 秒后重试'


class Telegram:
    def __init__(self, store, directory):
        self.store, self.directory = store, directory
        self.client = None
        self.initialized = False
        self.ready_at = None
        self.state = 'not_configured'
        self.error = ''
        self.qr_url = None
        self.login_task = None
        self.lock = asyncio.Lock()
        self.last_checked = {}
        self.check_resume_at = 0
        self.seen = OrderedDict()

    def listening(self):
        if self.ready_at is None:
            self.ready_at = time.time()
            LISTENER_LOG.info('Telegram listener ready since=%d', self.ready_at)
        self.state = 'connected'
        self.error = ''

    async def ensure(self):
        async with self.lock:
            config = self.store.get('telegram')
            if not config:
                return
            if self.client is None:
                proxy = http_proxy(os.environ.get('TELEGRAM_PROXY_URL'))
                self.client = TelegramClient(str(self.directory / 'telegram'), config['api_id'], config['api_hash'],
                                             auto_reconnect=False, connection_retries=1, catch_up=False,
                                             flood_sleep_threshold=0, proxy=proxy, base_logger=TRANSPORT_LOG)
                self.client.add_event_handler(self.message, events.NewMessage(incoming=True))
                self.client.add_event_handler(self.edited_message, events.MessageEdited(incoming=True))
            connected = self.client.is_connected()
            if connected and self.initialized:
                return
            self.ready_at = None
            self.initialized = False
            self.state = 'connecting'
            async with asyncio.timeout(30):
                if connected:
                    await self.client.disconnect()
                await self.client.connect()
                authorized = await self.client.is_user_authorized()
            self.initialized = True
            if authorized:
                self.listening()
            else:
                self.state = 'login_required'

    async def connect_once(self):
        previous = self.state
        try:
            await self.ensure()
        except Exception as error:
            self.ready_at = None
            self.state = 'proxy_config_error' if isinstance(error, ProxyConfigError) else 'disconnected'
            self.error = connection_error(error, bool(os.environ.get('TELEGRAM_PROXY_URL')))
            if previous != self.state:
                LISTENER_LOG.warning('Telegram listener state=%s error_type=%s', self.state, type(error).__name__)
            return False
        return True

    async def run(self):
        while True:
            if await self.connect_once():
                await self.check_recent()
            await asyncio.sleep(10)

    async def message(self, event):
        self.receive(event.chat_id, event.message, 'live')

    async def edited_message(self, event):
        self.receive(event.chat_id, event.message, 'edit')

    async def check_recent(self):
        client, ready_at = self.client, self.ready_at
        now = time.monotonic()
        if ready_at is None or not client or not client.is_connected() or now < self.check_resume_at:
            return
        sources = self.store.rows('SELECT id,enabled_at FROM sources WHERE enabled=1 ORDER BY id')
        if not sources:
            return
        source = min(sources, key=lambda row: self.last_checked.get(row['id'], float('-inf')))
        chat_id = source['id']
        if now - self.last_checked.get(chat_id, float('-inf')) < CHECK_INTERVAL:
            return
        self.last_checked[chat_id] = now
        cutoff = max(int(ready_at), source['enabled_at'], int(time.time()) - CHECK_WINDOW)
        try:
            messages = await asyncio.wait_for(client.get_messages(
                chat_id, limit=CHECK_LIMIT, reverse=True,
                offset_date=datetime.fromtimestamp(cutoff - 1, timezone.utc)), timeout=8)
            if client is not self.client or ready_at != self.ready_at or not client.is_connected():
                return
            for message in messages:
                self.receive(chat_id, message, 'check', cutoff)
            if len(messages) == CHECK_LIMIT:
                LISTENER_LOG.warning('Telegram check reached limit source=%s limit=%d', chat_id, CHECK_LIMIT)
        except FloodWaitError as error:
            self.check_resume_at = time.monotonic() + max(10, error.seconds)
            LISTENER_LOG.warning('Telegram check paused by Telegram for %d seconds', error.seconds)
        except Exception as error:
            LISTENER_LOG.warning('Telegram check failed source=%s error_type=%s', chat_id, type(error).__name__)

    def receive(self, chat_id, message, via, cutoff=0):
        if self.ready_at is None:
            return
        if not message or getattr(message, 'out', False) or not getattr(message, 'date', None):
            return
        sources = self.store.rows('SELECT * FROM sources WHERE id=? AND enabled=1', (chat_id,))
        if not sources:
            return
        source = sources[0]
        if message.date.timestamp() < max(int(self.ready_at), source['enabled_at'], cutoff):
            return
        text = getattr(message, 'raw_text', '') or ''
        urls = [entity.url for entity in getattr(message, 'entities', None) or [] if getattr(entity, 'url', None)]
        markup = getattr(message, 'reply_markup', None)
        urls.extend(button.url for row in getattr(markup, 'rows', [])
                    for button in row.buttons if getattr(button, 'url', None))
        links = extract(text, urls)
        key = (chat_id, message.id)
        version = (getattr(message, 'edit_date', None) or message.date).timestamp()
        signature = (version, tuple(link['share_id'] for link in links))
        previous = self.seen.get(key)
        if previous and (version < previous[0] or signature == previous):
            return
        title = resource_title(text) if len(links) == 1 else ''
        try:
            queued = sum(self.store.enqueue(link, source, message.id, title) is not None for link in links)
        except Exception as error:
            LISTENER_LOG.warning('Telegram enqueue failed source=%s id=%s via=%s error_type=%s',
                                 chat_id, message.id, via, type(error).__name__)
            return
        self.seen[key] = signature
        self.seen.move_to_end(key)
        if len(self.seen) > 2048:
            self.seen.popitem(last=False)
        LISTENER_LOG.info('Telegram message source=%s id=%s via=%s queued=%d duplicate=%d',
                          chat_id, message.id, via, queued, len(links) - queued)

    async def start_qr(self):
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            try:
                await self.login_task
            except asyncio.CancelledError:
                pass
        if not await self.connect_once():
            raise ValueError(self.error)
        if not self.client:
            raise ValueError('请先填写 Telegram API ID 和 API Hash')
        if await self.client.is_user_authorized():
            self.listening()
            return
        qr = await self.client.qr_login()
        self.qr_url = qr.url
        self.state = 'scan_qr'
        self.error = ''
        self.login_task = asyncio.create_task(self.wait_qr(qr))

    async def wait_qr(self, qr):
        try:
            await qr.wait()
            self.listening()
        except SessionPasswordNeededError:
            self.state = 'password_required'
        except asyncio.TimeoutError:
            self.state = 'qr_expired'
        except Exception:
            self.state = 'login_required'
            self.error = '扫码登录失败，请重新生成二维码'
        finally:
            self.qr_url = None

    async def password(self, password):
        if self.state != 'password_required':
            raise ValueError('当前无需两步验证密码')
        try:
            await self.client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise ValueError('两步验证密码错误') from None
        except FloodWaitError as error:
            raise ValueError(f'Telegram 要求等待 {error.seconds} 秒后重试') from None
        self.listening()

    async def dialogs(self):
        if self.state != 'connected':
            raise ValueError('请先登录 Telegram')
        async for dialog in self.client.iter_dialogs():
            if dialog.is_group or dialog.is_channel:
                self.store.execute('''INSERT INTO sources (id,title) VALUES (?,?)
                  ON CONFLICT(id) DO UPDATE SET title=excluded.title''', (utils.get_peer_id(dialog.entity), dialog.name))

    async def close(self):
        self.ready_at = None
        self.initialized = False
        if self.login_task:
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        if self.client:
            await self.client.disconnect()
