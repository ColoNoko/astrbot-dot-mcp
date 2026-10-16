import copy,hashlib,json,unittest
from unittest.mock import patch
from test_endpoint import endpoint_module as endpoint

class ToolManifestTests(unittest.TestCase):
    def test_exact_public_schema_hash_and_unknown_client(self):
        expected=hashlib.sha256(json.dumps(endpoint.TOOLS,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        value=endpoint.tool_manifest()
        self.assertEqual(value['sha256'],expected);self.assertEqual(value['count'],7)
        self.assertEqual(value['client_visible_tools'],'unknown');self.assertFalse(value['force_client_refresh_supported'])
    def test_changed_schema_changes_fingerprint_and_snapshot_is_detached(self):
        before=endpoint.tool_manifest();changed=copy.deepcopy(endpoint.TOOLS);changed[0]['description']+=' synthetic'
        with patch.object(endpoint,'TOOLS',changed):self.assertNotEqual(before['sha256'],endpoint.tool_manifest()['sha256'])
        before['names'].clear();self.assertEqual(endpoint.tool_manifest()['count'],7)
    def test_compat_protocol_is_static_and_respects_old_small_chunk(self):
        value=endpoint.media_compatibility();self.assertEqual(value['text_prefix'],'DOT_MEDIA_V1\n')
        self.assertEqual(value['legacy_small_chunk_bytes'],8192);self.assertEqual(value['client_large_payload_support'],'unverified')
        self.assertEqual(set(value['operations']),{'return_attachment','inline','begin','chunk','commit','abort'})
