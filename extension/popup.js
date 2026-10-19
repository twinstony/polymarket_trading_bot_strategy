// popup.js — 配置 FastAPI bridge 地址，存 chrome.storage.sync
const DEFAULT_URL = 'http://127.0.0.1:8787';
const urlInput = document.getElementById('url');
const saveBtn = document.getElementById('save');
const testBtn = document.getElementById('test');
const statusEl = document.getElementById('status');

async function load() {
  try {
    const data = await chrome.storage.sync.get('bridgeBaseUrl');
    urlInput.value = data.bridgeBaseUrl || DEFAULT_URL;
  } catch (e) {
    urlInput.value = DEFAULT_URL;
  }
}

async function save() {
  const v = (urlInput.value || '').trim();
  if (!v) {
    setStatus('err', '请输入地址');
    return;
  }
  try {
    await chrome.storage.sync.set({ bridgeBaseUrl: v });
    setStatus('ok', '已保存: ' + v);
  } catch (e) {
    setStatus('err', '保存失败: ' + e.message);
  }
}

async function test() {
  const v = (urlInput.value || '').trim();
  if (!v) {
    setStatus('err', '请输入地址');
    return;
  }
  setStatus('pending', '测试中...');
  try {
    const t0 = performance.now();
    const resp = await fetch(v + '/api/health', { cache: 'no-store' });
    const t1 = performance.now();
    if (!resp.ok) {
      setStatus('err', 'HTTP ' + resp.status);
      return;
    }
    const data = await resp.json();
    const latency = Math.round(t1 - t0);
    const count = data.strategies_count != null ? data.strategies_count : '?';
    const running = data.bot_running ? '运行中' : '已停止';
    setStatus('ok', '在线 · ' + latency + 'ms · ' + count + ' 策略 · bot ' + running);
  } catch (e) {
    setStatus('err', '离线 · ' + (e.message || e));
  }
}

function setStatus(kind, msg) {
  statusEl.className = 'status ' + kind;
  statusEl.textContent = msg;
}

saveBtn.onclick = save;
testBtn.onclick = test;
urlInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') save();
});

load();
