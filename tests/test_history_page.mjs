// Dependency-free DOM/bridge checks. These do not establish live AstrBot/QQ integration.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {test} from 'node:test';

const root = new URL('../pages/connection/', import.meta.url);
const source = fs.readFileSync(new URL('history.js', root), 'utf8');
const tabsSource = fs.readFileSync(new URL('tabs.js', root), 'utf8');
const settingsSource = fs.readFileSync(new URL('app.js', root), 'utf8');
const fullHtml = fs.readFileSync(new URL('index.html', root), 'utf8');
const html = fullHtml.split('<!-- HISTORY_PANEL_START -->')[1].split('<!-- HISTORY_PANEL_END -->')[0];
const css = fs.readFileSync(new URL('history.css', root), 'utf8');
const clone = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const store = {used_bytes: 20480, limit_bytes: 10485760, blocked: false};

class Element {
  constructor(tag = 'div') {
    this.tag = tag; this.children = []; this.events = {}; this.attributes = {};
    this.dataset = {}; this.disabled = false; this.hidden = false; this.value = '';
    this._text = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(''); }
  set innerHTML(_) { throw Error('unsafe HTML rendering'); }
  set outerHTML(_) { throw Error('unsafe HTML rendering'); }
  insertAdjacentHTML() { throw Error('unsafe HTML rendering'); }
  addEventListener(name, listener) { this.events[name] = listener; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  replaceChildren(...children) { this._text = ''; this.children = [...children]; }
  append(...children) { this.children.push(...children); }
  focus() { this.focused = true; }
}
function message(overrides = {}) {
  return {message_id: 'message-new', text: '一条私聊消息', timestamp: 1791960000, sender_id: '12345',
    conversation_type: 'private', group_id: '', processing_state: 'pending', replies: [], attachments: [], ...overrides};
}
function connectionStatus() {
  return {csrf_token: 'synthetic-csrf', revision: 1,
    settings: {enabled: false, platform_id: 'qq-main', adapter: 'aiocqhttp', self_id: '12345', listen_host: '127.0.0.1',
      listen_port: 8769, callback_hosts: [], tunnel_enabled: false, tunnel_id: '', proxy_enabled: false, proxy_url: ''},
    secrets: {mcp_token_set: true, tunnel_key_set: false, proxy_username_set: false, proxy_password_set: false},
    platforms: [{id: 'qq-main', adapter: 'aiocqhttp'}], runtime: {mcp_ready: false, tunnel: {state: 'stopped'}}, versions: {plugin: '0.4.0'}};
}
async function setup({noBridge = false, readyFailure = false, openHistory = true, loadSettings = false, status = {csrf_token: 'synthetic-csrf'},
  conversations = [{conversation_type: 'private', group_id: '', sender_id: '12345', message_count: 2},
    {conversation_type: 'group', group_id: '88888', message_count: 1}],
  firstMessages = [message()], nextBefore = 'older-page'} = {}) {
  const allElements = {};
  const elements = {};
  for (const match of fullHtml.matchAll(/<([a-z0-9]+)\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    const node = allElements[match[3]] = new Element(match[1]);
    if (match[3].startsWith('history-') && match[3] !== 'history-tab' && match[3] !== 'history-panel') elements[match[3].slice(8)] = node;
    node.hidden = /\bhidden(?:\s|$|=)/.test(match[2]);
    node.disabled = /\bdisabled(?:\s|$|=)/.test(match[2]);
    node.value = match[2].match(/\bvalue="([^"]*)"/)?.[1] || '';
    for (const [, name, value] of match[2].matchAll(/(aria-[\w-]+|tabindex)="([^"]*)"/g)) node.setAttribute(name, value);
  }
  const nodes = [];
  const calls = [];
  const control = {failGet: '', failPost: '', failAction: '', hold: false, release: null, contextListener: null,
    unsubscribed: false, callbacks: {}, status, conversations, storage: {...store}, nextBefore, conversationCursor: null, olderConversations: [], olderConversationCursor: null,
    firstMessages, olderMessages: [message({message_id: 'message-older', text: '更早的私聊消息', processing_state: 'completed'})],
    olderCursor: null, groupMessages: [message({message_id: 'group-message', conversation_type: 'group', group_id: '88888', text: '群聊消息'})]};
  const bridge = {
    ready: async () => { if (readyFailure) throw Error('bridge failure'); return {isDark: false}; },
    onContext: (listener) => { control.contextListener = listener; return () => { control.unsubscribed = true; }; },
    apiGet: async (endpoint) => {
      calls.push({method: 'GET', endpoint});
      if (control.failGet) throw Error(control.failGet);
      return clone(control.status);
    },
    apiPost: async (endpoint, body) => {
      calls.push({method: 'POST', endpoint, body: clone(body)});
      if (control.hold) await new Promise((resolve) => { control.release = resolve; });
      if (control.failPost && (!control.failAction || body.action === control.failAction)) throw Error(control.failPost);
      if (body.action === 'conversations') return clone({conversations: body.before ? control.olderConversations : control.conversations,
        next_before: body.before ? control.olderConversationCursor : control.conversationCursor, storage: control.storage});
      if (body.conversation_type === 'group') return clone({messages: control.groupMessages, next_before: null, storage: control.storage});
      return clone({messages: body.before ? control.olderMessages : control.firstMessages,
        next_before: body.before ? control.olderCursor : control.nextBefore, storage: control.storage});
    },
  };
  const panel = allElements['history-panel'];
  panel.querySelector = (selector) => selector.startsWith('#history-') ? elements[selector.slice(9)] : null;
  const document = {getElementById: (id) => allElements[id],
    createElement: (tag) => { const node = new Element(tag); nodes.push(node); return node; },
    documentElement: new Element('html'), body: new Element('body')};
  const context = vm.createContext({document, URL,
    window: {AstrBotPluginPage: noBridge ? undefined : bridge, addEventListener: (name, listener) => { control.callbacks[name] = listener; }},
  });
  vm.runInContext(source.replace('export function createHistoryPage', 'function createHistoryPage'), context);
  if (loadSettings) vm.runInContext(`(() => {${settingsSource}\n})();`, context);
  vm.runInContext(`(() => {${tabsSource.replace(/^import[^\n]+\n/, '')}\n})();`, context);
  await flush();
  if (openHistory) { allElements['history-tab'].events.click(); await flush(); }
  async function click(target) {
    const node = typeof target === 'string' ? elements[target] || allElements[target] : target;
    if (!node.disabled) node.events.click?.({preventDefault() {}});
    await flush();
  }
  return {elements, allElements, panel, nodes, calls, control, document, context, click,
    posts: () => calls.filter((call) => call.method === 'POST'),
    rendered: () => Object.values(elements).map((node) => node.textContent).join('\n')};
}

test('initial queries use official bridge and exact authenticated read-only payloads', async () => {
  const t = await setup();
  assert.deepEqual(t.calls, [
    {method: 'GET', endpoint: 'status'},
    {method: 'POST', endpoint: 'history', body: {csrf_token: 'synthetic-csrf', action: 'conversations', before: null, limit: 50}},
    {method: 'POST', endpoint: 'history', body: {csrf_token: 'synthetic-csrf', action: 'messages', conversation_type: 'private', group_id: '', sender_id: '12345', before: null, limit: 30}},
  ]);
  assert.equal(t.elements['conversations'].children.length, 2);
  assert.equal(t.elements['messages'].children.length, 1);
  assert.match(t.elements['messages'].textContent, /一条私聊消息/);
  assert.equal(t.elements.refresh.disabled, false);
  assert.equal(t.panel.attributes['aria-busy'], 'false');
});

test('first native page has accessible tabs and lazily opens history without any navigation', async () => {
  const t = await setup({openHistory: false});
  assert.equal(t.calls.length, 0);
  assert.equal(t.allElements['connection-panel'].hidden, false);
  assert.equal(t.panel.hidden, true);
  assert.equal(t.allElements['connection-tab'].attributes['aria-selected'], 'true');
  assert.equal(t.allElements['history-tab'].attributes['aria-selected'], 'false');
  await t.click('history-tab');
  assert.equal(t.allElements['connection-panel'].hidden, true);
  assert.equal(t.panel.hidden, false);
  assert.equal(t.allElements['connection-tab'].attributes['aria-selected'], 'false');
  assert.equal(t.allElements['history-tab'].attributes['aria-selected'], 'true');
  assert.equal(t.allElements['history-tab'].tabIndex, 0);
  assert.equal(t.calls.length, 3);
  const text = t.elements.messages.textContent;
  await t.click('connection-tab');
  await t.click('history-tab');
  await t.click('history-tab');
  assert.equal(t.calls.length, 3);
  assert.equal(t.elements.messages.textContent, text);
  assert.doesNotMatch(tabsSource + source, /window\.parent|window\.top|location|\.navigate\(|<iframe|fetch\(|innerHTML|localStorage|sessionStorage/);
  assert.match(tabsSource, /import \{createHistoryPage\} from '\.\/history\.js'/);
  assert.doesNotMatch(fullHtml, /\.\.\/history\/|<iframe/);
});

test('tab switching preserves dirty settings and secret inputs without any settings write', async () => {
  const t = await setup({openHistory: false, loadSettings: true, status: connectionStatus()});
  assert.equal(t.allElements['settings-fields'].disabled, false);
  t.allElements['listen-port'].value = '9001';
  t.allElements['listen-port'].events.input();
  t.allElements['tunnel-key'].value = 'unsaved-sensitive-input';
  t.allElements['tunnel-key'].events.input();
  assert.equal(t.allElements.save.disabled, false);
  await t.click('history-tab');
  assert.equal(t.panel.hidden, false);
  assert.match(t.elements.messages.textContent, /一条私聊消息/);
  assert.doesNotMatch(t.rendered(), /unsaved-sensitive-input/);
  assert.equal(t.allElements['notice'].className, 'notice info');
  assert.equal(t.elements.notice.className, 'h-notice h-info');
  await t.click('connection-tab');
  assert.equal(t.allElements['listen-port'].value, '9001');
  assert.equal(t.allElements['tunnel-key'].value, 'unsaved-sensitive-input');
  assert.equal(t.allElements.save.disabled, false);
  assert.equal(t.allElements.apply.disabled, true);
  assert.equal(t.posts().every((call) => call.endpoint === 'history'), true);
});

test('tab keyboard navigation uses roving focus and prevents arrow scrolling', async () => {
  const t = await setup({openHistory: false});
  let prevented = 0;
  t.allElements['connection-tab'].events.keydown({key: 'ArrowRight', preventDefault() { prevented++; }});
  await flush();
  assert.equal(prevented, 1);
  assert.equal(t.allElements['history-tab'].focused, true);
  assert.equal(t.panel.hidden, false);
  t.allElements['history-tab'].events.keydown({key: 'Home', preventDefault() { prevented++; }});
  assert.equal(t.allElements['connection-tab'].focused, true);
  assert.equal(t.panel.hidden, true);
  assert.equal(t.allElements['connection-tab'].tabIndex, 0);
  t.allElements['connection-tab'].events.keydown({key: 'End', preventDefault() { prevented++; }});
  assert.equal(t.panel.hidden, false);
  assert.equal(t.calls.length, 3);
});

test('switching away and back during initial history read keeps one request and leaves settings usable', async () => {
  const t = await setup({openHistory: false, loadSettings: true, status: connectionStatus()});
  t.control.hold = true;
  await t.click('history-tab');
  assert.equal(t.posts().length, 1);
  assert.equal(t.panel.attributes['aria-busy'], 'true');
  assert.equal(t.document.body.attributes['aria-busy'], 'false');
  await t.click('connection-tab');
  assert.equal(t.allElements['settings-fields'].disabled, false);
  await t.click('history-tab');
  await t.click('connection-tab');
  assert.equal(t.posts().length, 1);
  t.control.hold = false;
  t.control.release();
  await flush();
  assert.equal(t.posts().length, 2);
  assert.equal(t.panel.hidden, true);
  assert.equal(t.panel.attributes['aria-busy'], 'false');
  await t.click('history-tab');
  assert.match(t.elements.messages.textContent, /一条私聊消息/);
  assert.equal(t.posts().length, 2);
});

test('private is selected first even if server lists a group before it', async () => {
  const t = await setup({conversations: [{conversation_type: 'group', group_id: '88888'}, {conversation_type: 'private', group_id: '', sender_id: '12345'}]});
  assert.equal(t.posts()[1].body.conversation_type, 'private');
});

test('switching group replaces private text, uses only the returned group, and shows privacy notice', async () => {
  const t = await setup();
  assert.equal(t.elements['group-privacy'].hidden, true);
  await t.click(t.elements['conversations'].children[1]);
  assert.deepEqual(t.posts().at(-1).body, {csrf_token: 'synthetic-csrf', action: 'messages', conversation_type: 'group', group_id: '88888', before: null, limit: 30});
  assert.equal(t.elements['messages-heading'].textContent, '群 88888');
  assert.equal(t.elements['group-privacy'].hidden, false);
  assert.match(t.elements['messages'].textContent, /群聊消息/);
  assert.doesNotMatch(t.elements['messages'].textContent, /一条私聊消息/);
  assert.equal(t.elements['load-more'].hidden, true);
  assert.equal(t.elements['conversations'].children[1].attributes['aria-current'], 'true');
  await t.click(t.elements['conversations'].children[0]);
  assert.equal(t.elements['group-privacy'].hidden, true);
  assert.doesNotMatch(t.elements['messages'].textContent, /群聊消息/);
});

test('pagination sends opaque cursor, deduplicates overlap, and hides at end', async () => {
  const t = await setup();
  t.control.olderMessages.unshift(message());
  await t.click('load-more');
  assert.equal(t.posts().at(-1).body.before, 'older-page');
  assert.equal(t.elements.messages.children.length, 2);
  assert.match(t.elements['pagination-state'].textContent, /已显示 2 条/);
  assert.equal(t.elements['load-more'].hidden, true);
  await t.click('load-more');
  assert.equal(t.posts().length, 3);
});

test('conversation pagination appends selectable groups without mixing or replacing current messages', async () => {
  const t = await setup();
  t.control.conversationCursor = 'more-conversations';
  t.control.olderConversations = [t.control.conversations[1], {conversation_type: 'group', group_id: '99999', message_count: 3}];
  await t.click('refresh');
  assert.equal(t.elements['load-conversations'].hidden, false);
  const messagesBefore = t.elements.messages.textContent;
  await t.click('load-conversations');
  assert.deepEqual(t.posts().at(-1).body, {csrf_token: 'synthetic-csrf', action: 'conversations', before: 'more-conversations', limit: 50});
  assert.equal(t.elements.conversations.children.length, 3);
  assert.equal(t.elements.messages.textContent, messagesBefore);
  assert.equal(t.elements['messages-heading'].textContent, '私聊 QQ 12345');
  assert.match(t.elements['conversation-page-state'].textContent, /已加载 3 个会话 · 已全部加载/);
  assert.equal(t.elements['load-conversations'].hidden, true);
  t.control.groupMessages = [message({conversation_type: 'group', group_id: '99999', text: '第二页群聊'})];
  await t.click(t.elements.conversations.children[2]);
  assert.equal(t.posts().at(-1).body.group_id, '99999');
  assert.match(t.elements.messages.textContent, /第二页群聊/);
  assert.doesNotMatch(t.elements.messages.textContent, /一条私聊消息/);
});

test('conversation pages have no silent overall 50-group cap', async () => {
  const t = await setup({conversations: Array.from({length: 50}, (_, index) => ({conversation_type: 'group', group_id: String(10000 + index), message_count: 1}))});
  t.control.groupMessages = [];
  t.control.conversationCursor = 'second-conversation-page';
  t.control.olderConversations = Array.from({length: 50}, (_, index) => ({conversation_type: 'group', group_id: String(10050 + index), message_count: 1}));
  t.control.olderConversationCursor = 'third-conversation-page';
  await t.click('refresh');
  await t.click('load-conversations');
  t.control.olderConversations = [{conversation_type: 'private', group_id: '', sender_id: '12345', message_count: 1}];
  t.control.olderConversationCursor = null;
  await t.click('load-conversations');
  assert.equal(t.elements.conversations.children.length, 101);
  assert.equal(t.elements['conversation-count'].textContent, '101');
  assert.equal(t.elements['load-conversations'].hidden, true);
  await t.click(t.elements.conversations.children[100]);
  assert.match(t.elements.messages.textContent, /一条私聊消息/);
});

test('conversation pagination blocks repeated clicks and preserves both lists after errors', async () => {
  const t = await setup();
  t.control.conversationCursor = 'more-conversations';
  await t.click('refresh');
  t.control.hold = true;
  t.control.failPost = 'SECRET_CONVERSATION_ERROR';
  const callsBefore = t.posts().length;
  await t.click('load-conversations');
  await t.click('load-conversations');
  await t.click('load-more');
  await t.click('refresh');
  await t.click(t.elements.conversations.children[1]);
  assert.equal(t.posts().length, callsBefore + 1);
  t.control.release();
  await flush();
  assert.equal(t.elements.conversations.children.length, 2);
  assert.equal(t.elements.messages.children.length, 1);
  assert.doesNotMatch(t.rendered(), /SECRET_CONVERSATION_ERROR/);
  assert.equal(t.elements['load-conversations'].disabled, false);
});

test('malformed or repeating conversation cursors fail without discarding the selected history', async () => {
  const t = await setup();
  t.control.conversationCursor = 'more-conversations';
  await t.click('refresh');
  t.control.olderConversations = [{conversation_type: 'group', group_id: '99999'}];
  t.control.olderConversationCursor = 'more-conversations';
  await t.click('load-conversations');
  assert.equal(t.elements.conversations.children.length, 2);
  assert.equal(t.elements.messages.children.length, 1);
  assert.match(t.elements.notice.textContent, /无法安全显示/);
  t.control.conversationCursor = {invalid: true};
  await t.click('refresh');
  assert.equal(t.elements.conversations.children.length, 0);
  assert.equal(t.elements.messages.children.length, 0);
});

test('repeat clicks and newer navigation cannot race a pending request', async () => {
  const t = await setup();
  t.control.hold = true;
  await t.click('load-more');
  await t.click('load-more');
  await t.click('refresh');
  await t.click(t.elements.conversations.children[1]);
  assert.equal(t.posts().length, 3);
  assert.equal(t.elements['messages-heading'].textContent, '私聊 QQ 12345');
  assert.equal(t.panel.attributes['aria-busy'], 'true');
  t.control.release();
  await flush();
  assert.equal(t.panel.attributes['aria-busy'], 'false');
});

test('refresh preserves selection and re-reads CSRF without retaining old pages', async () => {
  const t = await setup();
  await t.click(t.elements.conversations.children[1]);
  t.control.status.csrf_token = 'refreshed-csrf';
  await t.click('refresh');
  assert.equal(t.posts().at(-1).body.csrf_token, 'refreshed-csrf');
  assert.equal(t.posts().at(-1).body.conversation_type, 'group');
  assert.equal(t.posts().at(-1).body.before, null);
  t.control.conversations = [{conversation_type: 'private', group_id: '', sender_id: '12345', message_count: 1}];
  await t.click('refresh');
  assert.equal(t.elements['messages-heading'].textContent, '私聊 QQ 12345');
  assert.equal(t.elements['group-privacy'].hidden, true);
});

test('empty storage, empty selected conversation, and unknown storage are honest empty states', async () => {
  const empty = await setup({conversations: []});
  assert.equal(empty.posts().length, 1);
  assert.equal(empty.elements['conversation-empty'].hidden, false);
  assert.equal(empty.elements['messages'].children.length, 0);
  assert.match(empty.elements.notice.textContent, /暂无保留记录/);
  const t = await setup({firstMessages: [], nextBefore: null});
  assert.match(t.elements['message-empty'].textContent, /暂无保留/);
  t.control.storage = {};
  await t.click('refresh');
  assert.equal(t.elements['storage-usage'].textContent, '容量暂不可用');
  assert.equal(t.elements['storage-state'].textContent, '写入状态未知');
});

test('storage meter shows bounded usage and blocked warning without rendering server reason', async () => {
  const t = await setup();
  assert.match(t.elements['storage-usage'].textContent, /20.0 KiB \/ 10.00 MiB/);
  t.control.storage = {limit_bytes: 10485760, used_bytes: 10485760, blocked: true, reason: 'SECRET_STORAGE_PATH'};
  await t.click('refresh');
  assert.equal(t.elements['storage-meter'].value, 100);
  assert.equal(t.elements['storage-warning'].hidden, false);
  assert.match(t.elements['storage-state'].textContent, /受阻/);
  assert.doesNotMatch(t.rendered(), /SECRET_STORAGE_PATH/);
  assert.equal(t.elements.messages.children.length, 1);
});

test('processing and adapter results never imply confirmed QQ delivery', async () => {
  const t = await setup({firstMessages: [message({processing_state: 'completed', replies: [
    {text: '回复 1', state: 'adapter_completed'}, {text: '回复 2', state: 'uncertain'},
    {text: '回复 3', state: 'not_sent'}, {text: '回复 4', state: 'sending'}, {text: '回复 5', state: 'sent'},
  ]})]});
  const text = t.elements.messages.textContent;
  for (const expected of ['已处理', '适配器已返回 · 非送达确认', '发送结果不确定 · 不自动重试', '未发送', '发送中', '状态未知']) assert.ok(text.includes(expected));
  assert.doesNotMatch(text, /已发送|已送达/);
  assert.equal(t.posts().length, 2);
});

test('hostile text remains plain text; unrelated secrets, routing, URLs and IDs never enter DOM', async () => {
  const hostile = '<img src="https://evil.example/x" onerror="alert(1)"><script>steal()</script> & <svg/onload=alert(2)>';
  const t = await setup({status: {csrf_token: 'secret-csrf', mcp_token: 'SECRET_MCP', umo: 'SECRET_UMO'},
    firstMessages: [message({text: hostile, replies: [{text: hostile, state: 'constructor'}], processing_state: '__proto__',
      source_message_id: 'SECRET_SOURCE', umo: 'SECRET_UMO', mcp_token: 'SECRET_MCP', message_id: 'PRIVATE_MESSAGE_ID',
      attachments: [{attachment_id: 'PRIVATE_ATTACHMENT_ID', kind: 'image', name: '<svg/onload=alert(3)>', mime_type: 'SECRET_MIME',
        size: 1024, state: '<img>', url: 'https://secret.example/attachment', data: 'data:image/svg+xml,SECRET_IMAGE'}]})]});
  assert.ok(t.elements.messages.textContent.includes(hostile));
  assert.match(t.elements.messages.textContent, /图片 · <svg\/onload=alert\(3\)> · 1.0 KiB · 仅文字信息/);
  assert.doesNotMatch(t.rendered(), /SECRET_|PRIVATE_MESSAGE_ID|PRIVATE_ATTACHMENT_ID|secret-csrf|secret\.example/);
  assert.equal(t.nodes.some((node) => ['img', 'script', 'iframe', 'a', 'svg'].includes(node.tag)), false);
  assert.equal(t.nodes.some((node) => 'src' in node || 'href' in node), false);
});

test('control characters are removed, newlines preserved, oversized text bounded, malformed metadata ignored', async () => {
  const t = await setup({firstMessages: [message({text: 'a\n\tb\u0000\u202ec' + 'x'.repeat(20000), timestamp: 'hostile',
    attachments: [{kind: '__proto__', name: '<b>test</b>', size: -1}], replies: [{text: {bad: 'SECRET_OBJECT'}, state: '__proto__'}]})]});
  assert.match(t.elements.messages.textContent, /a\n\tbc/);
  assert.match(t.elements.messages.textContent, /时间未知/);
  assert.match(t.elements.messages.textContent, /QQ 12345/);
  assert.ok(t.elements.messages.textContent.length < 17000);
  assert.doesNotMatch(t.rendered(), /SECRET_OBJECT|\u0000|\u202e/);
});

test('cross-conversation responses fail closed and cannot leak private text into a group', async () => {
  const t = await setup();
  t.control.groupMessages = [message({text: 'PRIVATE_CONTENT_MUST_NOT_RENDER'})];
  await t.click(t.elements.conversations.children[1]);
  assert.equal(t.elements.messages.children.length, 0);
  assert.doesNotMatch(t.rendered(), /PRIVATE_CONTENT_MUST_NOT_RENDER|一条私聊消息/);
  assert.match(t.elements.notice.textContent, /无法安全显示/);
});

test('invalid conversation identities and duplicate conversation entries stop before message queries', async () => {
  for (const conversations of [
    [{conversation_type: 'group', group_id: '88888?token=secret'}],
    [{conversation_type: 'private', group_id: '88888', sender_id: '12345'}],
    [{conversation_type: 'private', group_id: '', sender_id: '<img>'}],
    [{conversation_type: 'private', group_id: ''}],
    [{conversation_type: 'private', group_id: '', sender_id: '12345'}, {conversation_type: 'private', group_id: '', sender_id: '12345'}],
  ]) {
    const t = await setup({conversations});
    assert.equal(t.posts().length, 1);
    assert.equal(t.elements.conversations.children.length, 0);
    assert.match(t.elements.notice.textContent, /无法安全显示/);
  }
});

test('malformed or repeating cursors stop pagination without losing visible history', async () => {
  const malformed = await setup({nextBefore: {secret: 'invalid'}});
  assert.equal(malformed.elements.messages.children.length, 0);
  assert.equal(malformed.elements['load-more'].hidden, true);
  for (const [olderMessages, olderCursor] of [[ [message({message_id: 'different'})], 'older-page'], [[message()], 'changed-but-duplicate']]) {
    const t = await setup();
    t.control.olderMessages = olderMessages;
    t.control.olderCursor = olderCursor;
    await t.click('load-more');
    assert.equal(t.elements.messages.children.length, 1);
    assert.match(t.elements.notice.textContent, /无法安全显示/);
  }
});

test('oversized message responses and invalid message IDs are rejected', async () => {
  for (const firstMessages of [Array.from({length: 31}, (_, index) => message({message_id: String(index)})),
    [message({message_id: null})], [message({message_id: 'x\u0000'})]]) {
    const t = await setup({firstMessages});
    assert.equal(t.elements.messages.children.length, 0);
    assert.match(t.elements.notice.textContent, /无法安全显示/);
  }
});

test('raw server failures never render, requests never auto-retry, refresh can recover', async () => {
  const t = await setup();
  t.control.failPost = '<script>SECRET_STACK_TRACE</script>';
  await t.click('load-more');
  assert.equal(t.posts().length, 3);
  assert.equal(t.elements.messages.children.length, 1);
  assert.doesNotMatch(t.rendered(), /SECRET_STACK_TRACE/);
  t.control.failPost = '';
  await t.click('refresh');
  assert.match(t.elements.notice.textContent, /记录已更新/);
});

test('expired authorization disables further history queries until verified status refresh', async () => {
  const t = await setup();
  t.control.failPost = 'forbidden';
  await t.click('load-more');
  assert.equal(t.elements['load-more'].disabled, true);
  assert.equal(t.elements.conversations.children[1].disabled, true);
  assert.equal(t.elements.refresh.disabled, false);
  t.control.failPost = '';
  t.control.status.csrf_token = 'new-csrf';
  await t.click('refresh');
  assert.equal(t.posts().at(-1).body.csrf_token, 'new-csrf');
  assert.equal(t.elements['load-more'].disabled, false);
});

test('status failure or invalid CSRF never invokes history, clears old display, and permits refresh', async () => {
  for (const status of [{}, {csrf_token: ''}, {csrf_token: 'x'.repeat(257)}]) {
    const t = await setup({status});
    assert.equal(t.posts().length, 0);
    assert.equal(t.elements.refresh.disabled, false);
  }
  const t = await setup();
  t.control.failGet = 'SECRET_STATUS_ERROR';
  await t.click('refresh');
  assert.equal(t.posts().length, 2);
  assert.equal(t.elements.messages.children.length, 0);
  assert.equal(t.elements.conversations.children.length, 0);
  assert.doesNotMatch(t.rendered(), /SECRET_STATUS_ERROR/);
});

test('missing or failed bridge leaves controls disabled and never requests history', async () => {
  for (const options of [{noBridge: true}, {readyFailure: true}]) {
    const t = await setup(options);
    assert.equal(t.calls.length, 0);
    assert.equal(t.elements.refresh.disabled, true);
    assert.equal(t.elements['load-more'].disabled, true);
    assert.match(t.elements.notice.textContent, /插件详情/);
  }
});

test('context updates theme and unload unsubscribes without storage or external fetches', async () => {
  const t = await setup();
  assert.equal(t.document.documentElement.dataset.theme, 'light');
  t.control.contextListener({isDark: true});
  assert.equal(t.document.documentElement.dataset.theme, 'dark');
  t.control.callbacks.beforeunload();
  assert.equal(t.control.unsubscribed, true);
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|fetch\(|XMLHttpRequest|localStorage|sessionStorage|window\.parent/);
  assert.doesNotMatch(html, /https?:\/\/|<iframe|<form|<input/);
  assert.doesNotMatch(source, /apiPost\(['"](?:action|settings|reply|complete|delete)/);
  assert.deepEqual([...fullHtml.matchAll(/<script\b[^>]*src="([^"]+)"/g)].map((match) => match[1]), ['./app.js', './tabs.js']);
});

test('native page has only the expected local assets and localized sidebar entries', () => {
  for (const [locale, expected] of [['zh-CN', '连接设置与消息记录'], ['en-US', 'Connection and history']]) {
    const data = JSON.parse(fs.readFileSync(new URL(`../.astrbot-plugin/i18n/${locale}.json`, import.meta.url), 'utf8'));
    assert.equal(data.pages.connection.title, expected);
    assert.equal(data.pages.history, undefined);
  }
  assert.ok(fs.existsSync(new URL('history.css', root)));
  assert.match(html, /群聊回复会被群内成员看到/);
  assert.match(html, /不自动加载外部图片或下载文件/);
  const ids = [...fullHtml.matchAll(/\bid="([^"]+)"/g)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length);
  for (const [, id] of source.matchAll(/byId\('([^']+)'\)/g)) assert.ok(ids.includes(`history-${id}`), `Missing DOM target history-${id}`);
  assert.match(css, /:root\[data-theme="dark"\]/);
  assert.match(css, /@media\(max-width:600px\)/);
  assert.match(css, /button:focus-visible/);
  assert.match(css, /overflow-wrap:anywhere/);
  assert.match(css, /unicode-bidi:plaintext/);
  assert.doesNotMatch(css, /@import|https?:\/\/|url\(/);
});

test('display bounds disclose truncated text, attachment counts, and reply counts', async () => {
  const t = await setup({firstMessages: [message({text: 'x'.repeat(18000),
    attachments: Array.from({length: 21}, () => ({kind: 'file', name: 'test.txt'})),
    replies: Array.from({length: 21}, () => ({text: '回复', state: 'uncertain'})),
  })]});
  assert.match(t.elements.messages.textContent, /显示已截断/);
  assert.match(t.elements.messages.textContent, /仅显示前 20 个附件，共 21 个/);
  assert.match(t.elements.messages.textContent, /仅显示前 20 条回复，共 21 条/);
});


test('HTTP cached attachment provenance is visible without echoing unknown source metadata', async () => {
  const t = await setup({firstMessages: [message({attachments: [
    {kind: 'file', name: 'sample.txt', transport: 'http', integrity: 'unverified'},
    {kind: 'file', name: 'other.txt', transport: 'SECRET_TRANSPORT', integrity: 'SECRET_INTEGRITY'},
  ]})]});
  assert.match(t.elements.messages.textContent, /HTTP明文来源，完整性未验证/);
  assert.doesNotMatch(t.elements.messages.textContent, /SECRET_TRANSPORT|SECRET_INTEGRITY/);
});


test('separate private conversations select explicit senders and never append another sender', async () => {
  const t = await setup({conversations: [
    {conversation_type: 'private', group_id: '', sender_id: '12345', message_count: 1},
    {conversation_type: 'private', group_id: '', sender_id: '67890', message_count: 1},
  ]});
  assert.equal(t.elements.conversations.children.length, 2);
  assert.equal(t.posts().at(-1).body.sender_id, '12345');
  assert.match(t.elements.conversations.textContent, /QQ 12345/);
  assert.match(t.elements.conversations.textContent, /QQ 67890/);
  t.control.firstMessages = [message({message_id: 'second-private', sender_id: '67890', text: '第二个私聊'})];
  await t.click(t.elements.conversations.children[1]);
  assert.equal(t.posts().at(-1).body.sender_id, '67890');
  assert.equal(t.elements['messages-heading'].textContent, '私聊 QQ 67890');
  assert.match(t.elements.messages.textContent, /第二个私聊/);
  assert.doesNotMatch(t.elements.messages.textContent, /一条私聊消息/);
  t.control.olderMessages = [message({message_id: 'wrong-sender', text: 'CROSS_PRIVATE_SECRET'})];
  await t.click('load-more');
  assert.equal(t.posts().at(-1).body.sender_id, '67890');
  assert.doesNotMatch(t.rendered(), /CROSS_PRIVATE_SECRET/);
  assert.match(t.elements.notice.textContent, /无法安全显示/);
});

test('mismatched or malformed private sender response fails closed before rendering', async () => {
  for (const sender_id of ['67890', '', null, '<img>']) {
    const t = await setup({firstMessages: [message({sender_id, text: 'PRIVATE_SECRET'})]});
    assert.equal(t.elements.messages.children.length, 0);
    assert.doesNotMatch(t.rendered(), /PRIVATE_SECRET/);
    assert.match(t.elements.notice.textContent, /无法安全显示/);
  }
});

test('permission revocation refresh drops selected sender and cannot retain their display', async () => {
  const t = await setup();
  t.control.conversations = [{conversation_type: 'private', group_id: '', sender_id: '67890', message_count: 1}];
  t.control.firstMessages = [message({message_id: 'other-private', sender_id: '67890', text: '当前允许的私聊'})];
  await t.click('refresh');
  assert.equal(t.elements.conversations.children.length, 1);
  assert.equal(t.posts().at(-1).body.sender_id, '67890');
  assert.match(t.elements.messages.textContent, /当前允许的私聊/);
  assert.doesNotMatch(t.elements.messages.textContent, /一条私聊消息/);
});

test('multi-route dashboard limitation is specific and does not imply route MCP history is unavailable', async () => {
  const t = await setup();
  t.control.failPost = 'multi_route_history_unavailable';
  const before = t.posts().length;
  await t.click('refresh');
  assert.equal(t.posts().length, before + 1);
  assert.equal(t.posts().at(-1).body.action, 'conversations');
  assert.match(t.elements.notice.textContent, /多路由模式暂不支持在此管理页/);
  assert.match(t.elements.notice.textContent, /各路由的 MCP 历史查询工具仍可使用/);
  assert.match(t.elements['message-empty'].textContent, /对应路由的 MCP/);
  assert.doesNotMatch(t.elements['message-empty'].textContent, /重试/);
  assert.equal(t.elements.messages.children.length, 0);
  assert.equal(t.elements.conversations.children.length, 0);
  assert.equal(t.elements['load-more'].disabled, true);
  assert.equal(t.elements.refresh.disabled, false);
  t.control.failPost = '';
  await t.click('refresh');
  assert.equal(t.elements.messages.children.length, 1);
  assert.doesNotMatch(t.elements.notice.textContent, /多路由/);
});
