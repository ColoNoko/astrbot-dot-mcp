import {createHistoryPage} from './history.js';

// AstrBot v4.28.2 opens the first native page. Keep both views in that document.
const tabs = ['connection', 'history'];
let historyPage;
function activate(name, focus = false) {
  if (!tabs.includes(name)) return;
  for (const item of tabs) {
    const active = item === name;
    const tab = document.getElementById(`${item}-tab`);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    document.getElementById(`${item}-panel`).hidden = !active;
    if (active && focus) tab.focus();
  }
  if (name === 'history') {
    historyPage ||= createHistoryPage(document.getElementById('history-panel'), window.AstrBotPluginPage);
    void historyPage.initialize();
  }
}
for (const [index, name] of tabs.entries()) {
  const tab = document.getElementById(`${name}-tab`);
  tab.addEventListener('click', () => activate(name));
  tab.addEventListener('keydown', (event) => {
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 :
      event.key === 'ArrowRight' ? (index + 1) % tabs.length :
        event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : null;
    if (next === null) return;
    event.preventDefault();
    activate(tabs[next], true);
  });
}
