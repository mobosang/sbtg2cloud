import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
from fastapi.testclient import TestClient
from app.cloud import Cloud
from app.store import Store
from app.directory_clear import DirectoryClear
from app.worker import Worker


class ClearTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.store.set('target',{'id':'parent','name':'/Media'})
        self.cloud=Cloud(self.store)
        self.rows=[{'fileId':'a','fileName':'movie','resType':1,'fileSize':10},
                   {'fileId':'b','fileName':'series','resType':2}]
        self.cloud.pages=AsyncMock(return_value=self.rows)
        self.cloud.delete_cleanup_targets=AsyncMock(return_value='task')
        self.cloud.task=AsyncMock(return_value=True)
        self.clear=DirectoryClear(self.store,self.cloud)

    async def asyncTearDown(self):
        await self.cloud.http.aclose()
        self.store.db.close()
        self.tmp.cleanup()

    async def test_preview_confirm_preserves_parent_and_new_top_level_items(self):
        preview=await self.clear.preview('parent')
        self.assertEqual((preview['files'],preview['folders']),(1,1))
        self.cloud.delete_cleanup_targets.assert_not_awaited()
        self.clear.confirm(preview['id'])
        self.clear.confirm(preview['id'])
        self.cloud.pages.return_value=self.rows+[{'fileId':'new','fileName':'new','resType':1}]
        await self.clear.step()
        self.cloud.delete_cleanup_targets.assert_awaited_once_with(['a','b'])
        self.store.execute('UPDATE directory_clear_parts SET next_at=0')
        await self.clear.step()
        await self.clear.step()
        self.assertEqual(self.clear.latest()['state'],'done')
        self.cloud.delete_cleanup_targets.assert_awaited_once()
        with self.assertRaises(ValueError):
            await self.clear.preview('parent')

    async def test_preview_timeout_does_not_queue_deletion(self):
        self.cloud.pages.side_effect = TimeoutError()
        with self.assertRaisesRegex(ValueError, '读取目录超时'):
            await self.clear.preview('parent')
        self.assertEqual(self.store.rows('SELECT * FROM directory_clears'), [])
        self.cloud.delete_cleanup_targets.assert_not_awaited()

    async def test_empty_root_and_unknown_destinations(self):
        for parent in ('','unknown'):
            with self.assertRaises(ValueError):
                await self.clear.preview(parent)
        self.cloud.pages.assert_not_awaited()
        self.cloud.pages.return_value=[]
        preview=await self.clear.preview('parent')
        self.clear.confirm(preview['id'])
        self.assertEqual(self.clear.latest()['state'],'done')
        self.assertFalse(await self.clear.step())
        self.cloud.delete_cleanup_targets.assert_not_awaited()

    async def test_changed_directory_blocks_whole_batch(self):
        preview=await self.clear.preview('parent')
        self.clear.confirm(preview['id'])
        self.cloud.pages.return_value=[]
        await self.clear.step()
        self.assertEqual(self.clear.latest()['state'],'blocked')
        self.cloud.delete_cleanup_targets.assert_not_awaited()

    async def test_timeout_and_restart_never_resubmit(self):
        preview=await self.clear.preview('parent')
        self.clear.confirm(preview['id'])
        self.cloud.delete_cleanup_targets.side_effect=httpx.ReadTimeout('private')
        await self.clear.step()
        await self.clear.step()
        self.assertEqual(self.clear.latest()['state'],'uncertain')
        self.cloud.delete_cleanup_targets.assert_awaited_once()
        self.store.execute("UPDATE directory_clears SET state='running'")
        self.store.db.close()
        self.store=Store(self.tmp.name)
        self.clear=DirectoryClear(self.store,self.cloud)
        self.assertEqual(self.clear.latest()['state'],'uncertain')
        self.assertFalse(await self.clear.step())

    async def test_chunking_and_confirmation_expiry(self):
        self.cloud.pages.return_value=[{'fileId':str(i),'fileName':str(i),'resType':1} for i in range(201)]
        preview=await self.clear.preview('parent')
        self.store.execute('UPDATE directory_clears SET created=?',(time.time()-301,))
        with self.assertRaises(ValueError):
            self.clear.confirm(preview['id'])
        preview=await self.clear.preview('parent')
        self.clear.confirm(preview['id'])
        self.assertEqual(self.clear.latest()['total'],3)
        await self.clear.step()
        self.assertEqual(len(self.cloud.delete_cleanup_targets.call_args.args[0]),100)

    async def test_mutation_lock_blocks_preview(self):
        async with self.cloud.operation_lock:
            with self.assertRaises(ValueError):
                await self.clear.preview('parent')
        self.cloud.pages.assert_not_awaited()

    async def test_custom_delay_rechecks_pending_jobs_locally(self):
        self.store.set('auto_cleanup',True)
        self.store.enqueue({'share_id':'share','url':'https://www.guangyapan.com/s/share','code':''},
                           {'id':1,'target_id':'parent','target_name':'/Media'},1)
        self.store.update_job(1,status='success',completed_at=time.time()-600,cleanup_state='pending')
        worker=Worker(self.store,self.cloud)
        worker.cleanup=AsyncMock()
        self.store.set('cleanup_minutes',600)
        await worker.cleanup_due()
        worker.cleanup.assert_not_awaited()
        self.store.set('cleanup_minutes',5)
        await worker.cleanup_due()
        worker.cleanup.assert_awaited_once()


class ClearWebTests(unittest.TestCase):
    def test_delay_validation_and_clear_api(self):
        from app import main
        with tempfile.TemporaryDirectory() as tmp, patch.object(main,'DATA',Path(tmp)):
            with TestClient(main.app) as client:
                headers={'X-Sbtg-Client':'web'}
                self.assertEqual(client.get('/api/state',headers=headers).json()['cleanup_minutes'],60)
                for value in (4,601,5.5,'60',True):
                    self.assertEqual(client.post('/api/cleanup/delay',headers=headers,json={'minutes':value}).status_code,422)
                for value in (5,600):
                    self.assertEqual(client.post('/api/cleanup/delay',headers=headers,json={'minutes':value}).status_code,200)
                    self.assertEqual(client.get('/api/state',headers=headers).json()['cleanup_minutes'],value)
                client.portal.call(main.app.state.store.set,'target',{'id':'parent','name':'/Media'})
                main.app.state.cloud.pages=AsyncMock(return_value=[{'fileId':'a','fileName':'movie','resType':1}])
                self.assertEqual(client.get('/api/clear/targets',headers=headers).json(),[{'id':'parent','name':'/Media'}])
                preview=client.post('/api/clear/preview',headers=headers,json={'id':'parent'}).json()
                self.assertEqual(client.post('/api/clear/'+preview['id']+'/confirm',headers=headers,json={}).status_code,200)
                self.assertEqual(client.get('/api/state',headers=headers).json()['directory_clear']['state'],'queued')
