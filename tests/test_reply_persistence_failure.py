import asyncio
import sqlite3
import tempfile
from pathlib import Path
import unittest
from test_host import Store, BINDING, Event, Plain, snapshot, Host, Rejected

class ReplyPersistenceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name)/'inbox.sqlite3',BINDING)
        self.mid,_=self.store.ingest(snapshot(Event(),BINDING,Plain)); self.sent=[]
        async def preflight(row):return object()
        async def send(target,row,text):self.sent.append(text); return True
        self.host=Host(self.store,preflight,send)
    async def asyncTearDown(self):
        await self.host.close();self.store.close();self.tmp.cleanup()
    async def test_finish_full_keeps_durable_claim_and_never_resends(self):
        def full(*args):
            error=sqlite3.OperationalError('synthetic full')
            error.sqlite_errorcode=sqlite3.SQLITE_FULL
            raise error
        self.store.finish=full
        result=await self.host.reply(self.mid,'hello','k')
        self.assertEqual(result['state'],'uncertain');self.assertEqual(result['storage_error'],'storage_full')
        self.assertEqual(self.store.db.execute('SELECT state FROM replies').fetchone()[0],'sending')
        self.assertEqual((await self.host.reply(self.mid,'hello','k'))['state'],'sending')
        self.assertEqual(self.sent,['hello'])
    async def test_internal_send_override_still_claims_and_deduplicates(self):
        calls=[]
        async def media(target,row,text):calls.append(row['id']);return True
        self.assertEqual((await self.host.reply(self.mid,'fingerprint','m',_send_override=media))['state'],'adapter_completed')
        await self.host.reply(self.mid,'fingerprint','m',_send_override=media)
        self.assertEqual(calls,[self.mid]);self.assertEqual(self.sent,[])
    async def test_reserved_media_prefix_rejected_for_text(self):
        with self.assertRaises(Rejected):await self.host.reply(self.mid,'[dot-media-v1]:{}','bad')
        self.assertEqual(self.sent,[])

    async def test_cancel_preserved_if_finish_also_full(self):
        entered=asyncio.Event()
        async def sending(*args):
            entered.set()
            await asyncio.Event().wait()
        self.host.send=sending
        def full(*args):
            error=sqlite3.OperationalError('synthetic full')
            error.sqlite_errorcode=sqlite3.SQLITE_FULL
            raise error
        self.store.finish=full
        task=asyncio.create_task(self.host.reply(self.mid,'hello','cancel'))
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.store.db.execute('SELECT state FROM replies').fetchone()[0],'sending')
