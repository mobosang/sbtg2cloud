import json
import tempfile
import asyncio
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from app.cloud import Cloud, CloudError, TaskFailed, API
from app.cleanup import history_preview, queue_history, transferred_targets, file_spec
from app.store import Store
from app.worker import Worker


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cloud = Cloud(self.store)
        self.worker = Worker(self.store, self.cloud)
        self.source = {'id': 1, 'target_id': 'parent', 'target_name': '/Media'}

    async def asyncTearDown(self):
        await self.cloud.http.aclose()
        self.store.db.close()
        self.tmp.cleanup()

    def enqueue(self, key='one'):
        self.store.enqueue({'share_id': key, 'url': 'https://www.guangyapan.com/s/'+key, 'code': ''}, self.source, 1, 'Example (2026)')

    def job(self):
        return self.store.rows('SELECT * FROM jobs WHERE id=1')[0]

    def due(self):
        self.store.set('auto_cleanup', True)
        self.enqueue()
        self.store.update_job(1, status='success', completed_at=time.time()-3601,
                              cleanup_folder='own-folder', cleanup_name='sbtg-test',
                              cleanup_manifest='[]', cleanup_state='pending')
        self.cloud.cleanup_manifest = AsyncMock(return_value='[]')
        self.cloud.delete_cleanup_folder = AsyncMock(return_value='delete-task')

    async def test_default_and_legacy_are_never_scheduled(self):
        self.enqueue()
        self.store.update_job(1, status='success')
        self.store.set('auto_cleanup', True)
        self.enqueue('two')
        self.assertEqual(self.job()['cleanup_enabled'], 0)
        self.assertIsNone(self.job()['completed_at'])
        self.cloud.delete_cleanup_folder = AsyncMock()
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_not_awaited()
        self.assertEqual(self.store.rows('SELECT cleanup_enabled FROM jobs WHERE id=2')[0]['cleanup_enabled'], 1)

    async def test_direct_destination_and_success_clock_not_submission_or_updated(self):
        self.store.set('auto_cleanup', True)
        self.enqueue()
        self.cloud.prepare = AsyncMock(return_value=('access', ['source-file'], 'Title', [{'fileId':'source-file','fileName':'movie','resType':1,'fileSize':10}]))
        self.cloud.create_cleanup_folder = AsyncMock(return_value='own-folder')
        self.cloud.pages = AsyncMock(side_effect=[[], [{'fileId':'owned','fileName':'movie','resType':1,'fileSize':10}], [{'fileId':'owned','fileName':'movie','resType':1,'fileSize':10}]])
        self.cloud.submit = AsyncMock(return_value='transfer-task')
        self.cloud.task = AsyncMock(return_value=True)
        await self.worker.process(self.job())
        self.cloud.submit.assert_awaited_once_with('access', ['source-file'], 'parent')
        self.assertIsNone(self.job()['completed_at'])
        with patch('app.worker.time.time', return_value=10000):
            await self.worker.process(self.job())
        self.assertEqual(self.job()['completed_at'], 10000)
        self.store.update_job(1, resource_name='New title')
        self.assertEqual(self.job()['completed_at'], 10000)
        self.assertEqual(self.job()['cleanup_state'], 'pending')
        self.assertEqual(self.job()['cleanup_folder'], '')
        self.cloud.create_cleanup_folder.assert_not_awaited()
        self.cloud.pages = AsyncMock(return_value=[{'fileId':'owned','fileName':'movie','resType':1,'fileSize':10}])
        self.cloud.delete_cleanup_targets = AsyncMock(return_value='delete-task')
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_targets.assert_awaited_once_with(['owned'])

    async def test_direct_mapping_excludes_existing_and_unrelated_resources(self):
        source={'fileId':'source','fileName':'movie','resType':1,'fileSize':10}
        new={**source,'fileId':'owned'}
        unrelated={**source,'fileId':'unrelated','fileName':'other'}
        plan={'source':[file_spec(source)],'before':[]}
        self.assertEqual([x['id'] for x in transferred_targets(plan,[new,unrelated])],['owned'])
        with self.assertRaises(ValueError):
            transferred_targets({**plan,'before':[file_spec(new)]},[new])
        with self.assertRaises(ValueError):
            transferred_targets(plan,[new,{**new,'fileId':'duplicate'}])

    async def test_age_boundary_and_restart_poll_never_resubmits(self):
        self.due()
        self.store.update_job(1, completed_at=10000)
        with patch('app.worker.time.time', return_value=13599):
            await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_not_awaited()
        with patch('app.worker.time.time', return_value=13600):
            await self.worker.cleanup_due()
        self.assertEqual(self.job()['cleanup_state'], 'submitted')
        self.store.db.close()
        self.store = Store(self.tmp.name)
        self.worker = Worker(self.store, self.cloud)
        self.cloud.task = AsyncMock(return_value=True)
        await self.worker.cleanup_due()
        self.assertEqual(self.job()['cleanup_state'], 'done')
        self.assertEqual(self.job()['status'], 'success')
        self.cloud.delete_cleanup_folder.assert_awaited_once_with('own-folder')

    async def test_pause_blocks_due_delete_and_resume_uses_original_time(self):
        self.due()
        self.store.set('auto_cleanup', False)
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_not_awaited()
        self.store.set('auto_cleanup', True)
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_awaited_once()

    async def test_pause_during_preflight(self):
        self.due()
        async def checking(*args):
            self.store.set('auto_cleanup', False)
            return '[]'
        self.cloud.cleanup_manifest.side_effect = checking
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_not_awaited()

    async def test_increased_delay_during_preflight_stops_delete(self):
        self.due()
        async def checking(*args):
            self.store.set('cleanup_minutes',600)
            return '[]'
        self.cloud.cleanup_manifest.side_effect=checking
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_not_awaited()

    async def test_due_cleanup_accepts_reduced_snapshot_without_replacing_it(self):
        self.due()
        before = [['own-folder','one',1,'ep1',10,None,None,None],
                  ['own-folder','two',1,'ep2',20,None,None,None]]
        original = json.dumps([json.dumps(before)])
        self.store.update_job(1, cleanup_targets=json.dumps([{'id':'own-folder','name':'series'}]),
                              cleanup_manifest=original)
        self.cloud.pages = AsyncMock(return_value=[{'fileId':'own-folder','fileName':'series','resType':2}])
        self.cloud.cleanup_manifest = AsyncMock(return_value=json.dumps(before[1:]))
        self.cloud.delete_cleanup_targets = AsyncMock(return_value='delete-task')
        await self.worker.cleanup_due()
        self.assertEqual(self.job()['cleanup_state'], 'submitted')
        self.assertEqual(self.job()['cleanup_manifest'], original)
        self.cloud.delete_cleanup_targets.assert_awaited_once_with(['own-folder'])

    async def test_changed_or_unreadable_tree_blocks_delete(self):
        self.due()
        for result in ('new content', CloudError('missing')):
            self.store.update_job(1, cleanup_state='pending')
            self.cloud.cleanup_manifest = AsyncMock(return_value=result) if isinstance(result, str) else AsyncMock(side_effect=result)
            await self.worker.cleanup_due()
            self.assertEqual(self.job()['cleanup_state'], 'blocked')
        self.cloud.delete_cleanup_folder.assert_not_awaited()

    async def test_transient_read_failure_defers_without_deleting(self):
        self.due()
        for error in (httpx.ReadTimeout('secret-url'), CloudError('busy', retryable=True)):
            self.store.update_job(1, cleanup_state='pending', cleanup_next=0)
            self.cloud.cleanup_manifest.side_effect = error
            await self.worker.cleanup_due()
            self.assertEqual(self.job()['cleanup_state'], 'pending')
            self.assertGreater(self.job()['cleanup_next'], time.time() + 290)
            self.assertNotIn('secret', self.job()['cleanup_error'])
            self.cloud.cleanup_manifest.reset_mock()
            await self.worker.cleanup_due()
            self.cloud.cleanup_manifest.assert_not_awaited()
        self.cloud.cleanup_manifest.side_effect = None
        self.store.update_job(1, cleanup_next=0)
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_awaited_once()

    async def test_missing_target_and_changed_snapshot_are_distinct(self):
        self.due()
        self.cloud.cleanup_manifest.side_effect = ValueError('missing')
        await self.worker.cleanup_due()
        self.assertIn('不存在', self.job()['cleanup_error'])
        self.store.update_job(1, cleanup_state='pending')
        self.cloud.cleanup_manifest.side_effect = None
        self.cloud.cleanup_manifest.return_value = 'changed'
        await self.worker.cleanup_due()
        self.assertIn('快照不同', self.job()['cleanup_error'])
        self.cloud.delete_cleanup_folder.assert_not_awaited()

    async def test_timeout_never_retries_and_crash_recovers_uncertain(self):
        self.due()
        self.cloud.delete_cleanup_folder.side_effect = httpx.ReadTimeout('secret-url')
        await self.worker.cleanup_due()
        await self.worker.cleanup_due()
        self.assertEqual(self.job()['cleanup_state'], 'uncertain')
        self.assertNotIn('secret', self.job()['cleanup_error'])
        self.cloud.delete_cleanup_folder.assert_awaited_once()
        self.store.update_job(1, cleanup_state='deleting')
        self.store.db.close()
        self.store = Store(self.tmp.name)
        self.assertEqual(self.job()['cleanup_state'], 'uncertain')

    async def test_delete_progress_errors_keep_task_and_terminal_failure_stops(self):
        self.due()
        await self.worker.cleanup_due()
        self.cloud.task = AsyncMock(side_effect=httpx.ReadTimeout('timeout'))
        await self.worker.cleanup(self.job())
        self.assertEqual(self.job()['cleanup_state'], 'submitted')
        self.cloud.task = AsyncMock(side_effect=TaskFailed('failed'))
        await self.worker.cleanup(self.job())
        self.assertEqual(self.job()['cleanup_state'], 'blocked')
        self.cloud.delete_cleanup_folder.assert_awaited_once()

    async def test_manifest_recursive_and_moved_root_rejected(self):
        self.cloud.folders = AsyncMock(return_value=[{'id': 'owned', 'name': 'sbtg-test'}])
        self.cloud.pages = AsyncMock(side_effect=[
            [{'fileId': 'child', 'fileName': 'series', 'resType': 2}],
            [{'fileId': 'episode', 'fileName': 'e01.mkv', 'resType': 1, 'fileSize': 5}]])
        manifest = await self.cloud.cleanup_manifest('parent', 'owned', 'sbtg-test')
        self.assertIn('episode', manifest)
        self.cloud.folders.return_value = []
        with self.assertRaises(CloudError):
            await self.cloud.cleanup_manifest('parent', 'owned', 'sbtg-test')

    async def test_delete_request_no_401_retry_and_redacts_unknown_receipt(self):
        self.cloud.token = AsyncMock(return_value='token')
        self.cloud.raw = AsyncMock(side_effect=CloudError('expired', login=True))
        with self.assertRaises(CloudError):
            await self.cloud.delete_cleanup_folder('owned')
        self.cloud.raw.assert_awaited_once_with(API+'/file/delete_file', {'fileIds': ['owned']}, 'token')
        self.cloud.raw = AsyncMock(return_value={'data': {}})
        with self.assertRaises(CloudError):
            await self.cloud.delete_cleanup_folder('owned')

    async def test_shared_pacing_serializes_requests_without_network(self):
        self.cloud._raw = AsyncMock(return_value={'ok': True})
        with patch('app.cloud.time', SimpleNamespace(monotonic=lambda: 100)), patch('app.cloud.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await asyncio.gather(self.cloud.raw('url', {}), self.cloud.raw('url', {}))
        sleep.assert_awaited_once_with(2)
        self.assertEqual(self.cloud._raw.await_count, 2)

    async def test_rate_limit_starts_global_cooldown_without_retry(self):
        self.cloud.http.post = AsyncMock(return_value=httpx.Response(429, headers={'Retry-After': '120'}))
        with patch('app.cloud.time', SimpleNamespace(monotonic=lambda: 100)):
            with self.assertRaises(CloudError):
                await self.cloud.raw(API+'/file/get_file_list', {})
        self.assertEqual(self.cloud.next_request_at, 220)
        self.cloud.http.post.assert_awaited_once()

    async def test_history_preview_is_read_only_candidates_not_authority(self):
        self.enqueue()
        self.store.update_job(1, status='success')
        self.store.execute('UPDATE jobs SET created=? WHERE id=1', (time.time()-7201,))
        before = self.job()
        self.cloud.pages = AsyncMock(return_value=[{'fileId': 'candidate', 'fileName': 'Example (2026)', 'resType': 2},
                                                  {'fileId': 'unrelated', 'fileName': 'Other', 'resType': 2}])
        result = await history_preview(self.store, self.cloud)
        await history_preview(self.store, self.cloud)
        self.cloud.pages.assert_awaited_once()
        self.assertEqual([x['id'] for x in result['items'][0]['candidates']], ['candidate'])
        self.assertEqual(self.job(), before)

    async def test_legacy_age_gate_and_file_cleanup(self):
        self.enqueue()
        self.store.update_job(1, status='success')
        self.cloud.pages = AsyncMock(return_value=[{'fileId':'legacy-file','fileName':'Example (2026)','resType':1,'fileSize':10}])
        self.store.execute('UPDATE jobs SET created=10000 WHERE id=1')
        with patch('app.cleanup.time.time', return_value=13599):
            with self.assertRaises(ValueError):
                await queue_history(self.store,self.cloud,1,'legacy-file','Example (2026)')
        self.cloud.pages.assert_not_awaited()
        with patch('app.cleanup.time.time', return_value=13600):
            await queue_history(self.store,self.cloud,1,'legacy-file','Example (2026)')
        self.assertIsNone(self.job()['completed_at'])
        self.store.set('auto_cleanup',True)
        self.cloud.delete_cleanup_folder=AsyncMock(return_value='task')
        await self.worker.cleanup_due()
        self.cloud.delete_cleanup_folder.assert_awaited_once_with('legacy-file')

    async def test_legacy_duplicate_target_and_failed_job_rejected(self):
        self.enqueue()
        self.enqueue('two')
        self.store.execute("UPDATE jobs SET status='success',created=1")
        self.cloud.pages=AsyncMock(return_value=[{'fileId':'same','fileName':'Example (2026)','resType':1}])
        await queue_history(self.store,self.cloud,1,'same','Example (2026)')
        with self.assertRaises(ValueError):
            await queue_history(self.store,self.cloud,2,'same','Example (2026)')
        self.store.update_job(2,status='failed')
        with self.assertRaises(ValueError):
            await queue_history(self.store,self.cloud,2,'same','Example (2026)')

    async def test_young_history_preview_does_not_access_cloud(self):
        self.enqueue()
        self.store.update_job(1,status='success')
        self.cloud.history_files=AsyncMock()
        result=await history_preview(self.store,self.cloud)
        self.assertFalse(result['items'][0]['eligible'])
        self.cloud.history_files.assert_not_awaited()

    async def test_not_due_never_reads_cloud(self):
        self.due()
        self.store.update_job(1, completed_at=time.time())
        for _ in range(3):
            await self.worker.cleanup_due()
        self.cloud.cleanup_manifest.assert_not_awaited()
        self.cloud.delete_cleanup_folder.assert_not_awaited()

    async def test_empty_directory_protocol_and_unknown_schema(self):
        for body in ({}, {'total':0}, {'total':0,'list':None}, {'list':None}, {'files':None}, {'list':[]}):
            self.cloud.call=AsyncMock(return_value=body)
            self.assertEqual(await self.cloud.pages('/file/get_file_list',{}),[])
        for body in ({'unexpected':'schema'}, {'total':1}, {'total':1,'list':None}, {'total':0,'hasMore':True}):
            self.cloud.call=AsyncMock(return_value=body)
            with self.assertRaises(CloudError):
                await self.cloud.pages('/file/get_file_list',{})
        self.cloud.call=AsyncMock(return_value={})
        with self.assertRaises(CloudError):
            await self.cloud.pages('/get_share_page_files_list',{},cursor_mode=True)

    async def test_history_queue_handles_nested_empty_object_response(self):
        self.enqueue()
        self.store.execute("UPDATE jobs SET status='success',created=1")
        self.cloud.call=AsyncMock(side_effect=[
            {'total':1,'list':[{'fileId':'series','fileName':'Example (2026)','resType':2}]},
            {'total':1,'list':[{'fileId':'empty-child','fileName':'Extras','resType':2}]},
            {}])
        self.cloud.delete_cleanup_folder=AsyncMock()
        await queue_history(self.store,self.cloud,1,'series','Example (2026)')
        self.assertEqual(self.job()['cleanup_state'],'pending')
        self.assertIn('empty-child',self.job()['cleanup_manifest'])
        self.cloud.delete_cleanup_folder.assert_not_awaited()

    async def test_retry_old_empty_folder_saves_directly_to_selected_parent(self):
        self.store.set('auto_cleanup',True)
        self.enqueue()
        self.store.update_job(1,cleanup_folder='existing',cleanup_name='sbtg-existing')
        self.cloud.prepare=AsyncMock(return_value=('access',['source'],'Title',[]))
        self.cloud.call=AsyncMock(side_effect=[
            {'list':[{'fileId':'existing','fileName':'sbtg-existing','resType':2}]},
            {},
            {'list':[{'fileId':'existing','fileName':'sbtg-existing','resType':2}]}])
        self.cloud.create_cleanup_folder=AsyncMock()
        self.cloud.submit=AsyncMock(return_value='task')
        await self.worker.process(self.job())
        self.cloud.create_cleanup_folder.assert_not_awaited()
        self.cloud.submit.assert_awaited_once_with('access',['source'],'parent')

    async def test_retry_does_not_merge_with_partial_transfer(self):
        self.due()
        self.store.update_job(1, status='queued')
        self.cloud.prepare = AsyncMock(return_value=('access', ['source'], 'Title', []))
        self.cloud.submit = AsyncMock()
        await self.worker.process(self.job())
        self.cloud.submit.assert_not_awaited()
        self.assertEqual(self.job()['status'], 'failed')


class CleanupWebTests(unittest.TestCase):
    def test_settings_and_history_without_real_accounts(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)):
            with TestClient(main.app) as client:
                headers = {'X-Sbtg-Client': 'web'}
                self.assertFalse(client.get('/api/state', headers=headers).json()['auto_cleanup'])
                self.assertEqual(client.post('/api/cleanup/settings', headers=headers, json={'enabled': True}).status_code, 200)
                self.assertTrue(client.get('/api/state', headers=headers).json()['auto_cleanup'])
                self.assertEqual(client.get('/api/cleanup/history', headers=headers).json()['items'], [])
                self.assertEqual(client.get('/api/cleanup/history?page=0', headers=headers).status_code, 422)
