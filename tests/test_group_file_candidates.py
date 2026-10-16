"""Synthetic group source/request tests. No QQ, NAS or external downloads."""
import asyncio
import json
import tempfile
import types
import unittest
from unittest.mock import patch

from test_main import Plain, At, load_main
from test_groups import group_event
from test_media_snapshot import File
from test_attachments import FakeTransport


class Events:
    def __init__(self): self.enqueued=[]; self.wakes=0
    def enqueue(self, mid, now, **kwargs): self.enqueued.append(mid); return 1
    def notify_enqueued(self): self.wakes+=1


class GroupCandidateIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.module=load_main(self.tmp.name)
        self.config={'enabled':True, 'platform_id':'instance-1', 'adapter':'aiocqhttp',
            'self_id':'100', 'sender_id':'200', 'mcp_token':'x'*32, 'access_rules_version':1,
            'private_sender_ids':['200'], 'group_file_cache_enabled':True,
            'group_rules':[{'group_id':group, 'mode':'at', 'at_sender_scope':'all', 'sender_ids':[]}
                           for group in ('300','400')]}
        self.p=self.module.Main(object(),self.config); await self.p.initialize()
        self.p.media_types={'file':File}
        self.transport=FakeTransport(body=b'hello file')
        await self.p.host.store.attachments.transport.close()
        self.p.host.store.attachments.transport=self.transport
        self.calls=[]
        async def call_action(**kwargs):
            self.calls.append(kwargs); return {'url':'https://media.example.com/f'}
        self.bot=types.SimpleNamespace(call_action=call_action)
        self.events=Events(); self.p.host.store.events=self.events

    async def asyncTearDown(self):
        self.p.host.store.events=None
        await self.p.terminate(); self.tmp.cleanup()

    def event(self, source, text='', *, sender='200', group='300', files=(), mention=False, reply=None):
        e=group_event(group); e.sender=sender; e.bot=self.bot; e.get_self_id=lambda:'100'
        e.message_obj.message_id=source; raw=e.message_obj.raw_message
        raw.update(message_id=source,user_id=int(sender),sender={'user_id':int(sender)})
        raw['message']=[]; e.messages=[]
        if reply is not None: raw['message'].append({'type':'reply','data':{'id':reply}})
        if mention:
            raw['message'].append({'type':'at','data':{'qq':'100'}});e.messages.append(At('100'))
        if text:
            raw['message'].append({'type':'text','data':{'text':text}});e.messages.append(Plain(text.strip()))
        for filename in files:
            raw['message'].append({'type':'file','data':{'file_id':'id-'+filename,'name':filename}})
            e.messages.append(File())
        return e

    async def tick(self, count=10):
        for _ in range(count): await asyncio.sleep(0)

    async def candidate(self, source='file1', filename='report.txt', **kwargs):
        e=self.event(source,files=[filename],**kwargs)
        await self.p._on_message(e); await self.tick()
        return e

    async def request(self, source='request1', text='读取这个文件', **kwargs):
        e=self.event(source,text,mention=True,**kwargs)
        await self.p._on_message(e);await self.tick()
        return e

    def latest(self): return next(reversed(self.p.captured))

    async def test_file_first_prefetch_is_silent_then_one_event_and_exact_source(self):
        source=await self.candidate(); candidate=next(iter(self.p.group_candidates.candidates.values()))
        await candidate.task
        self.assertFalse(source.stopped)
        self.assertEqual(self.p.host.store.status()['messages'],0)
        self.assertEqual(self.events.enqueued,[]);self.assertEqual(self.events.wakes,0)
        self.assertEqual(len(self.transport.calls),1)
        request=await self.request(); self.assertTrue(request.stopped)
        mid=self.latest(); value=self.p.host.store.read(mid)
        self.assertEqual(value['group_file_context']['status'],'matched')
        self.assertEqual(value['trigger_evidence'],{'configured_group_mode':'at','top_level_bot_mention':True,'trigger_matched':True})
        self.assertEqual(len(value['attachments']),1)
        self.assertEqual(json.loads(self.p.host.store.message(mid)['media_json']),[])
        aid=value['attachments'][0]['attachment_id']
        result=await self.p._read_attachment(mid,aid,include_raw=False)
        self.assertFalse(result.get('isError',False));self.assertEqual(len(self.transport.calls),1)
        self.assertEqual((await self.p.host.reply(mid,'done','k'))['state'],'adapter_completed')
        self.assertEqual(len(request.sent),1);self.assertEqual(source.sent,[])
        self.assertEqual(self.events.wakes,1)
        self.assertEqual(self.calls,[{'action':'get_group_file_url','file_id':'id-report.txt','group_id':300,'self_id':100}])

    async def test_request_first_duplicate_request_and_file_publish_once(self):
        first=await self.request(); self.assertTrue(first.stopped)
        self.assertEqual(self.events.wakes,0)
        await self.p._on_message(first);await self.tick()
        self.assertEqual(len(self.p.group_candidates.requests),1)
        source=await self.candidate();await self.tick()
        self.assertEqual(self.events.wakes,1)
        await self.p._on_message(source);await self.p._on_message(first);await self.tick()
        self.assertEqual(self.events.wakes,1);self.assertEqual(len(self.p.group_candidates.requests),0)

    async def test_read_joins_prefetch_and_regular_chat_does_not_wait(self):
        self.transport.block=asyncio.Event()
        await self.candidate();await self.transport.started.wait()
        await self.request();mid=self.latest();aid=self.p.host.store.read(mid)['attachments'][0]['attachment_id']
        read=asyncio.create_task(self.p._read_attachment(mid,aid,include_raw=False));await self.tick()
        self.assertFalse(read.done())
        await self.request('chat','今天过得怎么样');self.assertEqual(self.events.wakes,2)
        self.transport.block.set();await read
        self.assertEqual(len(self.transport.calls),1)

    async def test_no_intent_no_match_and_no_fake_at(self):
        await self.candidate()
        await self.request(text='文件在哪里');self.assertEqual(self.events.wakes,1)
        self.assertEqual(self.p.host.store.read(self.latest())['attachments'],[])
        await self.request('no','不要读取文件');self.assertEqual(self.events.wakes,2)
        fake=self.event('fake','@100 读取文件')
        await self.p._on_message(fake);self.assertFalse(fake.stopped)
        self.assertEqual(self.events.wakes,2)

    async def test_ambiguity_is_per_attachment_and_filename_can_disambiguate(self):
        source=self.event('multi',files=['one.txt','two.txt'])
        await self.p._on_message(source);await self.tick()
        await self.request(reply='multi')
        value=self.p.host.store.read(self.latest())
        self.assertEqual(value['attachments'],[])
        self.assertEqual(value['group_file_context']['status'],'ambiguous')
        self.assertEqual(len(value['group_file_context']['choices']),2)
        await self.request('choose','读取 two.txt',reply='multi')
        self.assertEqual(self.p.host.store.read(self.latest())['attachments'][0]['name'],'two.txt')

    async def test_exact_raw_reply_beats_filename_and_no_get_msg(self):
        await self.candidate('first','one.txt');await self.candidate('second','two.txt')
        await self.request(reply='first')
        value=self.p.host.store.read(self.latest());self.assertEqual(value['attachments'][0]['name'],'one.txt')
        await self.request('wrong','读取 two.txt',reply='first')
        self.assertEqual(len(self.p.group_candidates.requests),1)
        self.assertTrue(all(call['action']=='get_group_file_url' for call in self.calls))

    async def test_same_identifiers_never_cross_sender_group_bot_or_private(self):
        await self.candidate('same','same.txt',group='400')
        await self.candidate('same','same.txt',sender='201')
        source=await self.candidate('changed','same.txt')
        source.bot=types.SimpleNamespace(call_action=self.bot.call_action)
        await self.request(reply='same')
        self.assertEqual(self.events.wakes,0)
        self.assertEqual(len(self.p.group_candidates.requests),1)
        self.assertEqual(self.p.host.store.status()['messages'],0)

    async def test_invalid_source_routes_cannot_download_or_suppress(self):
        for field,value in [('self_id',999),('group_id',400),('user_id',201),('post_type','notice')]:
            source=self.event('bad-'+field,files=['secret.txt'])
            source.message_obj.raw_message[field]=value
            await self.p._on_message(source);self.assertFalse(source.stopped)
        source=self.event('platform',files=['secret.txt']);source.platform='different'
        # get_platform_id is authoritative, independent of unused event attrs.
        source.get_platform_id=lambda:'wrong-platform'
        await self.p._on_message(source);await self.tick()
        self.assertEqual(self.calls,[]);self.assertEqual(self.events.wakes,0)

    async def test_expiry_disable_revocation_and_raw_mutation_deny_reads(self):
        source=await self.candidate();await self.request();mid=self.latest()
        aid=self.p.host.store.read(mid)['attachments'][0]['attachment_id']
        candidate=next(iter(self.p.group_candidates.candidates.values()))
        candidate.expires_at=0
        with self.assertRaises(Exception):await self.p._read_attachment(mid,aid)
        self.assertEqual(self.p.host.store.read(mid)['attachments'],[])
        self.assertEqual(self.p.host.store.read(mid)['group_file_context']['linked_attachment_state'],'expired')
        candidate.expires_at=self.p.group_candidates.clock()+60
        self.config['group_file_cache_enabled']=False
        with self.assertRaises(Exception):await self.p._read_attachment(mid,aid)
        self.config['group_file_cache_enabled']=True
        source.message_obj.raw_message['message'][0]['data']['file_id']='other'
        with self.assertRaises(Exception):await self.p._read_attachment(mid,aid)

    async def test_request_timeout_publishes_once_and_late_file_does_not_retrigger(self):
        await self.request();request=next(iter(self.p.group_candidates.requests.values()))
        request.deadline=0;request.wake.set();await self.tick()
        self.assertEqual(self.events.wakes,1)
        self.assertEqual(self.p.host.store.read(self.latest())['group_file_context']['status'],'no_file_found')
        await self.candidate();await self.tick();self.assertEqual(self.events.wakes,1)

    async def test_only_one_waiting_request_per_sender_group(self):
        await self.request('first');await self.request('second')
        self.assertEqual(len(self.p.group_candidates.requests),1)
        self.assertEqual(self.p.host.store.read(self.latest())['group_file_context']['status'],'request_queue_full')
        await self.candidate();self.assertEqual(self.events.wakes,2)

    async def test_full_store_rolls_back_linked_attachments_and_events(self):
        await self.candidate()
        def full(*args,**kwargs):raise self.module.Rejected('storage_full')
        self.p.host.store.history.admit=full
        await self.request()
        self.assertEqual(self.events.wakes,0);self.assertEqual(self.p.host.store.status()['messages'],0)
        self.assertEqual(self.p.host.store.db.execute('SELECT count(*) FROM message_attachments').fetchone()[0],0)
        self.assertEqual(self.p.runtime_error,'storage_full')

    async def test_feature_off_preserves_old_noninterception(self):
        self.config['group_file_cache_enabled']=False
        source=await self.candidate();self.assertFalse(source.stopped)
        self.assertEqual(self.calls,[]);self.assertEqual(len(self.p.group_candidates.candidates),0)
        await self.request();self.assertEqual(self.events.wakes,1)
        self.assertEqual(self.p.host.store.read(self.latest())['attachments'],[])

    async def test_candidate_duplicate_does_not_refresh_ttl_or_download(self):
        source=await self.candidate();item=next(iter(self.p.group_candidates.candidates.values()))
        await item.task;expires=item.expires_at
        await self.p._on_message(source);await self.tick()
        self.assertEqual(item.expires_at,expires);self.assertEqual(len(self.transport.calls),1)

    async def test_trigger_evidence_survives_restart_with_unknown_legacy(self):
        await self.request('ordinary','hello')
        mid=self.latest();self.p.host.store.events=None
        await self.p._stop_runtime();await self.p._start_enabled()
        value=self.p.host.store.read(mid);self.assertTrue(value['trigger_evidence']['top_level_bot_mention'])
        with self.p.host.store.db:
            self.p.host.store.db.execute("UPDATE messages SET trigger_evidence_json='{}' WHERE id=?",(mid,))
        self.assertIsNone(self.p.host.store.read(mid)['trigger_evidence']['top_level_bot_mention'])

    async def test_all_and_selected_modes_keep_immediate_text_behavior(self):
        for mode in ('all','selected'):
            self.config['group_rules'][0].update(mode=mode,sender_ids=['200'])
            await self.request(mode,'读取这个文件')
            self.assertEqual(len(self.p.group_candidates.requests),0)
            self.assertEqual(self.p.host.store.read(self.latest())['attachments'],[])
        self.assertEqual(self.events.wakes,2)

    async def test_expired_replay_is_not_a_new_candidate_or_download(self):
        source=await self.candidate();item=next(iter(self.p.group_candidates.candidates.values()))
        await item.task;item.expires_at=0;self.p._expire_candidates()
        self.assertEqual(len(self.p.group_candidates.candidates),0)
        await self.p._on_message(source);await self.tick()
        self.assertEqual(len(self.p.group_candidates.candidates),0)
        self.assertEqual(len(self.transport.calls),1)

    async def test_restart_preserves_trigger_but_not_candidate_access(self):
        await self.candidate();await self.request();mid=self.latest()
        aid=self.p.host.store.read(mid)['attachments'][0]['attachment_id']
        self.p.host.store.events=None
        await self.p._stop_runtime();await self.p._start_enabled()
        value=self.p.host.store.read(mid)
        self.assertEqual(value['text'],'读取这个文件')
        self.assertTrue(value['trigger_evidence']['top_level_bot_mention'])
        self.assertEqual(value['attachments'],[])
        self.assertEqual(value['group_file_context']['linked_attachment_state'],'unavailable')
        with self.assertRaises(Exception):await self.p._read_attachment(mid,aid)

    async def test_revocation_cancels_waiting_network_on_cleanup(self):
        self.transport.block=asyncio.Event()
        await self.candidate();await self.transport.started.wait()
        item=next(iter(self.p.group_candidates.candidates.values()))
        self.config['group_rules']=[]
        self.p._expire_candidates();await self.tick()
        self.assertTrue(item.task.cancelled())
        self.assertEqual(len(self.p.group_candidates.candidates),0)
        self.assertEqual(self.events.wakes,0)

    async def test_late_sqlite_full_rolls_back_all_public_rows(self):
        import sqlite3
        await self.candidate()
        register=self.p.host.store.attachments.register
        def failing(*args,**kwargs):
            register(*args,**kwargs)
            error=sqlite3.OperationalError('synthetic full');error.sqlite_errorcode=sqlite3.SQLITE_FULL
            raise error
        self.p.host.store.attachments.register=failing
        await self.request()
        self.assertEqual(self.events.wakes,0)
        self.assertEqual(self.p.host.store.db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        self.assertEqual(self.p.host.store.db.execute('SELECT count(*) FROM message_attachments').fetchone()[0],0)
        self.assertEqual(self.p.runtime_error,'storage_full')

    async def test_filename_substring_and_english_substring_are_not_intent(self):
        await self.candidate(filename='a.txt')
        await self.request('thread','a thread about the file')
        self.assertEqual(len(self.p.group_candidates.requests),0)
        await self.request('named','read data.txt')
        self.assertEqual(len(self.p.group_candidates.requests),1)
        self.assertEqual(self.events.wakes,1)

    async def test_prefetch_timing_is_separate_from_trigger_timing(self):
        await self.candidate();item=next(iter(self.p.group_candidates.candidates.values()));await item.task
        await self.request()
        context=self.p.host.store.read(self.latest())['group_file_context']
        self.assertGreater(context['source_received_at_ms'],0)
        timing=context['prefetch_timing'];self.assertIsInstance(timing['elapsed_ms'],int)
        self.assertGreaterEqual(timing['finished_at_ms'],timing['started_at_ms'])

    async def test_raw_candidate_budget_does_not_intercept_or_download(self):
        source=self.event('large',files=['a.txt'])
        source.message_obj.raw_message['extra']='x'*65537
        await self.p._on_message(source);await self.tick()
        self.assertFalse(source.stopped);self.assertEqual(self.calls,[])
        self.assertEqual(self.p._candidate_status()['rejected_count'],1)

    async def test_current_transport_configuration_mismatch_blocks_prefetch(self):
        self.transport.attachment_dns='google'
        source=await self.candidate();await self.tick()
        self.assertFalse(source.stopped)
        self.assertEqual(self.calls,[]);self.assertEqual(self.transport.calls,[])
        self.p._expire_candidates();self.assertEqual(len(self.p.group_candidates.candidates),0)

    async def test_capability_questions_never_link_or_wait(self):
        for cached in (False, True):
            if cached:
                await self.candidate()
            for index, text in enumerate(('你能读取文件吗', '支持分析文件吗', '怎样打开文件', '分析文件')):
                await self.request(f'capability-{cached}-{index}', text)
                self.assertEqual(self.p.host.store.read(self.latest())['attachments'], [])
                self.assertEqual(len(self.p.group_candidates.requests), 0)

    async def test_explicit_file_request_still_links(self):
        await self.candidate(filename='report.pdf')
        await self.request('explicit-reference', '请分析这个文件')
        self.assertEqual(len(self.p.host.store.read(self.latest())['attachments']), 1)
        await self.request('explicit-name', '分析 report.pdf')
        self.assertEqual(len(self.p.host.store.read(self.latest())['attachments']), 1)
        await self.request('explicit-reply', '读一下', reply='file1')
        self.assertEqual(len(self.p.host.store.read(self.latest())['attachments']), 1)

    async def test_chinese_adjacent_filename_request_links_exact_source(self):
        filename='qianzi-gongfang-source-latest.zip'
        await self.candidate(filename=filename)
        for index, suffix in enumerate(('帮我分析一下，并将分析结果用md文档回传本群。',
                                        ' 帮我分析一下', '，请分析', '。请分析',
                                        ', please analyze', '. Please analyze', '; please analyze')):
            await self.request(f'adjacent-{index}', filename+suffix)
            value=self.p.host.store.read(self.latest())
            self.assertEqual(value['group_file_context']['status'], 'matched')
            self.assertEqual(len(value['attachments']), 1)

    async def test_extended_filename_suffix_never_selects_shorter_candidate(self):
        await self.candidate(filename='report.zip')
        for index, suffix in enumerate(('x', '2', '_backup', '-backup', '.exe', '/payload', '\\payload')):
            await self.request(f'bad-suffix-{index}', 'report.zip'+suffix+'帮我分析一下')
            self.assertEqual(self.p.host.store.read(self.latest())['attachments'], [])
            self.assertEqual(len(self.p.group_candidates.requests), 0)

    async def test_adjacent_filename_keeps_capability_and_ambiguity_guards(self):
        await self.candidate(filename='report.zip')
        await self.request('capability-adjacent', '你能分析report.zip吗')
        self.assertEqual(self.p.host.store.read(self.latest())['attachments'], [])
        await self.candidate(source='other-file', filename='report.zip')
        await self.request('ambiguous-adjacent', 'report.zip帮我分析一下')
        value=self.p.host.store.read(self.latest())
        self.assertEqual(value['group_file_context']['status'], 'ambiguous')
        self.assertEqual(value['attachments'], [])

    async def test_delayed_file_request_does_not_gain_reminder_eligibility_after_toggle(self):
        self.config['experimental_unread_reminders']=False
        await self.request('before-enable')
        self.assertEqual(len(self.p.group_candidates.requests),1)
        self.config['experimental_unread_reminders']=True
        await self.candidate()
        await self.tick()
        self.assertFalse(self.p.host.store.processing_watch.get(self.latest())['eligible_at_capture'])
