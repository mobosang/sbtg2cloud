import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from scripts.diagnose_cleanup import ReadOnlyStore, unpack, differences


class DiagnosticTests(unittest.TestCase):
    def test_nested_snapshots_and_field_differences(self):
        old = json.dumps([json.dumps([['parent', 'file', 1, 'private-name', 10, None, 100, None]])])
        new = json.dumps([json.dumps([['parent', 'file', 1, 'private-name', 10, None, 101, None]])])
        delta = list(differences(unpack(old), unpack(new)))
        self.assertEqual(delta, ['$[0][0][6]: value changed (int -> int)'])
        self.assertNotIn('private-name', str(delta))

    def test_readonly_database_rejects_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            db = sqlite3.connect(os.path.join(folder, 'state.sqlite3'))
            db.execute('CREATE TABLE settings(key TEXT, value TEXT)')
            db.execute('INSERT INTO settings VALUES (?,?)', ('key', '123'))
            db.commit()
            db.close()
            with patch.dict(os.environ, {'DATA_DIR': folder}):
                store = ReadOnlyStore()
                try:
                    self.assertEqual(store.get('key'), 123)
                    with self.assertRaises(sqlite3.OperationalError):
                        store.db.execute('DELETE FROM settings')
                    with self.assertRaises(RuntimeError):
                        store.set('key', 999)
                finally:
                    store.db.close()
