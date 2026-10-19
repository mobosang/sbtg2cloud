import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from scripts.cleanup_confirmed import ConfirmedCleanup


class ConfirmedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'journal.sqlite3'
        self.cloud = SimpleNamespace(folders=AsyncMock(return_value=[{'id':'a','name':'A'},{'id':'b','name':'B'}]),
                                     token=AsyncMock(return_value='private'),raw=AsyncMock(return_value={'data':{'taskId':'task'}}),
                                     task=AsyncMock(return_value=True))
        self.manifest = {'version':1, 'targets':[{'id':'a','name':'A','parent_id':'parent'}, {'id':'b','name':'B','parent_id':'parent'}]}
        self.cleaner = ConfirmedCleanup(self.cloud,self.path,self.manifest)
        self.sleep = patch('scripts.cleanup_confirmed.asyncio.sleep', new_callable=AsyncMock)
        self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        self.cleaner.db.close()
        self.tmp.cleanup()

    async def test_preview_never_deletes_and_batch_has_exact_ids(self):
        self.assertEqual(await self.cleaner.run(), 'preview')
        self.cloud.raw.assert_not_awaited()
        self.assertEqual(await self.cleaner.run(True), 'submitted')
        self.assertEqual(self.cloud.raw.call_args.args[1], {'fileIds':['a','b']})
        self.cleaner.db.execute('UPDATE batches SET next_at=0')
        self.cleaner.db.commit()
        self.assertEqual(await self.cleaner.run(True), 'done')
        self.assertEqual(await self.cleaner.run(True), 'done')
        self.cloud.raw.assert_awaited_once()

    async def test_missing_directory_aborts_whole_batch(self):
        self.cloud.folders.return_value = [{'id':'a','name':'A'}]
        with self.assertRaises(ValueError):
            await self.cleaner.run(True)
        self.cloud.raw.assert_not_awaited()

    async def test_unknown_result_and_modified_manifest_cannot_repeat_delete(self):
        self.cloud.raw.side_effect = httpx.ReadTimeout('private')
        self.assertEqual(await self.cleaner.run(True), 'uncertain')
        self.assertEqual(await self.cleaner.run(True), 'uncertain')
        other = ConfirmedCleanup(self.cloud,self.path,{'version':1,'targets':self.manifest['targets'][:1]})
        try:
            self.assertEqual(await other.run(True), 'blocked')
        finally:
            other.db.close()
        self.cloud.raw.assert_awaited_once()

    async def test_inflight_recovery_never_resubmits(self):
        self.cleaner.save('submitting')
        self.assertEqual(await self.cleaner.run(True), 'uncertain')
        self.cloud.raw.assert_not_awaited()
