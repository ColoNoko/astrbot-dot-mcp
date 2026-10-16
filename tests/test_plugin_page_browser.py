"""Headless browser tests with a synthetic bridge; this is NOT a live AstrBot test.

Optional QA dependency: playwright + a local Chromium binary. No external requests,
real credentials, MCP calls, or Tunnel process are used. Production uses only the
injected AstrBotPluginPage bridge; this fixture never ships inside the page.
"""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import shutil
import threading
import unittest

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

PAGE_DIR = Path(__file__).resolve().parents[1] / 'pages' / 'connection'
CHROMIUM = shutil.which('chromium') or shutil.which('chromium-browser')
FIXTURE = r"""
(() => {
  const fixture = window.__fixture = {
    calls: [], failNext: '', failGet: false, hold: false, release: null,
    tools: {version: '0.5.0', tools: [{name: 'qq_read_attachment', description: '读取附件', inputSchema: {type: 'object', properties: {include_raw: {type: 'boolean', default: true}}}}]},
    data: {
      settings: {enabled: false, platform_id: 'qq-main', adapter: 'aiocqhttp', self_id: '123456789',
        listen_host: '127.0.0.1', listen_port: 8769, callback_hosts: ['callback.example.com'],
        tunnel_id: 'tunnel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', tunnel_enabled: false, proxy_enabled: false, proxy_url: ''},
      secrets: {mcp_token_set: true, tunnel_key_set: true, proxy_username_set: false, proxy_password_set: false},
      platforms: [{id: 'qq-main', adapter: 'aiocqhttp'}, {id: 'qq-other', adapter: 'aiocqhttp'},
        {id: 'official-qq', adapter: 'qq_official'}],
      runtime: {mcp_ready: false, tunnel: {state: 'stopped', process_running: false,
        healthy: false, ready: false, connected: null, roundtrip_verified: false, binary_available: true}},
      csrf_token: 'synthetic-csrf-for-local-test', revision: 1, versions: {plugin: '0.3.2'},
      mcp_token: 'SERVER_SECRET_MUST_NEVER_APPEAR', tunnel_key: 'SERVER_KEY_MUST_NEVER_APPEAR',
      proxy_username: 'SERVER_USERNAME_MUST_NEVER_APPEAR', proxy_password: 'SERVER_PASSWORD_MUST_NEVER_APPEAR'
    }
  };
  const clone = () => JSON.parse(JSON.stringify(fixture.data));
  window.AstrBotPluginPage = {
    ready: async () => ({isDark: false}), onContext: () => () => {},
    apiGet: async (endpoint) => {
      fixture.calls.push({method: 'GET', endpoint});
      if (fixture.failGet) throw new Error('synthetic failure');
      if (endpoint === 'tools') return JSON.parse(JSON.stringify(fixture.tools));
      return clone();
    },
    apiPost: async (endpoint, body) => {
      fixture.calls.push({method: 'POST', endpoint, body});
      if (fixture.hold) await new Promise((resolve) => { fixture.release = resolve; });
      if (fixture.failNext) { const code = fixture.failNext; fixture.failNext = ''; throw new Error(code); }
      if (body.revision !== fixture.data.revision) throw new Error('revision_conflict');
      if (endpoint === 'settings') {
        Object.assign(fixture.data.settings, body.settings);
        if (body.tunnel_key) fixture.data.secrets.tunnel_key_set = true;
        if (body.clear_proxy_auth) fixture.data.secrets.proxy_username_set = fixture.data.secrets.proxy_password_set = false;
        if (body.proxy_username) fixture.data.secrets.proxy_username_set = true;
        if (body.proxy_password) fixture.data.secrets.proxy_password_set = true;
        fixture.data.runtime.mcp_ready = false;

      } else if (body.action === 'apply') {
        fixture.data.runtime.mcp_ready = fixture.data.settings.enabled;
      } else if (body.action === 'stop') fixture.data.runtime.mcp_ready = false;
      fixture.data.revision += 1;
      fixture.data.csrf_token = 'synthetic-csrf-' + fixture.data.revision;
      return clone();
    }
  };
})();
"""


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.environ.get('RUN_BROWSER_QA') == '1' and sync_playwright and CHROMIUM,
                     'Optional browser QA requires explicit RUN_BROWSER_QA=1 in a permitted environment')
class PluginPageBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(PAGE_DIR)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/index.html'
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(executable_path=CHROMIUM, headless=True, args=['--no-sandbox'])
        except Exception as exc:
            cls.playwright.stop()
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join()
            if 'Operation not permitted' in str(exc):
                raise unittest.SkipTest('Chromium cannot launch: executor blocks required process sockets') from None
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        self.context.add_init_script(FIXTURE)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(self.url)
        self.page.wait_for_function("document.querySelector('#save-state').textContent === '设置已保存'")

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.context.close()

    def posts(self):
        return self.page.evaluate("__fixture.calls.filter((call) => call.method === 'POST')")

    def test_readonly_initial_load_and_writeonly_secrets(self):
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.page.locator('#platform-id option').count(), 3)
        self.assertEqual(self.page.locator('#mcp-token, #generate-token, #confirmation').count(), 0)
        self.assertIn('自动管理', self.page.inner_text('#mcp-auth-state'))
        self.assertEqual(self.page.input_value('#proxy-username'), '')
        self.assertEqual(self.page.input_value('#proxy-password'), '')
        self.assertFalse(self.page.is_checked('#proxy-enabled'))
        self.assertEqual(self.page.input_value('#tunnel-key'), '')
        self.assertNotIn('SERVER_SECRET', self.page.content())
        self.assertNotIn('SERVER_KEY', self.page.content())
        self.assertNotIn('SERVER_USERNAME', self.page.content())
        self.assertNotIn('SERVER_PASSWORD', self.page.content())
        self.assertTrue(self.page.locator('#save').is_disabled())
        self.assertIn('已关闭', self.page.inner_text('#plugin-status'))
        self.assertIn('尚未验证', self.page.inner_text('.status-footer'))

    def test_dns_selector_proxy_gate_roundtrip_and_no_credential_changes(self):
        self.assertEqual(self.page.input_value('#callback-dns'), 'system')
        self.assertEqual(self.page.locator('#callback-dns option').all_text_contents(),
                         ['系统DNS', 'Google加密DNS（经代理）'])
        self.assertEqual(self.page.input_value('#callback-backend'), 'aiohttp')
        self.page.select_option('#callback-dns', 'google')
        self.page.click('#save')
        self.assertEqual(self.posts(), [])
        self.assertIn('必须明确启用现有代理', self.page.inner_text('#notice'))
        self.assertFalse(self.page.is_checked('#proxy-enabled'))
        self.page.check('#proxy-enabled')
        self.page.fill('#proxy-url', 'http://192.0.2.10:7890')
        self.page.click('#save')
        self.page.wait_for_function('__fixture.data.revision === 2')
        body = self.posts()[0]['body']
        self.assertEqual(body['settings']['callback_dns'], 'google')
        self.assertEqual(body['settings']['callback_backend'], 'aiohttp')
        for key in ('proxy_username', 'proxy_password', 'mcp_token', 'tunnel_key'):
            self.assertNotIn(key, body)
        self.page.click('#apply')
        self.page.wait_for_function('__fixture.data.revision === 3')
        self.assertEqual(self.posts()[1]['body']['action'], 'apply')
        self.page.select_option('#callback-dns', 'system')
        self.page.click('#discard')
        self.assertEqual(self.page.input_value('#callback-dns'), 'google')
        self.assertEqual(len(self.posts()), 2)

    def test_save_minimal_payload_and_refresh_credentials(self):
        self.page.fill('#listen-port', '8770')
        self.page.click('#save')
        self.page.wait_for_function("__fixture.data.revision === 2 && !document.querySelector('#apply').disabled")
        body = self.posts()[0]['body']
        self.assertEqual(body['settings']['listen_port'], 8770)
        self.assertNotIn('enabled', body['settings'])
        self.assertNotIn('mcp_token', body)
        self.assertNotIn('tunnel_key', body)
        self.assertEqual(body['csrf_token'], 'synthetic-csrf-for-local-test')
        self.page.click('#apply')
        self.page.wait_for_function('__fixture.data.revision === 3')
        self.assertEqual(self.posts()[1]['body']['csrf_token'], 'synthetic-csrf-2')
        self.assertFalse(self.page.evaluate('__fixture.data.settings.enabled'))

    def test_failed_save_preserves_inputs_without_secret_echo(self):
        self.page.fill('#tunnel-key', 'synthetic-replacement-token-for-tests-only')
        self.page.evaluate("__fixture.failNext = 'save_failed'")
        self.page.click('#save')
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('保存失败')")
        self.assertEqual(self.page.input_value('#tunnel-key'), 'synthetic-replacement-token-for-tests-only')
        self.assertNotIn('synthetic-replacement', self.page.inner_text('body'))
        self.assertTrue(self.page.locator('#apply').is_disabled())
        self.assertEqual(len(self.posts()), 1)

    def test_refresh_preserves_edits_and_detects_revision_conflict(self):
        self.page.fill('#listen-port', '8770')
        self.page.evaluate("__fixture.data.settings.listen_port = 9000; __fixture.data.revision = 7")
        self.page.click('#refresh')
        self.page.wait_for_selector('#conflict', state='visible')
        self.assertEqual(self.page.input_value('#listen-port'), '8770')
        self.assertTrue(self.page.locator('#save').is_disabled())
        self.page.click('#reload')
        self.page.wait_for_function("document.querySelector('#listen-port').value === '9000'")
        self.assertTrue(self.page.locator('#conflict').is_hidden())
        self.assertEqual(self.posts(), [])

    def test_apply_cannot_double_submit(self):
        self.page.evaluate('__fixture.hold = true')
        self.page.click('#apply')
        self.page.wait_for_function('__fixture.release !== null')
        self.page.evaluate("document.querySelector('#apply').click()")
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(self.page.locator('#apply').is_disabled())
        self.page.evaluate('__fixture.release()')
        self.page.wait_for_function("!document.querySelector('#apply').disabled")
        self.assertEqual(self.posts()[0]['body']['action'], 'apply')

    def test_proxy_payload_writeonly_auth_and_explicit_clear(self):
        self.page.check('#proxy-enabled')
        self.page.fill('#proxy-url', 'http://192.0.2.10:7890')
        self.page.fill('#proxy-username', 'synthetic-user')
        self.page.fill('#proxy-password', 'synthetic-password')
        self.page.click('#save')
        self.page.wait_for_function('__fixture.data.revision === 2')
        body = self.posts()[0]['body']
        self.assertTrue(body['settings']['proxy_enabled'])
        self.assertEqual(body['proxy_username'], 'synthetic-user')
        self.assertEqual(body['proxy_password'], 'synthetic-password')
        self.assertFalse(body['clear_proxy_auth'])
        self.assertEqual(self.page.input_value('#proxy-username'), '')
        self.assertEqual(self.page.input_value('#proxy-password'), '')
        self.page.fill('#proxy-url', 'http://192.0.2.20:7890')
        self.page.click('#save')
        self.assertEqual(len(self.posts()), 1)
        self.assertIn('重新填写', self.page.inner_text('#notice'))
        self.page.check('#clear-proxy-auth')
        self.page.click('#save')
        self.page.wait_for_function('__fixture.data.revision === 3')
        cleared = self.posts()[1]['body']
        self.assertTrue(cleared['clear_proxy_auth'])
        self.assertNotIn('proxy_username', cleared)
        self.assertNotIn('proxy_password', cleared)
        self.assertFalse(self.page.is_checked('#clear-proxy-auth'))

    def test_proxy_rejects_non_http_urls_and_embedded_auth(self):
        self.page.check('#proxy-enabled')
        for url in ['socks5://192.0.2.10:7890', 'https://192.0.2.10:7890',
                    'http://user:password@192.0.2.10:7890', 'http://192.0.2.10',
                    'http://192.0.2.10:7890/path']:
            self.page.fill('#proxy-url', url)
            self.page.click('#save')
            self.assertEqual(self.posts(), [])
            self.assertIn('HTTP 代理地址', self.page.inner_text('#notice'))

    def test_stop_preserves_dirty_form(self):
        self.page.fill('#self-id', '987654321')
        self.page.click('#stop')
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('服务已停止')")
        self.assertEqual(self.page.input_value('#self-id'), '987654321')
        self.assertEqual(self.posts()[0]['body']['action'], 'stop')

    def test_validation_and_no_public_listener(self):
        self.page.fill('#callback-hosts', 'https://not-a-host.example.com')
        self.page.click('#save')
        self.assertIn('回调域名', self.page.inner_text('#notice'))
        self.assertEqual(self.posts(), [])
        self.assertEqual(self.page.locator('#listen-host option').count(), 1)
        self.assertEqual(self.page.get_attribute('#listen-port', 'min'), '1024')

    def test_untrusted_error_is_not_rendered_and_failure_disables_actions(self):
        self.page.evaluate("__fixture.failNext = 'raw-secret-do-not-echo'; __fixture.failGet = true")
        self.page.click('#apply')
        self.page.wait_for_function("document.querySelector('#notice').textContent.includes('未确认')")
        self.assertNotIn('raw-secret', self.page.inner_text('body'))
        self.assertTrue(self.page.locator('#apply').is_disabled())
        self.assertTrue(self.page.locator('#stop').is_disabled())
        self.assertEqual(len(self.posts()), 1)

    def test_mobile_desktop_dark_no_overflow(self):
        screenshots = os.environ.get('DOT_PAGE_SCREENSHOTS')
        for width, height, theme in [(1440, 1100, 'light'), (390, 844, 'light'), (390, 844, 'dark'), (320, 720, 'light')]:
            self.page.set_viewport_size({'width': width, 'height': height})
            self.page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
            self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
            if screenshots and width != 320:
                root = Path(screenshots)
                root.mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=str(root / f'connection-{width}-{theme}.png'), full_page=True)

    def test_tools_query_is_explicit_readonly_and_preserves_dirty_form(self):
        self.assertEqual(self.page.evaluate("__fixture.calls.filter(c => c.endpoint === 'tools').length"), 0)
        self.page.fill('#listen-port', '9000')
        self.page.click('#query-tools')
        self.page.wait_for_function("document.querySelector('#tools-status').textContent.includes('1 个工具')")
        self.assertEqual(self.page.input_value('#listen-port'), '9000')
        self.assertEqual(self.posts(), [])
        self.assertIn('客户端元数据未刷新', self.page.inner_text('#tools-status'))
        self.page.click('#tools-list summary')
        self.assertIn('include_raw', self.page.inner_text('#tools-list pre'))
        self.assertTrue(self.page.locator('#save').is_enabled())

    def test_help_is_local_and_does_not_change_connection_form(self):
        self.page.fill('#listen-port', '9000')
        with self.context.expect_page() as opened:
            self.page.click('a[href="./help.html"]')
        help_page = opened.value
        help_page.wait_for_load_state()
        self.assertTrue(help_page.url.endswith('/help.html'))
        self.assertIn('明文传输', help_page.inner_text('body'))
        self.assertIn('不获得插件管理权', help_page.inner_text('body'))
        self.assertEqual(self.page.input_value('#listen-port'), '9000')
        self.assertEqual(self.posts(), [])
        for width in (1440, 390, 320):
            help_page.set_viewport_size({'width': width, 'height': 900})
            self.assertLessEqual(help_page.evaluate('document.documentElement.scrollWidth'), width)
        help_page.close()

    def test_response_text_is_inert(self):
        self.page.evaluate("__fixture.data.versions.plugin = '<script>bad()</script>'")
        self.page.click('#refresh')
        self.page.wait_for_function("document.querySelector('#version').textContent.includes('<script>')")
        self.assertEqual(self.page.locator('#platform-detail img').count(), 0)
        self.assertEqual(self.page.locator('#version script').count(), 0)

    def test_sender_routes_desktop_mobile_status_and_native_config_preservation(self):
        self.page.evaluate("""() => {
            const data = __fixture.data;
            data.settings.enabled = true;
            data.settings.tunnel_enabled = true;
            data.settings.tunnel_id = '';
            data.settings.sender_routes = [
                {sender_id: '12345678901234567890', tunnel_id: 'tunnel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', listen_port: 8770},
                {sender_id: '23456789012345678901', tunnel_id: 'tunnel_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', listen_port: 8771},
            ];
            data.runtime.mcp_ready = true;
            data.runtime.routes = [
                {sender_id: '12345678901234567890', listen_port: 8770, mcp_ready: true,
                    tunnel: {state: 'ready', ready: true, healthy: true, binary_available: true}},
                {sender_id: '23456789012345678901', listen_port: 8771, mcp_ready: false,
                    error_code: 'startup_failed', tunnel: {state: 'error', last_error_code: 'binary_checksum_mismatch'}},
            ];
        }""")
        routes = self.page.evaluate('__fixture.data.settings.sender_routes')
        self.page.click('#refresh')
        self.page.wait_for_selector('#sender-routes-panel', state='visible')
        self.assertEqual(self.page.locator('#sender-routes-list > li').count(), 2)
        self.assertIn('发送者 QQ 12345678901234567890', self.page.inner_text('#sender-routes-list'))
        self.assertIn('127.0.0.1:8771/mcp', self.page.inner_text('#sender-routes-list'))
        self.assertIn('校验失败', self.page.inner_text('#sender-routes-list'))
        self.assertEqual(self.page.inner_text('#mcp-status'), '1 / 2 本地已启动')
        self.assertEqual(self.page.inner_text('#tunnel-status'), '1 / 2 本地就绪')
        self.assertTrue(self.page.locator('#save').is_disabled())
        self.assertTrue(self.page.locator('#reconnect-tunnel').is_disabled())
        self.page.evaluate("document.querySelector('#reconnect-tunnel').click()")
        self.assertEqual(self.posts(), [])

        screenshots = os.environ.get('DOT_PAGE_SCREENSHOTS')
        for width, height, theme in [(1440, 1100, 'light'), (390, 844, 'light'), (390, 844, 'dark'), (320, 720, 'light')]:
            with self.subTest(width=width, theme=theme):
                self.page.set_viewport_size({'width': width, 'height': height})
                self.page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
                self.assertTrue(self.page.locator('#sender-routes-panel').is_visible())
                for box in self.page.locator('#sender-routes-list > li').evaluate_all(
                        '(rows) => rows.map(row => {const r = row.getBoundingClientRect(); return {left: r.left, right: r.right, client: row.clientWidth, scroll: row.scrollWidth};})'):
                    self.assertGreaterEqual(box['left'], 0)
                    self.assertLessEqual(box['right'], width)
                    self.assertLessEqual(box['scroll'], box['client'])
                if screenshots:
                    root = Path(screenshots)
                    root.mkdir(parents=True, exist_ok=True)
                    self.page.screenshot(path=str(root / f'sender-routes-{width}-{theme}.png'), full_page=True)
                    self.page.locator('.status-panel').screenshot(path=str(root / f'sender-routes-status-{width}-{theme}.png'))

        self.page.fill('#self-id', '34567')
        self.page.click('#save')
        self.page.wait_for_function("__fixture.data.revision === 2 && !document.querySelector('#apply').disabled")
        self.assertEqual(len(self.posts()), 1)
        self.assertNotIn('sender_routes', self.posts()[0]['body']['settings'])
        self.assertEqual(self.posts()[0]['body']['settings']['tunnel_id'], '')
        self.assertEqual(self.page.evaluate('__fixture.data.settings.sender_routes'), routes)
        self.assertTrue(self.page.locator('#save').is_disabled())
        self.assertTrue(self.page.locator('#reconnect-tunnel').is_disabled())


if __name__ == '__main__':
    unittest.main()
