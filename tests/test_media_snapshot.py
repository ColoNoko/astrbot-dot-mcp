"""Captured raw media validation, no downloads or real adapter."""
import unittest
from test_main import Event, Plain, At
from test_host import snapshot, BINDING, Rejected

class Image: pass
class File: pass
MEDIA = {'image': Image, 'file': File}

class MediaSnapshotTests(unittest.TestCase):
    def event(self, raw, normalized):
        event=Event()
        event.message_obj.raw_message['message']=raw
        event.messages=normalized
        return event
    def snap(self, event):
        from dataclasses import replace
        return snapshot(event, replace(BINDING, adapter='aiocqhttp'), Plain, At, media_types=MEDIA)
    def test_image_only_uses_raw_url_not_normalized_path(self):
        image=Image(); image.path='/etc/passwd'; image.url='https://wrong.example/file'
        value=self.snap(self.event([{'type':'image','data':{'url':'https://media.example/image.png'}}],[image]))
        self.assertEqual(value['text'],'')
        self.assertEqual(value['attachments'][0]['url'],'https://media.example/image.png')
        self.assertNotIn('/etc/passwd',str(value))
    def test_file_segment_indices_and_ids_are_not_first_segment(self):
        value=self.snap(self.event([{'type':'text','data':{'text':' inspect '}},
          {'type':'file','data':{'file_id':'second','name':'two.txt'}},
          {'type':'file','data':{'file_id':'third','name':'three.pdf'}}], [Plain('inspect'),File(),File()]))
        self.assertEqual([(x['segment_index'],x['file_id']) for x in value['attachments']],[(1,'second'),(2,'third')])
    def test_normalizer_dropped_file_is_still_captured(self):
        value=self.snap(self.event([{'type':'file','data':{'file_id':'f'}}],[]))
        self.assertEqual(len(value['attachments']),1)
    def test_quote_marker_preserved_but_unbacked_media_and_unbound_sender_rejected(self):
        value=self.snap(self.event([{'type':'reply','data':{'id':'x'}},{'type':'image','data':{}}],[Image()]))
        self.assertEqual(value['text'],'[引用消息]')
        self.assertEqual(value['attachments'][0]['segment_index'],1)
        with self.assertRaises(Rejected):
            self.snap(self.event([{'type':'forward','data':{'id':'x'}}],[Image()]))
        event=self.event([{'type':'image','data':{}}],[Image()]);event.sender='999'
        with self.assertRaises(Rejected):self.snap(event)
    def test_extra_normalized_media_and_text_mismatch_rejected(self):
        for normalized in [[Image(),Image()],[Plain('invented'),Image()]]:
            with self.assertRaises(Rejected):self.snap(self.event([{'type':'image','data':{}}],normalized))
    def test_media_opt_in_and_bound_limit(self):
        event=self.event([{'type':'image','data':{}}],[Image()])
        with self.assertRaises(Rejected):snapshot(event,BINDING,Plain)
        with self.assertRaises(Rejected):self.snap(self.event([{'type':'image','data':{}}]*9,[Image()]*9))
