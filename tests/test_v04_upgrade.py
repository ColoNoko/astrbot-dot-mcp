"""Actual frozen v0.3.3 host creates a synthetic DB; no real AstrBot installer."""
import importlib.util
import sys
import sqlite3
import tempfile
from pathlib import Path
import unittest
import zipfile
from test_host import Store, BINDING

class UpgradeV04Tests(unittest.TestCase):
    def test_actual_previous_host_database_preserved_and_converted(self):
        root=Path(__file__).resolve().parents[1]
        archive=root/'dist/astrbot_plugin_dot_private_v0.3.3.zip'
        if not archive.exists():
            self.skipTest('Frozen old archive is external to installable package')
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory);modulepath=directory/'oldhost.py'
            with zipfile.ZipFile(archive) as package:
                modulepath.write_bytes(package.read('astrbot_plugin_dot_private/host.py'))
            spec=importlib.util.spec_from_file_location('synthetic_old_host',modulepath)
            module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
            old=module.Store(directory/'inbox.sqlite3',module.Binding('instance-1','mock','100','200'))
            pending,_=old.ingest({'source_id':'old-pending','umo':'private','text':'keep pending'})
            complete,_=old.ingest({'source_id':'old-complete','umo':'private','text':'done'})
            old.claim(complete,'reply','old-key');old.finish('old-key','adapter_completed');old.close()
            new=Store(directory/'inbox.sqlite3',BINDING)
            try:
                self.assertEqual(new.read(pending)['processing_state'],'pending')
                self.assertEqual(new.read(complete)['processing_state'],'completed')
                self.assertEqual(new.claim(complete,'reply','old-key'),('adapter_completed',False))
                self.assertEqual(new.db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
                self.assertEqual(new.db.execute('PRAGMA user_version').fetchone()[0],3)
                self.assertEqual(new.ingest({'source_id':'old-pending','umo':'private','text':'keep pending'}),(pending,False))
            finally:new.close()

    def test_v3_schema_changes_rollback_together_when_pages_full(self):
        from history_store import BoundedHistory
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'tiny.sqlite3';db=sqlite3.connect(path);db.row_factory=sqlite3.Row
            db.executescript("""CREATE TABLE messages(id TEXT PRIMARY KEY,source_id TEXT,umo TEXT,text TEXT,
                created INTEGER,conversation_type TEXT,group_id TEXT,sender_id TEXT);
                CREATE TABLE replies(reply_key TEXT PRIMARY KEY,message_id TEXT,text TEXT,state TEXT);
                INSERT INTO messages VALUES('m','s','umo','preserve',1,'private','','200');
                PRAGMA user_version=2;""")
            before=[row['name'] for row in db.execute('PRAGMA table_info(messages)')]
            count=db.execute('PRAGMA page_count').fetchone()[0]
            db.execute('PRAGMA max_page_count='+str(count))
            try:
                with self.assertRaises(sqlite3.Error):BoundedHistory(db,path)
                self.assertEqual([row['name'] for row in db.execute('PRAGMA table_info(messages)')],before)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],2)
                self.assertEqual(db.execute('SELECT text FROM messages').fetchone()[0],'preserve')
            finally:db.close()
