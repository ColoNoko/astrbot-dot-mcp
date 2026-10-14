"""Explicit opt-in all-sender group mode; old configs keep owner+@ behavior."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from test_main import Event, Plain, load_main
from test_groups import group_event


def all_event(sender='201', group='300', text='hello from alt'):
    e=group_event(group); e.sender=sender
    e.message_obj.raw_message['user_id']=int(sender)
    e.message_obj.raw_message['sender']['user_id']=int(sender)
    e.message_obj.raw_message['message']=[{'type':'text','data':{'text':text}}]
    e.messages=[Plain(text.strip())]
    return e

class GroupModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.module=load_main(self.tmp.name)
        self.config={'enabled':True,'platform_id':'instance-1','adapter':'aiocqhttp',
                     'self_id':'100','sender_id':'200','mcp_token':'x'*32,'group_ids':['300'],
                     'group_trigger_mode':'all_messages'}
        self.plugin=self.module.Main(object(),self.config); await self.plugin.initialize()
    async def asyncTearDown(self):
        await self.plugin.terminate(); self.tmp.cleanup()
    async def accept(self,e):
        await self.plugin.on_group_message(e); self.assertTrue(e.stopped)
        return next(reversed(self.plugin.captured))
    async def test_any_sender_without_at_is_captured_with_actual_sender_and_group(self):
        e=all_event(); mid=await self.accept(e)
        row=self.plugin.host.store.read(mid)
        self.assertEqual((row['sender_id'],row['group_id'],row['privacy_scope']),('201','300','group_shared'))
        self.assertEqual((await self.plugin.host.reply(mid,'reply in original group','k'))['state'],'adapter_completed')
        self.assertEqual(e.sent[0][0].text,'reply in original group')
    async def test_owner_without_at_is_also_accepted(self):
        await self.accept(all_event(sender='200'))
    async def test_optional_real_bot_at_remains_accepted_for_other_sender(self):
        e=group_event(); e.sender='201'
        e.message_obj.raw_message['user_id']=201; e.message_obj.raw_message['sender']['user_id']=201
        await self.accept(e)
    async def test_bot_self_echo_never_captured(self):
        e=all_event(sender='100'); await self.plugin.on_group_message(e)
        self.assertFalse(e.stopped); self.assertEqual(self.plugin.host.store.status()['messages'],0)
    async def test_outside_group_other_platform_or_self_id_never_accepted(self):
        cases=[('group','400'),('platform','other'),('adapter','other'),('bot','999')]
        for attr,value in cases:
            e=all_event(); setattr(e,attr,value)
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_raw_normalized_sender_route_and_source_mismatches_rejected(self):
        for key,value in [('user_id',202),('sender',{'user_id':202}),('self_id',999),
                          ('group_id',400),('message_id','other'),('user_id',True)]:
            e=all_event(); e.message_obj.raw_message[key]=value
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
        for value in (True,None,'0','-1','not-a-qq'):
            e=all_event(); e.sender=value
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_private_other_sender_still_rejected(self):
        e=Event(); e.sender='201'; e.message_obj.raw_message['user_id']=201
        e.message_obj.raw_message['sender']['user_id']=201
        await self.plugin.on_private_message(e); self.assertFalse(e.stopped)
    async def test_quote_media_and_other_at_components_remain_unsupported(self):
        for segment in ({'type':'reply','data':{'id':'quoted'}},
                        {'type':'forward','data':{'id':'forwarded'}},
                        {'type':'image','data':{'file':'x'}},
                        {'type':'at','data':{'qq':'all'}},
                        {'type':'at','data':{'qq':'999'}}):
            e=all_event(); e.message_obj.raw_message['message'].append(segment)
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_literal_at_text_is_normal_text_not_a_routing_instruction(self):
        e=all_event(text='@someone [CQ:reply,id=9] literal'); mid=await self.accept(e)
        self.assertEqual(self.plugin.host.store.read(mid)['text'],'@someone [CQ:reply,id=9] literal')
    async def test_duplicate_stops_without_new_message_or_second_reply(self):
        e=all_event(); mid=await self.accept(e)
        duplicate=all_event(); await self.plugin.on_group_message(duplicate)
        self.assertTrue(duplicate.stopped); self.assertEqual(self.plugin.host.store.status()['messages'],1)
        await self.plugin.host.reply(mid,'one reply','k'); await self.plugin.host.reply(mid,'one reply','k')
        self.assertEqual(len(duplicate.sent),1); self.assertEqual(e.sent,[])
    async def test_different_senders_keep_distinct_source_identity(self):
        first=await self.accept(all_event('201')); second=await self.accept(all_event('202'))
        self.assertNotEqual(first,second)
        self.assertEqual(self.plugin.host.store.read(first)['sender_id'],'201')
        self.assertEqual(self.plugin.host.store.read(second)['sender_id'],'202')
    async def test_mode_narrowing_blocks_pending_reply_and_future_capture(self):
        e=all_event(); mid=await self.accept(e); self.config['group_trigger_mode']='owner_at'
        self.assertEqual((await self.plugin.host.reply(mid,'x','k'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
        newer=all_event(); await self.plugin.on_group_message(newer); self.assertFalse(newer.stopped)
    async def test_mode_narrowing_after_preflight_cannot_late_send(self):
        e=all_event(); mid=await self.accept(e); entered=asyncio.Event(); release=asyncio.Event()
        original=self.plugin.host.preflight
        async def preflight(row):
            target=await original(row); entered.set(); await release.wait(); return target
        self.plugin.host.preflight=preflight
        task=asyncio.create_task(self.plugin.host.reply(mid,'x','k'))
        await entered.wait(); self.config['group_trigger_mode']='owner_at'; release.set()
        self.assertEqual((await task)['state'],'not_sent'); self.assertEqual(e.sent,[])
    async def test_sender_mutation_cannot_retarget_captured_reply(self):
        e=all_event(); mid=await self.accept(e); e.sender='202'
        e.message_obj.raw_message['user_id']=202; e.message_obj.raw_message['sender']['user_id']=202
        self.assertEqual((await self.plugin.host.reply(mid,'x','k'))['state'],'not_sent')
        self.assertEqual(e.sent,[])
    async def test_old_config_missing_mode_defaults_to_owner_at(self):
        self.config.pop('group_trigger_mode')
        no_at=all_event('200'); other=all_event('201')
        await self.plugin.on_group_message(no_at); await self.plugin.on_group_message(other)
        self.assertFalse(no_at.stopped); self.assertFalse(other.stopped)
        await self.accept(group_event())
    async def test_invalid_mode_fails_closed(self):
        for mode in (None,True,'all','',[]):
            self.config['group_trigger_mode']=mode; e=all_event()
            await self.plugin.on_group_message(e); self.assertFalse(e.stopped)
    async def test_restart_preserves_config_scope_dedupe_and_old_reply_keys(self):
        e=all_event(); mid=await self.accept(e)
        await self.plugin.host.reply(mid,'once','persisted-key')
        before=dict(self.config)
        await self.plugin.terminate(); await self.plugin.initialize()
        self.assertEqual(self.config,before)
        self.assertEqual(self.plugin.host.store.read(mid)['sender_id'],'201')
        self.assertEqual((await self.plugin.host.reply(mid,'once','persisted-key'))['state'],'adapter_completed')
        self.assertEqual((await self.plugin.host.reply(mid,'different','new-key'))['state'],'not_sent')
        replay=all_event(); await self.plugin.on_group_message(replay)
        self.assertEqual(self.plugin.host.store.status()['messages'],1)
        self.assertEqual(replay.sent,[])

if __name__=='__main__': unittest.main()
