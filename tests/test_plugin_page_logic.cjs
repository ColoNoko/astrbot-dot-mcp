/* Synthetic DOM/bridge unit checks only. Browser layout and real AstrBot are separate QA. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const root = path.join(__dirname, '..', 'pages', 'connection');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.value = ''; this.textContent = ''; this.children = [];
    this.hidden = false; this.disabled = false; this.checked = false; this.events = {};
    this.dataset = {}; this.attributes = {};
  }
  addEventListener(name, listener) { this.events[name] = listener; }
  setAttribute(name, value) { this.attributes[name] = value; }
  replaceChildren(...children) { this.children = children; this.value = children[0]?.value || ''; }
  append(element) { this.children.push(element); }
  focus() { this.focused = true; }
  scrollIntoView() {}
}
function fixture() {
  return {
    settings: {enabled: false, platform_id: 'qq-main', adapter: 'aiocqhttp', self_id: '123456789', listen_host: '127.0.0.1', listen_port: 8769,
      callback_hosts: ['callback.example.com'], tunnel_enabled: false, tunnel_id: 'tunnel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', proxy_enabled: false, proxy_url: ''},
    secrets: {mcp_token_set: true, tunnel_key_set: true, proxy_username_set: false, proxy_password_set: false}, platforms: [{id: 'qq-main', adapter: 'aiocqhttp'}, {id: 'unsupported', adapter: 'qq_official'}],
    runtime: {mcp_ready: false, tunnel: {state: 'stopped', process_running: false, healthy: false, ready: false, binary_available: true}},
    csrf_token: 'synthetic-csrf', revision: 1, versions: {plugin: '0.3.2'},
    mcp_token: 'SERVER_SECRET_SENTINEL', tunnel_key: 'SERVER_KEY_SENTINEL', proxy_username: 'SERVER_USERNAME_SENTINEL', proxy_password: 'SERVER_PASSWORD_SENTINEL',
  };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));
async function setup({noBridge = false} = {}) {
  const elements = {};
  for (const match of html.matchAll(/<([a-z0-9]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const element = elements[match[3]] = new Element(match[1]);
    element.hidden = /\bhidden(?:\s|$|=)/.test(match[2]);
    element.disabled = /\bdisabled(?:\s|$|=)/.test(match[2]);
    element.value = match[2].match(/\bvalue="([^"]*)"/)?.[1] || '';
  }
  const calls = [];
  const data = fixture();
  const control = {failGet: false, failPost: '', hold: false, release: null};
  const clone = () => JSON.parse(JSON.stringify(data));
  const bridge = {
    ready: async () => ({isDark: false}), onContext: () => () => {},
    apiGet: async (endpoint) => { calls.push({method: 'GET', endpoint}); if (control.failGet) throw Error('get_failed'); return clone(); },
    apiPost: async (endpoint, body) => {
      calls.push({method: 'POST', endpoint, body});
      if (control.hold) await new Promise((resolve) => { control.release = resolve; });
      if (control.failPost) throw Error(control.failPost);
      if (body.revision !== data.revision) throw Error('revision_conflict');
      if (endpoint === 'settings') {
        Object.assign(data.settings, body.settings);
        if (body.tunnel_key) data.secrets.tunnel_key_set = true;
        if (body.clear_proxy_auth) data.secrets.proxy_username_set = data.secrets.proxy_password_set = false;
        if (body.proxy_username) data.secrets.proxy_username_set = true;
        if (body.proxy_password) data.secrets.proxy_password_set = true;
      }
      data.revision++;
      data.csrf_token = `synthetic-csrf-${data.revision}`;
      return clone();
    },
  };
  const context = vm.createContext({URL,
    document: {getElementById: (id) => elements[id], createElement: (tag) => new Element(tag), documentElement: new Element('html'), body: new Element('body')},
    window: {AstrBotPluginPage: noBridge ? undefined : bridge, addEventListener() {}},
  });
  vm.runInContext(source, context);
  await flush();
  async function click(id) { if (!elements[id].disabled) elements[id].events.click?.({preventDefault() {}}); await flush(); }
  function input(id, value) { elements[id].value = value; elements[id].events.input?.(); }
  function check(id, checked) { elements[id].checked = checked; elements[id].events.input?.(); }
  async function submit() { elements['settings-form'].events.submit({preventDefault() {}}); await flush(); }
  return {elements, calls, data, control, click, input, check, submit, posts: () => calls.filter((call) => call.method === 'POST')};
}

test('load is read-only, filters exact adapter, and never hydrates credentials', async () => {
  const t = await setup();
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements['platform-id'].children.length, 2);
  assert.equal(t.elements['mcp-token'], undefined);
  assert.equal(t.elements['tunnel-key'].value, '');
  assert.equal(t.elements['proxy-username'].value, '');
  assert.equal(t.elements['proxy-password'].value, '');
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, false);
  assert.equal(JSON.stringify(t.elements).includes('SERVER_'), false);
  assert.equal(t.elements['proxy-enabled'].checked, false);
  assert.match(t.elements['mcp-auth-state'].textContent, /自动管理/);
});

test('save excludes enabled and empty secrets; next request uses refreshed CSRF/revision', async () => {
  const t = await setup();
  t.input('listen-port', '9000');
  await t.submit();
  const body = t.posts()[0].body;
  assert.equal(body.settings.listen_port, 9000);
  assert.equal('enabled' in body.settings, false);
  assert.equal('mcp_token' in body, false);
  assert.equal('tunnel_key' in body, false);
  assert.equal(body.revision, 1);
  await t.click('apply');
  assert.equal(t.posts()[1].body.csrf_token, 'synthetic-csrf-2');
  assert.equal(t.posts()[1].body.revision, 2);
});

test('save failure preserves replacement secret but does not echo it', async () => {
  const t = await setup();
  t.input('tunnel-key', 'synthetic-long-replacement-token-for-tests');
  t.control.failPost = 'save_failed';
  await t.submit();
  assert.equal(t.elements['tunnel-key'].value, 'synthetic-long-replacement-token-for-tests');
  assert.match(t.elements.notice.textContent, /保存失败/);
  assert.equal(t.elements.apply.disabled, true);
  assert.equal(t.posts().length, 1);
});

test('dirty refresh preserves edits and blocks overwriting a changed revision', async () => {
  const t = await setup();
  t.input('self-id', '987654321');
  t.data.revision = 7;
  t.data.settings.self_id = '222222222';
  await t.click('refresh');
  assert.equal(t.elements['self-id'].value, '987654321');
  assert.equal(t.elements.conflict.hidden, false);
  assert.equal(t.elements.save.disabled, true);
  await t.click('reload');
  assert.equal(t.elements['self-id'].value, '222222222');
  assert.equal(t.elements.conflict.hidden, true);
});

test('manual MCP key controls and generation action do not exist', async () => {
  const t = await setup();
  for (const id of ['mcp-token', 'generate-token', 'confirm-generate', 'cancel-generate', 'confirmation']) {
    assert.equal(t.elements[id], undefined);
  }
  assert.doesNotMatch(source, /generate_mcp_token|payload\.mcp_token/);
  assert.match(html, /无需填写、生成或复制 MCP 密钥/);
  t.data.secrets.mcp_token_set = false;
  await t.click('refresh');
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements['mcp-auth-state'].textContent, '自动管理 · 启动时配置');
});

test('apply is serialized and cannot double-submit', async () => {
  const t = await setup();
  t.control.hold = true;
  await t.click('apply');
  await t.click('apply');
  assert.equal(t.posts().length, 1);
  t.control.release();
  await flush();
  assert.equal(t.posts()[0].body.action, 'apply');
});

test('stop preserves unsaved form and never sends form values', async () => {
  const t = await setup();
  t.input('self-id', '987654321');
  await t.click('stop');
  assert.equal(t.elements['self-id'].value, '987654321');
  assert.equal(t.posts()[0].body.action, 'stop');
  assert.equal('settings' in t.posts()[0].body, false);
});

test('invalid host, port and callback hosts cannot issue POST', async () => {
  for (const [id, value] of [['listen-host', '0.0.0.0'], ['listen-port', '80'], ['callback-hosts', 'https://example.com'],
    ['callback-hosts', 'a.example.com\na.example.com'], ['callback-hosts', 'EXAMPLE.COM'], ['self-id', '0']]) {
    const t = await setup();
    t.input(id, value);
    await t.submit();
    assert.equal(t.posts().length, 0, id);
    assert.equal(t.elements.notice.className, 'notice error');
  }
});

test('untrusted backend errors are not rendered; unreconciled failure fails closed', async () => {
  const t = await setup();
  t.control.failPost = 'raw-secret-error';
  t.control.failGet = true;
  await t.click('apply');
  assert.equal(t.elements.notice.textContent.includes('raw-secret'), false);
  assert.equal(t.elements.apply.disabled, true);
  assert.equal(t.elements.stop.disabled, true);
  assert.equal(t.posts().length, 1);
});

test('missing official bridge cannot mutate or claim readiness', async () => {
  const t = await setup({noBridge: true});
  assert.equal(t.calls.length, 0);
  assert.equal(t.elements.apply.disabled, true);
  assert.match(t.elements.notice.textContent, /插件详情/);
});

test('local healthy process never claims remote or QQ success', async () => {
  const t = await setup();
  t.data.runtime.tunnel.ready = true;
  t.data.runtime.tunnel.healthy = true;
  t.data.runtime.tunnel.connected = true;
  t.data.runtime.tunnel.roundtrip_verified = true;
  await t.click('refresh');
  assert.equal(t.elements['tunnel-status'].textContent, '本地就绪');
  assert.equal(t.elements['tunnel-health'].textContent, '本地检查通过');
  assert.ok(html.includes('QQ 消息往返 <strong>尚未验证</strong>'));
  assert.ok(html.includes('<span>远端连接</span><strong>尚未验证</strong>'));
});

test('blank disabled setup stays clean and allows explicit apply', async () => {
  const t = await setup();
  t.data.settings.platform_id = '';
  t.data.settings.adapter = '';
  t.data.settings.self_id = '';
  t.data.settings.tunnel_id = '';
  await t.click('refresh');
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, false);
  t.input('listen-port', '9000');
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal(t.posts()[0].body.settings.adapter, '');
});

test('runtime and supervisor error codes render only fixed actionable messages', async () => {
  const t = await setup();
  t.data.runtime.error_code = 'tunnel_stop_incomplete';
  t.data.runtime.tunnel.last_error_code = 'binary_checksum_mismatch';
  await t.click('refresh');
  assert.equal(t.elements['runtime-issue'].hidden, false);
  assert.match(t.elements['runtime-issue'].textContent, /尚未完全停止/);
  assert.match(t.elements['runtime-issue'].textContent, /校验失败/);
  assert.equal(t.elements['runtime-issue'].textContent.includes('binary_checksum'), false);
  t.data.runtime.error_code = 'raw-unknown-secret';
  t.data.runtime.tunnel.last_error_code = 'raw-unknown-key';
  await t.click('refresh');
  assert.equal(t.elements['runtime-issue'].hidden, true);
});

test('static entry uses external module, relative assets, original PNG, no unsafe DOM or credential storage', () => {
  assert.match(html, /<script type="module" src="\.\/app\.js"><\/script>/);
  assert.doesNotMatch(source, /innerHTML|outerHTML|localStorage|sessionStorage|document\.cookie|\bfetch\(/);
  assert.match(source, /window\.AstrBotPluginPage/);
  assert.equal(Buffer.compare(fs.readFileSync(path.join(root, 'assets', 'logo.png')), fs.readFileSync(path.join(root, '..', '..', 'logo.png'))), 0);
});

test('Chinese platform survives discovery dropdown selection and save', async () => {
  const t = await setup();
  t.data.platforms = [{id:'示例机器人',adapter:'aiocqhttp'}];
  t.data.settings.platform_id='示例机器人';
  await t.click('refresh');
  assert.equal(t.elements['platform-status'].textContent,'实例已识别');
  assert.equal(t.elements['platform-id'].value,'示例机器人');
  t.input('listen-port','9000'); await t.submit();
  assert.equal(t.posts()[0].body.settings.platform_id,'示例机器人');
});
test('Unicode ID uses codepoints and preserves exact identity without normalization', async () => {
  const t = await setup(); const id='🤖'.repeat(128);
  t.data.platforms=[{id,adapter:'aiocqhttp'},{id:'bad\u200b',adapter:'aiocqhttp'}];
  t.data.settings.platform_id=id; await t.click('refresh');
  assert.equal(t.elements['platform-id'].value,id);
  assert.equal(t.elements['platform-status'].textContent,'实例已识别');
  assert.equal(t.elements['platform-id'].children.some(o=>o.value==='bad\u200b'),false);
});

test('malformed configured platform identity is rejected rather than sanitized', async () => {
  const t = await setup(); t.data.settings.platform_id='qq\n-main';
  await t.click('refresh');
  assert.equal(t.elements.save.disabled,true);
  assert.equal(t.posts().length,0);
});


test('proxy enable requires a valid explicit-port HTTP URL', async () => {
  for (const url of ['', 'http://proxy.example', 'https://proxy.example:7890', 'socks5://proxy.example:7890',
    'http://user:password@proxy.example:7890', 'http://user@proxy.example:7890', 'http://proxy.example:7890/path',
    'http://proxy.example:7890?secret=abc', 'http://proxy.example:7890#fragment', 'http://proxy.example:0',
    'http://proxy.example:65536', 'http://proxy.example:7890\\anything', 'http://proxy.example:7890/..',
    'http://proxy.example:7890?','http://proxy.example:7890#', 'http://bad host:7890', 'http://0.0.0.0:7890', 'http://224.0.0.1:7890', 'http://[::]:7890', 'http://[ff02::1]:7890', 'http://bad-.example:7890']) {
    const t = await setup();
    t.check('proxy-enabled', true);
    t.input('proxy-url', url);
    await t.submit();
    assert.equal(t.posts().length, 0, url);
    assert.equal(t.elements['proxy-url'].focused, true, url);
  }
  for (const url of ['http://192.0.2.10:7890', 'http://proxy.example:80', 'http://[::1]:7890', 'http://localhost:7890/']) {
    const t = await setup();
    t.check('proxy-enabled', true);
    t.input('proxy-url', url);
    await t.submit();
    assert.equal(t.posts().length, 1, url);
    assert.equal(t.posts()[0].body.settings.proxy_enabled, true);
    assert.equal(t.posts()[0].body.settings.proxy_url, url);
  }
});

test('proxy credentials are write-only, replacements clear after save, blanks retain stored values', async () => {
  const t = await setup();
  t.input('proxy-url', 'http://192.0.2.10:7890');
  t.check('proxy-enabled', true);
  t.input('proxy-username', 'synthetic-user');
  t.input('proxy-password', 'synthetic-password');
  await t.submit();
  const body = t.posts()[0].body;
  assert.equal(body.proxy_username, 'synthetic-user');
  assert.equal(body.proxy_password, 'synthetic-password');
  assert.equal(body.clear_proxy_auth, false);
  assert.equal('proxy_username' in body.settings, false);
  assert.equal('proxy_password' in body.settings, false);
  assert.equal(t.elements['proxy-username'].value, '');
  assert.equal(t.elements['proxy-password'].value, '');
  assert.match(t.elements['proxy-username-state'].textContent, /已设置/);
  assert.match(t.elements['proxy-password-state'].textContent, /已设置/);
  t.input('listen-port', '9000');
  await t.submit();
  assert.equal('proxy_username' in t.posts()[1].body, false);
  assert.equal('proxy_password' in t.posts()[1].body, false);
  assert.equal(t.posts()[1].body.clear_proxy_auth, false);
  assert.equal(t.data.secrets.proxy_username_set, true);
  assert.equal(t.data.secrets.proxy_password_set, true);
});

test('changing an authenticated proxy requires complete re-entry or explicit clear', async () => {
  for (const clear of [false, true]) {
    const t = await setup();
    t.data.settings.proxy_url = 'http://192.0.2.10:7890';
    t.data.secrets.proxy_username_set = t.data.secrets.proxy_password_set = true;
    await t.click('refresh');
    t.input('proxy-url', 'http://192.0.2.20:7890');
    assert.equal(t.elements['proxy-auth-warning'].hidden, false);
    await t.submit();
    assert.equal(t.posts().length, 0);
    t.input('proxy-username', 'replacement-user');
    await t.submit();
    assert.equal(t.posts().length, 0);
    if (clear) {
      t.input('proxy-password', 'ignored-typed-password');
      t.check('clear-proxy-auth', true);
      assert.equal(t.elements['proxy-password'].disabled, true);
    } else t.input('proxy-password', 'replacement-password');
    await t.submit();
    assert.equal(t.posts().length, 1);
    const body = t.posts()[0].body;
    assert.equal(body.clear_proxy_auth, clear);
    assert.equal('proxy_username' in body, !clear);
    assert.equal('proxy_password' in body, !clear);
    assert.equal(t.elements['clear-proxy-auth'].checked, false);
    assert.equal(t.data.secrets.proxy_username_set, !clear);
    assert.equal(t.data.secrets.proxy_password_set, !clear);
  }
});

test('proxy validation blocks credential control characters and ambiguous usernames', async () => {
  for (const [id, value] of [['proxy-username', 'bad:user'], ['proxy-username', 'bad\nuser'],
    ['proxy-username', 'x'.repeat(257)], ['proxy-password', 'bad\rpassword'], ['proxy-password', 'x'.repeat(4097)]]) {
    const t = await setup();
    t.input(id, value);
    await t.submit();
    assert.equal(t.posts().length, 0, id);
    assert.equal(t.elements[id].focused, true);
  }
});

test('failed proxy save retains inputs and clear intent without rendering secrets', async () => {
  const t = await setup();
  t.input('proxy-username', 'PRIVATE_TYPED_USER');
  t.input('proxy-password', 'PRIVATE_TYPED_PASSWORD');
  t.control.failPost = 'invalid_proxy_settings';
  await t.submit();
  assert.equal(t.elements['proxy-username'].value, 'PRIVATE_TYPED_USER');
  assert.equal(t.elements['proxy-password'].value, 'PRIVATE_TYPED_PASSWORD');
  assert.match(t.elements.notice.textContent, /代理认证/);
  assert.equal(t.elements.notice.textContent.includes('PRIVATE'), false);
  assert.equal(t.posts().length, 1);
  t.check('clear-proxy-auth', true);
  await t.submit();
  assert.equal(t.elements['clear-proxy-auth'].checked, true);
  assert.equal(t.posts()[1].body.clear_proxy_auth, true);
  assert.equal('proxy_password' in t.posts()[1].body, false);
});

test('unsafe proxy URL in status fails closed without echoing embedded credentials', async () => {
  const t = await setup();
  t.data.settings.proxy_url = 'http://LEAK_USER:LEAK_PASS@proxy.example:7890';
  await t.click('refresh');
  assert.equal(t.elements.apply.disabled, true);
  assert.equal(t.elements['proxy-url'].value, '');
  assert.equal(JSON.stringify(t.elements).includes('LEAK_'), false);
});

test('fixed proxy errors never report successful remote connectivity', async () => {
  const t = await setup();
  t.data.runtime.error_code = 'invalid_proxy_settings';
  await t.click('refresh');
  assert.match(t.elements['runtime-issue'].textContent, /不会自动绕过代理直连/);
  assert.match(html, /127.0.0.1 指 AstrBot 容器自身/);
  assert.match(html, /HTTPS 目标通过 CONNECT/);
  assert.match(html, /本地 MCP 和健康检查不走代理/);
});


test('redacted invalid saved proxy stays editable and shows a safe diagnostic', async () => {
  const t = await setup();
  t.data.settings.proxy_enabled = true;
  t.data.settings.proxy_url = '';
  t.data.runtime.error_code = 'invalid_proxy_settings';
  await t.click('refresh');
  assert.equal(t.elements['settings-fields'].disabled, false);
  assert.match(t.elements['runtime-issue'].textContent, /代理设置无效/);
  t.input('proxy-url', 'http://192.0.2.10:7890');
  await t.submit();
  assert.equal(t.posts().length, 1);
});

test('password requires a supplied or already stored username', async () => {
  const t = await setup();
  t.input('proxy-password', 'synthetic-password');
  await t.submit();
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements['proxy-username'].focused, true);
  t.data.secrets.proxy_username_set = true;
  await t.click('refresh');
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal(t.posts()[0].body.proxy_password, 'synthetic-password');
  assert.equal('proxy_username' in t.posts()[0].body, false);
});
