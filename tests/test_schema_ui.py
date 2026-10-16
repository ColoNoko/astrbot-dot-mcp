"""Display-only schema and byte-identical user icon regression tests."""
import hashlib
import json
from pathlib import Path
import struct
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASELINE={
 'enabled':{'type':'bool','default':False},
 'platform_id':{'type':'string','default':''},
 'adapter':{'type':'string','default':''},
 'self_id':{'type':'string','default':''},
 'sender_id':{'type':'string','default':''},
 'mcp_token':{'type':'string','default':'','secret':True},
 'callback_hosts':{'type':'list','default':[]},
 'listen_host':{'type':'string','default':'127.0.0.1'},
 'listen_port':{'type':'int','default':8769},
 'group_ids':{'type':'list','default':[]},
 'callback_backend':{'type':'string','default':'aiohttp'},
 'proxy_enabled':{'type':'bool','default':False},
 'proxy_url':{'type':'string','default':''},
 'proxy_username':{'type':'string','default':'','secret':True},
 'proxy_password':{'type':'string','default':'','secret':True},
 'tunnel_id':{'type':'string','default':''},
 'tunnel_enabled':{'type':'bool','default':False},
 'tunnel_runtime_key':{'type':'string','default':'','secret':True},
 'group_trigger_mode':{'type':'string','default':'owner_at','options':['owner_at','all_messages']},
}

class SchemaUITests(unittest.TestCase):
    def setUp(self): self.schema=json.loads((ROOT/'_conf_schema.json').read_text())
    def test_every_label_is_short_and_has_separate_supported_hint(self):
        for key,item in self.schema.items():
            with self.subTest(key=key):
                self.assertTrue(1 <= len(item['description']) <= 10)
                self.assertNotIn('：',item['description'])
                self.assertTrue(1 <= len(item['hint']) <= 150)
                self.assertFalse(item.get('obvious_hint',False))
    def test_all_stored_keys_types_defaults_and_permissions_unchanged(self):
        semantic={key:{name:value for name,value in item.items()
                       if name not in ('description','hint','labels','invisible')}
                  for key,item in self.schema.items() if key in BASELINE}
        self.assertEqual(semantic,BASELINE)
    def test_only_basic_keys_visible_and_legacy_keys_retained(self):
        visible={key for key,item in self.schema.items() if not item.get('invisible')}
        self.assertEqual(visible,{'enabled','sender_id','private_sender_ids','group_rules','remote_diagnostics_enabled','group_file_cache_enabled','experimental_unread_reminders','sender_routes'})
        self.assertTrue(set(BASELINE) <= self.schema.keys())
        self.assertFalse(self.schema['tunnel_enabled']['default'])
    def test_chinese_dropdown_titles_preserve_exact_machine_options(self):
        item=self.schema['group_trigger_mode']
        self.assertEqual(item['options'],['owner_at','all_messages'])
        self.assertEqual(item['labels'],['仅本人 @ 时回复','白名单群所有支持消息'])
        self.assertEqual(len(item['labels']),len(item['options']))
    def test_callback_dns_is_opt_in_hidden_and_does_not_replace_legacy_backend(self):
        item=self.schema['callback_dns']
        self.assertEqual(item['type'],'string')
        self.assertEqual(item['default'],'system')
        self.assertEqual(item['options'],['system','google'])
        self.assertEqual(item['labels'],['系统DNS','Google加密DNS（经代理）'])
        self.assertTrue(item['invisible'])
        self.assertTrue(self.schema['callback_backend']['invisible'])
        self.assertNotIn('options', self.schema['callback_backend'])
        self.assertNotIn('labels', self.schema['callback_backend'])

    def test_user_logo_is_original_png_with_official_filename(self):
        payload=(ROOT/'logo.png').read_bytes()
        self.assertEqual(payload[:8],b'\x89PNG\r\n\x1a\n')
        self.assertEqual(struct.unpack('>II',payload[16:24]),(370,370))
        self.assertEqual(len(payload),215581)
        self.assertEqual(hashlib.sha256(payload).hexdigest(),
                         '2f7c765f0e35044d8010ea3ed4c5a2745603566932692a9b4d152cb117452d1f')

if __name__=='__main__': unittest.main()

class GroupTemplateTests(unittest.TestCase):
    def test_native_group_template_defaults_to_selected_at(self):
        schema=json.loads((ROOT/'_conf_schema.json').read_text())
        self.assertEqual(schema['group_rules']['type'],'template_list')
        template=schema['group_rules']['templates']['group']
        self.assertEqual(template['display_item'],'group_id')
        fields=template['items']
        self.assertEqual(fields['mode']['default'],'at')
        self.assertEqual(fields['mode']['options'],['at','selected','all'])
        self.assertEqual(fields['at_sender_scope']['default'],'selected')
        self.assertEqual(fields['at_sender_scope']['condition'],{'mode':'at'})
        self.assertEqual(schema['access_rules_version']['default'],0)
        self.assertTrue(schema['access_rules_version']['invisible'])
