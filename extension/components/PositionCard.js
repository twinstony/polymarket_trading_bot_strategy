// PositionCard.js — 持仓区：单卡片展开/收起 + 全部展开/收起 + 状态色条 + 操作按钮
window.Components = window.Components || {};

window.Components.PositionCard = {
  render: function (state, actions) {
    const positions = state.positions || [];

    if (positions.length === 0) {
      return '' +
        '<div class="section" id="positions-section">' +
          '<div class="section-head">' +
            '<div class="section-title">POSITIONS</div>' +
            '<div class="section-meta">0 strategies</div>' +
          '</div>' +
          '<div class="empty-state">暂无策略记录<br/>从下单区添加新策略</div>' +
        '</div>';
    }

    const u = window.Components.utils;
    const expandedCount = positions.filter(function (p) { return state.expandedPositions[p.token_id]; }).length;
    const allExpanded = expandedCount === positions.length;
    const toggleLabel = allExpanded ? '▼ 全部收起' : '▶ 全部展开';

    return '' +
      '<div class="section" id="positions-section">' +
        '<div class="section-head">' +
          '<div class="section-title">POSITIONS</div>' +
          '<div class="section-meta" id="positions-section-meta">' +
            positions.length + ' strategies · cycle ' + (state.cycleCounter || 0) +
            '<span class="toggle-all" id="toggle-all-positions">' + toggleLabel + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="positions">' +
          positions.map(function (p) { return renderPositionCard(p, state); }).join('') +
        '</div>' +
      '</div>';
  },
};

// ===== 单卡片渲染 =====
function renderPositionCard(p, state) {
  const u = window.Components.utils;
  const stateKey = u.normalizeState(p.state);
  const s = window.Components.STATE_ENUM[stateKey];
  const parsed = u.parseLabel(p.label);
  const outcome = p.outcome || parsed.outcome;
  const marketQ = p.market_q || parsed.market_q;

  const book = u.getBook(state.urlSource === 'manual' && state.manualScenario ? state.manualScenario : state.tabScenario, p.token_id);
  const bid = book.bid;
  const actions = u.actionsFor(stateKey);
  const tpTrig = u.tpTrigger(p.avg_price, p.take_profit_pct);
  const slTrig = u.slTrigger(p.avg_price, p.stop_loss_pct);
  const holding = p.position_size > 0 && !p.closed;
  const pnl = holding ? p.position_size * ((bid != null ? bid : 0) - p.avg_price) : 0;
  const pnlPct = holding && p.avg_price > 0 ? ((bid != null ? bid : 0) - p.avg_price) / p.avg_price : 0;
  const isExpanded = !!state.expandedPositions[p.token_id];

  // 展开态 body 额外内容
  let bodyExtra = '';
  if (stateKey === 'CLOSED') {
    const realized = p.realized_pnl || 0;
    bodyExtra = '' +
      '<div class="pos-row">' +
        '<div class="pos-field"><span class="k">REALIZED</span><span class="v ' + (realized >= 0 ? 'mint' : 'rose') + '">' + (realized >= 0 ? '+' : '') + realized.toFixed(2) + '</span></div>' +
        '<div class="pos-field"><span class="k">TP/SL</span><span class="v">' + u.fmtPct(p.take_profit_pct) + ' / ' + u.fmtPct(p.stop_loss_pct) + '</span></div>' +
        '<div class="pos-field"><span class="k">SIZE</span><span class="v">0</span></div>' +
      '</div>';
  } else {
    bodyExtra = '' +
      '<div class="pos-row">' +
        '<div class="pos-field"><span class="k">AVG</span><span class="v">' + (p.avg_price > 0 ? u.fmtPrice(p.avg_price) : '—') + '</span></div>' +
        '<div class="pos-field"><span class="k">BID</span><span class="v mint">' + (bid != null ? bid.toFixed(4) : '—') + '</span></div>' +
        '<div class="pos-field"><span class="k">PNL</span><span class="v ' + (pnl >= 0 ? 'mint' : 'rose') + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' (' + u.fmtPct(pnlPct) + ')</span></div>' +
      '</div>' +
      '<div class="pos-triggers">' +
        '<div class="trig tp">' +
          '<span class="k">TP TRIGGER</span>' +
          '<span class="v">' + (tpTrig != null ? u.fmtPrice(tpTrig) + ' (' + u.fmtPct(p.take_profit_pct) + ')' : (p.take_profit_pct != null ? '成交后激活' : '未配置')) + '</span>' +
        '</div>' +
        '<div class="trig sl">' +
          '<span class="k">SL TRIGGER</span>' +
          '<span class="v">' + (slTrig != null ? u.fmtPrice(slTrig) + ' (' + u.fmtPct(p.stop_loss_pct) + ')' : (p.stop_loss_pct != null ? '成交后激活' : '未配置')) + '</span>' +
        '</div>' +
      '</div>';
  }

  // 收起态摘要行
  let summaryItems = '';
  if (stateKey === 'CLOSED') {
    const realized = p.realized_pnl || 0;
    summaryItems = '' +
      '<div class="item"><span class="k">E</span><span class="v">' + numFmt(p.entry_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">X</span><span class="v">' + numFmt(p.exit_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">REAL</span><span class="v ' + (realized >= 0 ? 'mint' : 'rose') + '">' + (realized >= 0 ? '+' : '') + realized.toFixed(2) + '</span></div>';
  } else if (holding) {
    summaryItems = '' +
      '<div class="item"><span class="k">E</span><span class="v">' + numFmt(p.entry_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">X</span><span class="v">' + numFmt(p.exit_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">SZ</span><span class="v">' + p.position_size + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">AVG</span><span class="v">' + u.fmtPrice(p.avg_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">PNL</span><span class="v ' + (pnl >= 0 ? 'mint' : 'rose') + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' (' + u.fmtPct(pnlPct) + ')</span></div>';
  } else {
    summaryItems = '' +
      '<div class="item"><span class="k">E</span><span class="v">' + numFmt(p.entry_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">X</span><span class="v">' + numFmt(p.exit_price) + '</span></div>' +
      '<span class="sep">·</span>' +
      '<div class="item"><span class="k">SZ</span><span class="v">' + (p.share_amount || 0) + '</span></div>' +
      '<div class="item"><span class="k">TP/SL</span><span class="v">' + u.fmtPct(p.take_profit_pct) + ' / ' + u.fmtPct(p.stop_loss_pct) + '</span></div>';
  }

  // 停止按钮文案按状态变化
  const stopBtnText = stateKey === 'BUY_PENDING' ? '撤单'
                    : stateKey === 'HOLDING_WITH_SELL' ? '撤卖单'
                    : '停止';

  return '' +
    '<div class="pos-card ' + s.key + ' ' + (isExpanded ? '' : 'collapsed') + '" data-pos-id="' + escapeAttr(p.token_id) + '">' +
      '<div class="pos-head" data-action="pos-toggle" data-id="' + escapeAttr(p.token_id) + '">' +
        '<div class="pos-head-left">' +
          '<span class="pos-chevron">▶</span>' +
          '<span class="pos-label">' + escapeHtml(outcome) + ' <span class="sub">' + escapeHtml(marketQ) + '</span></span>' +
        '</div>' +
        '<div>' +
          '<span class="pos-state"><span class="state-dot"></span>' + s.label + '</span>' +
          '<span class="pos-cycle">cyc ' + (p.cycle || 0) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="pos-summary">' + summaryItems + '</div>' +
      '<div class="pos-body">' +
        '<div class="pos-row">' +
          '<div class="pos-field"><span class="k">ENTRY</span><span class="v">' + numFmt(p.entry_price) + '</span></div>' +
          '<div class="pos-field"><span class="k">EXIT</span><span class="v">' + numFmt(p.exit_price) + '</span></div>' +
          '<div class="pos-field"><span class="k">SIZE</span><span class="v">' + (p.share_amount || 0) + '</span></div>' +
        '</div>' +
        bodyExtra +
      '</div>' +
      '<div class="pos-actions">' +
        '<button class="btn stop ' + (actions.stop ? '' : 'disabled') + '" data-action="pos-stop" data-id="' + escapeAttr(p.token_id) + '" data-state="' + escapeAttr(stateKey) + '">' + stopBtnText + '</button>' +
        '<button class="btn close ' + (actions.close ? '' : 'disabled') + '" data-action="pos-close" data-id="' + escapeAttr(p.token_id) + '">平仓</button>' +
        '<button class="btn reopen ' + (actions.reopen ? '' : 'disabled') + '" data-action="pos-reopen" data-id="' + escapeAttr(p.token_id) + '">重新开单</button>' +
        '<button class="btn delete ' + (actions.delete ? '' : 'disabled') + '" data-action="pos-delete" data-id="' + escapeAttr(p.token_id) + '">删除</button>' +
      '</div>' +
    '</div>';
}

// ===== 工具 =====
function numFmt(n) {
  if (n == null || n === '' || isNaN(n)) return '—';
  return Number(n).toFixed(2);
}
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escapeAttr(s) { return escapeHtml(s); }
