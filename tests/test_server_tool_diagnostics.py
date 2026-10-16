import copy,json,types,unittest,importlib.util,sys
from pathlib import Path
from unittest.mock import patch
import test_admin

class ServerToolDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=test_admin.AdminTests.asyncSetUp
    asyncTearDown=test_admin.AdminTests.asyncTearDown
    async def test_tools_are_authorized_server_definitions_and_not_client_claim(self):
        spec=importlib.util.spec_from_file_location('plugin_mock.diagnostic_endpoint',Path(__file__).resolve().parents[1]/'endpoint.py')
        endpoint=importlib.util.module_from_spec(spec);spec.loader.exec_module(endpoint)
        stub=sys.modules['plugin_mock.endpoint']
        stub.TOOLS=endpoint.TOOLS;stub.PLUGIN_VERSION=endpoint.PLUGIN_VERSION;stub.tool_manifest=endpoint.tool_manifest
        before=copy.deepcopy(dict(self.config))
        result=self.p.admin.tools(self.req)
        self.assertEqual(result['tool_count'],7)
        self.assertEqual(result['tools'],endpoint.TOOLS)
        self.assertIn('qq_reply_attachment',[tool['name'] for tool in result['tools']])
        attachment=next(t for t in result['tools'] if t['name']=='qq_read_attachment')
        self.assertIs(attachment['inputSchema']['properties']['include_raw']['default'],True)
        self.assertIs(result['client_refresh_supported'],False)
        self.assertEqual(result['client_visible_tools'],'unknown')
        self.assertEqual(dict(self.config),before)
        self.assertNotIn(self.config['mcp_token'],json.dumps(result))
        result['tools'][0]['name']='mutated'
        self.assertNotEqual(endpoint.TOOLS[0]['name'],'mutated')
        with self.assertRaisesRegex(Exception,'forbidden'):
            self.p.admin.tools(types.SimpleNamespace(username='',plugin_name='astrbot_plugin_dot_private'))
        self.p.admin.closed=True
        with self.assertRaisesRegex(Exception,'forbidden'):
            self.p.admin.tools(self.req)

    async def test_native_tools_get_is_registered_and_auth_gated(self):
        routes=[]
        self.p.context=types.SimpleNamespace(register_web_api=lambda path,handler,methods,desc:routes.append((path,handler,methods)))
        self.p._register_admin()
        route=next(row for row in routes if row[0].endswith('/tools'))
        self.assertEqual(route[2],['GET'])
        spec=importlib.util.spec_from_file_location('plugin_mock.tools_native_endpoint',Path(__file__).resolve().parents[1]/'endpoint.py')
        endpoint=importlib.util.module_from_spec(spec);spec.loader.exec_module(endpoint)
        sys.modules['plugin_mock.endpoint'].TOOLS=endpoint.TOOLS
        sys.modules['plugin_mock.endpoint'].PLUGIN_VERSION=endpoint.PLUGIN_VERSION
        sys.modules['plugin_mock.endpoint'].tool_manifest=endpoint.tool_manifest
        web=types.ModuleType('astrbot.api.web');web.request=self.req
        web.json_response=lambda value,headers:dict(status=200,value=value,headers=headers)
        web.error_response=lambda code,status_code,headers:dict(status=status_code,error=code)
        with patch.dict(sys.modules,{'astrbot.api.web':web}):
            result=await route[1]()
            self.assertEqual(result['status'],200)
            self.assertEqual(result['value']['tool_count'],7)
            self.assertEqual(result['headers']['Cache-Control'],'no-store')
            web.request=types.SimpleNamespace(username='',plugin_name='astrbot_plugin_dot_private')
            self.assertEqual((await route[1]())['status'],403)
