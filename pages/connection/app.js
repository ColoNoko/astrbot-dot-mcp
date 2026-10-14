// AstrBot v4.28.2 injects this bridge into pages/<name>/index.html.
// apiGet/apiPost already unwrap the official status/data compatibility envelope.
const bridge = window.AstrBotPluginPage;
const byId = (id) => document.getElementById(id);
const fields = ['platform-id', 'self-id', 'listen-host', 'listen-port', 'callback-hosts', 'tunnel-id', 'tunnel-enabled', 'tunnel-key', 'proxy-enabled', 'proxy-url', 'proxy-username', 'proxy-password', 'clear-proxy-auth'];
const errors = {
  invalid_settings: '设置未通过校验。请检查平台、QQ、端口、域名与密钥格式。',
  save_failed: '保存失败。输入已保留，请检查服务器状态后刷新。',
  revision_conflict: '配置已被其他页面更新。请重新载入最新配置后再编辑。',
  forbidden: '请求授权已失效。请刷新状态，或重新打开 AstrBot 插件页面。',
  runtime_failed: '服务未能启动。请检查已保存的设置、运行程序及 AstrBot 日志。',
  invalid_endpoint: '本地 MCP 地址无效。请检查监听地址与端口。',
  invalid_mcp_token: '本地 MCP 鉴权初始化失败。请检查服务器配置权限后重新应用。',
  invalid_proxy_settings: '代理设置无效。请使用带端口的 HTTP 代理地址，并检查代理认证信息；服务不会自动绕过代理直连。',
  proxy_auth_reentry_required: '更改代理地址后，请重新填写已保存的认证信息，或勾选清除代理认证。',
  invalid_key_file: '运行密钥文件无效。请检查服务器上的密钥文件权限。',
  unsupported_platform: '当前系统或处理器不支持此 Tunnel 程序，请检查部署平台。',
  binary_missing: '未找到已打包的 Tunnel 程序。请检查插件安装是否完整。',
  binary_untrusted: 'Tunnel 程序未通过可信性检查，请重新安装完整插件包。',
  binary_platform_mismatch: 'Tunnel 程序与当前系统不匹配，请检查部署平台。',
  binary_checksum_mismatch: 'Tunnel 程序校验失败，请重新安装完整插件包。',
  private_directory_required: '运行目录权限不安全，请检查服务器上的私有目录权限。',
  already_running: 'Tunnel 已有运行进程，请先停止再应用。',
  runtime_in_use: 'Tunnel 运行目录正被占用，请检查是否有重复实例。',
  start_failed: 'Tunnel 启动失败，请检查已保存的设置与服务器日志。',
  startup_failed: '本地服务启动失败，请检查绑定、密钥与端口。',
  startup_timeout: 'Tunnel 启动超时，请检查运行程序与服务器日志。',
  process_exited: 'Tunnel 进程已退出，请检查配置和日志后重新应用。',
  health_unavailable: '无法完成 Tunnel 本地健康检查，请刷新状态或检查日志。',
  not_ready: 'Tunnel 尚未在本地就绪，请稍后刷新状态。',
  stop_failed: '服务未能完全停止。请检查服务器进程，确认停止后再应用。',
  tunnel_stop_incomplete: 'Tunnel 尚未完全停止。请确认服务器上的旧进程已退出。',
  cleanup_failed: '运行文件清理失败，请检查服务器目录权限。',
  start_cancelled: 'Tunnel 启动已取消，请确认配置后重新应用。',
};
const tunnelStates = {
  disabled: '未启用', stopped: '已停止', starting: '正在启动', running: '进程运行中',
  ready: '本地就绪', healthy: '本地就绪', missing: '缺少运行程序',
  binary_missing: '缺少运行程序', unavailable: '不可用', error: '启动异常',
  failed: '启动异常', stopping: '正在停止', not_configured: '待配置', waiting: '等待启动',
};
const state = {snapshot: null, formRevision: null, busy: false, stale: true, conflict: false, dirty: false};
let unsubscribeContext;

function safeText(value, limit = 128) {
  return typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, '').slice(0, limit) : '';
}
function validPlatformId(value) {
  return typeof value === 'string' && [...value].length >= 1 && [...value].length <= 128 &&
    !/[\p{C}\p{Z}\s]/u.test(value) && /^[A-Za-z0-9_.:\-\u0080-\u{10ffff}]+$/u.test(value);
}
function validProxyUrl(value) {
  if (typeof value !== 'string' || value.length > 1024 || /[\s\p{C}]/u.test(value)) return false;
  // Check the explicit port before URL normalizes a default :80 away.
  const match = /^http:\/\/(\[[0-9a-fA-F:.]+\]|[a-zA-Z0-9.-]+):([0-9]{1,5})\/?$/.exec(value);
  if (!match || Number(match[2]) < 1 || Number(match[2]) > 65535) return false;
  if (!match[1].startsWith('[') && !match[1].split('.').every((part) => /^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/.test(part))) return false;
  try {
    const parsed = new URL(value);
    const host = parsed.hostname;
    if (host === '[::]' || /^\[ff[0-9a-f]{2}:/i.test(host)) return false;
    if (/^[0-9.]+$/.test(host) && (host === '0.0.0.0' || (Number(host.split('.')[0]) >= 224 && Number(host.split('.')[0]) <= 239))) return false;
    return parsed.protocol === 'http:' && !!parsed.hostname && !parsed.username && !parsed.password &&
      !parsed.search && !parsed.hash && parsed.pathname === '/';
  } catch { return false; }
}
function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}
function normalizeStatus(raw) {
  const value = object(raw);
  const settings = object(value.settings);
  const secrets = object(value.secrets);
  const runtime = object(value.runtime);
  const tunnel = object(runtime.tunnel);
  if (typeof value.csrf_token !== 'string' || !value.csrf_token || value.csrf_token.length > 256 ||
      !['string', 'number'].includes(typeof value.revision) ||
      (typeof value.revision === 'number' && !Number.isSafeInteger(value.revision)) ||
      String(value.revision).length > 256 || typeof settings.enabled !== 'boolean') {
    throw new Error('invalid_response');
  }
  if (settings.platform_id !== '' && !validPlatformId(settings.platform_id)) throw new Error('invalid_response');
  if (typeof settings.proxy_enabled !== 'boolean' || typeof settings.proxy_url !== 'string' ||
      (settings.proxy_url && !validProxyUrl(settings.proxy_url))) throw new Error('invalid_response');
  const platforms = Array.isArray(value.platforms) ? value.platforms.slice(0, 100)
    .filter((item) => item && item.adapter === 'aiocqhttp' && validPlatformId(item.id))
    .map((item) => ({id: item.id, adapter: 'aiocqhttp'})).filter((item) => item.id) : [];
  return {
    csrf_token: value.csrf_token, revision: value.revision,
    settings: {
      enabled: settings.enabled, platform_id: settings.platform_id, adapter: safeText(settings.adapter, 32),
      self_id: safeText(settings.self_id, 20), listen_host: safeText(settings.listen_host, 32) || '127.0.0.1',
      listen_port: Number.isInteger(settings.listen_port) ? settings.listen_port : 8769,
      callback_hosts: Array.isArray(settings.callback_hosts) ? settings.callback_hosts.slice(0, 8).map((host) => safeText(host, 253)) : [],
      tunnel_enabled: settings.tunnel_enabled === true, tunnel_id: safeText(settings.tunnel_id, 256),
      proxy_enabled: settings.proxy_enabled, proxy_url: settings.proxy_url,
    },
    secrets: {mcp_token_set: secrets.mcp_token_set === true, tunnel_key_set: secrets.tunnel_key_set === true,
      proxy_username_set: secrets.proxy_username_set === true, proxy_password_set: secrets.proxy_password_set === true},
    platforms,
    runtime: {mcp_ready: runtime.mcp_ready === true, error_code: Object.hasOwn(errors, runtime.error_code) ? runtime.error_code : '', tunnel: {
      state: safeText(tunnel.state, 40), process_running: tunnel.process_running === true,
      healthy: tunnel.healthy === true, ready: tunnel.ready === true, binary_available: tunnel.binary_available === true,
      error_code: Object.hasOwn(errors, tunnel.last_error_code) ? tunnel.last_error_code :
        Object.hasOwn(errors, tunnel.error_code) ? tunnel.error_code : '',
    }},
    version: safeText(object(value.versions).plugin, 24),
  };
}
function notice(text, type = 'info') {
  const element = byId('notice');
  element.textContent = text;
  element.className = `notice ${type}`;
}
function currentSettings() {
  return {
    platform_id: byId('platform-id').value, adapter: byId('platform-id').value ? 'aiocqhttp' : (state.snapshot?.settings.adapter || ''), self_id: byId('self-id').value.trim(),
    listen_host: byId('listen-host').value, listen_port: Number(byId('listen-port').value),
    callback_hosts: byId('callback-hosts').value.split(/\r?\n/).map((host) => host.trim()).filter(Boolean),
    tunnel_enabled: byId('tunnel-enabled').checked, tunnel_id: byId('tunnel-id').value.trim(),
    proxy_enabled: byId('proxy-enabled').checked, proxy_url: byId('proxy-url').value.trim(),
  };
}
function savedSettings() {
  if (!state.snapshot) return null;
  const {enabled, ...settings} = state.snapshot.settings;
  return settings;
}
function recomputeDirty() {
  state.dirty = !!state.snapshot && (JSON.stringify(currentSettings()) !== JSON.stringify(savedSettings()) ||
    byId('tunnel-key').value !== '' || byId('proxy-username').value !== '' ||
    byId('proxy-password').value !== '' || byId('clear-proxy-auth').checked);
  byId('host-warning').hidden = byId('listen-host').value === '127.0.0.1';
  updateControls();
}
function updateControls() {
  const unavailable = state.busy || state.stale || !state.snapshot;
  byId('settings-fields').disabled = unavailable;
  byId('refresh').disabled = state.busy || !bridge;
  byId('save').disabled = unavailable || state.conflict || !state.dirty;
  byId('discard').disabled = state.busy || (!state.dirty && !state.conflict);
  byId('reload').disabled = state.busy;
  byId('apply').disabled = unavailable || state.dirty || state.conflict;
  byId('stop').disabled = unavailable;
  byId('proxy-username').disabled = unavailable || byId('clear-proxy-auth').checked;
  byId('proxy-password').disabled = unavailable || byId('clear-proxy-auth').checked;
  byId('proxy-auth-warning').hidden = !state.snapshot ||
    byId('proxy-url').value.trim() === state.snapshot.settings.proxy_url ||
    !(state.snapshot.secrets.proxy_username_set || state.snapshot.secrets.proxy_password_set);
  byId('conflict').hidden = !state.conflict;
  byId('save-state').textContent = !state.snapshot ? '读取配置后即可编辑' : state.busy ? '正在处理，请稍候…' :
    state.conflict ? '配置有更新，请重新载入' : state.dirty ? '有未保存的修改' : '设置已保存';
  document.body.setAttribute('aria-busy', String(state.busy));
}
function option(value, label, disabled = false) {
  const element = document.createElement('option');
  element.value = value;
  element.textContent = label;
  element.disabled = disabled;
  return element;
}
function populateForm(snapshot, clearSecrets = true) {
  const settings = snapshot.settings;
  const select = byId('platform-id');
  select.replaceChildren(option('', snapshot.platforms.length ? '请选择平台实例' : '未发现 aiocqhttp 实例'));
  const ids = new Set();
  for (const item of snapshot.platforms) {
    if (!ids.has(item.id)) { select.append(option(item.id, `${item.id} · aiocqhttp`)); ids.add(item.id); }
  }
  if (settings.platform_id && !ids.has(settings.platform_id)) {
    select.append(option(settings.platform_id, `${settings.platform_id} · 当前未发现`, true));
  }
  select.value = settings.platform_id;
  byId('self-id').value = settings.self_id;
  const host = byId('listen-host');
  host.replaceChildren(option('127.0.0.1', '127.0.0.1 · 仅本机'));
  if (settings.listen_host !== '127.0.0.1') host.append(option(settings.listen_host, `${settings.listen_host} · 旧配置`, true));
  host.value = settings.listen_host;
  byId('listen-port').value = String(settings.listen_port);
  byId('callback-hosts').value = settings.callback_hosts.join('\n');
  byId('tunnel-enabled').checked = settings.tunnel_enabled;
  byId('tunnel-id').value = settings.tunnel_id;
  byId('proxy-enabled').checked = settings.proxy_enabled;
  byId('proxy-url').value = settings.proxy_url;
  if (clearSecrets) {
    byId('tunnel-key').value = '';
    byId('proxy-username').value = '';
    byId('proxy-password').value = '';
    byId('clear-proxy-auth').checked = false;
  }
  state.formRevision = snapshot.revision;
  state.conflict = false;
  recomputeDirty();
}
function renderRuntime(snapshot) {
  const {settings, runtime, secrets} = snapshot;
  const selected = snapshot.platforms.some((item) => item.id === settings.platform_id);
  const plugin = byId('plugin-status');
  plugin.textContent = settings.enabled ? '常规配置：已开启' : '常规配置：已关闭';
  plugin.className = 'badge neutral';
  byId('platform-status').textContent = selected ? '实例已识别' : settings.platform_id ? '实例未发现' : '尚未绑定';
  byId('platform-detail').textContent = settings.platform_id || '仅支持 aiocqhttp';
  byId('mcp-status').textContent = runtime.mcp_ready ? '本地已启动' : '已停止';
  byId('mcp-detail').textContent = `${settings.listen_host}:${settings.listen_port}/mcp`;
  const tunnel = runtime.tunnel;
  byId('tunnel-status').textContent = tunnel.ready ? '本地就绪' :
    Object.hasOwn(tunnelStates, tunnel.state) ? tunnelStates[tunnel.state] :
      tunnel.process_running ? '进程运行中' : settings.tunnel_enabled ? '未运行' : '未启用';
  byId('tunnel-detail').textContent = tunnel.error_code ? errors[tunnel.error_code] : '进程状态不代表外网连通';
  byId('binary-status').textContent = tunnel.binary_available ? '已检测到' : '未检测到';
  byId('tunnel-health').textContent = tunnel.healthy ? '本地检查通过' : '尚未就绪';
  const issue = byId('runtime-issue');
  issue.textContent = [runtime.error_code, tunnel.error_code].filter((code, index, codes) => code && codes.indexOf(code) === index).map((code) => errors[code]).join(' ');
  issue.hidden = !issue.textContent;
  byId('mcp-auth-state').textContent = secrets.mcp_token_set ? '自动管理 · 已配置' : '自动管理 · 启动时配置';
  for (const [id, present] of [['tunnel-secret-state', secrets.tunnel_key_set],
    ['proxy-username-state', secrets.proxy_username_set], ['proxy-password-state', secrets.proxy_password_set]]) {
    byId(id).textContent = present ? '已设置 · 不回显' : '未设置';
    byId(id).className = `secret-state${present ? ' set' : ''}`;
  }
  byId('version').textContent = snapshot.version ? ` · v${snapshot.version}` : '';
  byId('last-updated').textContent = `更新于 ${new Date().toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false})}`;
}
function acceptStatus(raw, replaceForm = false, preserveSecrets = false) {
  const snapshot = normalizeStatus(raw);
  const hadEdits = state.dirty;
  state.snapshot = snapshot;
  state.stale = false;
  renderRuntime(snapshot);
  if (replaceForm || !hadEdits) populateForm(snapshot, !preserveSecrets);
  else state.conflict = state.formRevision !== snapshot.revision;
  updateControls();
}
async function refresh({replaceForm = false, silent = false} = {}) {
  if (state.busy) return;
  state.busy = true;
  updateControls();
  try {
    acceptStatus(await bridge.apiGet('status'), replaceForm);
    if (!silent) notice(state.dirty ? '状态已刷新，未保存的输入已保留。' : '状态已刷新。保存不会自动开启插件。');
  } catch {
    state.stale = true;
    notice('无法读取连接状态。请刷新重试；若持续失败，请从 AstrBot 插件详情重新打开此页。', 'error');
  } finally { state.busy = false; updateControls(); }
}
function validationError() {
  const settings = currentSettings();
  if (settings.listen_host !== '127.0.0.1') return ['listen-host', '高级页仅允许本机监听。请选择 127.0.0.1 后再保存。'];
  if (!Number.isInteger(settings.listen_port) || settings.listen_port < 1024 || settings.listen_port > 65535) return ['listen-port', '端口需要是 1024 到 65535 之间的整数。'];
  if (settings.platform_id && !state.snapshot.platforms.some((item) => item.id === settings.platform_id)) return ['platform-id', '请选择列表中的 aiocqhttp 平台实例。'];
  if (settings.self_id && !/^[1-9][0-9]{0,19}$/.test(settings.self_id)) return ['self-id', '请填写有效的机器人 QQ 号。'];
  const hosts = settings.callback_hosts;
  const hostname = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
  if (hosts.length > 8 || new Set(hosts).size !== hosts.length || hosts.some((host) => !hostname.test(host))) return ['callback-hosts', '回调域名最多 8 个，每行一个小写域名；请移除重复项、协议、端口、路径和通配符。'];
  if (settings.tunnel_id && !/^tunnel_(?:[a-z0-9]{4}_)?[a-z0-9]{32}$/.test(settings.tunnel_id)) return ['tunnel-id', '请填写完整的 Tunnel ID：以 tunnel_ 开头，保留原始格式。'];
  if (settings.tunnel_enabled && !settings.tunnel_id) return ['tunnel-id', '启用 Tunnel 前，请填写已有的 Tunnel ID。'];
  if (settings.tunnel_enabled && !state.snapshot.secrets.tunnel_key_set && !byId('tunnel-key').value) return ['tunnel-key', '启用 Tunnel 前，请填写专用运行密钥。'];
  if (byId('tunnel-key').value && !/^[\x21-\x7e]{32,4096}$/.test(byId('tunnel-key').value)) return ['tunnel-key', '运行密钥需为 32 到 4096 个可见 ASCII 字符，不能包含空格。'];
  if ((settings.proxy_enabled && !settings.proxy_url) || (settings.proxy_url && !validProxyUrl(settings.proxy_url))) {
    return ['proxy-url', '请填写带端口的 HTTP 代理地址，例如 http://192.0.2.10:7890；不支持 HTTPS 或 SOCKS 代理，也不能包含账号、密码、路径或参数。'];
  }
  const username = byId('proxy-username').value;
  const password = byId('proxy-password').value;
  if (!byId('clear-proxy-auth').checked) {
    if (username && (username.length > 256 || !/^[\x20-\x7e]*$/.test(username) || username.includes(':'))) return ['proxy-username', '代理用户名最多 256 个 ASCII 字符，不能包含冒号或控制字符。'];
    if (password && (password.length > 4096 || !/^[\x20-\x7e]*$/.test(password))) return ['proxy-password', '代理密码最多 4096 个 ASCII 字符，不能包含控制字符。'];
    if (password && !username && !state.snapshot.secrets.proxy_username_set) return ['proxy-username', '填写代理密码时，也需要填写代理用户名。'];
    if (settings.proxy_url !== state.snapshot.settings.proxy_url) {
      if (state.snapshot.secrets.proxy_username_set && !username) return ['proxy-username', errors.proxy_auth_reentry_required];
      if (state.snapshot.secrets.proxy_password_set && !password) return ['proxy-password', errors.proxy_auth_reentry_required];
    }
  }
  return null;
}
async function mutate(endpoint, payload, successMessage) {
  if (state.busy || state.stale || !state.snapshot) return;
  state.busy = true;
  updateControls();
  try {
    const result = await bridge.apiPost(endpoint, {
      csrf_token: state.snapshot.csrf_token,
      revision: endpoint === 'settings' ? state.formRevision : state.snapshot.revision,
      ...payload,
    });
    // Stop is allowed while editing and must not erase unsaved inputs.
    const keepEdits = endpoint === 'action' && payload.action === 'stop' && state.dirty;
    acceptStatus(result, !keepEdits);
    if (payload.action === 'apply' && !state.snapshot.settings.enabled) {
      notice('常规配置中的插件开关仍关闭，本次未启动服务。请先在常规配置中开启插件。');
    } else notice(successMessage, 'success');
  } catch (error) {
    const code = Object.hasOwn(errors, error?.message) ? error.message : '';
    notice(code ? errors[code] : '操作结果未确认。请先刷新状态检查结果；未保存的输入已保留。', 'error');
    state.stale = true;
    // Read-only reconciliation, never automatically retry a mutation.
    try { acceptStatus(await bridge.apiGet('status'), false, true); } catch { /* Keep actions unavailable until a verified refresh. */ }
    if (code === 'revision_conflict') state.conflict = true;
  } finally { state.busy = false; updateControls(); }
}
byId('settings-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (state.busy || state.stale || state.conflict || !state.dirty) return;
  const error = validationError();
  if (error) { notice(error[1], 'error'); byId(error[0]).focus(); return; }
  const payload = {settings: currentSettings()};
  if (byId('tunnel-key').value) payload.tunnel_key = byId('tunnel-key').value;
  payload.clear_proxy_auth = byId('clear-proxy-auth').checked;
  if (!payload.clear_proxy_auth) {
    if (byId('proxy-username').value) payload.proxy_username = byId('proxy-username').value;
    if (byId('proxy-password').value) payload.proxy_password = byId('proxy-password').value;
  }
  void mutate('settings', payload, '设置已保存，服务已停止。点击“应用并重启”使新设置生效。');
});
for (const id of fields) byId(id).addEventListener('input', () => {
  recomputeDirty();
});
byId('refresh').addEventListener('click', () => void refresh());
byId('discard').addEventListener('click', () => {
  if (state.busy || !state.snapshot) return;
  populateForm(state.snapshot);
  notice('未保存的修改已撤销。');
});
byId('reload').addEventListener('click', () => void refresh({replaceForm: true}));
byId('apply').addEventListener('click', () => {
  if (state.dirty || state.conflict) return;
  void mutate('action', {action: 'apply'}, '已执行应用。请根据上方本地运行状态检查结果；QQ 往返仍需实际验证。');
});
byId('stop').addEventListener('click', () => {
  void mutate('action', {action: 'stop'}, '服务已停止，常规配置中的插件开关保持原值。');
});
function updateContext(context) {
  if (typeof context?.isDark === 'boolean') document.documentElement.dataset.theme = context.isDark ? 'dark' : 'light';
}
window.addEventListener('beforeunload', () => { if (typeof unsubscribeContext === 'function') unsubscribeContext(); });
async function initialize() {
  if (!bridge || typeof bridge.ready !== 'function' || typeof bridge.apiGet !== 'function' || typeof bridge.apiPost !== 'function') {
    notice('请从 AstrBot 的插件详情打开“连接设置”。此页面需要 AstrBot v4.28.2 的插件页面接口。', 'error');
    return;
  }
  try {
    updateContext(await bridge.ready());
    if (typeof bridge.onContext === 'function') unsubscribeContext = bridge.onContext(updateContext);
    await refresh({replaceForm: true});
  } catch {
    notice('未能连接 AstrBot 页面接口。请重新打开插件详情。', 'error');
    updateControls();
  }
}
void initialize();
