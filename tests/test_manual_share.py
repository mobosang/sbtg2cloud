import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import main
from app.extract import extract


class ManualShareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(main, 'DATA', Path(self.tmp.name))
        self.worker_patch = patch.object(main.Worker, 'run', AsyncMock())
        self.data_patch.start()
        self.worker_patch.start()
        self.client = TestClient(main.app)
        self.client.__enter__()
        self.store = main.app.state.store
        self.headers = {'X-Sbtg-Client': 'web'}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.worker_patch.stop()
        self.data_patch.stop()
        self.tmp.cleanup()

    def call(self, function, *args):
        return self.client.portal.call(function, *args)

    def submit(self, text):
        return self.client.post('/api/jobs/manual', json={'text': text}, headers=self.headers)

    def job(self, job_id):
        return self.call(lambda: self.store.rows('SELECT * FROM jobs WHERE id=?', (job_id,))[0])

    def test_default_target_code_and_cleanup_are_snapshotted_without_telegram(self):
        self.call(self.store.set, 'target', {'id': 'media', 'name': '/Media'})
        self.call(self.store.set, 'auto_cleanup', True)
        result = self.submit('https://app.guangyapan.com/s/manual?pwd=A123#/share')
        self.assertEqual(result.status_code, 200)
        data = result.json()
        self.assertTrue(data['created'])
        job = self.job(data['job_id'])
        self.assertEqual((job['status'], job['source_id'], job['message_id']), ('queued', 0, 0))
        self.assertEqual((job['target_id'], job['target_name'], job['code']), ('media', '/Media', 'A123'))
        self.assertEqual(job['cleanup_enabled'], 1)
        self.call(self.store.set, 'target', {'id': 'next', 'name': '/Next'})
        self.call(self.store.set, 'auto_cleanup', False)
        self.assertEqual(self.job(data['job_id'])['target_id'], 'media')
        second = self.submit('https://www.guangyapan.com/s/second 提取码：B456').json()
        self.assertEqual((self.job(second['job_id'])['target_id'], self.job(second['job_id'])['cleanup_enabled']), ('next', 0))
        for endpoint in ('/api/jobs', '/api/job-pages'):
            response = self.client.get(endpoint, headers=self.headers).json()
            jobs = response if isinstance(response, list) else response['items']
            self.assertEqual({j['source_title'] for j in jobs}, {'手动提交'})
        self.assertEqual(self.client.get('/api/sources', headers=self.headers).json(), [])

    def test_duplicates_preserve_existing_status_origin_target_and_receipts(self):
        source = {'id': 17, 'target_id': 'original', 'target_name': '/Original'}
        for status in ('queued', 'submitted', 'success', 'failed', 'uncertain', 'discarded'):
            with self.subTest(status=status):
                link = extract('https://www.guangyapan.com/s/'+status+'?pwd=Old1')[0]
                job_id = self.call(self.store.enqueue, link, source, 99, 'Existing title')
                self.call(lambda: self.store.update_job(job_id, status=status, remote_task='original-receipt'))
                before = self.job(job_id)
                result = self.submit(link['url']+'?pwd=New2').json()
                self.assertEqual(result, {'created': False, 'job_id': job_id, 'status': status})
                self.assertEqual(self.job(job_id), before)
        self.assertEqual(self.call(lambda: self.store.rows('SELECT COUNT(*) n FROM jobs')[0]['n']), 6)
        manual = self.submit('https://www.guangyapan.com/s/manual-first').json()
        self.assertIsNone(self.call(self.store.enqueue, extract('https://www.guangyapan.com/s/manual-first')[0], source, 100))
        self.assertEqual(self.job(manual['job_id'])['source_id'], 0)

    def test_invalid_multiple_and_cross_site_requests_never_enqueue(self):
        for value in (' ', 'https://re0.me/resource/example', 'https://www.guangyapan.com.evil/s/abc',
                      'https://user@www.guangyapan.com/s/abc',
                      'https://evil.test/?next=https://www.guangyapan.com/s/abc',
                      'https://www.guangyapan.com/s/one https://www.guangyapan.com/s/two'):
            with self.subTest(value=value):
                self.assertEqual(self.submit(value).status_code, 400)
        for value in ('', 'x'*4097):
            self.assertEqual(self.submit(value).status_code, 422)
        payload = {'text': 'https://www.guangyapan.com/s/valid'}
        self.assertEqual(self.client.post('/api/jobs/manual', json=payload).status_code, 403)
        self.assertEqual(self.client.post('/api/jobs/manual', json=payload,
                         headers={**self.headers, 'Origin': 'http://evil.test'}).status_code, 403)
        self.assertEqual(self.call(lambda: self.store.rows('SELECT COUNT(*) n FROM jobs')[0]['n']), 0)

    def test_manual_job_uses_normal_transfer_and_cleanup_receipts(self):
        self.call(self.store.set, 'target', {'id': 'media', 'name': '/Media'})
        self.call(self.store.set, 'auto_cleanup', True)
        self.call(self.store.set, 'cleanup_minutes', 30)
        job_id = self.submit('https://www.guangyapan.com/s/example').json()['job_id']
        cloud, worker = main.app.state.cloud, main.app.state.worker
        source = {'fileId': 'shared', 'fileName': 'Example.mkv', 'resType': 1, 'fileSize': 10}
        owned = {**source, 'fileId': 'owned'}
        cloud.prepare = AsyncMock(return_value=('access', ['shared'], 'Example', [source]))
        cloud.pages = AsyncMock(side_effect=[[], [owned], [owned], [owned]])
        cloud.submit = AsyncMock(return_value='save-receipt')
        cloud.task = AsyncMock(return_value=True)
        cloud.delete_cleanup_targets = AsyncMock(return_value='delete-receipt')
        self.call(worker.process, self.job(job_id))
        cloud.submit.assert_awaited_once_with('access', ['shared'], 'media')
        self.assertEqual(self.job(job_id)['status'], 'submitted')
        self.call(worker.process, self.job(job_id))
        job = self.job(job_id)
        self.assertEqual((job['status'], job['cleanup_state'], job['resource_name']), ('success', 'pending', 'Example'))
        self.assertIsNotNone(job['completed_at'])
        pending = self.client.get('/api/job-pages?status_filter=cleanup', headers=self.headers).json()
        self.assertEqual([(j['id'], j['source_title']) for j in pending['items']], [(job_id, '手动提交')])
        self.call(worker.cleanup, self.job(job_id))
        cloud.delete_cleanup_targets.assert_not_awaited()
        with patch('app.worker.time.time', return_value=job['completed_at']+1801):
            self.call(worker.cleanup, self.job(job_id))
        cloud.delete_cleanup_targets.assert_awaited_once_with(['owned'])
        self.assertEqual(self.job(job_id)['cleanup_state'], 'submitted')
        self.call(worker.cleanup, self.job(job_id))
        self.assertEqual(self.job(job_id)['cleanup_state'], 'done')
        self.assertIsNotNone(self.job(job_id)['cleanup_completed_at'])


if __name__ == '__main__':
    unittest.main()
