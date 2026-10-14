import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from host import Binding, Host, Store, Rejected, snapshot

class Plain:
    def __init__(self, text): self.text = text

class Event:
    def __init__(self):
        self.private, self.group = True, ''
        self.platform, self.adapter, self.bot, self.sender = 'instance-1', 'mock', '100', '200'
        self.message_obj = types.SimpleNamespace(message_id='original-1')
        self.unified_msg_origin = 'observed-umo'
        self.messages = [Plain('hello')]
        self.stopped = False
        self.sent = []
    def is_private_chat(self): return self.private
    def get_group_id(self): return self.group
    def get_platform_id(self): return self.platform
    def get_platform_name(self): return self.adapter
    def get_self_id(self): return self.bot
    def get_sender_id(self): return self.sender
    def get_messages(self): return self.messages
    def stop_event(self): self.stopped = True
    def plain_result(self, text): return [Plain(text)]
    async def send(self, value): self.sent.append(value)

BINDING = Binding('instance-1', 'mock', '100', '200')

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'inbox.sqlite3'
        self.store = Store(self.path, BINDING)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def test_exact_binding_group_and_malformed(self):
        for attr, value in [('private',False), ('group','123'), ('platform','other'),
                            ('adapter','other'), ('bot','999'), ('sender','999'),
                            ('messages',[]), ('messages',[object()]), ('messages',[Plain('')])]:
            event = Event(); setattr(event, attr, value)
            with self.subTest(attr=attr, value=str(value)), self.assertRaises(Rejected):
                snapshot(event, BINDING, Plain)
        self.assertEqual(snapshot(Event(), BINDING, Plain)['umo'], 'observed-umo')
    def test_malformed_identifiers_rejected(self):
        for attr, value in [('sender', True), ('bot', None), ('sender', object()), ('unified_msg_origin', '  '), ('unified_msg_origin', 'secret\n')]:
            event=Event(); setattr(event,attr,value)
            with self.assertRaises(Rejected): snapshot(event,BINDING,Plain)
        for value in (True, '', ' ', 'x\n'):
            event=Event(); event.message_obj.message_id=value
            with self.assertRaises(Rejected): snapshot(event,BINDING,Plain)
    def test_duplicate_restart_and_binding_change(self):
        value = snapshot(Event(), BINDING, Plain)
        mid, fresh = self.store.ingest(value)
        self.assertTrue(fresh)
        self.assertEqual(self.store.ingest(value), (mid, False))
        self.store.claim(mid, 'reply', 'key')
        self.store.close(); self.store = Store(self.path, BINDING)
        self.assertEqual(self.store.read(mid)['text'], 'hello')
        self.assertEqual(self.store.claim(mid, 'reply','key'), ('uncertain',False))
        self.assertEqual(self.store.message(mid)['source_id'], 'original-1')
        with self.assertRaises(Rejected): Store(self.path, Binding('x','mock','100','200'))
    def test_collision_unknown_message_and_key_conflict(self):
        value = snapshot(Event(), BINDING, Plain)
        mid, _ = self.store.ingest(value)
        with self.assertRaises(Rejected): self.store.ingest(dict(value,text='changed'))
        with self.assertRaises(Rejected): self.store.claim('arbitrary-recipient','x','k')
        self.store.claim(mid,'x','k')
        with self.assertRaises(Rejected): self.store.claim(mid,'y','k')
    def test_cq_is_literal(self):
        event=Event(); event.messages=[Plain('[CQ:at,qq=all]')]
        mid,_=self.store.ingest(snapshot(event,BINDING,Plain))
        self.assertEqual(self.store.read(mid)['text'],'[CQ:at,qq=all]')

class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'db',BINDING)
        self.mid,_=self.store.ingest(snapshot(Event(),BINDING,Plain))
        self.sent=[]
        async def preflight(row): return 'target'
        async def send(target,row,text): self.sent.append(text); return True
        self.host=Host(self.store,preflight,send,timeout=.03)
    async def asyncTearDown(self):
        await self.host.close(); self.store.close(); self.tmp.cleanup()
    async def test_adapter_completed_not_delivery_receipt_and_no_repeat(self):
        self.assertEqual((await self.host.reply(self.mid,'[CQ:at,qq=all]','key'))['state'],'adapter_completed')
        await self.host.reply(self.mid,'[CQ:at,qq=all]','key')
        self.assertEqual(self.sent,['[CQ:at,qq=all]'])
    async def test_timeout_preflight_suppress_cancel_cannot_late_send(self):
        release=asyncio.Event()
        async def slow(row):
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            return 'target'
        self.host.preflight=slow
        result=await self.host.reply(self.mid,'x','key')
        self.assertEqual(result['state'],'not_sent')
        release.set(); await asyncio.sleep(.01)
        self.assertFalse(self.host.revoked)
        self.assertEqual(self.sent,[])
        await self.host.reply(self.mid,'x','key')
        self.assertEqual(self.sent,[])
    async def test_shutdown_after_await_prevents_send(self):
        entered=asyncio.Event()
        async def slow(row):
            entered.set()
            try: await asyncio.sleep(10)
            except asyncio.CancelledError: return 'target'
        self.host.preflight=slow
        reply=asyncio.create_task(self.host.reply(self.mid,'x','key'))
        await entered.wait(); await self.host.close(); await reply
        self.assertEqual(self.sent,[])
    async def test_send_timeout_uncertain_no_retry(self):
        async def send(target,row,text): self.sent.append(text); await asyncio.sleep(10)
        self.host.send=send
        result=await self.host.reply(self.mid,'x','key')
        self.assertEqual(result['state'],'uncertain')
        await self.host.reply(self.mid,'x','key')
        self.assertEqual(self.sent,['x'])
    async def test_exception_redaction(self):
        async def send(*args): raise RuntimeError('token=secret message=private')
        self.host.send=send
        result=await self.host.reply(self.mid,'x','key')
        self.assertNotIn('secret',str(result)); self.assertEqual(result['state'],'uncertain')

if __name__ == '__main__': unittest.main()
