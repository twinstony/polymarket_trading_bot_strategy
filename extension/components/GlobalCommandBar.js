// GlobalCommandBar.js — 全局指令栏 + ImpactConfirmModal 二次确认弹窗
window.Components = window.Components || {};

window.Components.GlobalCommandBar = {
  render: function (state, actions) {
    return '' +
      '<div class="section" id="global-cmd-section">' +
        '<div class="section-head">' +
          '<div class="section-title">GLOBAL COMMAND</div>' +
          '<div class="section-meta">影响汇总 · 二次确认</div>' +
        '</div>' +
        '<div class="global-cmd">' +
          '<button class="gbtn stop" data-action="global-stop">全部停止</button>' +
          '<button class="gbtn close" data-action="global-close">全部平仓</button>' +
          '<button class="gbtn delete" data-action="global-delete">全部删除</button>' +
        '</div>' +
      '</div>';
  },

  // action ∈ 'stop_all' | 'close_all' | 'delete_all'
  // preview 为 /api/global/preview 响应：{ action, impact: {...}, affected_positions: [...] }
  openModal: function (action, preview) {
    const variantMap = { stop_all: 'amber', close_all: '', delete_all: 'violet' };
    const titleMap = { stop_all: '全部停止', close_all: '全部平仓', delete_all: '全部删除' };
    const confirmTextMap = { stop_all: '确认停止', close_all: '确认平仓', delete_all: '确认删除' };
    const warnMap = {
      stop_all: '此操作撤单将立即下发至 CLOB · 不影响已持仓',
      close_all: '此操作不可撤销 · 卖单将立即下发至 CLOB · 网络异常时可能部分成交',
      delete_all: '此操作不可恢复 · 仅清除扩展端记录，不影响 CLOB 实际挂单',
    };

    const variant = variantMap[action] || '';
    const title = titleMap[action] || action;
    const confirmText = confirmTextMap[action] || '确认执行';
    const warn = warnMap[action] || '';

    const impact = (preview && preview.impact) || {};
    const affected = (preview && preview.affected_positions) || [];

    // 构造影响项列表
    const items = [];
    if (impact.cancel_buy_orders != null) {
      items.push({ text: '撤销待成交买单', cls: 'amber', num: String(impact.cancel_buy_orders) });
    }
    if (impact.cancel_sell_orders != null) {
      items.push({ text: '撤销保护卖单', cls: 'amber', num: String(impact.cancel_sell_orders) });
    }
    if (impact.stop_strategies != null) {
      items.push({ text: '停止监控策略', cls: 'amber', num: String(impact.stop_strategies) });
    }
    if (impact.market_sell_positions != null) {
      items.push({ text: '按 best_bid 市价卖出持仓', cls: 'mint', num: String(impact.market_sell_positions) });
    }
    if (impact.estimated_recover_usdc != null) {
      items.push({ text: '预估回收 USDC', cls: 'mint', num: Number(impact.estimated_recover_usdc).toFixed(2) });
    }
    if (impact.current_mark_usdc != null) {
      items.push({ text: '当前 mark 价值', cls: '', num: Number(impact.current_mark_usdc).toFixed(2) });
    }
    if (impact.slippage_risk_usdc != null) {
      items.push({ text: '滑点风险 ±USDC', cls: 'rose', num: Number(impact.slippage_risk_usdc).toFixed(2) });
    }
    if (impact.remove_records != null) {
      items.push({ text: '清除策略记录', cls: 'violet', num: String(impact.remove_records) });
    }
    if (impact.note) {
      items.push({ text: impact.note });
    }

    // 受影响持仓列表
    let affectedHtml = '';
    if (affected.length > 0) {
      affectedHtml = '' +
        '<div class="impact-list" style="margin-top:4px">' +
          '<div style="font-size:9px;color:var(--text-muted);letter-spacing:0.1em;margin-bottom:6px">受影响持仓 (' + affected.length + '):</div>' +
          affected.map(function (p) {
            return '<div class="impact-item"><span>' + escapeHtml(p.label || p.token_id) + ' · ' + (p.size || 0) + ' @ ' + Number(p.avg_price || 0).toFixed(4) + '</span></div>';
          }).join('') +
        '</div>';
    }

    // 委托 sidepanel.js 的 openConfirm 渲染弹窗
    window.App.openConfirm({
      variant: variant,
      title: title,
      items: items,
      extraHtml: affectedHtml,
      warn: warn,
      confirmText: confirmText,
      onConfirm: function () {
        if (action === 'stop_all') {
          window.App.actions.onGlobalStopConfirm();
        } else if (action === 'close_all') {
          window.App.actions.onGlobalCloseConfirm();
        } else if (action === 'delete_all') {
          window.App.actions.onGlobalDeleteConfirm();
        }
      },
    });
  },
};

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
