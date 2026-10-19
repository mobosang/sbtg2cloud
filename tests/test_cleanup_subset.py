import json
import tempfile
import unittest
from app.cleanup import cleanup_snapshot_matches, resume_reduced_cleanup
from app.store import Store


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.a = ['root', 'a', 1, 'episode1', 10, 'hash', 1, 1]
        self.b = ['root', 'b', 1, 'episode2', 20, 'hash2', 1, 1]

    def matches(self, old, new):
        return cleanup_snapshot_matches(json.dumps(old), json.dumps(new))

    def test_removed_reordered_and_empty_descendants(self):
        self.assertTrue(self.matches([self.a, self.b], [self.b]))
        self.assertTrue(self.matches([self.a, self.b], [self.b, self.a]))
        self.assertTrue(self.matches([self.a, self.b], []))
        folder = ['root', 'nested', 2, 'season', None, None, None, None]
        child = [*self.a]; child[0] = 'nested'
        self.assertTrue(self.matches([folder, child, self.b], [self.b]))

    def test_new_modified_moved_renamed_replaced_and_duplicates_rejected(self):
        for index in range(8):
            changed = self.a.copy()
            changed[index] = 2 if index == 2 else 'different'
            self.assertFalse(self.matches([self.a, self.b], [changed]), index)
        self.assertFalse(self.matches([self.a], [self.a, self.b]))
        self.assertFalse(self.matches([self.a], [self.a, self.a]))
        self.assertFalse(self.matches([self.a, self.a], [self.a]))

    def test_multiple_roots_and_standalone_files(self):
        old = json.dumps([json.dumps([self.a, self.b]), json.dumps([self.b])])
        new = json.dumps([json.dumps([self.a]), json.dumps([])])
        self.assertTrue(cleanup_snapshot_matches(old, new, True))
        self.assertFalse(cleanup_snapshot_matches(old, json.dumps([json.dumps([self.a])]), True))
        file = ['a', 'episode', 10, 'hash', 1, 1]
        self.assertTrue(self.matches(file, file))
        self.assertFalse(self.matches(file, []))
        self.assertFalse(self.matches(file, [*file[:-1], 2]))
        for invalid in ('bad', '{}', 'null', '[1]'):
            self.assertFalse(cleanup_snapshot_matches(invalid, invalid))


class ResumeTests(unittest.TestCase):
    def test_only_unsubmitted_owned_mismatches_resume_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            try:
                for i in range(1, 6):
                    store.enqueue({'share_id':str(i),'url':'https://example.com','code':''},
                                  {'id':1,'target_id':'parent','target_name':'/Media'},i,'title')
                    store.update_job(i,status='success',cleanup_enabled=1,cleanup_state='blocked',
                                     cleanup_manifest='[]',cleanup_folder=str(i),
                                     cleanup_error='目录位置或内容变化，或核对失败；请手动检查，不会自动删除')
                    store.claim_cleanup(i,[str(i)])
                store.update_job(2,cleanup_task='already-submitted')
                store.update_job(3,cleanup_state='uncertain')
                store.execute('INSERT INTO directory_clear_claims VALUES (?,?)',('4','clear'))
                store.update_job(5,cleanup_manifest='')
                resume_reduced_cleanup(store)
                self.assertEqual([r['cleanup_state'] for r in store.rows('SELECT cleanup_state FROM jobs ORDER BY id')],
                                 ['pending','blocked','uncertain','blocked','blocked'])
                store.update_job(1,cleanup_state='blocked')
                resume_reduced_cleanup(store)
                self.assertEqual(store.rows('SELECT cleanup_state FROM jobs WHERE id=1')[0]['cleanup_state'],'blocked')
                self.assertEqual(store.rows('SELECT cleanup_manifest FROM jobs WHERE id=1')[0]['cleanup_manifest'],'[]')
            finally:
                store.db.close()
