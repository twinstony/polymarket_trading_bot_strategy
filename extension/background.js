// Polymarket Trading Desk - Service Worker (Manifest V3)
// 不持有任何状态，只做事件路由

const POLYMARKET_HOST = 'polymarket.com';

// OKX 钱包风格：点扩展图标直接开/关侧边栏（无需 popup 中转）
// setPanelBehavior 让 action 点击切换 side panel 显隐，不再触发 action.onClicked
chrome.runtime.onInstalled.addListener(() => {
  try {
    if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
      chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(() => {});
    }
  } catch (e) {
    // sidePanel API 不可用时忽略
  }
});

// 当 tab URL 是 polymarket.com 时，通知 sidepanel 刷新
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!changeInfo.url && changeInfo.status !== 'complete') return;
  const url = (tab && tab.url) || changeInfo.url;
  if (!url || url.indexOf(POLYMARKET_HOST) === -1) return;
  const payload = { type: 'POLYMARKET_TAB_UPDATED', tabId, url };
  try {
    chrome.runtime.sendMessage(payload).catch(() => {
      // sidepanel 未打开时 sendMessage 会失败，忽略
    });
  } catch (e) {
    // 忽略
  }
});
