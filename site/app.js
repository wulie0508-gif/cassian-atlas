const menuButton = document.querySelector('#menu-toggle');
const navigation = document.querySelector('#primary-navigation');
const tabs = [...document.querySelectorAll('[data-demo-tab]')];
const panels = [...document.querySelectorAll('[data-demo-panel]')];
const copyButton = document.querySelector('#copy-command');
const statusRegion = document.querySelector('#site-status');

function announce(message) {
  statusRegion.textContent = '';
  window.requestAnimationFrame(() => { statusRegion.textContent = message; });
}

function closeMenu({restoreFocus = false} = {}) {
  const wasOpen = navigation.classList.contains('is-open');
  navigation.classList.remove('is-open');
  menuButton.setAttribute('aria-expanded', 'false');
  if (restoreFocus && wasOpen) menuButton.focus();
}

menuButton.addEventListener('click', () => {
  const open = !navigation.classList.contains('is-open');
  navigation.classList.toggle('is-open', open);
  menuButton.setAttribute('aria-expanded', String(open));
  if (open) navigation.querySelector('a')?.focus();
});

navigation.addEventListener('click', event => {
  if (event.target.closest('a')) closeMenu();
});

document.addEventListener('click', event => {
  if (!event.target.closest('.nav-shell')) closeMenu();
});

document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && navigation.classList.contains('is-open')) {
    event.preventDefault();
    closeMenu({restoreFocus: true});
  }
});

function selectDemoTab(nextTab, {moveFocus = false} = {}) {
  const key = nextTab.dataset.demoTab;
  tabs.forEach(tab => {
    const active = tab === nextTab;
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  panels.forEach(panel => { panel.hidden = panel.dataset.demoPanel !== key; });
  if (moveFocus) nextTab.focus();
  announce(`${nextTab.textContent.trim()}演示已显示`);
}

tabs.forEach((tab, index) => {
  tab.addEventListener('click', () => selectDemoTab(tab));
  tab.addEventListener('keydown', event => {
    let nextIndex = null;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % tabs.length;
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + tabs.length) % tabs.length;
    if (event.key === 'Home') nextIndex = 0;
    if (event.key === 'End') nextIndex = tabs.length - 1;
    if (nextIndex == null) return;
    event.preventDefault();
    selectDemoTab(tabs[nextIndex], {moveFocus: true});
  });
});

async function copyInstallCommand() {
  const command = copyButton.dataset.copy;
  try {
    if (!navigator.clipboard) throw new Error('Clipboard API unavailable');
    await navigator.clipboard.writeText(command);
    copyButton.textContent = '已复制';
    announce('安装命令已复制');
  } catch {
    const textarea = document.createElement('textarea');
    textarea.value = command;
    textarea.setAttribute('readonly', '');
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.append(textarea);
    textarea.select();
    const copied = document.execCommand('copy');
    textarea.remove();
    copyButton.textContent = copied ? '已复制' : '请手动复制';
    announce(copied ? '安装命令已复制' : '复制失败，请手动选择安装命令');
  }
  window.setTimeout(() => { copyButton.textContent = '复制'; }, 1800);
}

copyButton.addEventListener('click', copyInstallCommand);
document.querySelector('#current-year').textContent = String(new Date().getFullYear());
