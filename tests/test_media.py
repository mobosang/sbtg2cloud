import asyncio
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from websockets.asyncio.server import serve

from app.cloud import Cloud, CloudError
from app.media import MediaProxy, mapped_path, media_settings
from app.store import Store


CONFIG = {'enabled': True, 'upstream_url': 'http://emby.test:8096',
          'emby_prefix': '/CloudNAS/CloudDrive/光鸭云盘', 'cloud_prefix': '/'}
PATH = CONFIG['emby_prefix'] + '/Media/影片/视频.mkv'
LINK = 'https://node.guangyacdn.com/download/?signature=temporary-test'


class SettingsTests(unittest.TestCase):
    def test_explicit_mapping_preserves_names_and_requires_directory_boundary(self):
        settings = media_settings(CONFIG)
        self.assertEqual(mapped_path(PATH, settings), '/Media/影片/视频.mkv')
        self.assertIsNone(mapped_path(CONFIG['emby_prefix'] + 'other/video.mkv', settings))
        self.assertIsNone(mapped_path('http://other.test/video.mkv', settings))
        for tail in ('/../secret.mkv', '/./secret.mkv', '//secret.mkv', '/bad\x00name.mkv'):
            with self.assertRaises(ValueError):
                mapped_path(CONFIG['emby_prefix'] + tail, settings)
        self.assertEqual(mapped_path(PATH, {**settings, 'cloud_prefix': '/Private'}), '/Private/Media/影片/视频.mkv')

    def test_invalid_upstreams_do_not_echo_credentials(self):
        for url in ('file:///tmp/file', 'http://user:SECRET@emby.test', 'http://emby.test:0',
                    'http://emby.test:65536', 'http://emby.test?token=SECRET',
                    'http://emby.test/#SECRET', 'http://bad host', 'http://emby.test/../admin'):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                media_settings({**CONFIG, 'upstream_url': url})
            self.assertNotIn('SECRET', str(error.exception))
        with self.assertRaises(ValueError):
            media_settings({**CONFIG, 'emby_prefix': '/'})


class MediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.store.set('media_proxy', CONFIG)
        self.store.set('cloud_token', {'access_token': 'cloud-test', 'refresh_token': 'refresh-test',
                                      'expires_at': time.time() + 3600})
        self.cloud = Cloud(self.store)
        self.media = MediaProxy(self.store, self.cloud)
        self.cloud.pages = AsyncMock(side_effect=self.directory)
        self.cloud.download_url = AsyncMock(return_value=(LINK, 120))
        self.requests = []
        await self.media.http.aclose()
        self.media.http = httpx.AsyncClient(transport=httpx.MockTransport(self.upstream), follow_redirects=False)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(self.media.app), base_url='http://proxy.test')
        self.auth = {'X-Emby-Token': 'emby-test', 'User-Agent': 'SenPlayer/Test'}

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.media.close()
        await self.cloud.http.aclose()
        self.store.db.close()
        self.tmp.cleanup()

    def directory(self, endpoint, payload):
        self.assertEqual(endpoint, '/file/get_file_list')
        rows = {'': [{'fileId': 'media', 'fileName': 'Media', 'resType': 2}],
                'media': [{'fileId': 'movie', 'fileName': '影片', 'resType': 2}],
                'movie': [{'fileId': 'video', 'fileName': '视频.mkv', 'resType': 1}]}
        return rows[payload['parentId']]

    def upstream(self, request):
        self.requests.append(request)
        if request.url.path.endswith('/PlaybackInfo'):
            if request.headers.get('x-emby-token') != 'emby-test' and request.url.params.get('api_key') != 'emby-test':
                return httpx.Response(401)
            return httpx.Response(200, json={'MediaSources': [{'Id': 'source', 'Path': PATH}]})
        return httpx.Response(206, headers={'Content-Range': 'bytes 100-103/1000', 'Accept-Ranges': 'bytes',
                                           'Content-Type': 'video/x-matroska', 'Content-Length': '4',
                                           'Set-Cookie': 'test=1', 'Connection': 'keep-alive'},
                              stream=httpx.ByteStream(b'data'))

    async def test_senplayer_original_get_and_head_return_302_without_proxying_video(self):
        for method, uri in [('GET', '/emby/videos/123/original.mkv?MediaSourceId=source'),
                            ('HEAD', '/Videos/123/stream.mkv?Static=true&MediaSourceId=source')]:
            r = await self.client.request(method, uri, headers={**self.auth, 'Range': 'bytes=100-', 'If-Range': 'etag'})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(r.headers['location'], LINK)
            self.assertEqual(r.headers['cache-control'], 'no-store')
            self.assertEqual(r.content, b'')
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(r.url.path.endswith('/PlaybackInfo') for r in self.requests))
        self.assertTrue(all('range' not in r.headers and 'if-range' not in r.headers for r in self.requests))
        self.cloud.download_url.assert_awaited_once_with('video')
        self.assertEqual(self.cloud.pages.await_count, 3)
        self.assertEqual(self.media.status()['redirects'], 2)
        self.assertNotIn('temporary-test', str(self.media.status()))
        self.assertNotIn('cloud-test', str(self.media.status()))

    async def test_each_redirect_checks_emby_authorization_even_when_link_is_cached(self):
        uri = '/Videos/123/original.mkv'
        self.assertEqual((await self.client.get(uri)).status_code, 401)
        self.assertEqual(self.requests, [])
        self.cloud.pages.assert_not_awaited()
        self.assertEqual((await self.client.get(uri, headers=self.auth)).status_code, 302)
        self.assertEqual((await self.client.get(uri, headers={'X-Emby-Token': 'revoked'})).status_code, 401)
        self.cloud.download_url.assert_awaited_once()

    async def test_query_credentials_are_forwarded_only_to_the_emby_upstream(self):
        r = await self.client.get('/Videos/123/original.mkv?api_key=emby-test')
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('emby-test', r.headers['location'])
        self.cloud.download_url.assert_awaited_once_with('video')

    async def test_other_drives_keep_original_range_response_and_headers(self):
        def remote(request):
            if request.url.path.endswith('/PlaybackInfo'):
                return httpx.Response(200, json={'MediaSources': [{'Id': 'source', 'Path': '/Other/video.mkv'}]})
            return self.upstream(request)
        self.media.http = httpx.AsyncClient(transport=httpx.MockTransport(remote))
        r = await self.client.get('/Videos/123/original.mkv', headers={**self.auth, 'Range': 'bytes=100-103'})
        self.assertEqual((r.status_code, r.content), (206, b'data'))
        self.assertEqual(r.headers['content-range'], 'bytes 100-103/1000')
        self.assertNotIn('connection', r.headers)
        self.assertEqual(self.requests[-1].headers['range'], 'bytes=100-103')
        self.cloud.pages.assert_not_awaited()

    async def test_hls_transcoding_subtitles_and_control_requests_pass_through(self):
        for uri in ('/Videos/123/stream.mkv?Static=false', '/Videos/123/stream.mkv',
                    '/Videos/123/master.m3u8', '/Videos/123/stream.m3u8?Static=true',
                    '/Videos/123/original.mkv?SubtitleMethod=Encode', '/Videos/123/Subtitles/1/Stream.srt',
                    '/System/Info/Public'):
            self.assertEqual((await self.client.get(uri, headers=self.auth)).status_code, 206)
        self.cloud.pages.assert_not_awaited()
        self.assertFalse(any(r.url.path.endswith('/PlaybackInfo') for r in self.requests))
        r = await self.client.post('/Sessions/Playing/Progress', headers=self.auth, json={'PositionTicks': 123})
        self.assertEqual(r.status_code, 206)
        self.assertEqual(self.requests[-1].method, 'POST')
        self.assertIn(b'123', self.requests[-1].content)

    async def test_ambiguous_media_source_does_not_select_another_version(self):
        r = await self.client.get('/Videos/123/original.mkv?MediaSourceId=missing', headers=self.auth)
        self.assertEqual(r.status_code, 206)
        self.cloud.pages.assert_not_awaited()

    async def test_simultaneous_seeks_share_one_lookup_and_refresh_after_expiry(self):
        responses = await asyncio.gather(*[self.client.get('/Videos/123/original.mkv', headers=self.auth) for _ in range(4)])
        self.assertEqual([r.status_code for r in responses], [302] * 4)
        self.cloud.download_url.assert_awaited_once()
        self.assertEqual(len(self.requests), 4)
        self.media.links['/Media/影片/视频.mkv'] = (0, 'expired')
        self.assertEqual((await self.client.get('/Videos/123/original.mkv', headers=self.auth)).status_code, 302)
        self.assertEqual(self.cloud.download_url.await_count, 2)
        self.assertEqual(self.cloud.pages.await_count, 3)

    async def test_missing_cached_child_refreshes_only_its_parent(self):
        self.media.cache_owner = 'refresh-test'
        self.media.directories['movie'] = (time.monotonic() + 300, [])
        r = await self.client.get('/Videos/123/original.mkv', headers=self.auth)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.cloud.pages.await_count, 3)

    async def test_account_change_invalidates_all_download_and_directory_caches(self):
        await self.client.get('/Videos/123/original.mkv', headers=self.auth)
        self.store.set('cloud_token', {'access_token': 'other', 'refresh_token': 'other-refresh',
                                      'expires_at': time.time() + 3600})
        self.assertEqual((await self.client.get('/Videos/123/original.mkv', headers=self.auth)).status_code, 302)
        self.assertEqual(self.cloud.download_url.await_count, 2)
        self.assertEqual(self.cloud.pages.await_count, 6)

    async def test_lookup_failure_does_not_silently_stream_through_the_nas(self):
        self.cloud.pages.return_value = []
        self.cloud.pages.side_effect = None
        r = await self.client.get('/Videos/123/original.mkv', headers=self.auth)
        self.assertEqual(r.status_code, 502)
        self.assertIn('路径映射', r.json()['detail'])
        self.cloud.download_url.assert_not_awaited()
        self.assertEqual(len(self.requests), 1)

    async def test_network_failure_does_not_expose_token_or_url(self):
        self.cloud.pages.side_effect = httpx.ReadTimeout('SECRET private-url')
        r = await self.client.get('/Videos/123/original.mkv', headers=self.auth)
        self.assertEqual(r.status_code, 502)
        self.assertNotIn('SECRET', r.text + str(self.media.status()))

    async def test_disabled_proxy_never_contacts_upstream_or_cloud(self):
        self.store.set('media_proxy', {**CONFIG, 'enabled': False})
        self.assertEqual((await self.client.get('/System/Info/Public')).status_code, 503)
        self.assertEqual(self.requests, [])
        self.cloud.pages.assert_not_awaited()

    async def test_optional_listener_uses_the_same_event_loop_and_store(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        with patch.dict('os.environ', {'EMBY_PROXY_LISTEN_PORT': str(port)}):
            await self.media.start()
            for _ in range(100):
                if self.media.status()['listening']:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(self.media.status()['listening'])
            async with httpx.AsyncClient(trust_env=False) as client:
                r = await client.get(f'http://127.0.0.1:{port}/Videos/123/original.mkv', headers=self.auth)
                self.assertEqual(r.status_code, 302)
                self.assertEqual(r.headers['location'], LINK)

    async def test_bad_listener_port_is_reported_without_exposing_environment_value(self):
        with patch.dict('os.environ', {'EMBY_PROXY_LISTEN_PORT': 'SECRET'}):
            await self.media.start()
        self.assertFalse(self.media.status()['listening'])
        self.assertIn('监听失败', self.media.error)
        self.assertNotIn('SECRET', self.media.error)

    async def test_server_origin_is_fixed_and_forwarded_headers_cannot_be_spoofed(self):
        await self.client.get('/other/path?next=http://evil.test', headers={**self.auth, 'X-Forwarded-For': 'evil',
                                                                          'Connection': 'X-Secret', 'X-Secret': 'drop'})
        request = self.requests[-1]
        self.assertEqual(request.url.host, 'emby.test')
        self.assertNotEqual(request.headers['x-forwarded-for'], 'evil')
        self.assertNotIn('x-secret', request.headers)
        self.assertNotIn('x-secret', request.headers.get('connection', '').lower())
        self.assertEqual(request.url.params['next'], 'http://evil.test')

    async def test_websocket_control_messages_relay_without_using_cloud_credentials(self):
        async def echo(remote):
            self.assertEqual(remote.request.headers['X-Emby-Token'], 'emby-test')
            async for message in remote:
                await remote.send(message)
        async with serve(echo, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.store.set('media_proxy', {**CONFIG, 'upstream_url': f'http://127.0.0.1:{port}'})
            incoming, outgoing = asyncio.Queue(), asyncio.Queue()
            scope = {'type': 'websocket', 'asgi': {'version': '3.0'}, 'scheme': 'ws',
                     'path': '/embywebsocket', 'raw_path': b'/embywebsocket', 'query_string': b'',
                     'root_path': '', 'server': ('proxy.test', 80), 'client': ('127.0.0.1', 1234),
                     'headers': [(b'x-emby-token', b'emby-test')], 'subprotocols': []}
            task = asyncio.create_task(self.media.app(scope, incoming.get, outgoing.put))
            try:
                await incoming.put({'type': 'websocket.connect'})
                self.assertEqual((await asyncio.wait_for(outgoing.get(), 3))['type'], 'websocket.accept')
                for kind, value in [('text', 'control'), ('bytes', b'ping')]:
                    await incoming.put({'type': 'websocket.receive', kind: value})
                    message = await asyncio.wait_for(outgoing.get(), 3)
                    self.assertEqual(message[kind], value)
                await incoming.put({'type': 'websocket.disconnect', 'code': 1000})
                await asyncio.wait_for(task, 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.cloud.pages.assert_not_awaited()


class CloudDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_interface_payload_and_safe_redirect_target(self):
        cloud = Cloud(None)
        try:
            cloud.call = AsyncMock(return_value={'signedURL': LINK, 'urlDuration': 21600})
            self.assertEqual(await cloud.download_url('video'), (LINK, 21600))
            cloud.call.assert_awaited_once_with('/get_res_download_url', {'fileId': 'video', 'requestId': ''})
            for url in ('http://node.guangyacdn.com/a', 'https://guangyacdn.com.evil.test/a',
                        'https://user:SECRET@node.guangyacdn.com/a', 'https://node.guangyacdn.com:0/a',
                        'https://node.guangyacdn.com/a\nSECRET', None):
                cloud.call.return_value = {'signedURL': url}
                with self.assertRaises(CloudError) as error:
                    await cloud.download_url('video')
                self.assertNotIn('SECRET', str(error.exception))
        finally:
            await cloud.http.aclose()


class MediaWebTests(unittest.TestCase):
    def test_settings_require_management_guard_persist_and_reject_proxy_loops(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)), patch.dict('os.environ', {}, clear=True):
            with TestClient(main.app) as client:
                headers = {'X-Sbtg-Client': 'web'}
                self.assertEqual(client.post('/api/media/settings', json=CONFIG).status_code, 403)
                self.assertEqual(client.post('/api/media/settings', json=CONFIG, headers=headers).status_code, 200)
                state = client.get('/api/state', headers=headers).json()
                self.assertEqual(state['media_proxy']['emby_prefix'], CONFIG['emby_prefix'])
                self.assertFalse(state['media_proxy']['listening'])
                self.assertEqual(client.post('/api/media/settings', json={**CONFIG, 'upstream_url': 'http://testserver:12359'},
                                             headers=headers).status_code, 400)
                self.assertEqual(client.post('/api/media/settings', json={**CONFIG, 'emby_prefix': '/'},
                                             headers=headers).status_code, 400)
                self.assertEqual(client.portal.call(main.app.state.store.get, 'media_proxy'), CONFIG)
