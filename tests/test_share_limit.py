import tempfile
import unittest
from datetime import datetime, timezone
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

    async def test_daily_quota_shared_at_exact_boundary_persists_until_beijing_midnight(self):
        midnight = datetime(2026, 11, 20, 16, tzinfo=timezone.utc).timestamp()
        with patch('app.store.time.time', return_value=midnight - 1) as clock:
            self.store.set_daily_transfer_limit(2)
            worker = Worker(self.store, self.cloud)
            self.transfer_mocks(1_000_000_000)
            first = self.enqueue('listener')
            await worker.process(first)
            self.cloud.share_size.return_value = 1_000_000_001
            waiting = self.enqueue('overflow')
            await worker.process(waiting)
            saved = self.job(waiting['id'])
            self.assertEqual((saved['status'], saved['next_at'], saved['attempts']), ('queued', midnight, 0))
            self.cloud.prepare.assert_awaited_once()

            self.cloud.share_size.return_value = 1_000_000_000
            self.cloud.submit.side_effect = httpx.ReadTimeout('no receipt')
            manual = self.enqueue('manual', 0)
            await worker.process(manual)
            self.assertEqual(self.job(manual['id'])['status'], 'uncertain')
            self.assertEqual(self.store.transfer_quota()['used_bytes'], 2_000_000_000)
            await worker.process(self.enqueue('full'))
            self.assertEqual(self.cloud.share_size.await_count, 3)

            self.store.db.close()
            self.store = self.cloud.store = Store(self.tmp.name)
            self.assertEqual(self.store.transfer_quota()['remaining_bytes'], 0)
            self.assertEqual(self.store.transfer_quota_status()['completed_bytes'], 0)
            clock.return_value = midnight
            self.assertEqual(self.store.transfer_quota()['day'], '2026-11-21')
            self.assertEqual(self.store.transfer_quota()['used_bytes'], 0)
            self.cloud.task = AsyncMock(return_value=True)
            worker = Worker(self.store, self.cloud)
            await worker.process(self.job(first['id']))
            quota = self.store.transfer_quota_status()
            self.assertEqual((quota['completed_bytes'], quota['used_bytes']), (1_000_000_000, 0))
            self.cloud.submit.side_effect = None
            await worker.process(self.job(waiting['id']))
            self.assertEqual(self.job(waiting['id'])['status'], 'submitted')
            self.assertEqual(self.store.transfer_quota()['used_bytes'], 1_000_000_000)

    async def test_lowered_daily_limit_is_rechecked_at_submit_and_raising_wakes_only_quota_waiters(self):
        self.store.set_daily_transfer_limit(3)
        self.transfer_mocks(1_000_000_000)
        worker = Worker(self.store, self.cloud)
        await worker.process(self.enqueue('first'))

        async def lower_limit(*args):
            self.store.set_daily_transfer_limit(1)
            return 'access', ['file'], 'Example'

        job = self.enqueue('waiting')
        self.cloud.prepare.side_effect = lower_limit
        await worker.process(job)
        self.assertEqual(self.job(job['id'])['quota_wait'], 1)
        self.cloud.submit.assert_awaited_once()
        self.assertEqual(self.store.transfer_quota()['used_bytes'], 1_000_000_000)
        retry = self.enqueue('retry')
        self.store.update_job(retry['id'], next_at=9000000000)
        self.store.set_daily_transfer_limit(2)
        self.assertEqual(self.job(job['id'])['next_at'], 0)
        self.assertEqual(self.job(retry['id'])['next_at'], 9000000000)
        self.cloud.prepare.side_effect = None
        await worker.process(self.job(job['id']))
        self.assertEqual(self.job(job['id'])['status'], 'submitted')
        self.assertEqual(self.store.transfer_quota()['remaining_bytes'], 0)

    async def test_share_larger_than_the_daily_limit_needs_action(self):
        self.store.set_daily_transfer_limit(1)
        self.transfer_mocks(1_000_000_001)
        job = self.enqueue()
        await Worker(self.store, self.cloud).process(job)
        self.assertEqual(self.store.actionable_jobs(), [{'id': job['id'], 'status': 'failed'}])
        self.assertIn('每日转存上限 1 GB', self.job(job['id'])['error'])
        self.assertEqual(self.store.transfer_quota()['used_bytes'], 0)
        self.cloud.prepare.assert_not_awaited()
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

    def test_daily_limit_default_validation_and_persistence(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, 'DATA', Path(tmp)):
            headers = {'X-Sbtg-Client': 'web'}
            with TestClient(main.app) as client:
                quota = client.get('/api/state', headers=headers).json()['daily_transfer_quota']
                self.assertEqual((quota['limit_gb'], quota['used_bytes'], quota['remaining_bytes'], quota['completed_bytes']),
                                 (1000, 0, 1_000_000_000_000, 0))
                for value in (0, -1, 1.5, '750', True, 10**10):
                    self.assertEqual(client.post('/api/shares/daily-limit', headers=headers,
                                                 json={'gb': value}).status_code, 422)
                self.assertEqual(client.post('/api/shares/daily-limit', headers=headers,
                                             json={'gb': 750}).status_code, 200)
            with TestClient(main.app) as client:
                state = client.get('/api/state', headers=headers).json()
                self.assertEqual(state['daily_transfer_quota']['limit_gb'], 750)
                self.assertEqual(state['share_size_limit_gb'], 200)
