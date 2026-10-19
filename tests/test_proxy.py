import asyncio
import logging
import os
import tempfile
import base64
import socketserver
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import socks
import httpx
from fastapi.testclient import TestClient
from app.proxy import http_proxy, ProxyConfigError
from app.telegram import Telegram, connection_error, TRANSPORT_LOG
from app.cloud import Cloud, CloudError
from app.store import Store


class ProxyTests(unittest.TestCase):
    def test_real_pysocks_http_connect_and_basic_auth(self):
        received = []
        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(3)
                request = b''
                while b'\r\n\r\n' not in request:
                    part = self.request.recv(4096)
                    if not part:
                        return
                    request += part
                received.append(request)
                self.request.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        with socketserver.TCPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                proxy = http_proxy(f'http://user%40domain:p%40ss%3Aword@127.0.0.1:{server.server_address[1]}')
                sock = socks.socksocket()
                try:
                    sock.set_proxy(*proxy)
                    sock.settimeout(3)
                    sock.connect(('telegram.invalid', 443))
                finally:
                    sock.close()
                self.assertIn(b'CONNECT telegram.invalid:443 HTTP/1.1', received[0])
                self.assertIn(b'Proxy-Authorization: basic ' + base64.b64encode(b'user@domain:p@ss:word'), received[0])
            finally:
                server.shutdown()
                thread.join(timeout=3)

    def test_direct_and_http(self):
        self.assertIsNone(http_proxy(None))
        self.assertIsNone(http_proxy(''))
        self.assertEqual(http_proxy('http://proxy.example:6152'), (socks.HTTP, 'proxy.example', 6152, True, None, None))
        self.assertEqual(http_proxy('http://proxy.example')[2], 8080)
        self.assertEqual(http_proxy('http://[::1]:8080')[1], '::1')

    def test_percent_encoded_credentials(self):
        self.assertEqual(http_proxy('http://user%40domain:p%40ss%3Aword@proxy.example:6152')[4:],
                         ('user@domain', 'p@ss:word'))

    def test_invalid_values_never_echo_credentials(self):
        values = ['socks5://user:SECRET@proxy.example:1080', 'https://proxy.example',
                  'proxy.example:8080', 'http://', 'http://proxy.example:0',
                  'http://proxy.example:65536', 'http://proxy.example:abc', 'http://proxy.example:',
                  'http://bad host:80', 'http://bad_host:80', 'http://999.1.1.1', 'http://[::1',
                  'http://proxy.example/path', 'http://proxy.example?password=SECRET',
                  'http://proxy.example#SECRET', 'http://user@proxy.example',
                  'http://user:@proxy.example', 'http://:SECRET@proxy.example',
                  'http://user:SECRET%ZZ@proxy.example', 'http://user:SECRET%0a@proxy.example',
                  'http://user:SECRET%FF@proxy.example', 'http://user:SECRET@x@proxy.example']
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ProxyConfigError) as caught:
                    http_proxy(value)
                self.assertNotIn('SECRET', str(caught.exception))
                self.assertNotIn(value, str(caught.exception))

    def test_error_messages_are_distinct_and_redacted(self):
        self.assertIn('超时', connection_error(TimeoutError('SECRET'), True))
        self.assertIn('直连', connection_error(OSError('SECRET'), False))
        self.assertIn('CONNECT', connection_error(socks.HTTPError('407 SECRET'), True))
        self.assertNotIn('SECRET', connection_error(OSError('SECRET'), True))
        self.assertFalse(TRANSPORT_LOG.getChild('network').isEnabledFor(logging.ERROR))

    def test_invalid_proxy_keeps_webui_available_without_exposing_password(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)), \
             patch.dict(os.environ, {'HTTPS_PROXY': 'socks5://user:SECRET@proxy.example:1080'}, clear=True):
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/healthz').json(), {'status': 'ok'})
                self.assertEqual(client.get('/').status_code, 200)
                response = client.get('/api/state', headers={'X-Sbtg-Client':'web'})
                self.assertIn('HTTPS_PROXY', response.json()['cloud']['error'])
                self.assertNotIn('SECRET', response.text)


class ProxyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.set('telegram', {'api_id': 123, 'api_hash': 'test-api-hash'})

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()
        self.env.stop()

    def mock_client(self):
        client = Mock()
        client.is_connected.return_value = False
        client.connect = AsyncMock()
        client.is_user_authorized = AsyncMock(return_value=True)
        return client

    async def test_telegram_passes_explicit_proxy_and_preserves_semantics(self):
        for value in ('', 'http://proxy.example:8080', 'http://user:p%40ss@proxy.example:6152'):
            with self.subTest(value=value), patch.dict(os.environ, {'TELEGRAM_PROXY_URL': value}):
                client = self.mock_client()
                tg = Telegram(self.store, Path(self.tmp.name))
                with patch('app.telegram.TelegramClient', return_value=client) as factory:
                    await tg.ensure()
                    options = factory.call_args.kwargs
                    self.assertEqual(options['proxy'], http_proxy(value))
                    self.assertFalse(options['catch_up'])
                    self.assertFalse(options['auto_reconnect'])
                    self.assertEqual(options['connection_retries'], 1)
                    self.assertEqual(tg.state, 'connected')
                    client.sign_in.assert_not_called()

    async def test_telegram_does_not_inherit_http_proxy(self):
        os.environ['HTTPS_PROXY'] = 'http://proxy.example:8080'
        with patch('app.telegram.TelegramClient', return_value=self.mock_client()) as factory:
            await Telegram(self.store, Path(self.tmp.name)).ensure()
            self.assertIsNone(factory.call_args.kwargs['proxy'])

    async def test_bad_proxy_keeps_session_and_configuration(self):
        os.environ['TELEGRAM_PROXY_URL'] = 'socks5://user:SECRET@proxy.example:1080'
        session = Path(self.tmp.name) / 'telegram.session'
        session.write_bytes(b'untouched-session-fixture')
        tg = Telegram(self.store, Path(self.tmp.name))
        with patch('app.telegram.TelegramClient') as factory:
            self.assertFalse(await tg.connect_once())
            factory.assert_not_called()
        self.assertEqual(tg.state, 'proxy_config_error')
        self.assertNotIn('SECRET', tg.error)
        self.assertEqual(session.read_bytes(), b'untouched-session-fixture')
        self.assertEqual(self.store.get('telegram')['api_hash'], 'test-api-hash')

    async def test_connect_failure_retries_without_recreating_client(self):
        os.environ['TELEGRAM_PROXY_URL'] = 'http://user:SECRET@proxy.example:8080'
        client = self.mock_client()
        client.connect.side_effect = [TimeoutError('SECRET'), None]
        tg = Telegram(self.store, Path(self.tmp.name))
        with patch('app.telegram.TelegramClient', return_value=client) as factory:
            self.assertFalse(await tg.connect_once())
            self.assertEqual(tg.state, 'disconnected')
            self.assertIsNone(tg.ready_at)
            self.assertIn('超时', tg.error)
            self.assertNotIn('SECRET', tg.error)
            self.assertTrue(await tg.connect_once())
            factory.assert_called_once()
            self.assertEqual(client.connect.await_count, 2)
        self.assertEqual(tg.state, 'connected')
        self.assertEqual(tg.error, '')

    async def test_supervisor_keeps_ten_second_retry_interval(self):
        tg = Telegram(self.store, Path(self.tmp.name))
        tg.connect_once = AsyncMock(return_value=False)
        with patch('app.telegram.asyncio.sleep', new=AsyncMock(side_effect=asyncio.CancelledError)) as sleep:
            with self.assertRaises(asyncio.CancelledError):
                await tg.run()
            sleep.assert_awaited_once_with(10)

    async def test_httpx_uses_environment_proxy_and_exact_no_proxy(self):
        os.environ.update(HTTPS_PROXY='http://proxy.example:8080', NO_PROXY='localhost,api.guangyapan.com')
        cloud = Cloud(self.store)
        try:
            # Inspect transport selection without sending requests or using credentials.
            direct = cloud.http._transport
            self.assertIsNot(cloud.http._transport_for_url(httpx.URL('https://account.guangyapan.com')), direct)
            self.assertIs(cloud.http._transport_for_url(httpx.URL('https://api.guangyapan.com')), direct)
            self.assertIs(cloud.http._transport_for_url(httpx.URL('http://localhost')), direct)
        finally:
            await cloud.http.aclose()

    async def test_invalid_http_proxy_never_silently_falls_back(self):
        os.environ['HTTPS_PROXY'] = 'socks5://user:SECRET@proxy.example:1080'
        cloud = Cloud(self.store)
        try:
            self.assertNotIn('SECRET', cloud.login_error)
            with patch.object(cloud.http, 'post', new=AsyncMock()) as request:
                with self.assertRaises(CloudError):
                    await cloud.raw('https://api.guangyapan.com/test', {})
                request.assert_not_awaited()
        finally:
            await cloud.http.aclose()
