// api.js — FastAPI bridge HTTP 客户端 + 轮询调度
// 硬约束：REST + 全量轮询，无 WebSocket/SSE；指数退避重试；只读降级 + 10s 自动恢复
window.Api = (function () {
  'use strict';

  // ===== 内存缓存 =====
  let bridgeBaseUrl = 'http://127.0.0.1:8787';

  // ===== 轮询状态 =====
  let pollTimer = null;
  let reconnectTimer = null;
  let disconnected = false;
  let inFlight = false;
  let currentCallbacks = null;

  // ===== 常量 =====
  const RETRY_DELAYS = [200, 600, 1800]; // 指数退避 3 次
  const POLL_INTERVAL = 2500;             // 2.5s
  const RECONNECT_INTERVAL = 10000;       // 10s 自动恢复

  // ===== bridgeBaseUrl 管理 =====
  async function loadBaseUrl() {
    try {
      const data = await chrome.storage.sync.get('bridgeBaseUrl');
      if (data && data.bridgeBaseUrl) bridgeBaseUrl = data.bridgeBaseUrl;
    } catch (e) {
      // chrome.storage 不可用时使用默认值
    }
    return bridgeBaseUrl;
  }

  function getBaseUrl() {
    return bridgeBaseUrl;
  }

  // 侧边栏内嵌设置面板调用：保存新地址并立即生效
  async function setBaseUrl(url) {
    const v = (url || '').trim().replace(/\/+$/, '');
    if (!v) return false;
    bridgeBaseUrl = v;
    try {
      await chrome.storage.sync.set({ bridgeBaseUrl: v });
    } catch (e) {
      // storage 不可用时仅更新内存
    }
    return true;
  }

  // 监听 popup 保存的地址变化
  try {
    chrome.storage.onChanged.addListener((changes, area) => {
      if (area === 'sync' && changes.bridgeBaseUrl && changes.bridgeBaseUrl.newValue) {
        bridgeBaseUrl = changes.bridgeBaseUrl.newValue;
      }
    });
  } catch (e) {
    // 忽略
  }

  // ===== 带重试的 fetch（GET 退避 3 次，POST 不重试） =====
  function delay(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  async function fetchWithRetry(path, options, maxRetries) {
    let lastErr;
    for (let i = 0; i <= maxRetries; i++) {
      try {
        const url = bridgeBaseUrl + path;
        const resp = await fetch(url, options);
        // 5xx 视为可重试错误
        if (resp.status >= 500) {
          throw new Error('HTTP ' + resp.status);
        }
        return resp;
      } catch (e) {
        lastErr = e;
        if (i < maxRetries) {
          await delay(RETRY_DELAYS[i] || RETRY_DELAYS[RETRY_DELAYS.length - 1]);
        }
      }
    }
    throw lastErr || new Error('fetch failed');
  }

  async function getJson(path, maxRetries) {
    const resp = await fetchWithRetry(path, { cache: 'no-store' }, maxRetries == null ? 3 : maxRetries);
    if (!resp.ok) {
      const err = new Error('HTTP ' + resp.status);
      err.status = resp.status;
      try { err.body = await resp.json(); } catch (_) { err.body = null; }
      throw err;
    }
    return resp.json();
  }

  async function postJson(path, body) {
    // POST 不自动重试，由用户决定
    const resp = await fetchWithRetry(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      cache: 'no-store',
    }, 0);
    if (!resp.ok) {
      const err = new Error('HTTP ' + resp.status);
      err.status = resp.status;
      try { err.body = await resp.json(); } catch (_) { err.body = null; }
      throw err;
    }
    return resp.json();
  }

  // ===== 读接口 =====
  function getHealth() { return getJson('/api/health', 3); }
  function getConfig() { return getJson('/api/config', 3); }
  function getMarkets(slug) { return getJson('/api/markets/' + encodeURIComponent(slug), 3); }
  function getPositions() { return getJson('/api/positions', 3); }
  function getOrders() { return getJson('/api/orders', 3); }
  function getStatus() { return getJson('/api/status', 3); }

  function parseUrl(urlOrSlug) {
    const s = (urlOrSlug || '').trim();
    if (!s) return Promise.reject(new Error('empty url/slug'));
    const isUrl = s.indexOf('://') !== -1 || s.indexOf('http') === 0;
    const q = isUrl ? ('url=' + encodeURIComponent(s)) : ('slug=' + encodeURIComponent(s));
    return getJson('/api/url/parse?' + q, 3);
  }

  // ===== 写接口 =====
  function placeOrder(req) { return postJson('/api/order', req); }
  function cancelOrder(req) { return postJson('/api/order/cancel', req); }
  function closePosition(req) { return postJson('/api/position/close', req); }
  function stopPosition(tokenId) { return postJson('/api/position/stop', { token_id: tokenId }); }
  function deletePosition(tokenId) { return postJson('/api/position/delete', { token_id: tokenId }); }

  // ===== 全局指令 =====
  function globalPreview(action) { return postJson('/api/global/preview', { action: action }); }
  function globalStopAll() { return postJson('/api/global/stop_all', {}); }
  function globalCloseAll() { return postJson('/api/global/close_all', {}); }
  function globalDeleteAll() { return postJson('/api/global/delete_all', {}); }

  // ===== 轮询调度 =====
  function startPolling(callbacks) {
    stopPolling();
    currentCallbacks = callbacks || {};
    // 首次立即拉取一次
    pollOnce();
    pollTimer = setInterval(pollOnce, POLL_INTERVAL);
  }

  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    stopReconnectTimer();
    disconnected = false;
    inFlight = false;
    currentCallbacks = null;
  }

  async function pollOnce() {
    if (inFlight) return;
    if (!currentCallbacks) return;
    inFlight = true;
    try {
      // 并发拉取 positions + status
      const [positionsResp, statusResp] = await Promise.all([
        fetchWithRetry('/api/positions', { cache: 'no-store' }, 3),
        fetchWithRetry('/api/status', { cache: 'no-store' }, 3),
      ]);
      if (!positionsResp.ok || !statusResp.ok) {
        throw new Error('HTTP ' + (positionsResp.status || statusResp.status));
      }
      const positionsData = await positionsResp.json();
      const statusData = await statusResp.json();

      // 恢复连接
      if (disconnected) {
        disconnected = false;
        stopReconnectTimer();
        if (currentCallbacks.onReconnect) currentCallbacks.onReconnect();
      }
      if (currentCallbacks.onPositions) {
        currentCallbacks.onPositions(positionsData.positions || []);
      }
      if (currentCallbacks.onStatus) {
        currentCallbacks.onStatus(statusData);
      }
    } catch (e) {
      if (!disconnected) {
        disconnected = true;
        if (currentCallbacks.onDisconnect) currentCallbacks.onDisconnect(e);
      }
      startReconnectTimer();
    } finally {
      inFlight = false;
    }
  }

  function startReconnectTimer() {
    if (reconnectTimer) return;
    reconnectTimer = setInterval(async () => {
      try {
        await getJson('/api/health', 0);
        // 恢复：停止定时器，立即全量同步
        stopReconnectTimer();
        disconnected = false;
        if (currentCallbacks && currentCallbacks.onReconnect) {
          currentCallbacks.onReconnect();
        }
        pollOnce();
      } catch (e) {
        // 继续等待
      }
    }, RECONNECT_INTERVAL);
  }

  function stopReconnectTimer() {
    if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
  }

  // 手动触发一次全量同步（写操作后调用，加快 UI 反馈）
  function pollNow() {
    if (!currentCallbacks) return;
    pollOnce();
  }

  function isDisconnected() {
    return disconnected;
  }

  return {
    loadBaseUrl,
    getBaseUrl,
    setBaseUrl,
    getHealth,
    getConfig,
    parseUrl,
    getMarkets,
    getPositions,
    getOrders,
    getStatus,
    placeOrder,
    cancelOrder,
    closePosition,
    stopPosition,
    deletePosition,
    globalPreview,
    globalStopAll,
    globalCloseAll,
    globalDeleteAll,
    startPolling,
    stopPolling,
    pollNow,
    isDisconnected,
  };
})();
