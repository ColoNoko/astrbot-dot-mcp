"""Owner-only group boundary tests with synthetic AstrBot API objects."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from test_main import Event, Plain, At, load_main
from test_host import Binding, Store, Rejected


def group_event(group='300'):
    event=Event(); event.private=False; event.group=group
    event.unified_msg_origin='instance-1:GroupMessage:200_'+group
    event.message_obj.raw_message.update(message_type='group',group_id=int(group), message=[
        {'type':'text','data':{'text':' hello '}}, {'type':'at','data':{'qq':'100'}},
        {'type':'text','data':{'text':' world '}}])
    event.messages=[Plain('hello'),At('100'),Plain('world')]
    return event

class GroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.module=load_main(self.tmp.name)
        self.config={'enabled':True,'platform_id':'instance-1','adapter':'aiocqhttp',
                     'self_id':'100','sender_id':'200','mcp_token':'x'*32,'group_ids':['300','400']}
        self.plugin=self.module.Main(object(),self.config); await self.plugin.initialize()
    async def asyncTearDown(self):
        await self.plugin.terminate(); self.tmp.cleanup()
    async def accept(self,event):
        await self.plugin.on_group_message(event)
        self.assertTrue(event.stopped)
        return next(reversed(self.plugin.captured))
    async def test_owner_real_at_group_reply_and_scope(self):
        e=group_event(); mid=await self.accept(e)
        data=self.plugin.host.store.read(mid)
        self.assertEqual(data['conversation_type'],'group'); self.assertEqual(data['group_id'],'300')
        self.assertEqual(data['sender_id'],'200'); self.assertEqual(data['privacy_scope'],'group_shared')
        self.assertEqual(data['text'],' hello  world ')
        self.assertEqual((await self.plugin.host.reply(mid,'[CQ:at,qq=all]','k'))['state'],'adapter_completed')
        self.assertEqual(e.sent[0][0].text,'[CQ:at,qq=all]')
    async def test_private_scope_unchanged(self):
        e=Event(); await self.plugin.on_private_message(e); mid=next(iter(self.plugin.captured))
        data=self.plugin.host.store.read(mid)
        self.assertEqual((data['conversation_type'],data['group_id'],data['privacy_scope']),('private',None,'private_owner_only'))
    async def test_default_empty_whitelist_never_accepts_groups(self):
        self.config.pop('group_ids'); e=group_event()
        await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_other_group_and_other_owner_rejected(self):
        e=group_event('500'); await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
        e=group_event(); e.sender='999'; e.message_obj.raw_message['user_id']=999
        e.message_obj.raw_message['sender']['user_id']=999
        await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_raw_sender_route_and_identifier_mismatches(self):
        for key,value in [('sender',{'user_id':999}),('user_id',999),('group_id',400),
                          ('self_id',999),('message_id','other'),('group_id',True)]:
            e=group_event(); e.message_obj.raw_message[key]=value
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_text_at_atall_and_other_target_are_not_bot_mentions(self):
        for message in ([{'type':'text','data':{'text':'@100 [CQ:at,qq=100]'}}],
                        [{'type':'at','data':{'qq':'all'}},{'type':'text','data':{'text':'hello'}}],
                        [{'type':'at','data':{'qq':'999'}},{'type':'text','data':{'text':'hello'}}]):
            e=group_event(); e.message_obj.raw_message['message']=message; e.messages=[Plain('hello')]
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_opaque_segments_preserve_authorized_group_body(self):
        for kind in ('reply','forward','node','image','record','video','file','markdown','mface'):
            e=group_event(); e.message_obj.raw_message['message'].append({'type':kind,'data':{'text':'100'}})
            e.message_obj.message_id=kind; e.message_obj.raw_message['message_id']=kind
            mid=await self.accept(e)
            marker='[引用消息]' if kind=='reply' else '[暂不支持的消息]'
            self.assertEqual(self.plugin.host.store.read(mid)['text'],' hello  world '+marker)
    async def test_raw_mention_authoritative_when_normalizer_drops_at(self):
        e=group_event(); e.messages=[Plain('hello'),Plain('world')]
        await self.accept(e)
    async def test_wrong_normalized_at_or_text_rejected(self):
        for segments in ([Plain('hello'),At('all'),Plain('world')], [Plain('changed'),At('100'),Plain('world')]):
            e=group_event(); e.messages=segments
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_removed_group_prevents_reply(self):
        e=group_event(); mid=await self.accept(e); self.config['group_ids']=['400']
        self.assertEqual((await self.plugin.host.reply(mid,'x','k'))['state'],'not_sent'); self.assertEqual(e.sent,[])
    async def test_changed_group_route_cannot_redirect_reply(self):
        e=group_event(); mid=await self.accept(e)
        e.group='400'; e.message_obj.raw_message['group_id']=400
        self.assertEqual((await self.plugin.host.reply(mid,'x','k'))['state'],'not_sent'); self.assertEqual(e.sent,[])
    async def test_group_removal_after_awaited_preflight_prevents_send(self):
        e=group_event(); mid=await self.accept(e); entered=asyncio.Event(); release=asyncio.Event()
        original=self.plugin.host.preflight
        async def wait(row):
            result=await original(row); entered.set(); await release.wait(); return result
        self.plugin.host.preflight=wait
        task=asyncio.create_task(self.plugin.host.reply(mid,'x','k'))
        await entered.wait(); self.config['group_ids']=[]; release.set()
        self.assertEqual((await task)['state'],'not_sent'); self.assertEqual(e.sent,[])
    async def test_cross_conversation_ids_do_not_collide(self):
        private=Event(); await self.plugin.on_private_message(private)
        private_mid=next(iter(self.plugin.captured))
        first=group_event('300'); first_mid=await self.accept(first)
        second=group_event('400'); second_mid=await self.accept(second)
        self.assertEqual(len({private_mid,first_mid,second_mid}),3)
        await self.plugin.on_group_message(group_event('300'))
        self.assertEqual(self.plugin.host.store.status()['messages'],3)
        await self.plugin.host.reply(first_mid,'only group 300','k')
        self.assertEqual(private.sent,[]); self.assertEqual(second.sent,[])
    async def test_malformed_group_whitelist_fails_closed(self):
        for groups in (['300','300'],[300],[True],['all'],None):
            self.config['group_ids']=groups; e=group_event()
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)

class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'old.sqlite3'
        self.binding=Binding('instance-1','aiocqhttp','100','200')
        db=sqlite3.connect(self.path)
        db.executescript('''CREATE TABLE binding(value TEXT NOT NULL);
        CREATE TABLE messages(id TEXT PRIMARY KEY, source_id TEXT UNIQUE NOT NULL,
            umo TEXT NOT NULL,text TEXT NOT NULL,created INTEGER NOT NULL);
        CREATE TABLE replies(reply_key TEXT PRIMARY KEY,message_id TEXT NOT NULL REFERENCES messages(id),
            text TEXT NOT NULL,state TEXT NOT NULL);
        CREATE TABLE retained_events(message_id TEXT, payload TEXT);''')
        db.execute('INSERT INTO binding VALUES (?)',(json.dumps(asdict(self.binding),sort_keys=True),))
        db.execute('INSERT INTO messages VALUES (?,?,?,?,?)',('msg_'+'a'*32,'same','old-umo','private-old',123))
        db.execute('INSERT INTO replies VALUES (?,?,?,?)',('old-key','msg_'+'a'*32,'old-reply','sending'))
        db.execute('INSERT INTO retained_events VALUES (?,?)',('msg_'+'a'*32,'opaque-event'))
        db.commit(); db.close()
    def tearDown(self): self.tmp.cleanup()
    def test_migration_preserves_ids_replies_events_and_marks_private(self):
        store=Store(self.path,self.binding)
        try:
            row=store.read('msg_'+'a'*32)
            self.assertEqual((row['text'],row['conversation_type'],row['sender_id'],row['privacy_scope']),
                             ('private-old','private','200','private_owner_only'))
            self.assertEqual(store.claim('msg_'+'a'*32,'old-reply','old-key'),('uncertain',False))
            self.assertEqual(store.db.execute('SELECT payload FROM retained_events').fetchone()[0],'opaque-event')
            self.assertEqual(store.db.execute('PRAGMA foreign_key_check').fetchall(),[])
            mid,_=store.ingest({'source_id':'same','umo':'group-umo','text':'group-new',
                'conversation_type':'group','group_id':'300','sender_id':'200'})
        finally: store.close()
        store=Store(self.path,self.binding)
        try: self.assertEqual(store.read(mid)['group_id'],'300')
        finally: store.close()
    def test_failed_migration_rolls_back_all_old_tables(self):
        class Broken(Store):
            def _create_tables(self,suffix=''):
                super()._create_tables(suffix)
                raise RuntimeError('simulated disk/schema failure')
        with self.assertRaises(RuntimeError): Broken(self.path,self.binding)
        db=sqlite3.connect(self.path)
        try:
            self.assertEqual(db.execute('SELECT text FROM messages').fetchone()[0],'private-old')
            self.assertNotIn('conversation_type',{r[1] for r in db.execute('PRAGMA table_info(messages)')})
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE '%_v2'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT state FROM replies').fetchone()[0],'sending')
        finally: db.close()
    def test_wrong_binding_does_not_migrate_or_rebind(self):
        with self.assertRaises(Rejected): Store(self.path,Binding('different','aiocqhttp','100','200'))
        db=sqlite3.connect(self.path)
        try: self.assertNotIn('conversation_type',{r[1] for r in db.execute('PRAGMA table_info(messages)')})
        finally: db.close()

if __name__=='__main__': unittest.main()
