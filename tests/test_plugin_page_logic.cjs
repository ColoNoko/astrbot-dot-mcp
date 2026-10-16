/* Synthetic DOM/bridge unit checks only. Browser layout and real AstrBot are separate QA. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const root = path.join(__dirname, '..', 'pages', 'connection');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const helpHtml = fs.readFileSync(path.join(root, 'help.html'), 'utf8');

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
  const control = {failGet: false, failPost: '', hold: false, release: null, holdTools: false, releaseTools: null,
    tools: {version: '0.5.0', tools: [{name: 'qq_read_attachment', description: '读取附件', inputSchema: {type: 'object', properties: {include_raw: {type: 'boolean', default: true}}}}]}};
  const clone = () => JSON.parse(JSON.stringify(data));
  const bridge = {
    ready: async () => ({isDark: false}), onContext: () => () => {},
    apiGet: async (endpoint) => {
      calls.push({method: 'GET', endpoint});
      if (control.failGet) throw Error('get_failed');
      if (endpoint === 'tools') {
        if (control.holdTools) await new Promise((resolve) => { control.releaseTools = resolve; });
        return JSON.parse(JSON.stringify(control.tools));
      }
      return clone();
    },
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
  assert.match(helpHtml, /无需填写、生成或复制 MCP 密钥/);
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
  assert.match(helpHtml, /127.0.0.1 指 AstrBot 容器自身/);
  assert.match(helpHtml, /HTTPS 目标通过 CONNECT/);
  assert.match(helpHtml, /本地 MCP 和健康检查不走代理/);
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

test('retired callback backend and separate DNS controls cannot be edited or sent', async () => {
  const t = await setup();
  for (const id of ['callback-backend', 'callback-dns', 'attachment-dns']) assert.equal(t.elements[id], undefined);
  assert.doesNotMatch(html, /name="(?:callback_backend|callback_dns|attachment_dns)"|wechat_httpcore/);
  assert.doesNotMatch(source, /callback-backend|callback_backend|wechat_httpcore/);
  t.input('listen-port', '9000');
  await t.submit();
  const settings = t.posts()[0].body.settings;
  assert.equal(settings.network_dns, 'system');
  for (const key of ['callback_backend', 'callback_dns', 'attachment_dns']) assert.equal(key in settings, false);
});

test('retired backend values are ignored without disclosure, automatic writes, or overwriting stored values', async () => {
  for (const value of ['aiohttp', 'wechat_httpcore', '', null, true, 1, [], {}, 'SECRET_BACKEND_SENTINEL']) {
    const t = await setup();
    t.data.settings.callback_backend = value;
    const original = JSON.stringify(t.data);
    await t.click('refresh');
    assert.equal(t.elements['settings-fields'].disabled, false);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.posts().length, 0);
    assert.equal(JSON.stringify(t.data), original);
    assert.equal(JSON.stringify(t.elements).includes('SECRET_BACKEND_SENTINEL'), false);
    t.input('listen-port', '9000');
    await t.submit();
    assert.equal(t.posts().length, 1);
    assert.equal('callback_backend' in t.posts()[0].body.settings, false);
    assert.deepEqual(t.data.settings.callback_backend, value);
  }
});

test('unified DNS selector exposes two choices and one disabled legacy-conflict placeholder', () => {
  assert.match(html, /<label for="network-dns">统一 DNS<\/label>/);
  const selector = html.match(/<select id="network-dns"[^>]*>.*?<\/select>/s)?.[0];
  assert.ok(selector);
  assert.match(selector, /name="network_dns"/);
  assert.match(selector, /<option value="system">系统\s*DNS<\/option>/);
  assert.match(selector, /<option value="google">Google\s*加密\s*DNS（经代理）<\/option>/);
  assert.match(selector, /<option value="legacy" disabled>旧配置不同，请选择统一模式<\/option>/);
  assert.equal([...selector.matchAll(/<option\b/g)].length, 3);
  assert.match(html, /回调与附件共用/);
  assert.match(html, /向 Google 查询下载\/回调域名，不含 URL 路径/);
  assert.match(html, /旧模式冲突时先保留原路径/);
});

test('help explains unified DNS privacy and no longer advertises the removed backend', () => {
  assert.match(helpHtml, /统一\s*DNS/);
  assert.doesNotMatch(helpHtml, /回调 DNS 与附件 DNS 独立|旧兼容方式保留|wechat_httpcore/);
  assert.match(helpHtml, /Google/);
  assert.match(helpHtml, /域名/);
  assert.match(helpHtml, /查询参数/);
  assert.match(helpHtml, /令牌/);
  assert.match(helpHtml, /附件内容/);
  assert.match(helpHtml, /代理/);
  assert.match(helpHtml, /不(?:会)?回退系统\s*DNS/);
  assert.match(helpHtml, /应用并重启/);
});

test('absent DNS settings display system without writes or silently materializing a new mode', async () => {
  const t = await setup();
  const original = JSON.stringify(t.data);
  assert.equal(t.elements['network-dns'].value, 'system');
  assert.equal(t.elements.save.disabled, true);
  await t.click('refresh');
  assert.equal(t.posts().length, 0);
  assert.equal(JSON.stringify(t.data), original);
  for (const key of ['network_dns', 'callback_dns', 'attachment_dns']) assert.equal(key in t.data.settings, false);
});

test('exact unified DNS status modes stay clean and never enable the proxy automatically', async () => {
  for (const mode of ['system', 'google', 'legacy']) {
    const t = await setup();
    t.data.settings.network_dns = mode;
    const original = JSON.stringify(t.data);
    await t.click('refresh');
    assert.equal(t.elements['network-dns'].value, mode);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.elements['proxy-enabled'].checked, false);
    assert.equal(t.elements.apply.disabled, false);
    assert.equal(t.posts().length, 0);
    assert.equal(JSON.stringify(t.data), original);
  }
});

test('matching legacy DNS paths display their common mode without migrating on read', async () => {
  for (const [callback, attachment, expected] of [
    ['system', 'system', 'system'], ['google', 'google', 'google'],
    ['system', undefined, 'system'], [undefined, 'system', 'system'],
  ]) {
    const t = await setup();
    if (callback !== undefined) t.data.settings.callback_dns = callback;
    if (attachment !== undefined) t.data.settings.attachment_dns = attachment;
    const original = JSON.stringify(t.data);
    await t.click('refresh');
    assert.equal(t.elements['network-dns'].value, expected);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.posts().length, 0);
    assert.equal(JSON.stringify(t.data), original);
    assert.equal('network_dns' in t.data.settings, false);
  }
});

test('conflicting legacy DNS preserves both paths until an explicit unified selection', async () => {
  for (const [callback, attachment] of [['google', 'system'], ['system', 'google']]) {
    const t = await setup();
    t.data.settings.callback_dns = callback;
    t.data.settings.attachment_dns = attachment;
    t.data.settings.proxy_enabled = true;
    t.data.settings.proxy_url = 'http://192.0.2.10:7890';
    const original = JSON.stringify(t.data);
    await t.click('refresh');
    assert.equal(t.elements['network-dns'].value, 'legacy');
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.posts().length, 0);
    assert.equal(JSON.stringify(t.data), original);
    t.input('listen-port', '9000');
    await t.submit();
    assert.equal(t.posts().length, 0);
    assert.equal(t.elements['network-dns'].focused, true);
    assert.match(t.elements.notice.textContent, /请选择系统 DNS 或 Google 加密 DNS/);
    t.input('network-dns', 'system');
    await t.submit();
    assert.equal(t.posts().length, 1);
    assert.equal(t.posts()[0].body.settings.network_dns, 'system');
    assert.equal('callback_dns' in t.posts()[0].body.settings, false);
    assert.equal('attachment_dns' in t.posts()[0].body.settings, false);
    assert.equal(t.data.settings.callback_dns, callback);
    assert.equal(t.data.settings.attachment_dns, attachment);
    assert.equal(t.data.settings.proxy_enabled, true);
  }
});

test('explicit unified DNS takes precedence over retired separate settings without echoing them', async () => {
  for (const mode of ['system', 'google']) {
    const t = await setup();
    t.data.settings.network_dns = mode;
    t.data.settings.callback_dns = 'SECRET_CALLBACK_DNS_SENTINEL';
    t.data.settings.attachment_dns = {secret: 'SECRET_ATTACHMENT_DNS_SENTINEL'};
    await t.click('refresh');
    assert.equal(t.elements['network-dns'].value, mode);
    assert.equal(t.elements['settings-fields'].disabled, false);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(JSON.stringify(t.elements).includes('SECRET_'), false);
    assert.equal(t.posts().length, 0);
  }
});

test('unified DNS change is dirty and requires a separate save and explicit apply', async () => {
  const t = await setup();
  t.data.settings.proxy_enabled = true;
  t.data.settings.proxy_url = 'http://192.0.2.10:7890';
  await t.click('refresh');
  t.input('network-dns', 'google');
  assert.equal(t.elements.save.disabled, false);
  assert.equal(t.elements.apply.disabled, true);
  await t.click('apply');
  assert.equal(t.posts().length, 0);
  await t.submit();
  assert.equal(t.posts().length, 1);
  const body = t.posts()[0].body;
  assert.equal(t.posts()[0].endpoint, 'settings');
  assert.equal(body.settings.network_dns, 'google');
  assert.equal(body.settings.tunnel_id, fixture().settings.tunnel_id);
  assert.equal(body.settings.tunnel_enabled, false);
  assert.equal(body.settings.attachment_private_http, false);
  assert.equal('enabled' in body.settings, false);
  assert.equal(t.elements['network-dns'].value, 'google');
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, false);
  await t.click('apply');
  assert.equal(t.posts().length, 2);
  assert.equal(t.posts()[1].endpoint, 'action');
  assert.equal(t.posts()[1].body.action, 'apply');
  assert.equal('settings' in t.posts()[1].body, false);
  assert.equal(t.posts()[1].body.revision, 2);
  assert.equal(t.posts()[1].body.csrf_token, 'synthetic-csrf-2');
});

test('unified Google DNS requires explicit proxy consent and retains stored credentials', async () => {
  const t = await setup();
  t.data.settings.proxy_url = 'http://192.0.2.10:7890';
  t.data.secrets.proxy_username_set = t.data.secrets.proxy_password_set = true;
  await t.click('refresh');
  t.input('network-dns', 'google');
  await t.submit();
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements['proxy-enabled'].focused, true);
  assert.match(t.elements.notice.textContent, /Google 加密 DNS 需要启用已有代理/);
  assert.equal(t.elements['proxy-enabled'].checked, false);
  t.check('proxy-enabled', true);
  await t.submit();
  const body = t.posts()[0].body;
  assert.equal(body.settings.network_dns, 'google');
  assert.equal(body.settings.proxy_enabled, true);
  assert.equal(body.settings.proxy_url, 'http://192.0.2.10:7890');
  for (const key of ['proxy_username', 'proxy_password', 'tunnel_key', 'mcp_token']) assert.equal(key in body, false);
  assert.equal(t.data.secrets.proxy_username_set, true);
  assert.equal(t.data.secrets.proxy_password_set, true);
  assert.equal(JSON.stringify(t.elements).includes('SERVER_'), false);
});

test('unified Google DNS cannot silently disable proxy or use an invalid proxy URL', async () => {
  const t = await setup();
  t.data.settings.network_dns = 'google';
  t.data.settings.proxy_enabled = true;
  t.data.settings.proxy_url = 'http://192.0.2.10:7890';
  await t.click('refresh');
  t.check('proxy-enabled', false);
  await t.submit();
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements['proxy-enabled'].focused, true);
  t.check('proxy-enabled', true);
  for (const url of ['', 'http://192.0.2.10', 'https://192.0.2.10:7890', 'http://USER:PASSWORD@192.0.2.10:7890']) {
    t.input('proxy-url', url);
    await t.submit();
    assert.equal(t.posts().length, 0, url);
    assert.equal(t.elements['proxy-url'].focused, true);
    assert.doesNotMatch(t.elements.notice.textContent, /USER|PASSWORD/);
  }
  t.input('proxy-url', '');
  t.input('network-dns', 'system');
  t.check('proxy-enabled', false);
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal(t.posts()[0].body.settings.network_dns, 'system');
  assert.equal(t.posts()[0].body.settings.proxy_enabled, false);
});

test('unknown or placeholder unified DNS form values cannot submit or echo raw values', async () => {
  for (const mode of ['', 'legacy', 'GOOGLE', 'google ', ' system', 'auto', 'SECRET_NETWORK_DNS']) {
    const t = await setup();
    t.input('network-dns', mode);
    await t.submit();
    assert.equal(t.posts().length, 0, mode);
    assert.equal(t.elements['network-dns'].focused, true, mode);
    assert.match(t.elements.notice.textContent, /请选择系统 DNS 或 Google 加密 DNS/);
    assert.equal(t.elements.notice.textContent.includes('SECRET_NETWORK_DNS'), false);
  }
});

test('malformed unified DNS status fails closed without replacing the last verified value', async () => {
  for (const mode of ['', null, false, 0, [], {}, 'GOOGLE', 'google ', 'SECRET_NETWORK_DNS']) {
    const t = await setup();
    t.data.settings.network_dns = mode;
    await t.click('refresh');
    assert.equal(t.elements['network-dns'].value, 'system');
    assert.equal(t.elements['settings-fields'].disabled, true);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.elements.apply.disabled, true);
    assert.equal(t.elements['reconnect-tunnel'].disabled, true);
    assert.equal(t.posts().length, 0);
    assert.equal(JSON.stringify(t.elements).includes('SECRET_NETWORK_DNS'), false);
  }
});

test('malformed separate legacy DNS status cannot masquerade as a safe default or conflict', async () => {
  for (const key of ['callback_dns', 'attachment_dns']) {
    for (const mode of ['', null, false, 0, [], {}, 'GOOGLE', 'google ', 'SECRET_LEGACY_DNS']) {
      const t = await setup();
      t.data.settings[key] = mode;
      await t.click('refresh');
      assert.equal(t.elements['settings-fields'].disabled, true, `${key}: ${JSON.stringify(mode)}`);
      assert.equal(t.elements['network-dns'].value, 'system');
      assert.equal(t.elements.save.disabled, true);
      assert.equal(t.elements.apply.disabled, true);
      assert.equal(t.posts().length, 0);
      assert.equal(JSON.stringify(t.elements).includes('SECRET_LEGACY_DNS'), false);
    }
  }
});

test('refresh and stop preserve unsaved unified DNS and all replacement secrets until explicit discard', async () => {
  const t = await setup();
  t.input('network-dns', 'google');
  t.input('tunnel-key', 'synthetic-long-unsaved-token-for-tests');
  t.input('proxy-username', 'unsaved-user');
  t.input('proxy-password', 'unsaved-password');
  t.check('clear-proxy-auth', true);
  await t.click('refresh');
  await t.click('stop');
  assert.equal(t.posts().length, 1);
  assert.equal(t.posts()[0].body.action, 'stop');
  for (const key of ['settings', 'tunnel_key', 'proxy_username', 'proxy_password']) assert.equal(key in t.posts()[0].body, false);
  assert.equal(t.elements['network-dns'].value, 'google');
  assert.equal(t.elements['tunnel-key'].value, 'synthetic-long-unsaved-token-for-tests');
  assert.equal(t.elements['proxy-username'].value, 'unsaved-user');
  assert.equal(t.elements['proxy-password'].value, 'unsaved-password');
  assert.equal(t.elements['clear-proxy-auth'].checked, true);
  assert.equal(t.elements.apply.disabled, true);
  await t.click('discard');
  assert.equal(t.elements['network-dns'].value, 'system');
  assert.equal(t.elements['tunnel-key'].value, '');
  assert.equal(t.elements['proxy-username'].value, '');
  assert.equal(t.elements['proxy-password'].value, '');
  assert.equal(t.elements['clear-proxy-auth'].checked, false);
  assert.equal(t.elements.save.disabled, true);
});

test('DNS revision conflict preserves unsaved choice and secrets until explicit reload', async () => {
  const t = await setup();
  t.input('network-dns', 'google');
  t.input('tunnel-key', 'synthetic-revision-conflict-token-for-tests');
  t.data.revision = 7;
  t.data.settings.network_dns = 'system';
  t.data.settings.callback_backend = 'RETIRED_BACKEND_SENTINEL';
  await t.click('refresh');
  assert.equal(t.elements['network-dns'].value, 'google');
  assert.equal(t.elements['tunnel-key'].value, 'synthetic-revision-conflict-token-for-tests');
  assert.equal(t.elements.conflict.hidden, false);
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, true);
  await t.submit();
  await t.click('apply');
  assert.equal(t.posts().length, 0);
  await t.click('reload');
  assert.equal(t.elements['network-dns'].value, 'system');
  assert.equal(t.elements['tunnel-key'].value, '');
  assert.equal(t.elements.conflict.hidden, true);
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, false);
  assert.equal(JSON.stringify(t.elements).includes('RETIRED_BACKEND_SENTINEL'), false);
  assert.equal(t.posts().length, 0);
});

test('DNS save revision rejection never retries or overwrites a newer server mode', async () => {
  const t = await setup();
  t.check('proxy-enabled', true);
  t.input('proxy-url', 'http://192.0.2.10:7890');
  t.input('network-dns', 'google');
  t.data.revision = 8;
  t.data.settings.network_dns = 'system';
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal(t.posts()[0].body.revision, 1);
  assert.equal(t.data.settings.network_dns, 'system');
  assert.equal(t.elements['network-dns'].value, 'google');
  assert.equal(t.elements.conflict.hidden, false);
  await t.submit();
  await t.click('apply');
  assert.equal(t.posts().length, 1);
});

test('DNS save failure retains selection and secrets with no automatic retry, fallback, or apply', async () => {
  const t = await setup();
  t.input('network-dns', 'google');
  t.check('proxy-enabled', true);
  t.input('proxy-url', 'http://192.0.2.10:7890');
  t.input('tunnel-key', 'synthetic-unsaved-token-after-dns-failure');
  t.control.failPost = 'network_dns_requires_proxy';
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal(t.elements['network-dns'].value, 'google');
  assert.equal(t.elements['tunnel-key'].value, 'synthetic-unsaved-token-after-dns-failure');
  assert.match(t.elements.notice.textContent, /Google 加密 DNS 需要启用已有代理/);
  assert.equal(t.elements.apply.disabled, true);
  assert.equal('network_dns' in t.data.settings, false);
  t.control.failPost = '';
  await t.submit();
  assert.equal(t.posts().length, 2);
  assert.equal(t.data.settings.network_dns, 'google');
  assert.equal(t.elements['tunnel-key'].value, '');
  t.control.failPost = 'RAW_DNS_SECRET_FAILURE';
  await t.click('apply');
  assert.equal(t.posts().length, 3);
  assert.equal(t.posts()[2].body.action, 'apply');
  assert.equal(t.elements['network-dns'].value, 'google');
  assert.equal(t.data.settings.network_dns, 'google');
  assert.equal(JSON.stringify(t.elements).includes('RAW_DNS_SECRET_FAILURE'), false);
});

test('unified DNS diagnostics show only fixed actionable text', async () => {
  const t = await setup();
  for (const [code, message] of [
    ['invalid_network_dns', /请选择系统 DNS 或 Google 加密 DNS/],
    ['network_dns_requires_proxy', /Google 加密 DNS 需要启用已有代理/],
  ]) {
    t.data.runtime.error_code = code;
    await t.click('refresh');
    assert.match(t.elements['runtime-issue'].textContent, message);
    assert.equal(t.posts().length, 0);
  }
  t.data.runtime.error_code = 'SECRET_DNS_RUNTIME_ERROR';
  await t.click('refresh');
  assert.equal(t.elements['runtime-issue'].hidden, true);
  assert.equal(JSON.stringify(t.elements).includes('SECRET_DNS_RUNTIME_ERROR'), false);
});

test('private HTTP is explicit default-off and warns about plaintext integrity', async () => {
  const t = await setup();
  assert.equal(t.elements['attachment-private-http'].checked, false);
  assert.equal('attachment_private_http' in t.data.settings, false);
  assert.equal(t.posts().length, 0);
  assert.match(html, /明文传输，可能被查看或篡改/);
  assert.match(html, /SHA-256 不能证明原文件真实性/);
  assert.match(helpHtml, /不用于图片、群文件、任意链接或内网/);
  t.check('attachment-private-http', true);
  await t.submit();
  assert.equal(t.posts()[0].body.settings.attachment_private_http, true);
  assert.equal(t.data.settings.enabled, false);
  t.check('attachment-private-http', false);
  await t.submit();
  assert.equal(t.posts()[1].body.settings.attachment_private_http, false);
});

test('private HTTP malformed status never becomes checked or leaks text', async () => {
  for (const value of ['SECRET_HTTP_SENTINEL', 1, null, []]) {
    const t = await setup();
    t.data.settings.attachment_private_http = value;
    await t.click('refresh');
    assert.equal(t.elements['attachment-private-http'].checked, false);
    assert.equal(t.posts().length, 0);
    assert.doesNotMatch(t.elements.notice.textContent, /SECRET_HTTP_SENTINEL/);
  }
});


test('manual reconnect requires MCP ready, plugin enabled, and saved Tunnel enabled', async () => {
  for (const enabled of [false, true]) {
    for (const tunnelEnabled of [false, true]) {
      for (const ready of [false, true]) {
        const t = await setup();
        t.data.settings.enabled = enabled;
        t.data.settings.tunnel_enabled = tunnelEnabled;
        t.data.runtime.mcp_ready = ready;
        await t.click('refresh');
        const allowed = enabled && tunnelEnabled && ready;
        assert.equal(t.elements['reconnect-tunnel'].disabled, !allowed);
        assert.equal(t.posts().length, 0);
        await t.click('reconnect-tunnel');
        assert.equal(t.posts().length, allowed ? 1 : 0);
        if (allowed) assert.equal(t.posts()[0].body.action, 'reconnect_tunnel');
      }
    }
  }
});

test('manual reconnect stays blocked for dirty, conflicted, or stale forms', async () => {
  const t = await setup();
  t.data.settings.enabled = true;
  t.data.settings.tunnel_enabled = true;
  t.data.runtime.mcp_ready = true;
  await t.click('refresh');
  assert.equal(t.elements['reconnect-tunnel'].disabled, false);
  t.input('listen-port', '9000');
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  t.elements['reconnect-tunnel'].events.click();
  await flush();
  assert.equal(t.posts().length, 0);
  t.data.revision++;
  await t.click('refresh');
  assert.equal(t.elements.conflict.hidden, false);
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  await t.click('reconnect-tunnel');
  assert.equal(t.posts().length, 0);
  await t.click('reload');
  assert.equal(t.elements['reconnect-tunnel'].disabled, false);
  t.control.failGet = true;
  await t.click('refresh');
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  await t.click('reconnect-tunnel');
  assert.equal(t.posts().length, 0);
  t.control.failGet = false;
  await t.click('refresh');
  assert.equal(t.elements['reconnect-tunnel'].disabled, false);
});

test('manual reconnect is unavailable without the official bridge', async () => {
  const t = await setup({noBridge: true});
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  await t.click('reconnect-tunnel');
  assert.equal(t.calls.length, 0);
});

test('manual reconnect sends only its action and fresh revision authorization, never settings or secrets', async () => {
  const t = await setup();
  t.data.settings.enabled = true;
  t.data.settings.tunnel_enabled = true;
  t.data.runtime.mcp_ready = true;
  t.data.revision = 7;
  t.data.csrf_token = 'synthetic-refreshed-csrf';
  await t.click('refresh');
  const originalSettings = JSON.stringify(t.data.settings);
  await t.click('reconnect-tunnel');
  assert.equal(t.posts().length, 1);
  assert.equal(t.posts()[0].endpoint, 'action');
  assert.deepEqual(JSON.parse(JSON.stringify(t.posts()[0].body)), {
    csrf_token: 'synthetic-refreshed-csrf', revision: 7, action: 'reconnect_tunnel',
  });
  assert.equal(JSON.stringify(t.data.settings), originalSettings);
  assert.equal(JSON.stringify(t.posts()).includes('SERVER_'), false);
  assert.match(t.elements.notice.textContent, /已重建 Tunnel 连接/);
  assert.match(t.elements.notice.textContent, /客户端刷新确认/);
  assert.match(html, /重连仅断开并重建 Tunnel，不会重开 ChatGPT 会话/);
  assert.match(html, /QQ 消息往返 <strong>尚未验证<\/strong>/);
});

test('reconnect clicks serialize and subsequent explicit reconnect uses the returned revision', async () => {
  const t = await setup();
  t.data.settings.enabled = true;
  t.data.settings.tunnel_enabled = true;
  t.data.runtime.mcp_ready = true;
  await t.click('refresh');
  t.control.hold = true;
  await t.click('reconnect-tunnel');
  await t.click('reconnect-tunnel');
  await t.click('apply');
  assert.equal(t.posts().length, 1);
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  assert.equal(t.elements.apply.disabled, true);
  assert.equal(t.elements['settings-fields'].disabled, true);
  t.control.hold = false;
  t.control.release();
  await flush();
  assert.equal(t.elements['reconnect-tunnel'].disabled, false);
  await t.click('reconnect-tunnel');
  assert.equal(t.posts().length, 2);
  assert.equal(t.posts()[1].body.action, 'reconnect_tunnel');
  assert.equal(t.posts()[1].body.revision, 2);
  assert.equal(t.posts()[1].body.csrf_token, 'synthetic-csrf-2');
});

test('reconnect failures reconcile read-only without retry, fallback apply, or exposing raw errors', async () => {
  for (const code of ['tunnel_reconnect_failed', 'reconnect_failed', 'runtime_not_ready', 'RAW_RECONNECT_SECRET']) {
    const t = await setup();
    t.data.settings.enabled = true;
    t.data.settings.tunnel_enabled = true;
    t.data.runtime.mcp_ready = true;
    await t.click('refresh');
    t.control.failPost = code;
    await t.click('reconnect-tunnel');
    assert.equal(t.posts().length, 1, code);
    assert.equal(t.posts()[0].body.action, 'reconnect_tunnel');
    assert.equal(t.calls.at(-1).method, 'GET');
    assert.equal(t.calls.at(-1).endpoint, 'status');
    assert.equal(t.elements.notice.className, 'notice error');
    assert.equal(JSON.stringify(t.elements).includes('RAW_RECONNECT_SECRET'), false);
    assert.equal(t.data.settings.enabled, true);
    assert.equal(t.data.settings.tunnel_enabled, true);
    assert.equal(t.data.runtime.mcp_ready, true);
  }
});

test('unreconciled reconnect failure fails closed until a verified refresh', async () => {
  const t = await setup();
  t.data.settings.enabled = true;
  t.data.settings.tunnel_enabled = true;
  t.data.runtime.mcp_ready = true;
  await t.click('refresh');
  t.control.failPost = 'RAW_RECONNECT_SECRET';
  t.control.failGet = true;
  await t.click('reconnect-tunnel');
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  assert.equal(t.elements.apply.disabled, true);
  assert.equal(t.elements.stop.disabled, true);
  assert.equal(t.posts().length, 1);
  assert.equal(JSON.stringify(t.elements).includes('RAW_RECONNECT_SECRET'), false);
  t.control.failGet = false;
  await t.click('refresh');
  assert.equal(t.elements['reconnect-tunnel'].disabled, false);
  assert.equal(t.posts().length, 1);
});

test('connection settings have no attached-history control or payload wiring', async () => {
  assert.doesNotMatch(html, /chat-context-messages|chat_context_messages|连续聊天上下文/);
  assert.doesNotMatch(source, /chat-context-messages|chat_context_messages|chatContextMessages/);
  const t = await setup();
  assert.equal(t.elements['chat-context-messages'], undefined);
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, false);
  await t.click('refresh');
  assert.equal(t.posts().length, 0);
  t.input('listen-port', '9000');
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal('chat_context_messages' in t.posts()[0].body.settings, false);
  assert.equal('chat_context_messages' in t.data.settings, false);
});

test('legacy context settings including malformed values are ignored without disclosure or writes', async () => {
  for (const value of [8, 0, null, true, -1, 9, 1.5, '8', [], {}, 'SECRET_CONTEXT_VALUE']) {
    const t = await setup();
    t.data.settings.chat_context_messages = value;
    const before = JSON.stringify(t.data);
    await t.click('refresh');
    assert.equal(t.elements['settings-fields'].disabled, false);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.elements.apply.disabled, false);
    assert.equal(t.posts().length, 0);
    assert.equal(JSON.stringify(t.data), before);
    assert.equal(JSON.stringify(t.elements).includes('SECRET_CONTEXT_VALUE'), false);
    t.input('listen-port', '9000');
    await t.submit();
    assert.equal(t.posts().length, 1);
    assert.equal('chat_context_messages' in t.posts()[0].body.settings, false);
    assert.deepEqual(t.data.settings.chat_context_messages, value);
    assert.equal(t.elements.save.disabled, true);
    assert.equal(t.elements.apply.disabled, false);
  }
});

test('retired context value does not erase unrelated edits or weaken revision conflict checks', async () => {
  const t = await setup();
  t.input('listen-port', '9000');
  t.data.settings.chat_context_messages = 8;
  await t.click('refresh');
  assert.equal(t.elements['listen-port'].value, '9000');
  assert.equal(t.elements.conflict.hidden, true);
  t.data.revision++;
  await t.click('refresh');
  assert.equal(t.elements['listen-port'].value, '9000');
  assert.equal(t.elements.conflict.hidden, false);
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, true);
  await t.click('reload');
  assert.equal(t.elements['listen-port'].value, '8769');
  assert.equal(t.elements.conflict.hidden, true);
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.posts().length, 0);
});

test('capacity rejection is visible after refresh with honest persistence state', async () => {
  const t = await setup();
  t.data.runtime.error_code = 'storage_full';
  t.data.capacity_alert = {active: true, count: 3, persistent: false, secret: 'DO_NOT_RENDER'};
  await t.click('refresh');
  const text=t.elements['runtime-issue'].textContent;
  assert.match(text,/已拒收3次/); assert.match(text,/仅在内存/); assert.match(text,/重发/);
  assert.doesNotMatch(text,/DO_NOT_RENDER/);
  t.data.capacity_alert={active:true,count:'secret',persistent:true};
  await t.click('refresh');
  assert.match(t.elements['runtime-issue'].textContent,/已拒收0次/);
  assert.match(t.elements['runtime-issue'].textContent,/已保存在本地/);
});


test('tools query is explicit, read-only, parameter-aware, and preserves unsaved settings', async () => {
  const t = await setup();
  assert.deepEqual(t.calls.map(call => call.endpoint), ['status']);
  t.input('listen-port', '9000');
  t.input('tunnel-key', 'synthetic-unsaved-token-for-tools-query');
  await t.click('query-tools');
  assert.equal(t.calls.at(-1).endpoint, 'tools');
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements['listen-port'].value, '9000');
  assert.equal(t.elements['tunnel-key'].value, 'synthetic-unsaved-token-for-tools-query');
  assert.equal(t.elements.save.disabled, false);
  assert.equal(t.elements['tools-list'].hidden, false);
  assert.equal(t.elements['tools-list'].children[0].children[0].textContent, 'qq_read_attachment');
  assert.match(t.elements['tools-list'].children[0].children[2].children[1].textContent, /include_raw/);
  assert.match(t.elements['tools-status'].textContent, /1 个工具/);
  assert.match(t.elements['tools-status'].textContent, /客户端目录未知/);
  assert.doesNotMatch(t.elements['tools-status'].textContent, /客户端元数据未刷新|客户端已刷新/);
});

test('tools repeated clicks serialize without blocking unrelated settings edits', async () => {
  const t = await setup();
  t.control.holdTools = true;
  await t.click('query-tools');
  await t.click('query-tools');
  assert.equal(t.calls.filter(call => call.endpoint === 'tools').length, 1);
  assert.equal(t.elements['settings-fields'].disabled, false);
  t.control.releaseTools();
  await flush();
  assert.equal(t.elements['query-tools'].disabled, false);
  assert.equal(t.posts().length, 0);
});

test('tool metadata is inert and bounded, unrelated secret fields are ignored', async () => {
  const t = await setup();
  t.control.tools.tools[0].description = '<img src=x onerror=bad()>' + 'x'.repeat(2000);
  t.control.tools.tools[0].inputSchema.properties.unsafe = {description: '</pre><script>bad()</script>'};
  t.control.tools.secret = 'SERVER_SECRET_MUST_NOT_RENDER';
  await t.click('query-tools');
  const row = t.elements['tools-list'].children[0];
  assert.equal(row.children[1].textContent.length, 600);
  assert.match(row.children[1].textContent, /<img/);
  assert.equal(row.children[1].children.length, 0);
  assert.equal(row.children[2].children[1].children.length, 0);
  assert.equal(JSON.stringify(t.elements).includes('SERVER_SECRET_MUST_NOT_RENDER'), false);
  assert.equal(t.posts().length, 0);
});

test('tools query errors discard stale results without disabling settings or echoing errors', async () => {
  const t = await setup();
  await t.click('query-tools');
  t.control.failGet = true;
  await t.click('query-tools');
  assert.equal(t.elements['tools-list'].hidden, true);
  assert.equal(t.elements['tools-list'].children.length, 0);
  assert.match(t.elements['tools-status'].textContent, /无法查询/);
  assert.equal(t.elements['settings-fields'].disabled, false);
  assert.equal(t.posts().length, 0);
});

test('invalid tool lists are rejected instead of partially presented', async () => {
  for (const tools of [null, Array.from({length: 65}, () => ({name: 'qq_status', description: 'x'})),
    [{name: '<script>unsafe', description: 'x'}], [{name: 'qq_status', description: 123}],
    [{name: 'qq_status', description: 'x'}, {name: 'qq_status', description: 'y'}]]) {
    const t = await setup();
    t.control.tools.tools = tools;
    await t.click('query-tools');
    assert.equal(t.elements['tools-list'].hidden, true);
    assert.match(t.elements['tools-status'].textContent, /无法查询/);
    assert.equal(t.posts().length, 0);
  }
});

test('tools manifest fingerprint is displayed as server evidence while client state stays unknown', async () => {
  const t = await setup();
  const fingerprint = '0123456789abcdef'.repeat(4);
  t.control.tools.version = 'v0.5.1';
  t.control.tools.manifest = {version: '0.5.1', sha256: fingerprint, count: 1,
    names: ['qq_read_attachment'], client_visible_tools: 'unknown', force_client_refresh_supported: false};
  assert.equal(t.calls.some(call => call.endpoint === 'tools'), false);
  await t.click('query-tools');
  const status = t.elements['tools-status'].textContent;
  assert.match(status, /服务端返回 1 个工具 · v0\.5\.1/);
  assert.ok(status.includes(`清单指纹 ${fingerprint}`));
  assert.match(status, /客户端目录未知/);
  assert.doesNotMatch(status, /客户端已刷新|客户端元数据未刷新|会话已刷新/);
  assert.equal(t.posts().length, 0);
  assert.equal(t.elements.save.disabled, true);
});

test('malformed manifest fingerprints are omitted rather than rendered or coerced', async () => {
  for (const fingerprint of ['', null, true, 123, {}, ['a'.repeat(64)], 'a'.repeat(64) + '\n',
    'A'.repeat(64), 'a'.repeat(63), 'a'.repeat(65), 'SECRET_MANIFEST_SHA']) {
    const t = await setup();
    t.control.tools.manifest = {sha256: fingerprint, secret: 'SECRET_MANIFEST_FIELD'};
    await t.click('query-tools');
    assert.equal(t.elements['tools-list'].hidden, false, JSON.stringify(fingerprint));
    assert.match(t.elements['tools-status'].textContent, /客户端目录未知/);
    assert.doesNotMatch(t.elements['tools-status'].textContent, /清单指纹/, JSON.stringify(fingerprint));
    assert.equal(JSON.stringify(t.elements).includes('SECRET_MANIFEST_'), false);
    assert.equal(t.posts().length, 0);
  }
});

test('server claims cannot imply that the client catalogue or session has refreshed', async () => {
  const t = await setup();
  t.control.tools.client_visible_tools = ['qq_read_attachment'];
  t.control.tools.client_refresh_supported = true;
  t.control.tools.manifest = {client_visible_tools: 'all', force_client_refresh_supported: true,
    secret: 'SECRET_MANIFEST_CLIENT_STATE'};
  await t.click('query-tools');
  assert.match(t.elements['tools-status'].textContent, /客户端目录未知/);
  assert.doesNotMatch(t.elements['tools-status'].textContent, /客户端已刷新|会话已刷新|客户端元数据未刷新/);
  assert.equal(JSON.stringify(t.elements).includes('SECRET_MANIFEST_CLIENT_STATE'), false);
  assert.equal(t.posts().length, 0);
});

test('help and refresh are honest local help and official client directions', () => {
  assert.match(html, /<title>ChatGPT Dot MCP助手<\/title>/);
  assert.match(html, /href="\.\/help\.html"/);
  assert.match(helpHtml, /id="help-back"/);
  assert.match(helpHtml, /src="\.\/help\.js"/);
  assert.match(html, /id="query-tools"[^>]*>查询服务端工具<\/button>/);
  assert.match(html, /https:\/\/developers.openai.com\/plugins\/deploy\/connect-chatgpt#refresh-metadata/);
  assert.doesNotMatch(helpHtml, /<iframe|https:\/\/[^"\s]+\.(?:js|css)["\s]/);
  for (const text of ['配置文件', '不加密', '明文', '不能证明原文件真实性', '不获得插件管理权', '硬隔离', '当前消息']) {
    assert.ok(helpHtml.includes(text), text);
  }
});

test('native sender routes render per-route local state without making the form dirty or posting routes', async () => {
  const t = await setup();
  t.data.settings.enabled = true;
  t.data.settings.tunnel_enabled = true;
  t.data.settings.tunnel_id = '';
  t.data.settings.sender_routes = [
    {sender_id: '12345', tunnel_id: 'tunnel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', listen_port: 8770},
    {sender_id: '23456', tunnel_id: 'tunnel_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', listen_port: 8771},
  ];
  t.data.runtime.mcp_ready = true;
  t.data.runtime.routes = [
    {sender_id: '12345', listen_port: 8770, mcp_ready: true,
      tunnel: {state: 'ready', ready: true, healthy: true, binary_available: true}},
    {sender_id: '23456', listen_port: 8771, mcp_ready: false, error_code: 'startup_failed',
      tunnel: {state: 'error', last_error_code: 'binary_checksum_mismatch'}},
  ];
  await t.click('refresh');
  assert.equal(t.elements['sender-routes-panel'].hidden, false);
  assert.equal(t.elements['sender-routes-empty'].hidden, true);
  assert.equal(t.elements['sender-routes-list'].children.length, 2);
  assert.equal(t.elements['sender-routes-list'].children[0].children[0].textContent, '发送者 QQ 12345');
  assert.equal(t.elements['sender-routes-list'].children[0].children[1].textContent, '127.0.0.1:8770/mcp');
  assert.match(t.elements['sender-routes-list'].children[1].children[3].textContent, /本地服务启动失败.*校验失败/);
  assert.equal(t.elements['mcp-status'].textContent, '1 / 2 本地已启动');
  assert.equal(t.elements['tunnel-status'].textContent, '1 / 2 本地就绪');
  assert.equal(t.elements['tunnel-health'].textContent, '1 / 2 本地检查通过');
  assert.equal(t.elements['binary-status'].textContent, '1 / 2 已检测到');
  assert.equal(t.elements.save.disabled, true);
  assert.equal(t.elements.apply.disabled, false);
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  await t.click('reconnect-tunnel');
  assert.equal(t.posts().length, 0);
  t.input('self-id', '34567');
  await t.submit();
  assert.equal(t.posts().length, 1);
  assert.equal('sender_routes' in t.posts()[0].body.settings, false);
  assert.equal(t.posts()[0].body.settings.tunnel_id, '');
  assert.equal(t.data.settings.sender_routes.length, 2);
  assert.equal(t.elements.save.disabled, true);
  await t.click('apply');
  assert.equal(t.posts()[1].body.action, 'apply');
  assert.equal('settings' in t.posts()[1].body, false);
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
});

test('configured routes without active children show an honest empty state and still require a shared key', async () => {
  const t = await setup();
  t.data.settings.sender_routes = [{sender_id: '12345', tunnel_id: 'tunnel_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', listen_port: 8770}];
  t.data.settings.tunnel_enabled = true;
  t.data.settings.tunnel_id = '';
  t.data.secrets.tunnel_key_set = false;
  await t.click('refresh');
  assert.equal(t.elements['sender-routes-panel'].hidden, false);
  assert.equal(t.elements['sender-routes-empty'].hidden, false);
  assert.equal(t.elements['sender-routes-list'].children.length, 0);
  assert.equal(t.elements['mcp-status'].textContent, '路由未运行');
  assert.equal(t.elements['tunnel-status'].textContent, '路由未运行');
  t.input('self-id', '34567');
  await t.submit();
  assert.equal(t.posts().length, 0);
  assert.match(t.elements.notice.textContent, /专用运行密钥/);
});

test('single-route compatibility requires a global Tunnel ID and restores standalone reconnect after native routes clear', async () => {
  const t = await setup();
  t.data.settings.enabled = true;
  t.data.settings.tunnel_enabled = true;
  t.data.runtime.mcp_ready = true;
  t.data.runtime.tunnel.ready = true;
  t.data.runtime.routes = [{sender_id: '12345', listen_port: 8770, mcp_ready: true, tunnel: {ready: true}}];
  await t.click('refresh');
  assert.equal(t.elements['reconnect-tunnel'].disabled, true);
  t.data.runtime.routes = [];
  t.data.settings.sender_routes = [];
  await t.click('refresh');
  assert.equal(t.elements['sender-routes-panel'].hidden, true);
  assert.equal(t.elements['sender-routes-list'].children.length, 0);
  assert.equal(t.elements['mcp-status'].textContent, '本地已启动');
  assert.equal(t.elements['mcp-detail'].textContent, '127.0.0.1:8769/mcp');
  assert.equal(t.elements['tunnel-status'].textContent, '本地就绪');
  assert.equal(t.elements['reconnect-tunnel'].disabled, false);
  t.input('tunnel-id', '');
  await t.submit();
  assert.equal(t.posts().length, 0);
  assert.match(t.elements.notice.textContent, /填写已有的 Tunnel ID/);
});

test('route diagnostics use only fixed errors and never render unrelated route data or raw errors', async () => {
  const t = await setup();
  t.data.runtime.routes = [{sender_id: '12345', listen_port: 8770, mcp_ready: false,
    error_code: 'RAW_ROUTE_SECRET', token: 'RAW_ROUTE_TOKEN',
    tunnel: {state: 'RAW_TUNNEL_STATE', last_error_code: 'RAW_TUNNEL_SECRET', log: 'RAW_TUNNEL_LOG'}}];
  t.data.runtime.error_code = 'invalid_sender_routes';
  await t.click('refresh');
  assert.equal(JSON.stringify(t.elements).includes('RAW_'), false);
  assert.match(t.elements['runtime-issue'].textContent, /原生插件设置/);
  t.data.runtime.error_code = 'multi_route_apply_required';
  await t.click('refresh');
  assert.match(t.elements['runtime-issue'].textContent, /重启所有路由/);
  assert.match(html, /共用下方运行密钥/);
  assert.match(html, /不使用全局 Tunnel ID 与端口/);
});

test('malformed or oversized route status fails closed without displaying unsafe identities', async () => {
  for (const routes of [null, {}, [{sender_id: '<secret>', listen_port: 8770}], [{sender_id: '12345', listen_port: 80}],
    Array.from({length: 17}, (_, index) => ({sender_id: String(index + 1), listen_port: 8770 + index}))]) {
    const t = await setup();
    t.data.runtime.routes = routes;
    await t.click('refresh');
    assert.equal(t.elements.apply.disabled, true);
    assert.equal(t.elements['sender-routes-list'].children.length, 0);
    assert.equal(JSON.stringify(t.elements).includes('<secret>'), false);
  }
});
