import asyncio
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from app.extract import extract, resource_title
from app.store import Store
from app.cloud import Cloud, CloudError, TaskFailed
from app.worker import Worker
from app.telegram import Telegram


class ExtractTests(unittest.TestCase):
    def test_resource_title_from_caption(self):
        self.assertEqual(resource_title('战锋·尖峰对决 (2020)\n类型：国产电影\nhttps://www.guangyapan.com/s/a'), '战锋·尖峰对决 (2020)')
        self.assertEqual(resource_title('🎬 片名：测试剧集 第01集\n大小：2GB'), '测试剧集 第01集')
        self.assertEqual(resource_title('https://www.guangyapan.com/s/a\n提取码：abcd'), '')

    def test_text_hidden_and_button_urls(self):
        links = extract('链接 https://www.guangyapan.com/s/abc_123。 提取码：A123',
                        ['https://www.guangyapan.com/s/abc_123', 'https://app.guangyapan.com/s/other?pwd=B456'])
        self.assertEqual([x['share_id'] for x in links], ['abc_123', 'other'])
        self.assertEqual([x['code'] for x in links], ['A123', 'B456'])

    def test_reject_lookalike_and_other_urls(self):
        self.assertFalse(extract(' '.join(['https://www.guangyapan.com.evil/s/abc',
          'https://evil.test/?next=https://www.guangyapan.com/s/abc',
          'https://user@www.guangyapan.com/s/abc', 'https://www.guangyapan.com/activity/foo'])))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.source = {'id': 1, 'target_id': None, 'target_name': None}
        self.link = extract('https://www.guangyapan.com/s/abc')[0]

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_dedupe_and_destination_snapshot(self):
        self.store.set('target', {'id': 'folder1', 'name': '/first'})
        self.store.enqueue(self.link, self.source, 1)
        self.store.set('target', {'id': 'folder2', 'name': '/second'})
        self.store.enqueue(self.link, self.source, 2)
        jobs = self.store.rows('SELECT * FROM jobs')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['target_id'], 'folder1')

    def test_crash_recovery_never_blindly_resubmits(self):
        self.store.enqueue(self.link, self.source, 1)
        self.store.update_job(1, status='submitting')
        self.store.db.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.store.rows('SELECT * FROM jobs')[0]['status'], 'uncertain')

    def test_explicit_root_overrides_default(self):
        self.store.set('target', {'id': 'other', 'name': '/other'})
        self.store.enqueue(self.link, {**self.source, 'target_id': '', 'target_name': '/'}, 1)
        self.assertEqual(self.store.rows('SELECT * FROM jobs')[0]['target_id'], '')

    def test_batch_selection_persists_and_rejects_partial_invalid_save(self):
        self.store.execute("INSERT INTO sources(id,title,enabled,enabled_at) VALUES (1,'A',1,50),(2,'B',0,0)")
        self.store.save_sources([{'id': 1, 'enabled': True, 'target': None},
                                 {'id': 2, 'enabled': True, 'target': {'id': 'folder', 'name': '/电影'}}])
        self.assertEqual(self.store.rows('SELECT enabled_at FROM sources WHERE id=1')[0]['enabled_at'], 50)
        with self.assertRaises(ValueError):
            self.store.save_sources([{'id': 1, 'enabled': False}, {'id': 999, 'enabled': True}])
        self.assertEqual(self.store.rows('SELECT enabled FROM sources WHERE id=1')[0]['enabled'], 1)
        self.store.db.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.store.rows('SELECT target_name FROM sources WHERE id=2')[0]['target_name'], '/电影')

    def test_existing_database_migration_preserves_jobs(self):
        self.store.enqueue(self.link, self.source, 1)
        self.store.execute('ALTER TABLE jobs DROP COLUMN resource_name')
        self.store.db.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.store.rows('SELECT * FROM jobs')[0]['resource_name'], '')
        self.assertEqual(self.store.rows('SELECT * FROM jobs')[0]['share_id'], 'abc')


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cloud = Cloud(self.store)
        self.store.enqueue(extract('https://www.guangyapan.com/s/test')[0],
                           {'id': 1, 'target_id': '', 'target_name': '/'}, 1)

    async def asyncTearDown(self):
        await self.cloud.http.aclose()
        self.store.db.close()
        self.tmp.cleanup()

    def job(self):
        return self.store.rows('SELECT * FROM jobs')[0]

    async def test_cursor_pages_include_all_items(self):
        self.cloud.call = AsyncMock(side_effect=[
          {'list': [{'fileId': 'a'}], 'hasMore': True, 'cursor': 'next'},
          {'list': [{'fileId': 'b'}], 'hasMore': False}])
        rows = await self.cloud.pages('/test', {}, cursor_mode=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(self.cloud.call.call_args_list[1].args[1]['cursor'], 'next')

    async def test_missing_cursor_fails_closed(self):
        self.cloud.call = AsyncMock(return_value={'list': [{'fileId': 'a'}], 'hasMore': True})
        with self.assertRaises(CloudError):
            await self.cloud.pages('/test', {}, cursor_mode=True)

    async def test_folder_res_type(self):
        self.cloud.pages = AsyncMock(return_value=[{'fileId': '1', 'fileName': 'folder', 'resType': 2},
                                                  {'fileId': '2', 'fileName': 'file', 'resType': 1}])
        self.assertEqual(await self.cloud.folders(), [{'id': '1', 'name': 'folder'}])

    async def test_submit_is_not_success(self):
        self.cloud.prepare = AsyncMock(return_value=('share-token', ['a'], '测试影片'))
        self.cloud.submit = AsyncMock(return_value='remote-task')
        worker = Worker(self.store, self.cloud)
        await worker.process(self.job())
        self.assertEqual(self.job()['status'], 'submitted')
        self.assertEqual(self.job()['resource_name'], '测试影片')
        self.cloud.task = AsyncMock(return_value=True)
        await worker.process(self.job())
        self.assertEqual(self.job()['status'], 'success')
        self.cloud.submit.assert_awaited_once()

    async def test_submit_timeout_requires_human_reconciliation(self):
        self.cloud.prepare = AsyncMock(return_value=('share-token', ['a'], '测试影片'))
        self.cloud.submit = AsyncMock(side_effect=httpx.ReadTimeout('timeout'))
        await Worker(self.store, self.cloud).process(self.job())
        self.assertEqual(self.job()['status'], 'uncertain')

    async def test_network_read_retries_are_bounded(self):
        self.cloud.prepare = AsyncMock(side_effect=httpx.ConnectError('offline'))
        worker = Worker(self.store, self.cloud)
        await worker.process(self.job())
        self.assertEqual(self.job()['status'], 'queued')
        await worker.process(self.job())
        await worker.process(self.job())
        self.assertEqual(self.job()['status'], 'failed')

    async def test_progress_failure_does_not_resubmit(self):
        self.store.update_job(1, status='submitted', remote_task='existing')
        self.cloud.task = AsyncMock(side_effect=CloudError('unknown protocol'))
        await Worker(self.store, self.cloud).process(self.job())
        self.assertEqual(self.job()['status'], 'submitted')
        self.assertEqual(self.job()['remote_task'], 'existing')

    async def test_provider_terminal_failure(self):
        self.cloud.call = AsyncMock(return_value={'status': 2, 'detail': {'code': 9}})
        with self.assertRaises(TaskFailed):
            await self.cloud.task('x')

    async def test_token_refresh_is_persisted(self):
        self.store.set('cloud_token', {'access_token': 'old', 'refresh_token': 'old-refresh', 'expires_at': 0})
        self.cloud.raw = AsyncMock(return_value={'access_token': 'new', 'refresh_token': 'new-refresh', 'expires_in': 7200})
        self.assertEqual(await self.cloud.token(), 'new')
        self.assertEqual(self.store.get('cloud_token')['refresh_token'], 'new-refresh')

    async def test_oauth_http_400_pending_is_not_a_failure(self):
        await self.cloud.http.aclose()
        self.cloud.http = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={'error': 'authorization_pending', 'error_code': 4050})))
        self.assertEqual(await self.cloud.poll_qr('ephemeral-device'), 'authorization_pending')
        self.assertIsNone(self.store.get('cloud_token'))

    async def test_share_protocol_payloads(self):
        self.cloud.call = AsyncMock(side_effect=[
            {'accessToken': 'share-access'},
            {'list': [{'fileId': 'file-a'}, {'fileId': 'folder-b', 'resType': 2}], 'hasMore': False},
            {'taskId': 'task-123'}])
        access, ids, title = await self.cloud.prepare('share-1', 'ABCD')
        self.assertEqual(await self.cloud.submit(access, ids, 'my-folder'), 'task-123')
        self.assertEqual(self.cloud.call.call_args_list[0].args, ('/get_share_access_token', {'shareId': 'share-1', 'code': 'ABCD'}))
        self.assertEqual(self.cloud.call.call_args_list[2].args, ('/restore_share',
            {'accessToken': 'share-access', 'fileIds': ['file-a', 'folder-b'], 'parentId': 'my-folder'}))

    async def test_telegram_ignores_history_and_disabled_sources(self):
        self.store.execute("INSERT INTO sources(id,title,enabled,enabled_at) VALUES (5,'channel',1,100)")
        tg = Telegram(self.store, Path(self.tmp.name))
        tg.ready_at = 200
        def event(timestamp, chat=5):
            msg = NS(date=datetime.fromtimestamp(timestamp, timezone.utc), entities=[],
                     reply_markup=NS(rows=[NS(buttons=[NS(url='https://www.guangyapan.com/s/button')])]))
            return NS(chat_id=chat, message=msg, id=9, raw_text='button-only')
        await tg.message(event(150))
        await tg.message(event(250, 6))
        self.assertEqual(len(self.store.rows('SELECT * FROM jobs')), 1)
        await tg.message(event(250))
        self.assertEqual(len(self.store.rows('SELECT * FROM jobs')), 2)


class WebTests(unittest.TestCase):
    def test_job_pagination_sizes_boundaries_and_snapshot(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)):
            with TestClient(main.app) as client:
                headers = {'X-Sbtg-Client': 'web'}
                store = main.app.state.store
                def seed():
                    for i in range(101):
                        store.enqueue({'share_id':str(i),'url':'https://www.guangyapan.com/s/'+str(i),'code':''},
                                      {'id':1,'target_id':None,'target_name':None}, i)
                empty = client.get('/api/job-pages', headers=headers).json()
                self.assertEqual((empty['total'],empty['pages'],empty['items']), (0,1,[]))
                client.portal.call(seed)
                first = client.get('/api/job-pages', headers=headers).json()
                self.assertEqual((first['page_size'],len(first['items']),first['pages']), (20,20,6))
                for size in (10,20,50,100):
                    result = client.get('/api/job-pages?page_size='+str(size), headers=headers).json()
                    self.assertEqual(len(result['items']),size)
                last = client.get('/api/job-pages?page=999', headers=headers).json()
                self.assertEqual((last['page'],len(last['items'])),(6,1))
                client.portal.call(store.enqueue, {'share_id':'new','url':'https://www.guangyapan.com/s/new','code':''},
                                   {'id':1,'target_id':None,'target_name':None}, 102)
                second = client.get('/api/job-pages?page=2&max_id='+str(first['max_id']), headers=headers).json()
                self.assertEqual(second['total'],101)
                self.assertFalse({j['id'] for j in first['items']} & {j['id'] for j in second['items']})
                self.assertEqual(second['items'][0]['id'],81)
                self.assertEqual(client.get('/api/job-pages', headers=headers).json()['total'],102)
                for query in ('page_size=15','page=0','max_id=-1'):
                    self.assertEqual(client.get('/api/job-pages?'+query, headers=headers).status_code,422)

    def test_source_save_and_old_title_backfill_are_read_only_for_cloud(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)):
            with TestClient(main.app) as client:
                headers = {'X-Sbtg-Client': 'web'}
                store = main.app.state.store
                client.portal.call(store.execute, "INSERT INTO sources(id,title) VALUES (1,'A'),(2,'B')")
                result = client.post('/api/sources/save', headers=headers, json={'sources':[
                    {'id':1,'enabled':True}, {'id':2,'enabled':False}]})
                self.assertEqual(result.status_code, 200)
                self.assertEqual([s['enabled'] for s in client.get('/api/sources', headers=headers).json()], [1,0])
                client.portal.call(store.enqueue, extract('https://www.guangyapan.com/s/old')[0],
                                   {'id':1,'target_id':None,'target_name':None}, 1)
                client.portal.call(lambda: store.update_job(1, status='success'))
                main.app.state.cloud.prepare = AsyncMock(return_value=('access', ['file'], '历史电影'))
                main.app.state.cloud.submit = AsyncMock()
                self.assertEqual(client.post('/api/jobs/1/title', headers=headers, json={}).status_code, 200)
                job = client.get('/api/jobs', headers=headers).json()[0]
                self.assertEqual((job['resource_name'],job['status']), ('历史电影','success'))
                main.app.state.cloud.submit.assert_not_awaited()

    def test_api_origin_guard_validation_and_secret_redaction(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)):
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/healthz').status_code, 200)
                self.assertEqual(client.get('/').status_code, 200)
                self.assertEqual(client.get('/api/state').status_code, 403)
                headers = {'X-Sbtg-Client': 'web'}
                self.assertEqual(client.get('/api/state', headers={**headers, 'Origin': 'https://evil.test'}).status_code, 403)
                self.assertEqual(client.post('/api/telegram/config', headers=headers,
                  json={'api_id': -1, 'api_hash': 'bad'}).status_code, 422)
                client.portal.call(main.app.state.store.set, 'telegram', {'api_id': 123, 'api_hash': 'secret-value'})
                client.portal.call(main.app.state.store.set, 'cloud_token', {'access_token': 'secret-token'})
                result = client.get('/api/state', headers=headers)
                self.assertNotIn('secret', result.text)


if __name__ == '__main__':
    unittest.main()
