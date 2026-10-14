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


@unittest.skipUnless(sync_playwright and CHROMIUM, 'Optional playwright/Chromium QA dependency not installed')
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

    def test_response_text_is_inert(self):
        self.page.evaluate("__fixture.data.versions.plugin = '<script>bad()</script>'")
        self.page.click('#refresh')
        self.page.wait_for_function("document.querySelector('#version').textContent.includes('<script>')")
        self.assertEqual(self.page.locator('#platform-detail img').count(), 0)
        self.assertEqual(self.page.locator('#version script').count(), 0)


if __name__ == '__main__':
    unittest.main()
