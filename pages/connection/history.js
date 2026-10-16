// Shared in-document history controller. All DOM access is scoped to its panel.
export function createHistoryPage(root, bridge) {
  // AstrBot v4.28.2 injects this bridge and unwraps its status/data envelope.
  // POST history is a read-only query. The page never modifies message state.
  const byId = (id) => root.querySelector(`#history-${id}`);
  const PAGE_SIZE = 30;
  const CONVERSATION_PAGE_SIZE = 50;
  const errors = {
    forbidden: '页面授权已失效。请刷新记录，或重新打开 AstrBot 插件页面。',
    invalid_request: '无法读取这段记录，请刷新后重试。',
    invalid_cursor: '分页位置已失效，请刷新后重新查看。',
    invalid_history_request: '无法读取这段记录，请刷新后重试。',
    invalid_history_cursor: '分页位置已失效，请刷新后重新查看。',
    private_sender_required: '请选择具体的私聊会话后重试。',
    invalid_history_scope: '这个会话已不可用，请刷新会话列表。',
    binding_inactive: '消息权限已变化，请刷新会话列表。',
    history_unavailable: '消息记录暂时不可用，请检查服务器状态后刷新。',
    multi_route_history_unavailable: '多路由模式暂不支持在此管理页查看消息记录。各路由的 MCP 历史查询工具仍可使用。',
    storage_unavailable: '本地记录存储暂时不可用，请检查服务器状态后刷新。',
    invalid_response: '服务器返回的记录无法安全显示，请刷新后重试。',
  };
  const processingStates = {pending: '待处理', completed: '已处理'};
  const replyStates = {sending: '发送中', adapter_completed: '适配器已返回 · 非送达确认', uncertain: '发送结果不确定 · 不自动重试', not_sent: '未发送'};
  const attachmentKinds = {image: '图片', file: '文件', audio: '音频', record: '语音', video: '视频'};
  const state = {ready: false, busy: false, csrf: '', conversations: [], nextConversations: null, selected: null, messages: [], nextBefore: null};
  let unsubscribeContext;

  function object(value) {
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  }
  function safeText(value, limit = 16384) {
    if (typeof value !== 'string') return '';
    const text = value.replace(/[\u0000-\u0008\u000b-\u001f\u007f\u202a-\u202e\u2066-\u2069]/g, '');
    return text.length > limit ? `${text.slice(0, limit)}…（显示已截断）` : text;
  }
  function identifier(value) {
    return typeof value === 'string' && /^[1-9][0-9]{0,19}$/.test(value) ? value : '';
  }
  function cursor(value, limit = 1024) {
    if (value === null || value === undefined) return null;
    if (typeof value !== 'string' || !value || value.length > limit || /[\p{C}]/u.test(value)) throw new Error('invalid_response');
    return value;
  }
  function conversation(value) {
    const item = object(value);
    if (item.conversation_type === 'private' && (item.group_id === '' || item.group_id === null) && identifier(item.sender_id)) {
      return {conversation_type: 'private', group_id: '', sender_id: item.sender_id};
    }
    if (item.conversation_type === 'group' && identifier(item.group_id)) {
      return {conversation_type: 'group', group_id: item.group_id};
    }
    throw new Error('invalid_response');
  }
  function key(item) { return `${item.conversation_type}:${item.group_id}:${item.conversation_type === 'private' ? item.sender_id : ''}`; }
  function count(value) { return Number.isSafeInteger(value) && value >= 0 ? value : 0; }
  function normalizeConversations(raw) {
    const value = object(raw);
    if (!Array.isArray(value.conversations) || value.conversations.length > CONVERSATION_PAGE_SIZE) throw new Error('invalid_response');
    const seen = new Set();
    const conversations = value.conversations.map((rawItem) => {
      const item = {...conversation(rawItem), message_count: count(rawItem.message_count)};
      if (seen.has(key(item))) throw new Error('invalid_response');
      seen.add(key(item));
      return item;
    });
    const nextBefore = cursor(value.next_before);
    if (nextBefore && !conversations.length) throw new Error('invalid_response');
    return {conversations, nextBefore};
  }
  function normalizeMessages(raw, selected) {
    const value = object(raw);
    if (!Array.isArray(value.messages) || value.messages.length > PAGE_SIZE) throw new Error('invalid_response');
    const messages = value.messages.map((rawItem) => {
      const item = object(rawItem);
      if (key(conversation(item)) !== key(selected)) throw new Error('invalid_response');
      const messageId = cursor(item.message_id, 128);
      if (!messageId) throw new Error('invalid_response');
      return {
        message_id: messageId,
        text: safeText(item.text), sender_id: identifier(item.sender_id),
        timestamp: Number.isSafeInteger(item.timestamp) && item.timestamp >= 0 && item.timestamp <= 253402300799 ? item.timestamp : null,
        processing_state: Object.hasOwn(processingStates, item.processing_state) ? item.processing_state : 'unknown',
        reply_count: Array.isArray(item.replies) ? item.replies.length : 0,
        replies: Array.isArray(item.replies) ? item.replies.slice(0, 20).map((reply) => ({
          text: safeText(object(reply).text),
          state: Object.hasOwn(replyStates, object(reply).state) ? reply.state : 'unknown',
        })) : [],
        attachments: Array.isArray(item.attachments) ? item.attachments.slice(0, 20).map((attachment) => {
          const value = object(attachment);
          return {kind: Object.hasOwn(attachmentKinds, value.kind) ? value.kind : 'file', name: safeText(value.name, 160),
            size: Number.isSafeInteger(value.size) && value.size >= 0 ? value.size : null,
            http_unverified: value.transport === 'http'};
        }) : [],
        attachment_count: Array.isArray(item.attachments) ? item.attachments.length : 0,
      };
    });
    const nextBefore = cursor(value.next_before);
    if (nextBefore && !messages.length) throw new Error('invalid_response');
    return {messages, nextBefore};
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className.split(' ').map((name) => `h-${name}`).join(' ');
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function notice(text, type = 'info') {
    byId('notice').textContent = text;
    byId('notice').className = `h-notice h-${type}`;
  }
  function showError(error) {
    notice(Object.hasOwn(errors, error?.message) ? errors[error.message] : '读取失败，请刷新记录后重试。', 'error');
    if (error?.message === 'forbidden') state.csrf = '';
  }
  function bytes(value) {
    return value >= 1024 * 1024 ? `${(value / (1024 * 1024)).toFixed(2)} MiB` :
      value >= 1024 ? `${(value / 1024).toFixed(1)} KiB` : `${value} B`;
  }
  function renderStorage(raw) {
    const storage = object(raw);
    const valid = Number.isSafeInteger(storage.used_bytes) && storage.used_bytes >= 0 &&
      Number.isSafeInteger(storage.limit_bytes) && storage.limit_bytes > 0;
    byId('storage-usage').textContent = valid ? `${bytes(storage.used_bytes)} / ${bytes(storage.limit_bytes)}` : '容量暂不可用';
    byId('storage-meter').value = valid ? Math.min(100, storage.used_bytes / storage.limit_bytes * 100) : 0;
    byId('storage-state').textContent = storage.blocked === true ? '记录写入受阻' : storage.blocked === false ? '记录空间可用' : '写入状态未知';
    byId('storage-warning').hidden = storage.blocked !== true;
    byId('storage-warning').textContent = storage.blocked === true ? '存储空间或写入状态受限，新消息可能无法保存。现有记录仍可查看，请检查 AstrBot 服务器。' : '';
  }
  function updateControls() {
    byId('refresh').disabled = state.busy || !state.ready;
    byId('load-more').disabled = state.busy || !state.csrf || !state.nextBefore;
    byId('load-more').hidden = !state.nextBefore;
    byId('load-conversations').disabled = state.busy || !state.csrf || !state.nextConversations;
    byId('load-conversations').hidden = !state.nextConversations;
    for (const button of byId('conversations').children) button.disabled = state.busy || !state.csrf;
    root.setAttribute('aria-busy', String(state.busy));
  }
  function renderConversations() {
    byId('conversations').replaceChildren();
    byId('conversation-count').textContent = String(state.conversations.length);
    byId('conversation-page-state').textContent = state.conversations.length ? `已加载 ${state.conversations.length} 个会话${state.nextConversations ? '' : ' · 已全部加载'}` : '';
    byId('conversation-empty').hidden = !!state.conversations.length;
    byId('conversation-empty').textContent = '暂无会话记录';
    for (const item of state.conversations) {
      const button = element('button', 'conversation');
      button.type = 'button';
      button.setAttribute('aria-current', String(!!state.selected && key(item) === key(state.selected)));
      button.append(element('span', 'conversation-name', item.conversation_type === 'private' ? `私聊 QQ ${item.sender_id}` : `群 ${item.group_id}`),
        element('span', 'conversation-count', `${item.message_count} 条记录`));
      button.addEventListener('click', () => void selectConversation(item));
      byId('conversations').append(button);
    }
  }
  function renderMessages() {
    const selected = state.selected;
    byId('messages-heading').textContent = !selected ? '选择一个会话' : selected.conversation_type === 'private' ? `私聊 QQ ${selected.sender_id}` : `群 ${selected.group_id}`;
    byId('conversation-detail').textContent = selected ? '最新消息在前，时间按浏览器时区显示。处理状态不代表 QQ 已送达。' : '只展示插件实际收到并保留的记录，可能不是完整 QQ 聊天记录。';
    byId('group-privacy').hidden = !selected || selected.conversation_type !== 'group';
    byId('message-empty').hidden = !!state.messages.length;
    byId('message-empty').textContent = selected ? '这个会话暂无保留的消息' : '暂无消息记录';
    byId('messages').replaceChildren();
    for (const message of state.messages) {
      const item = element('li', 'message');
      const header = element('div', 'message-header');
      const meta = element('div', 'message-meta');
      const time = element('time', 'message-time', message.timestamp === null ? '时间未知' : new Date(message.timestamp * 1000).toLocaleString('zh-CN', {hour12: false}));
      if (message.timestamp !== null) time.dateTime = new Date(message.timestamp * 1000).toISOString();
      meta.append(element('strong', 'sender', message.sender_id ? `QQ ${message.sender_id}` : '发送者未知'), time);
      header.append(meta, element('span', `badge ${message.processing_state}`, processingStates[message.processing_state] || '状态未知'));
      item.append(header, element('p', 'message-text', message.text || (message.attachments.length ? '（附件消息）' : '（无文字内容）')));
      if (message.attachments.length) {
        const attachments = element('ul', 'attachments');
        for (const attachment of message.attachments) {
          const label = [attachmentKinds[attachment.kind], attachment.name, attachment.size === null ? '' : bytes(attachment.size)].filter(Boolean).join(' · ');
          attachments.append(element('li', 'attachment', `${label} · 仅文字信息${attachment.http_unverified ? ' · HTTP明文来源，完整性未验证' : ''}`));
        }
        item.append(attachments);
        if (message.attachment_count > message.attachments.length) item.append(element('p', 'help', `仅显示前 ${message.attachments.length} 个附件，共 ${message.attachment_count} 个。`));
      }
      if (message.replies.length) {
        const replies = element('div', 'replies');
        for (const reply of message.replies) {
          const replyNode = element('div', 'reply');
          const replyHeader = element('div', 'reply-header');
          replyHeader.append(element('strong', '', 'dot 回复'), element('span', `reply-state ${reply.state}`, replyStates[reply.state] || '状态未知'));
          replyNode.append(replyHeader, element('p', 'message-text', reply.text || '（无文字内容）'));
          replies.append(replyNode);
        }
        item.append(replies);
        if (message.reply_count > message.replies.length) item.append(element('p', 'help', `仅显示前 ${message.replies.length} 条回复，共 ${message.reply_count} 条。`));
      } else item.append(element('p', 'no-reply', '暂无已记录的回复'));
      byId('messages').append(item);
    }
    byId('pagination-state').textContent = state.messages.length ? `已显示 ${state.messages.length} 条${state.nextBefore ? '' : ' · 已到保留记录的开头'}` : '';
    updateControls();
  }
  async function readMessages(append = false) {
    const before = append ? state.nextBefore : null;
    const response = await bridge.apiPost('history', {csrf_token: state.csrf, action: 'messages',
      conversation_type: state.selected.conversation_type, group_id: state.selected.group_id,
      ...(state.selected.conversation_type === 'private' ? {sender_id: state.selected.sender_id} : {}), before, limit: PAGE_SIZE});
    const result = normalizeMessages(response, state.selected);
    if (before && result.nextBefore === before) throw new Error('invalid_response');
    const previous = append ? state.messages : [];
    const seen = new Set(previous.map((message) => message.message_id));
    const additions = result.messages.filter((message) => {
      if (seen.has(message.message_id)) return false;
      seen.add(message.message_id);
      return true;
    });
    if (append && result.nextBefore && !additions.length) throw new Error('invalid_response');
    state.messages = [...previous, ...additions];
    state.nextBefore = result.nextBefore;
    renderStorage(object(response).storage);
    renderMessages();
    byId('last-updated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
  }
  async function selectConversation(item) {
    if (state.busy || !state.csrf || (state.selected && key(state.selected) === key(item))) return;
    state.busy = true;
    state.selected = item;
    state.messages = [];
    state.nextBefore = null;
    renderConversations();
    renderMessages();
    byId('message-empty').textContent = '正在读取消息…';
    notice('正在读取所选会话…');
    try {
      await readMessages();
      notice('记录已更新。此页面只查看记录，不会更改处理状态。');
    } catch (error) {
      showError(error);
      byId('message-empty').textContent = '读取失败，请刷新记录后重试。';
    } finally { state.busy = false; updateControls(); }
  }
  async function refresh() {
    if (state.busy || !state.ready) return;
    const selectedKey = state.selected ? key(state.selected) : null;
    state.busy = true;
    state.csrf = '';
    state.selected = null;
    state.conversations = [];
    state.nextConversations = null;
    state.messages = [];
    state.nextBefore = null;
    renderConversations();
    renderMessages();
    renderStorage(null);
    byId('conversation-empty').textContent = '正在读取会话…';
    byId('message-empty').textContent = '正在读取记录…';
    notice('正在读取本地消息记录…');
    try {
      const status = object(await bridge.apiGet('status'));
      if (typeof status.csrf_token !== 'string' || !status.csrf_token || status.csrf_token.length > 256) throw new Error('invalid_response');
      state.csrf = status.csrf_token;
      const response = await bridge.apiPost('history', {csrf_token: state.csrf, action: 'conversations', before: null, limit: CONVERSATION_PAGE_SIZE});
      const result = normalizeConversations(response);
      state.conversations = result.conversations;
      state.nextConversations = result.nextBefore;
      state.selected = state.conversations.find((item) => key(item) === selectedKey) ||
        state.conversations.find((item) => item.conversation_type === 'private') || state.conversations[0] || null;
      renderStorage(object(response).storage);
      renderConversations();
      renderMessages();
      if (state.selected) await readMessages();
      notice(state.selected ? '记录已更新。此页面只查看记录，不会更改处理状态。' : '暂无保留记录。收到并保存消息后，会话会出现在这里。');
      byId('last-updated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour12: false})}`;
    } catch (error) {
      showError(error);
      const multiRouteHistory = error?.message === 'multi_route_history_unavailable';
      byId('conversation-empty').textContent = multiRouteHistory ? '管理页暂不显示多路由记录' : '会话读取未完成';
      byId('message-empty').textContent = multiRouteHistory ? '请通过对应路由的 MCP 历史查询工具查看记录。' : '读取失败，请刷新记录后重试。';
    } finally { state.busy = false; updateControls(); }
  }
  async function loadConversations() {
    if (state.busy || !state.csrf || !state.nextConversations) return;
    state.busy = true;
    updateControls();
    try {
      const before = state.nextConversations;
      const response = await bridge.apiPost('history', {csrf_token: state.csrf, action: 'conversations', before, limit: CONVERSATION_PAGE_SIZE});
      const result = normalizeConversations(response);
      const seen = new Set(state.conversations.map(key));
      const additions = result.conversations.filter((item) => !seen.has(key(item)));
      if (result.nextBefore === before || (result.nextBefore && !additions.length)) throw new Error('invalid_response');
      state.conversations.push(...additions);
      state.nextConversations = result.nextBefore;
      renderConversations();
      renderStorage(object(response).storage);
      notice('更多会话已载入，当前消息记录保持不变。');
    } catch (error) { showError(error); }
    finally { state.busy = false; updateControls(); }
  }
  async function loadMore() {
    if (state.busy || !state.csrf || !state.selected || !state.nextBefore) return;
    state.busy = true;
    updateControls();
    try {
      await readMessages(true);
      notice('更早的记录已载入。');
    } catch (error) { showError(error); }
    finally { state.busy = false; updateControls(); }
  }
  function updateContext(context) {
    if (typeof context?.isDark === 'boolean') document.documentElement.dataset.theme = context.isDark ? 'dark' : 'light';
  }
  byId('refresh').addEventListener('click', () => void refresh());
  byId('load-more').addEventListener('click', () => void loadMore());
  byId('load-conversations').addEventListener('click', () => void loadConversations());
  window.addEventListener('beforeunload', () => { if (typeof unsubscribeContext === 'function') unsubscribeContext(); });
  async function initialize() {
    if (!bridge || typeof bridge.ready !== 'function' || typeof bridge.apiGet !== 'function' || typeof bridge.apiPost !== 'function') {
      notice('请从 AstrBot 插件详情打开“消息记录”。此页面需要 AstrBot v4.28.2 的插件页面接口。', 'error');
      byId('conversation-empty').textContent = '尚未连接 AstrBot';
      return;
    }
    try {
      updateContext(await bridge.ready());
      if (typeof bridge.onContext === 'function') unsubscribeContext = bridge.onContext(updateContext);
      state.ready = true;
      await refresh();
    } catch {
      notice('未能连接 AstrBot 页面接口。请重新打开插件详情。', 'error');
      updateControls();
    }
  }
  let initialization;
  return {initialize: () => initialization ||= initialize()};

}
