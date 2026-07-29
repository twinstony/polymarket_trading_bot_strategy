// OrderArea.js — 下单区：url-bar 三态 + direction-list top5 + Layer2 表单 + reopen-banner
window.Components = window.Components || {};

window.Components.OrderArea = {
  render: function (state, actions) {
    const u = window.Components.utils;
    const isManual = state.urlSource === 'manual';
    const loading = isManual ? state.manualScenarioLoading : state.tabScenarioLoading;
    const sc = isManual && state.manualScenario
      ? state.manualScenario
      : state.tabScenario;

    // 按市场分组：每个 market 一行，选中后再在 Layer 2 选 outcome
    // 命名匹配用户预期：如"LoL: LGD Gaming vs Team WE - Game 1 Winner"作为一行，
    // 选中后 Layer 2 出现 Team WE / LGD Gaming 切换按钮
    const marketRows = [];
    if (sc && sc.markets) {
      sc.markets.forEach(function (m, mi) {
        const outcomes = m.outcomes || [];
        const tokenIds = m.clob_token_ids || [];
        if (outcomes.length === 0 || tokenIds.length === 0) return;
        // 聚合该 market 的盘口与持仓状态
        let bestBid = null, bestAsk = null;
        let hasPosition = false, hasOrder = false;
        const outcomeInfos = outcomes.map(function (name, i) {
          const tid = tokenIds[i];
          if (!tid) return null;
          const book = u.getBook(sc, tid);
          if (book.bid != null && (bestBid == null || book.bid > bestBid)) bestBid = book.bid;
          if (book.ask != null && (bestAsk == null || book.ask < bestAsk)) bestAsk = book.ask;
          const pos = (state.positions || []).find(function (p) { return p.token_id === tid; });
          if (pos && pos.position_size > 0 && !pos.closed) hasPosition = true;
          if (pos && (u.normalizeState(pos.state) === 'BUY_PENDING' || u.normalizeState(pos.state) === 'HOLDING_WITH_SELL')) hasOrder = true;
          return { outcome: name, token_id: tid, bid: book.bid, ask: book.ask, pos: pos };
        }).filter(Boolean);
        // 排序与命令行 market_setup.py select_market 完全一致：
        // 按 Gamma API 返回的 markets 数组原顺序，不做任何 priority 重排
        marketRows.push({
          market_q: m.question || m.event_title || '',
          event_title: m.event_title || '',
          market_idx: mi,
          outcomes: outcomeInfos,
          best_bid: bestBid,
          best_ask: bestAsk,
          has_position: hasPosition,
          has_order: hasOrder,
        });
      });
    }
    // 不排序：保持 Gamma API 返回顺序（Game 1 / Game 2 / BO3 等天然在前）

    const TOP = 5;
    const top = marketRows.slice(0, TOP);
    const rest = marketRows.slice(TOP);

    const showSelected = !!state.selectedDirection;
    const showReopenBanner = state.reopenSource != null;

    // url-bar 三态
    let urlBarHtml;
    if (state.urlInputMode) {
      // 输入态：SLUG 标签 + input + 解析 + ✕
      urlBarHtml = '' +
        '<div class="url-bar manual">' +
          '<span class="url-tag">SLUG</span>' +
          '<input class="url-input" id="url-input" placeholder="输入 slug 或完整 URL，如 lpl-s13-finals" value="' + escapeAttr(state.manualSlug || '') + '" />' +
          '<span class="url-parse-btn" id="url-parse">解析</span>' +
          '<span class="url-switch" id="url-cancel-input" title="取消">✕</span>' +
        '</div>';
    } else if (state.urlSource === 'manual' && state.manualScenario) {
      // 已解析态：SLUG + 手动 URL + 手动标记 + ⤺ 返回
      const displayUrl = state.manualScenario.url || ('polymarket.com/event/' + (state.manualScenario.slug || ''));
      urlBarHtml = '' +
        '<div class="url-bar manual">' +
          '<span class="url-tag">SLUG</span>' +
          '<span class="url-text">' + escapeHtml(displayUrl) + '</span>' +
          '<span class="url-source-tag">手动</span>' +
          '<span class="url-back" id="url-back" title="返回当前页 URL">⤺ 返回</span>' +
        '</div>';
    } else {
      // 默认态：URL 标签 + 当前页 URL（只读） + ⇄
      const tabUrl = state.currentTabUrl || (sc && (sc.url || ('polymarket.com/event/' + (sc.slug || '')))) || '未检测到 polymarket.com 标签页';
      urlBarHtml = '' +
        '<div class="url-bar">' +
          '<span class="url-tag">URL</span>' +
          '<span class="url-text">' + escapeHtml(tabUrl) + '</span>' +
          '<span class="url-switch" id="url-switch" title="手动输入 slug">⇄</span>' +
        '</div>';
    }

    const sourceLabel = state.urlSource === 'manual' ? '手动 slug 解析' : '当前 URL 解析';

    // 解析中：骨架屏 + 文案
    let dirListHtml;
    if (loading) {
      dirListHtml = '' +
        '<div class="skeleton-state">' +
          '<div class="skeleton-text">⏳ 正在解析市场…</div>' +
          '<div class="skeleton-row"></div>' +
          '<div class="skeleton-row"></div>' +
          '<div class="skeleton-row"></div>' +
          '<div class="skeleton-row"></div>' +
          '<div class="skeleton-row"></div>' +
        '</div>';
    } else if (!sc) {
      dirListHtml = '<div class="empty-state">未解析到市场<br/>请打开 polymarket.com/event/&lt;slug&gt; 或手动输入 slug</div>';
    } else if (marketRows.length === 0) {
      dirListHtml = '<div class="empty-state">该市场无可用方向</div>';
    } else {
      dirListHtml = '' +
        top.map(function (mr) { return renderMarketRow(mr, state); }).join('') +
        (rest.length > 0
          ? '<div class="more-row ' + (state.expandedMore ? 'expanded' : '') + '" id="more-row">' +
              (state.expandedMore ? '▲ 收起' : '▼ 展开更多 (' + rest.length + ')') +
            '</div>'
          : '') +
        (state.expandedMore && rest.length > 0
          ? rest.map(function (mr) { return renderMarketRow(mr, state); }).join('')
          : '');
    }

    return '' +
      '<div class="section order-section" id="order-section">' +
        '<div class="order-l1">' +
          '<div class="section-head">' +
            '<div class="section-title">ORDER · L1 DIRECTIONS</div>' +
            '<div class="section-meta">' + escapeHtml(sourceLabel) + ' · top ' + TOP + '</div>' +
          '</div>' +
          urlBarHtml +
          '<div class="direction-list">' + dirListHtml + '</div>' +
        '</div>' +
        '<div class="order-l2 ' + (showSelected ? '' : 'collapsed') + ' ' + (showReopenBanner ? 'l2-highlight' : '') + '" id="order-l2">' +
          renderOrderForm(state) +
        '</div>' +
      '</div>';
  },
};

// ===== 单市场行（两行布局：market_q 独占一行，outcomes 概览 + 盘口移到第二行） =====
function renderMarketRow(mr, state) {
  const cls = ['direction'];
  // 选中态按 market_q 匹配（同一 market 任意 outcome 选中都高亮）
  if (state.selectedDirection && state.selectedDirection.market_q === mr.market_q) cls.push('active');
  if (mr.has_position) cls.push('has-position');
  else if (mr.has_order) cls.push('has-order');
  const badge = mr.has_position ? '<span class="badge pos">持仓</span>'
              : mr.has_order    ? '<span class="badge ord">挂单</span>'
              : '';
  const bidStr = mr.best_bid == null ? '—' : Number(mr.best_bid).toFixed(2);
  const askStr = mr.best_ask == null ? '—' : Number(mr.best_ask).toFixed(2);
  // 把整个 market 的 outcome/token 列表序列化到 data-* 供 sidepanel.js 读取
  const outcomesJson = escapeAttr(JSON.stringify(mr.outcomes));
  return '' +
    '<div class="' + cls.join(' ') + '" data-action="select-market" data-market="' + escapeAttr(mr.market_q) + '" data-outcomes="' + outcomesJson + '" title="' + escapeAttr(mr.market_q) + '">' +
      '<div class="dir-name-row">' +
        '<span class="dir-name">' + escapeHtml(mr.market_q) + '</span>' +
        ' ' + badge +
      '</div>' +
      '<div class="dir-prices-row">' +
        '<span class="dir-market-q">' + mr.outcomes.length + ' 方向 · ' + escapeHtml(mr.outcomes.map(function (o) { return o.outcome; }).join(' / ')) + '</span>' +
        '<span class="dir-prices">' +
          '<span><span class="label">BID</span><span class="bid">' + bidStr + '</span></span>' +
          '<span><span class="label">ASK</span><span class="ask">' + askStr + '</span></span>' +
        '</span>' +
      '</div>' +
    '</div>';
}

// ===== Layer 2 下单表单 =====
function renderOrderForm(state) {
  if (!state.selectedDirection) return '';
  const u = window.Components.utils;
  const d = state.selectedDirection; // {market_q, outcome, token_id, outcomes: [...]}
  const v = state.formValues || {};
  const est = (v.entry_price || 0) * (v.share_amount || 0);
  const showReopen = state.reopenSource != null;
  const sourceLabel = showReopen
    ? ((state.positions || []).find(function (p) { return p.token_id === state.reopenSource; }) || {}).label || ''
    : '';

  const tpSfx = v.take_profit_pct != null ? '+' + (v.take_profit_pct * 100).toFixed(0) + '%' : '+0%';
  const slSfx = v.stop_loss_pct != null ? '-' + (v.stop_loss_pct * 100).toFixed(0) + '%' : '-0%';

  // outcome 切换器：从 d.outcomes（整个 market 的所有 outcome）渲染按钮
  // 选中后切换 token_id，并刷新当前 outcome 的盘口显示
  const outcomes = d.outcomes || [];
  let outcomeSwitcherHtml = '';
  if (outcomes.length > 1) {
    outcomeSwitcherHtml = '<div class="outcome-switcher">' +
      '<div class="outcome-switcher-label">下注方向</div>' +
      '<div class="outcome-tabs">' +
        outcomes.map(function (o) {
          const isActive = o.token_id === d.token_id;
          const cls = 'outcome-tab' + (isActive ? ' active' : '');
          const bidStr = o.bid == null ? '—' : Number(o.bid).toFixed(2);
          const askStr = o.ask == null ? '—' : Number(o.ask).toFixed(2);
          return '<div class="' + cls + '" data-action="select-outcome" data-token="' + escapeAttr(o.token_id) + '" data-outcome="' + escapeAttr(o.outcome) + '">' +
            '<span class="ot-name">' + escapeHtml(o.outcome) + '</span>' +
            '<span class="ot-prices"><span class="ot-bid">B ' + bidStr + '</span><span class="ot-ask">A ' + askStr + '</span></span>' +
          '</div>';
        }).join('') +
      '</div>' +
    '</div>';
  } else if (outcomes.length === 1) {
    // 单 outcome market：直接显示盘口
    const o = outcomes[0];
    outcomeSwitcherHtml = '<div class="outcome-switcher single">' +
      '<div class="outcome-tabs">' +
        '<div class="outcome-tab active">' +
          '<span class="ot-name">' + escapeHtml(o.outcome) + '</span>' +
          '<span class="ot-prices"><span class="ot-bid">B ' + (o.bid == null ? '—' : Number(o.bid).toFixed(2)) + '</span><span class="ot-ask">A ' + (o.ask == null ? '—' : Number(o.ask).toFixed(2)) + '</span></span>' +
        '</div>' +
      '</div>' +
    '</div>';
  }

  return '' +
    '<div class="l2-head">' +
      '<div class="l2-title">' + escapeHtml(d.market_q) + '</div>' +
      '<div class="l2-head-right">' +
        '<div class="l2-est">预估投入 <span class="val">' + est.toFixed(2) + ' USDC</span></div>' +
        '<div class="l2-collapse" id="l2-collapse" title="收起价格区（Esc）">✕</div>' +
      '</div>' +
    '</div>' +
    outcomeSwitcherHtml +
    (showReopen
      ? '<div class="reopen-banner">' +
          '<span>⚠ 来自持仓 [' + escapeHtml(sourceLabel) + '] 的重新开单参数已预填</span>' +
          '<span class="back" id="reopen-back">← 返回当前 URL 下单区</span>' +
        '</div>'
      : '') +
    '<div class="form-grid">' +
      '<div class="field">' +
        '<div class="field-label">买入价 <span class="req">ENTRY · 必填</span></div>' +
        '<div class="field-suffix">' +
          '<input class="field-input" id="f-entry" type="number" step="0.01" value="' + fmtVal(v.entry_price) + '" />' +
          '<span class="sfx">USDC</span>' +
        '</div>' +
      '</div>' +
      '<div class="field">' +
        '<div class="field-label">卖出价 <span class="req">EXIT · 必填</span></div>' +
        '<div class="field-suffix">' +
          '<input class="field-input" id="f-exit" type="number" step="0.01" value="' + fmtVal(v.exit_price) + '" />' +
          '<span class="sfx">USDC</span>' +
        '</div>' +
      '</div>' +
      '<div class="field full">' +
        '<div class="field-label">数量 <span class="opt">SHARE_AMOUNT</span></div>' +
        '<div class="field-suffix">' +
          '<input class="field-input" id="f-size" type="number" step="1" value="' + fmtVal(v.share_amount) + '" />' +
          '<span class="sfx">SHARES</span>' +
        '</div>' +
      '</div>' +
      '<div class="field">' +
        '<div class="field-label">止盈 <span class="opt">TP · 可空</span></div>' +
        '<div class="field-suffix">' +
          '<input class="field-input" id="f-tp" type="number" step="0.01" value="' + fmtVal(v.take_profit_pct) + '" placeholder="未设置" />' +
          '<span class="sfx">' + tpSfx + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="field">' +
        '<div class="field-label">止损 <span class="opt">SL · 可空</span></div>' +
        '<div class="field-suffix">' +
          '<input class="field-input" id="f-sl" type="number" step="0.01" value="' + fmtVal(v.stop_loss_pct) + '" placeholder="未设置" />' +
          '<span class="sfx">' + slSfx + '</span>' +
        '</div>' +
      '</div>' +
    '</div>' +
    '<div class="l2-actions">' +
      '<button class="btn primary" data-action="place-order">▶ 下单 · BUY ' + fmtVal(v.share_amount) + ' @ ' + fmtVal(v.entry_price) + '</button>' +
      '<button class="btn ghost" data-action="reset-form">重置</button>' +
    '</div>';
}

// ===== 工具：HTML 转义 =====
function escapeHtml(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function escapeAttr(s) {
  return escapeHtml(s);
}
function fmtVal(v) {
  if (v == null || v === '') return '';
  return String(v);
}
