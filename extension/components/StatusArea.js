// StatusArea.js — 紧凑单行 4 格状态区
// 同时为后续 component 提供共享 utils（在 window.Components 下）
window.Components = window.Components || {};

// ===== 共享状态枚举（对齐 runtime.py 5 状态机 + 设计文档 4.1） =====
window.Components.STATE_ENUM = {
  WAITING:          { key: 'state-waiting',           label: '待入场',          color: 'cyan'   },
  BUY_PENDING:      { key: 'state-buy-pending',       label: '已挂买单·待成交',  color: 'amber'  },
  HOLDING_NO_SELL:  { key: 'state-holding-no-sell',   label: '持仓·待挂卖单',    color: 'rose'   },
  HOLDING_WITH_SELL:{ key: 'state-holding-with-sell', label: '持仓·已挂卖单',    color: 'mint'   },
  CLOSED:           { key: 'state-closed',            label: '已平仓',          color: 'muted'  },
};

// ===== 共享 utils =====
window.Components.utils = {
  fmtPct: function (p) {
    if (p == null || p === '' || isNaN(p)) return '—';
    return (p * 100).toFixed(1) + '%';
  },
  fmtPrice: function (p) {
    if (p == null || p === '' || isNaN(p)) return '—';
    return Number(p).toFixed(4);
  },
  fmtNum: function (n, d) {
    if (n == null || n === '' || isNaN(n)) return '—';
    return Number(n).toFixed(d == null ? 2 : d);
  },
  // 将中文/英文 state 归一为枚举 key
  normalizeState: function (s) {
    const map = {
      '待入场': 'WAITING',
      '已挂买单（待成交）': 'BUY_PENDING',
      '已挂买单·待成交': 'BUY_PENDING',
      '持仓中（待挂卖单）': 'HOLDING_NO_SELL',
      '持仓·待挂卖单': 'HOLDING_NO_SELL',
      '持仓中（已挂保护卖单）': 'HOLDING_WITH_SELL',
      '持仓·已挂卖单': 'HOLDING_WITH_SELL',
      '已平仓': 'CLOSED',
      'WAITING': 'WAITING',
      'WAITING_ENTRY': 'WAITING',
      'BUY_PENDING': 'BUY_PENDING',
      'HOLDING_NO_SELL': 'HOLDING_NO_SELL',
      'HOLDING_WITH_SELL': 'HOLDING_WITH_SELL',
      'CLOSED': 'CLOSED',
    };
    return map[s] || 'WAITING';
  },
  // 解析 label "{event_title} [{outcome}]" → { market_q, outcome }
  parseLabel: function (label) {
    if (!label) return { market_q: '', outcome: '' };
    const m = label.match(/^(.+?)\s*\[(.+?)\]$/);
    if (m) return { market_q: m[1], outcome: m[2] };
    return { market_q: '', outcome: label };
  },
  // 按 state 计算可用 actions（对齐设计文档 6.4 表格）
  actionsFor: function (stateKey) {
    switch (stateKey) {
      case 'WAITING':           return { stop: true,  close: false, reopen: true,  delete: true  };
      case 'BUY_PENDING':       return { stop: true,  close: false, reopen: true,  delete: true  };
      case 'HOLDING_NO_SELL':   return { stop: false, close: true,  reopen: true,  delete: false };
      case 'HOLDING_WITH_SELL': return { stop: true,  close: true,  reopen: true,  delete: false };
      case 'CLOSED':            return { stop: false, close: false, reopen: true,  delete: true  };
      default:                  return { stop: false, close: false, reopen: false, delete: false };
    }
  },
  tpTrigger: function (avg, tp) {
    return (tp != null && avg > 0) ? avg * (1 + tp) : null;
  },
  slTrigger: function (avg, sl) {
    return (sl != null && avg > 0) ? avg * (1 - sl) : null;
  },
  // 从 scenario.markets 查找 token 的盘口
  getBook: function (scenario, tokenId) {
    if (!scenario || !scenario.markets) return { bid: null, ask: null };
    for (let i = 0; i < scenario.markets.length; i++) {
      const m = scenario.markets[i];
      if (m && m.books) {
        for (let j = 0; j < m.books.length; j++) {
          if (m.books[j].token_id === tokenId) {
            return { bid: m.books[j].best_bid, ask: m.books[j].best_ask };
          }
        }
      }
    }
    return { bid: null, ask: null };
  },
  // 触发元素短暂闪烁（pulse/flash 类）
  flash: function (el) {
    if (!el) return;
    el.classList.remove('flash');
    // 强制重排以重启动画
    void el.offsetWidth;
    el.classList.add('flash');
    setTimeout(function () { el.classList.remove('flash'); }, 220);
  },
};

// ===== StatusArea 组件 =====
// 紧凑单行 4 格：在线状态（绿点+ONLINE/OFFLINE）/ 策略数 / PNL（mint/rose 着色）/ 周期
// 模块级 prev：用于 render 时检测值变化并触发 flash 动画
const prevStatValues = { pnl: null, cycle: null, count: null };

window.Components.StatusArea = {
  render: function (state) {
    const u = window.Components.utils;
    const c = state.connection || {};
    const online = !!c.online;
    const onlineCls = online ? 'mint' : 'rose';
    const onlineTxt = online ? 'ONLINE' : 'OFFLINE';

    const positions = state.positions || [];
    const holding = positions.filter(function (p) {
      return (p.position_size > 0) && !p.closed;
    });

    // PNL：优先用 API status 字段，否则按本地持仓 + mark 估算
    let pnl = 0, pnlPct = 0;
    if (state.status) {
      pnl = state.status.pnl_usdc != null ? state.status.pnl_usdc : 0;
      pnlPct = state.status.pnl_pct != null ? state.status.pnl_pct : 0;
    }
    const pnlClass = pnl >= 0 ? 'mint' : 'rose';
    const pnlStr = (pnl >= 0 ? '+' : '') + pnl.toFixed(2);

    const cycle = state.cycleCounter || 0;

    // 检测 PNL / 周期 / 持仓数 变化，触发 flash 闪烁
    const changedIds = [];
    if (prevStatValues.pnl !== pnl) changedIds.push('stat-pnl');
    if (prevStatValues.cycle !== cycle) changedIds.push('stat-cycle');
    if (prevStatValues.count !== holding.length) changedIds.push('stat-pos');
    prevStatValues.pnl = pnl;
    prevStatValues.cycle = cycle;
    prevStatValues.count = holding.length;
    // render 返回字符串后由调用方 innerHTML 写入 DOM；用 setTimeout(0) 延迟到 DOM 就绪再触发闪烁
    if (changedIds.length) {
      setTimeout(function () {
        changedIds.forEach(function (id) {
          const el = document.getElementById(id);
          if (el) u.flash(el);
        });
      }, 0);
    }

    return '' +
      '<div class="section" id="status-section" style="padding:8px 14px">' +
        '<div class="status-grid">' +
          '<div class="stat">' +
            '<span class="stat-label">CONN</span>' +
            '<span class="stat-value ' + onlineCls + '"><span class="conn-dot' + (online ? '' : ' offline') + '"></span>' + onlineTxt + '</span>' +
          '</div>' +
          '<div class="stat">' +
            '<span class="stat-label">POS</span>' +
            '<span class="stat-value" id="stat-pos">' + holding.length + '<span class="stat-unit">/' + positions.length + '</span></span>' +
          '</div>' +
          '<div class="stat">' +
            '<span class="stat-label">PNL</span>' +
            '<span class="stat-value ' + pnlClass + '" id="stat-pnl">' + pnlStr + '<span class="stat-unit">(' + u.fmtPct(pnlPct) + ')</span></span>' +
          '</div>' +
          '<div class="stat">' +
            '<span class="stat-label">CYCLE</span>' +
            '<span class="stat-value" id="stat-cycle">' + cycle + '</span>' +
          '</div>' +
        '</div>' +
      '</div>';
  },
};
