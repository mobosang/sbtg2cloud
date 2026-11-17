import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from app.cloud import Cloud, CloudError
from app.store import Store
from app.worker import Worker


class ShareLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.cloud = Cloud(self.store)

    async def asyncTearDown(self):
        await self.cloud.http.aclose()
        self.store.db.close()
        self.tmp.cleanup()

    def enqueue(self, key='one', source=1):
        self.store.enqueue({'share_id': key, 'url': 'https://www.guangyapan.com/s/' + key, 'code': ''},
                           {'id': source, 'target_id': 'parent', 'target_name': '/Media'}, 1, 'Example')
        return self.store.rows('SELECT * FROM jobs WHERE share_id=?', (key,))[0]

    def job(self, jid):
        return self.store.rows('SELECT * FROM jobs WHERE id=?', (jid,))[0]

    def transfer_mocks(self, size):
        self.cloud.share_size = AsyncMock(return_value=size)
        self.cloud.prepare = AsyncMock(return_value=('access', ['file'], 'Example'))
        self.cloud.pages = AsyncMock(return_value=[])
        self.cloud.submit = AsyncMock(return_value='receipt')

    async def test_summary_reads_total_size_without_scanning_folders(self):
        self.cloud.call = AsyncMock(return_value={'totalFileSize': 400_000_000_000_000})
        self.cloud.pages = AsyncMock()
        self.assertEqual(await self.cloud.share_size('share'), 400_000_000_000_000)
        self.cloud.call.assert_awaited_once_with('/get_share_summary', {'shareId': 'share', 'needFileNum': False})
        self.cloud.pages.assert_not_awaited()
        self.cloud.call.return_value = {'totalFileSize': '200000000000'}
        self.assertEqual(await self.cloud.share_size('share'), 200_000_000_000)
        self.cloud.call.return_value = {'totalFileSize': 0}
        self.assertEqual(await self.cloud.share_size('share'), 0)

    async def test_unknown_or_invalid_total_is_never_treated_as_zero(self):
        for data in (None, [], {}, {'totalFileSize': None}, {'totalFileSize': True},
                     {'totalFileSize': -1}, {'totalFileSize': 1.5}, {'totalFileSize': '-1'},
                     {'totalFileSize': 'NaN'}, {'totalFileSize': '200 GB'}):
            with self.subTest(data=data):
                self.cloud.call = AsyncMock(return_value=data)
                with self.assertRaisesRegex(CloudError, '无法确认分享总大小'):
                    await self.cloud.share_size('share')

    async def test_oversized_listener_and_manual_shares_are_discarded_before_other_reads(self):
        self.store.set('auto_cleanup', True)
        for source, size in ((1, 400_000_000_000_000), (0, 200_000_000_001)):
            with self.subTest(source=source):
                self.transfer_mocks(size)
                job = self.enqueue(str(source), source)
                await Worker(self.store, self.cloud).process(job)
                saved = self.job(job['id'])
                self.assertEqual(saved['status'], 'discarded')
                self.assertIn('200 GB', saved['error'])
                self.assertIn(f'{size:,}', saved['error'])
                self.assertFalse(saved['remote_task'])
                self.assertFalse(saved['cleanup_state'])
                self.cloud.prepare.assert_not_awaited()
                self.cloud.pages.assert_not_awaited()
                self.cloud.submit.assert_not_awaited()
                self.assertFalse(self.store.actionable_jobs())
                self.assertIsNone(self.store.enqueue(
                    {'share_id': str(source), 'url': job['url'], 'code': ''},
                    {'id': source, 'target_id': 'parent', 'target_name': '/Media'}, 2))

    async def test_default_and_custom_limits_allow_exact_boundary(self):
        for limit in (200, 1, 500):
            with self.subTest(limit=limit):
                if limit != 200:
                    self.store.set('share_size_limit_gb', limit)
                self.transfer_mocks(limit * 1_000_000_000)
                job = self.enqueue(str(limit))
                await Worker(self.store, self.cloud).process(job)
                saved = self.job(job['id'])
                self.assertEqual(saved['status'], 'submitted')
                self.assertEqual(saved['target_id'], 'parent')
                self.cloud.submit.assert_awaited_once_with('access', ['file'], 'parent')
                self.assertFalse(saved['cleanup_state'])

    async def test_lowering_limit_while_reading_share_prevents_submission(self):
        self.transfer_mocks(150_000_000_000)

        async def prepare(*args):
            self.store.set('share_size_limit_gb', 100)
            return 'access', ['file'], 'Example'

        self.cloud.prepare.side_effect = prepare
        job = self.enqueue()
        await Worker(self.store, self.cloud).process(job)
        self.assertEqual(self.job(job['id'])['status'], 'discarded')
        self.assertIn('100 GB', self.job(job['id'])['error'])
        self.cloud.submit.assert_not_awaited()
        self.cloud.share_size.assert_awaited_once()

    async def test_missing_size_stops_transfer_and_transient_errors_have_bounded_retries(self):
        for key, error, attempts, status in (
            ('invalid', CloudError('无法确认分享总大小'), 1, 'failed'),
            ('network', httpx.ConnectError('private diagnostic text'), 3, 'failed'),
        ):
            with self.subTest(key=key):
                self.transfer_mocks(0)
                self.cloud.share_size.side_effect = error
                job = self.enqueue(key)
                worker = Worker(self.store, self.cloud)
                for _ in range(attempts):
                    await worker.process(self.job(job['id']))
                saved = self.job(job['id'])
                self.assertEqual(saved['status'], status)
                self.assertEqual(saved['attempts'], attempts)
                self.assertIn('核对分享大小失败', saved['error'])
                self.assertIn('未提交转存', saved['error'])
                self.assertNotIn('private diagnostic text', saved['error'])
                self.cloud.prepare.assert_not_awaited()
                self.cloud.submit.assert_not_awaited()

    async def test_existing_cloud_receipt_is_polled_without_size_check_or_resubmission(self):
        self.transfer_mocks(400_000_000_000_000)
        self.cloud.task = AsyncMock(return_value=True)
        job = self.enqueue()
        self.store.update_job(job['id'], status='submitted', remote_task='existing-receipt')
        await Worker(self.store, self.cloud).process(self.job(job['id']))
        self.assertEqual(self.job(job['id'])['status'], 'success')
        self.cloud.task.assert_awaited_once_with('existing-receipt')
        self.cloud.share_size.assert_not_awaited()
        self.cloud.submit.assert_not_awaited()


class ShareLimitWebTests(unittest.TestCase):
    def test_default_validation_and_persistence(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)):
            headers = {'X-Sbtg-Client': 'web'}
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/api/state', headers=headers).json()['share_size_limit_gb'], 200)
                for value in (0, -1, 1.5, '200', True, None):
                    self.assertEqual(client.post('/api/shares/limit', headers=headers,
                                                 json={'gb': value}).status_code, 422)
                for value in (1, 1000, 500):
                    self.assertEqual(client.post('/api/shares/limit', headers=headers,
                                                 json={'gb': value}).status_code, 200)
                    self.assertEqual(client.get('/api/state', headers=headers).json()['share_size_limit_gb'], value)
            with TestClient(main.app) as client:
                self.assertEqual(client.get('/api/state', headers=headers).json()['share_size_limit_gb'], 500)
