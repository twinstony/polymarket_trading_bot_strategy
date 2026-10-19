// sidepanel.js — 主逻辑：state + render + bindEvents
// 从动态原型提取，移除 demo-panel 与 mock 数据，改为调 api.js + components
// 硬约束：REST 全量轮询；策略配置纯内存；不直连 Polymarket；不绕过 bot 安全检查
(function () {
  'use strict';

  // ==========================================================================
  // 全局状态（纯内存，刷新即重读 FastAPI）
  // ==========================================================================
  const state = {
    connection: { online: false, latency: null }, // 初始假设离线，首条 health/positions 成功后转在线
    currentTabUrl: '',                              // chrome.tabs 查询到的当前页 URL
    tabScenario: null,                              // 当前 tab URL 解析结果（/api/url/parse）
    tabScenarioLoading: false,                      // tab URL 解析进行中（防重入）
    manualScenarioLoading: false,                   // 手动 slug 解析进行中
    positions: [],                                  // GET /api/positions 返回的 StrategySnapshot[]
    status: null,                                   // GET /api/status 返回的聚合状态
    globalConfig: {                                 // GET /api/config 返回的默认值
      default_entry_price: 0.50,
      default_exit_price: 0.55,
      default_share_amount: 10,
      default_take_profit_pct: 0.10,
      default_stop_loss_pct: 0.05,
      conditional_entry: true,
      trading_enabled: true,
    },
    cycleCounter: 0,
    // 下单区
    selectedDirection: null,                        // {market_q, outcome, token_id} 或 null
    expandedMore: false,
    reopenSource: null,                             // 持仓 token_id 或 null
    formValues: { entry_price: 0.50, exit_price: 0.55, share_amount: 10, take_profit_pct: 0.10, stop_loss_pct: 0.05 },
    // URL 来源管理：'tab'=当前页解析，'manual'=手动输入 slug
    urlSource: 'tab',
    manualSlug: '',
    manualScenario: null,                           // 手动解析出的 scenario（/api/url/parse 结果）
    urlInputMode: false,                            // url-bar 是否处于输入模式
    // 持仓卡片展开状态：{token_id: true}；未列入=收起（默认全部收起）
    expandedPositions: {},
    // 写操作进行中标记（避免重复点击）
    busy: false,
    // 内嵌设置面板
    settingsOpen: false,
    settingsUrl: '',
    settingsTesting: false,
    settingsResult: null, // {kind:'ok'|'err'|'pending', msg:'...'}
  };

  // ==========================================================================
  // 辅助
  // ==========================================================================
  function $(sel) { return document.querySelector(sel); }

  function findPositionByToken(token_id) {
    return state.positions.find(function (p) { return p.token_id === token_id; });
  }

  // 取当前生效的 scenario（手动 slug 优先）
  function activeScenario() {
    if (state.urlSource === 'manual' && state.manualScenario) return state.manualScenario;
    return state.tabScenario;
  }

  // 从 globalConfig 预填表单默认值
  function defaultFormValues() {
    const g = state.globalConfig || {};
    return {
      entry_price: g.default_entry_price != null ? g.default_entry_price : 0.50,
      exit_price: g.default_exit_price != null ? g.default_exit_price : 0.55,
      share_amount: g.default_share_amount != null ? g.default_share_amount : 10,
      take_profit_pct: g.default_take_profit_pct,
      stop_loss_pct: g.default_stop_loss_pct,
    };
  }

  // ==========================================================================
  // 渲染：顶栏
  // ==========================================================================
  function renderTopbar() {
    const c = state.connection || {};
    const online = !!c.online;
    const cls = online ? 'conn' : 'conn offline';
    const txt = online ? ('FASTAPI' + (c.latency != null ? ' · ' + c.latency + 'ms' : '')) : 'OFFLINE';
    const settingsActive = state.settingsOpen ? ' active' : '';
    return '' +
      '<div class="topbar">' +
        '<div class="brand">poly<span class="dot">·</span>desk</div>' +
        '<div class="topbar-right">' +
          '<div class="' + cls + '"><span class="conn-dot"></span><span>' + txt + '</span></div>' +
          '<div class="icon-btn' + settingsActive + '" id="toggle-settings" title="Bridge 设置">⚙</div>' +
        '</div>' +
      '</div>';
  }

  // 顶栏下方的内嵌设置面板（OKX 风格：点齿轮展开，全部操作在侧边栏内完成）
  function renderSettingsPanel() {
    if (!state.settingsOpen) return '';
    const url = state.settingsUrl || window.Api.getBaseUrl();
    const testing = state.settingsTesting;
    const result = state.settingsResult || {};
    const resultCls = result.kind ? ' ' + result.kind : '';
    const resultTxt = result.msg || '点击测试连接验证 bridge 是否可达';
    const testBtnText = testing ? '测试中...' : '测试连接';
    return '' +
      '<div class="settings-panel">' +
        '<div class="settings-row">' +
          '<span class="settings-label">BRIDGE URL</span>' +
          '<input class="settings-input" id="settings-url" type="text" value="' + escapeAttr(url) + '" placeholder="http://127.0.0.1:8787" />' +
        '</div>' +
        '<div class="settings-actions">' +
          '<button class="settings-btn primary" id="settings-save" type="button">保存</button>' +
          '<button class="settings-btn" id="settings-test" type="button"' + (testing ? ' disabled' : '') + '>' + testBtnText + '</button>' +
        '</div>' +
        '<div class="settings-result' + resultCls + '" id="settings-result">' + escapeHtml(resultTxt) + '</div>' +
      '</div>';
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }
  function escapeAttr(s) { return escapeHtml(s); }

  // ==========================================================================
  // 主渲染
  // ==========================================================================
  function render() {
    const sidebar = $('#sidebar');
    if (!sidebar) return;

    // 焦点保护：若焦点在表单 input 内，先 sync 当前值到 state，
    // 记录焦点元素 id + selectionStart/End，render 后恢复焦点与光标位置
    const active = document.activeElement;
    const isFormInput = active && active.tagName === 'INPUT' &&
      ['f-entry', 'f-exit', 'f-size', 'f-tp', 'f-sl', 'settings-url', 'url-input'].indexOf(active.id) !== -1;
    let focusInfo = null;
    if (isFormInput) {
      // 同步表单值到 state，防止 innerHTML 重建丢失用户刚输入的内容
      if (['f-entry', 'f-exit', 'f-size', 'f-tp', 'f-sl'].indexOf(active.id) !== -1) {
        syncFormFromInputs();
      }
      focusInfo = {
        id: active.id,
        selStart: active.selectionStart,
        selEnd: active.selectionEnd,
      };
    }

    const html =
      renderTopbar() +
      renderSettingsPanel() +
      '<div id="status-area-wrap">' + window.Components.StatusArea.render(state) + '</div>' +
      '<div id="global-cmd-wrap">' + window.Components.GlobalCommandBar.render(state) + '</div>' +
      '<div id="order-area-wrap">' + window.Components.OrderArea.render(state) + '</div>' +
      '<div id="positions-area-wrap">' + window.Components.PositionCard.render(state) + '</div>';
    sidebar.innerHTML = html;
    bindEvents();

    // 恢复焦点与光标位置
    if (focusInfo) {
      const el = document.getElementById(focusInfo.id);
      if (el) {
        try {
          el.focus();
          const len = (el.value || '').length;
          const start = focusInfo.selStart != null ? Math.min(focusInfo.selStart, len) : len;
          const end = focusInfo.selEnd != null ? Math.min(focusInfo.selEnd, len) : len;
          el.setSelectionRange(start, end);
        } catch (e) {
          // 某些 input 类型不支持 setSelectionRange，忽略
        }
      }
    }
  }

  // 局部刷新：持仓区/状态区/全局指令栏 — 不触碰下单区，避免打断表单输入
  function renderPositionsArea() {
    const wrap = document.getElementById('positions-area-wrap');
    if (!wrap) { render(); return; }
    wrap.innerHTML = window.Components.PositionCard.render(state);
    bindPositionsEvents();
  }
  function renderStatusArea() {
    const wrap = document.getElementById('status-area-wrap');
    if (!wrap) { render(); return; }
    wrap.innerHTML = window.Components.StatusArea.render(state);
    bindStatusEvents();
  }
  // 局部刷新：下单区（URL 变化 / slug 解析完成时调用）
  function renderOrderArea() {
    const wrap = document.getElementById('order-area-wrap');
    if (!wrap) { render(); return; }
    wrap.innerHTML = window.Components.OrderArea.render(state);
    bindOrderEvents();
  }

  // 仅刷新周期号等轻量字段，避免全量 render 打断表单输入
  function refreshCycleOnly() {
    const cycleEl = document.getElementById('stat-cycle');
    if (cycleEl) cycleEl.textContent = String(state.cycleCounter || 0);
    document.querySelectorAll('.pos-cycle').forEach(function (el, i) {
      const p = state.positions[i];
      if (p) el.textContent = 'cyc ' + (p.cycle || 0);
    });
  }

  // ==========================================================================
  // 事件绑定（每次 render 后调用）
  // ==========================================================================
  function bindEvents() {
    bindTopbarEvents();
    bindStatusEvents();
    bindOrderEvents();
    bindPositionsEvents();
  }

  // 顶栏 + 设置面板事件
  function bindTopbarEvents() {
    // 顶栏齿轮 → 切换内嵌设置面板（不再依赖 popup）
    const toggle = document.getElementById('toggle-settings');
    if (toggle) {
      toggle.onclick = function () {
        state.settingsOpen = !state.settingsOpen;
        if (state.settingsOpen && !state.settingsUrl) {
          state.settingsUrl = window.Api.getBaseUrl();
        }
        render();
      };
    }

    // 设置面板内的保存/测试/输入
    const sUrl = document.getElementById('settings-url');
    if (sUrl) {
      sUrl.oninput = function () { state.settingsUrl = sUrl.value; };
      sUrl.onkeydown = function (e) { if (e.key === 'Enter') saveSettings(); };
    }
    const sSave = document.getElementById('settings-save');
    if (sSave) sSave.onclick = saveSettings;
    const sTest = document.getElementById('settings-test');
    if (sTest) sTest.onclick = testSettings;
  }

  // 状态区 + 全局指令栏事件
  function bindStatusEvents() {
    // 全局指令：先拉 preview，再交 GlobalCommandBar.openModal 渲染确认弹窗
    bindAll('[data-action="global-stop"]', 'onclick', function () { openGlobalModal('stop_all'); });
    bindAll('[data-action="global-close"]', 'onclick', function () { openGlobalModal('close_all'); });
    bindAll('[data-action="global-delete"]', 'onclick', function () { openGlobalModal('delete_all'); });
  }

  // 下单区事件
  function bindOrderEvents() {
    // 选中 market：把整个 market 的 outcomes 塞到 selectedDirection，Layer 2 出现 outcome 选择器
    bindAll('[data-action="select-market"]', 'onclick', function (b) {
      const market = b.dataset.market;
      let outcomes = [];
      try { outcomes = JSON.parse(b.dataset.outcomes || '[]'); } catch (e) { outcomes = []; }
      if (!outcomes.length) { toast('error', '该市场无可用方向'); return; }
      // 默认选第一个 outcome
      const first = outcomes[0];
      const existing = findPositionByToken(first.token_id);
      state.selectedDirection = {
        market_q: market,
        outcome: first.outcome,
        token_id: first.token_id,
        outcomes: outcomes,
      };
      state.reopenSource = null;
      if (existing) {
        state.formValues = {
          entry_price: existing.entry_price,
          exit_price: existing.exit_price,
          share_amount: existing.share_amount,
          take_profit_pct: existing.take_profit_pct,
          stop_loss_pct: existing.stop_loss_pct,
        };
      } else {
        // 用第一个 outcome 的 ask 作为 entry 默认值
        const ask = first.ask;
        state.formValues = Object.assign({}, state.formValues, {
          entry_price: (typeof ask === 'number' && ask > 0) ? Math.round(ask * 100) / 100 : (state.formValues.entry_price || 0),
        });
      }
      renderOrderArea();
    });

    // Layer 2 内切换 outcome（同一 market 内 Team WE ↔ LGD Gaming）
    bindAll('[data-action="select-outcome"]', 'onclick', function (b) {
      const token = b.dataset.token;
      const outcome = b.dataset.outcome;
      if (!state.selectedDirection) return;
      const outcomes = state.selectedDirection.outcomes || [];
      const sel = outcomes.find(function (o) { return o.token_id === token; });
      if (!sel) return;
      state.selectedDirection.outcome = sel.outcome;
      state.selectedDirection.token_id = sel.token_id;
      // 若该 outcome 已有持仓，预填其参数；否则用其 ask 作为 entry 默认值
      const existing = findPositionByToken(sel.token_id);
      if (existing) {
        state.formValues = {
          entry_price: existing.entry_price,
          exit_price: existing.exit_price,
          share_amount: existing.share_amount,
          take_profit_pct: existing.take_profit_pct,
          stop_loss_pct: existing.stop_loss_pct,
        };
      } else {
        const ask = sel.ask;
        if (typeof ask === 'number' && ask > 0) {
          state.formValues = Object.assign({}, state.formValues, { entry_price: Math.round(ask * 100) / 100 });
        }
      }
      renderOrderArea();
    });

    // 展开更多 / 收起
    const moreRow = document.getElementById('more-row');
    if (moreRow) {
      moreRow.onclick = function () { state.expandedMore = !state.expandedMore; renderOrderArea(); };
    }

    // URL 切换：⇄ 进入手动输入模式
    const urlSwitch = document.getElementById('url-switch');
    if (urlSwitch) {
      urlSwitch.onclick = function () {
        state.urlInputMode = true;
        state.manualSlug = '';
        renderOrderArea();
        const inp = document.getElementById('url-input');
        if (inp) inp.focus();
      };
    }

    // 手动输入模式：解析按钮 → 调 GET /api/url/parse?slug=...
    const urlParse = document.getElementById('url-parse');
    if (urlParse) {
      urlParse.onclick = function () { parseManualSlugFromInput(); };
    }

    // 手动输入模式：取消
    const urlCancelInput = document.getElementById('url-cancel-input');
    if (urlCancelInput) {
      urlCancelInput.onclick = function () {
        state.urlInputMode = false;
        state.manualSlug = '';
        renderOrderArea();
      };
    }

    // 手动输入模式：回车提交 / ESC 取消
    const urlInput = document.getElementById('url-input');
    if (urlInput) {
      urlInput.onkeydown = function (e) {
        if (e.key === 'Enter') {
          e.preventDefault();
          parseManualSlugFromInput();
        } else if (e.key === 'Escape') {
          state.urlInputMode = false;
          renderOrderArea();
        }
      };
    }

    // 手动 slug 已解析态：⤺ 返回当前页 URL
    const urlBack = document.getElementById('url-back');
    if (urlBack) {
      urlBack.onclick = function () {
        state.urlSource = 'tab';
        state.manualSlug = '';
        state.manualScenario = null;
        state.urlInputMode = false;
        state.selectedDirection = null;
        state.reopenSource = null;
        state.expandedMore = false;
        toast('info', '已返回当前页 URL 下单区');
        renderOrderArea();
      };
    }

    // 重新开单 banner 的返回
    const reopenBack = document.getElementById('reopen-back');
    if (reopenBack) {
      reopenBack.onclick = function () {
        state.reopenSource = null;
        state.selectedDirection = null;
        renderOrderArea();
      };
    }

    // 表单输入绑定
    bindFormInput('f-entry', 'entry_price', false);
    bindFormInput('f-exit', 'exit_price', false);
    bindFormInput('f-size', 'share_amount', false);
    bindFormInput('f-tp', 'take_profit_pct', true);
    bindFormInput('f-sl', 'stop_loss_pct', true);

    // 下单 / 重置
    const placeBtn = document.querySelector('[data-action="place-order"]');
    if (placeBtn) placeBtn.onclick = placeOrder;
    const resetBtn = document.querySelector('[data-action="reset-form"]');
    if (resetBtn) resetBtn.onclick = resetForm;

    // 收起价格区（Layer 2）：点 ✕ 或按 Esc 隐藏价格区，回到方向列表
    const l2Collapse = document.getElementById('l2-collapse');
    if (l2Collapse) {
      l2Collapse.onclick = function () {
        state.selectedDirection = null;
        state.reopenSource = null;
        renderOrderArea();
      };
    }
  }

  // 全局快捷键：Ctrl+Enter（Win/Linux）/ Cmd+Enter（macOS）下单
  // 在 init 里通过 document.addEventListener('keydown', ...) 注册一次
  function _isPlaceShortcut(e) {
    if (e.key !== 'Enter') return false;
    return (e.ctrlKey && !e.metaKey) || (!e.ctrlKey && e.metaKey); // Ctrl+Enter or Cmd+Enter
  }
  function _handleGlobalKeydown(e) {
    // 快捷键下单：仅当价格区可见且焦点不在 url-input / settings-url 上时触发
    if (_isPlaceShortcut(e)) {
      if (!state.selectedDirection) return;
      const active = document.activeElement;
      if (active && active.tagName === 'INPUT' &&
          ['url-input', 'settings-url'].indexOf(active.id) !== -1) return;
      e.preventDefault();
      placeOrder();
      return;
    }
    // Esc 收起价格区（仅当焦点不在手动 slug 输入框上，避免与 url-input 的 ESC 冲突）
    if (e.key === 'Escape' && state.selectedDirection) {
      const active = document.activeElement;
      if (active && active.tagName === 'INPUT' && active.id === 'url-input') return;
      state.selectedDirection = null;
      state.reopenSource = null;
      renderOrderArea();
    }
  }

  // 持仓区事件
  function bindPositionsEvents() {
    // 持仓卡片 actions
    bindAll('[data-action="pos-stop"]', 'onclick', function (b) { posStop(b.dataset.id, b.dataset.state); });
    bindAll('[data-action="pos-close"]', 'onclick', function (b) { posClose(b.dataset.id); });
    bindAll('[data-action="pos-reopen"]', 'onclick', function (b) { posReopen(b.dataset.id); });
    bindAll('[data-action="pos-delete"]', 'onclick', function (b) { posDelete(b.dataset.id); });

    // 持仓卡片收起/展开
    bindAll('[data-action="pos-toggle"]', 'onclick', function (b, e) {
      if (e) e.stopPropagation();
      const id = b.dataset.id;
      if (state.expandedPositions[id]) delete state.expandedPositions[id];
      else state.expandedPositions[id] = true;
      renderPositionsArea();
    });

    // 全部展开/全部收起
    const toggleAll = document.getElementById('toggle-all-positions');
    if (toggleAll) {
      toggleAll.onclick = function () {
        const allExpanded = state.positions.every(function (p) { return state.expandedPositions[p.token_id]; });
        state.expandedPositions = {};
        if (!allExpanded) {
          state.positions.forEach(function (p) { state.expandedPositions[p.token_id] = true; });
        }
        renderPositionsArea();
      };
    }
  }

  // 批量绑定辅助：按 selector 找到所有元素，设置事件并回传元素本身
  function bindAll(sel, ev, handler) {
    document.querySelectorAll(sel).forEach(function (el) {
      el[ev] = function (e) { handler(el, e); };
    });
  }

  // ===== 内嵌设置面板：保存 / 测试连接 =====
  async function saveSettings() {
    const v = (state.settingsUrl || '').trim();
    if (!v) { state.settingsResult = { kind: 'err', msg: '地址不能为空' }; render(); return; }
    const ok = await window.Api.setBaseUrl(v);
    if (ok) {
      state.settingsResult = { kind: 'ok', msg: '已保存: ' + v + '（轮询将自动用新地址）' };
      // 立即触发一次健康检查 + 全量同步
      try { await window.Api.getHealth(); } catch (e) {}
      window.Api.pollNow && window.Api.pollNow();
    } else {
      state.settingsResult = { kind: 'err', msg: '保存失败' };
    }
    render();
  }

  async function testSettings() {
    const v = (state.settingsUrl || '').trim();
    if (!v) { state.settingsResult = { kind: 'err', msg: '请先输入地址' }; render(); return; }
    state.settingsTesting = true;
    state.settingsResult = { kind: 'pending', msg: '测试中...' };
    render();
    // 先临时切换 baseUrl 以便测试新地址
    const prev = window.Api.getBaseUrl();
    await window.Api.setBaseUrl(v);
    try {
      const t0 = performance.now();
      const data = await window.Api.getHealth();
      const t1 = performance.now();
      const latency = Math.round(t1 - t0);
      const count = data && data.strategies_count != null ? data.strategies_count : '?';
      const running = data && data.bot_running ? '运行中' : '已停止';
      state.settingsResult = { kind: 'ok', msg: '在线 · ' + latency + 'ms · ' + count + ' 策略 · bot ' + running };
    } catch (e) {
      // 测试失败回退到原地址
      await window.Api.setBaseUrl(prev);
      state.settingsResult = { kind: 'err', msg: '离线 · ' + (e && e.message ? e.message : e) };
    } finally {
      state.settingsTesting = false;
      render();
    }
  }

  function bindFormInput(id, key, nullable) {
    const el = document.getElementById(id);
    if (!el) return;
    el.oninput = function () {
      const raw = el.value;
      if (raw === '' && nullable) {
        state.formValues[key] = null;
      } else {
        const v = parseFloat(raw);
        if (!isNaN(v)) state.formValues[key] = v;
      }
      updateFormDerived();
    };
  }

  function updateFormDerived() {
    const v = state.formValues;
    const est = (v.entry_price || 0) * (v.share_amount || 0);
    const estEl = document.querySelector('.l2-est .val');
    if (estEl) estEl.textContent = est.toFixed(2) + ' USDC';
    const btn = document.querySelector('[data-action="place-order"]');
    if (btn) btn.textContent = '▶ 下单 · BUY ' + fmtVal(v.share_amount) + ' @ ' + fmtVal(v.entry_price);
  }

  function fmtVal(v) {
    if (v == null || v === '') return '';
    return String(v);
  }

  // ==========================================================================
  // URL 解析
  // ==========================================================================
  function parseManualSlugFromInput() {
    const inp = document.getElementById('url-input');
    const slug = inp ? inp.value : '';
    if (!slug || !slug.trim()) {
      toast('error', '请输入 slug 或 URL');
      return;
    }
    toast('info', '解析中...');
    state.manualScenarioLoading = true;
    renderOrderArea();
    window.Api.parseUrl(slug).then(function (scenario) {
      state.manualSlug = slug;
      state.manualScenario = scenario;
      state.urlSource = 'manual';
      state.urlInputMode = false;
      state.manualScenarioLoading = false;
      state.selectedDirection = null;
      state.reopenSource = null;
      state.expandedMore = false;
      state.formValues = defaultFormValues();
      toast('info', '已解析手动 slug → ' + (scenario.slug || slug));
      renderOrderArea();
    }).catch(function (err) {
      state.manualScenarioLoading = false;
      renderOrderArea();
      toast('error', '解析失败：' + (err && err.message ? err.message : err));
      // 停留输入态
    });
  }

  // 查询当前 polymarket.com tab URL 并解析
  function refreshTabUrl() {
    if (!chrome.tabs || !chrome.tabs.query) return;
    chrome.tabs.query({ active: true, currentWindow: true }, function (tabs) {
      const tab = tabs && tabs[0];
      const url = tab && tab.url;
      if (!url || url.indexOf('polymarket.com') === -1) {
        state.currentTabUrl = '';
        state.tabScenario = null;
        // 非 polymarket 页：清空下单区选择，避免残留旧 slug 内容
        state.selectedDirection = null;
        state.reopenSource = null;
        state.expandedMore = false;
        renderOrderArea();
        return;
      }
      state.currentTabUrl = url;
      if (state.urlSource !== 'tab') return; // 手动模式下不打扰
      if (state.tabScenarioLoading) return;
      state.tabScenarioLoading = true;
      // 进入解析中：立即 render 显示骨架屏 + "⏳ 正在解析市场…"
      renderOrderArea();
      window.Api.parseUrl(url).then(function (scenario) {
        // slug 变化：清空下单区残留状态（selectedDirection / formValues / expandedMore），
        // 让下单区 UI 联动到新 slug
        state.tabScenario = scenario;
        state.tabScenarioLoading = false;
        state.selectedDirection = null;
        state.reopenSource = null;
        state.expandedMore = false;
        state.formValues = defaultFormValues();
        renderOrderArea();
      }).catch(function () {
        state.tabScenarioLoading = false;
        renderOrderArea();
        toast('error', 'URL 解析失败');
      });
    });
  }

  // ==========================================================================
  // 业务动作
  // ==========================================================================

  // 同步表单 → state.formValues（下单前调用，防止 oninput 未触发的遗漏）
  function syncFormFromInputs() {
    const map = { 'f-entry': 'entry_price', 'f-exit': 'exit_price', 'f-size': 'share_amount', 'f-tp': 'take_profit_pct', 'f-sl': 'stop_loss_pct' };
    Object.keys(map).forEach(function (id) {
      const el = document.getElementById(id);
      if (!el) return;
      const key = map[id];
      const raw = el.value;
      state.formValues[key] = raw === '' ? null : parseFloat(raw);
    });
  }

  // 下单 → POST /api/order
  function placeOrder() {
    if (state.busy) return;
    syncFormFromInputs();
    const v = state.formValues;
    if (!v.exit_price || v.exit_price <= 0) {
      toast('error', 'EXIT_PRICE 必填 — 强制约束');
      const el = document.getElementById('f-exit');
      if (el) el.classList.add('error');
      return;
    }
    if (!v.entry_price || v.entry_price <= 0 || v.entry_price >= 1) {
      toast('error', 'ENTRY_PRICE 必填且 ∈ (0,1)');
      return;
    }
    if (!v.share_amount || v.share_amount <= 0) {
      toast('error', 'SHARE_AMOUNT 必填');
      return;
    }
    const d = state.selectedDirection;
    if (!d) {
      toast('error', '未选择方向');
      return;
    }
    const req = {
      token_id: d.token_id,
      label: (d.market_q || '') + ' [' + (d.outcome || '') + ']',
      entry_price: v.entry_price,
      exit_price: v.exit_price,
      share_amount: v.share_amount,
      take_profit_pct: v.take_profit_pct,
      stop_loss_pct: v.stop_loss_pct,
      enabled: true,
    };
    state.busy = true;
    toast('info', '下单中...');
    window.Api.placeOrder(req).then(function () {
      state.reopenSource = null;
      state.selectedDirection = null;
      toast('mint', '挂单中 · BUY ' + v.share_amount + ' @ ' + v.entry_price + ' (' + d.outcome + ')');
      window.Api.pollNow(); // 立即全量同步
      render();
    }).catch(function (err) {
      handleApiError(err, '下单失败');
    }).then(function () {
      state.busy = false;
    });
  }

  function resetForm() {
    state.formValues = defaultFormValues();
    render();
    toast('info', '已重置为默认值');
  }

  // 停止策略 → POST /api/position/stop
  function posStop(tokenId, stateKey) {
    if (state.busy) return;
    // BUY_PENDING 状态下点"撤单"：撤买单 + 删除策略记录
    // 语义：未成交买单撤掉后，策略无持仓无挂单，保留记录无意义，直接删除
    if (stateKey === 'BUY_PENDING') {
      state.busy = true;
      window.Api.cancelOrder({ token_id: tokenId, side: 'BUY' }).then(function () {
        return window.Api.deletePosition(tokenId);
      }).then(function () {
        toast('amber', '已撤单并删除策略');
        window.Api.pollNow();
        renderPositionsArea();
      }).catch(function (err) {
        handleApiError(err, '撤单失败');
      }).then(function () { state.busy = false; });
      return;
    }
    // 其他状态（HOLDING_WITH_SELL 撤卖单 / WAITING_ENTRY 停止监控）：保留记录
    state.busy = true;
    window.Api.stopPosition(tokenId).then(function () {
      toast('amber', '已下发停止指令');
      window.Api.pollNow();
      renderPositionsArea();
    }).catch(function (err) {
      handleApiError(err, '停止失败');
    }).then(function () { state.busy = false; });
  }

  // 平仓 → POST /api/position/close
  function posClose(tokenId) {
    if (state.busy) return;
    state.busy = true;
    window.Api.closePosition({ token_id: tokenId, mode: 'market' }).then(function () {
      toast('mint', '已下发平仓指令');
      window.Api.pollNow();
      // 立即乐观刷新持仓区
      renderPositionsArea();
    }).catch(function (err) {
      handleApiError(err, '平仓失败');
    }).then(function () { state.busy = false; });
  }

  // 重新开单 → 注入参数到表单（不立即下单，由用户确认）
  function posReopen(tokenId) {
    const p = findPositionByToken(tokenId);
    if (!p) return;
    state.reopenSource = tokenId;
    // 重新开单场景：只渲染当前持仓的 outcome（无 market 完整 outcomes 列表时，
    // outcome-switcher 退化为单 outcome 显示，仍可正常下单）
    state.selectedDirection = {
      market_q: p.market_q || p.label,
      outcome: p.outcome || '',
      token_id: p.token_id,
      outcomes: [{ outcome: p.outcome || '', token_id: p.token_id, bid: null, ask: null }],
    };
    state.formValues = {
      entry_price: p.entry_price,
      exit_price: p.exit_price,
      share_amount: p.share_amount,
      take_profit_pct: p.take_profit_pct,
      stop_loss_pct: p.stop_loss_pct,
    };
    render();
    const l2 = document.getElementById('order-l2');
    if (l2) l2.scrollIntoView({ behavior: 'smooth', block: 'center' });
    toast('info', '已预填参数，等待你点击下单');
  }

  // 删除策略记录 → 二次确认 → POST /api/position/delete
  function posDelete(tokenId) {
    const p = findPositionByToken(tokenId);
    if (!p) return;
    const label = p.outcome || p.label || tokenId;
    openConfirm({
      variant: 'violet',
      title: '删除策略 · ' + label,
      items: [
        { text: '清除策略记录', cls: 'violet', num: '1' },
        { text: p.closed ? '已平仓策略将被永久移除' : (p.position_size > 0 ? '⚠ 该策略仍持仓，删除仅清记录不平仓' : '该策略无持仓，可安全删除') },
      ],
      warn: '此操作不可恢复 · 现存挂单需先"停止"',
      confirmText: '确认删除',
      onConfirm: function () {
        window.Api.deletePosition(tokenId).then(function () {
          toast('info', '已删除 · ' + label);
          window.Api.pollNow();
          // 立即乐观刷新持仓区
          renderPositionsArea();
        }).catch(function (err) {
          handleApiError(err, '删除失败');
        });
      },
    });
  }

  // ==========================================================================
  // 全局指令：先 preview → 弹窗 → 确认后下发
  // ==========================================================================
  function openGlobalModal(action) {
    // action ∈ 'stop_all' | 'close_all' | 'delete_all'
    toast('info', '计算影响汇总...');
    window.Api.globalPreview(action).then(function (preview) {
      window.Components.GlobalCommandBar.openModal(action, preview);
    }).catch(function (err) {
      handleApiError(err, '影响汇总获取失败');
    });
  }

  // 全局指令确认回调（由 GlobalCommandBar.openModal 内的 onConfirm 触发）
  function onGlobalStopConfirm() {
    window.Api.globalStopAll().then(function () {
      toast('amber', '已执行全部停止');
      window.Api.pollNow();
    }).catch(function (err) { handleApiError(err, '全部停止失败'); });
  }
  function onGlobalCloseConfirm() {
    window.Api.globalCloseAll().then(function () {
      toast('mint', '已执行全部平仓');
      window.Api.pollNow();
    }).catch(function (err) { handleApiError(err, '全部平仓失败'); });
  }
  function onGlobalDeleteConfirm() {
    window.Api.globalDeleteAll().then(function () {
      toast('info', '已清空策略记录');
      window.Api.pollNow();
    }).catch(function (err) { handleApiError(err, '全部删除失败'); });
  }

  // ==========================================================================
  // 错误处理：对齐设计文档错误码
  // ==========================================================================
  function handleApiError(err, prefix) {
    prefix = prefix || '操作失败';
    if (!err) { toast('error', prefix); return; }
    const code = err.body && err.body.error;
    const status = err.status;
    let msg = prefix;
    if (code === 'EXIT_PRICE_REQUIRED') msg = 'EXIT_PRICE 必填';
    else if (code === 'INVALID_PRICE') msg = '价格越界 (0,1)';
    else if (code === 'DUPLICATE_ORDER') msg = '已有同意图挂单';
    else if (code === 'MARKET_CLOSED') msg = '市场已关闭/结算中';
    else if (code === 'INSUFFICIENT_BALANCE') msg = '余额不足';
    else if (code === 'TRADING_DISABLED') msg = 'trading_enabled=false，已禁用下单';
    else if (code === 'TOKEN_NOT_FOUND') msg = 'token_id 无效，请重新解析 URL';
    else if (code === 'BOT_ERROR') msg = 'bot 内部异常，请查看 bot 日志';
    else if (code === 'CLOB_UNAVAILABLE') msg = 'CLOB 不可达';
    else if (status) msg = prefix + ' · HTTP ' + status;
    else msg = prefix + ' · ' + (err.message || err);
    toast('error', msg);
  }

  // ==========================================================================
  // 确认弹窗（供 GlobalCommandBar 与 posDelete 复用）
  // ==========================================================================
  function openConfirm(opts) {
    const overlay = $('#modal-overlay');
    const modal = $('#modal');
    if (!overlay || !modal) return;
    modal.className = 'modal ' + (opts.variant || '');
    const titleCls = opts.variant ? 'modal-title ' + opts.variant : 'modal-title';
    const confirmCls = 'btn confirm ' + (opts.variant || '');
    modal.innerHTML = '' +
      '<div class="modal-head">' +
        '<div class="' + titleCls + '">确认【' + escapeHtml(opts.title || '') + '】</div>' +
        '<div class="modal-close" id="modal-x">✕</div>' +
      '</div>' +
      '<div class="modal-body">' +
        (opts.extraHtml || '') +
        '<div class="impact-list">' +
          (opts.items || []).map(function (it) {
            return '<div class="impact-item ' + (it.cls || '') + '">' +
              '<span>' + escapeHtml(it.text || '') + (it.num != null ? ' <span class="num ' + (it.cls || '') + '">' + escapeHtml(String(it.num)) + '</span>' : '') + '</span>' +
            '</div>';
          }).join('') +
        '</div>' +
        (opts.warn ? '<div class="impact-warn">⚠ ' + escapeHtml(opts.warn) + '</div>' : '') +
      '</div>' +
      '<div class="modal-actions">' +
        '<button class="btn cancel" id="modal-cancel">取消</button>' +
        '<button class="' + confirmCls + '" id="modal-confirm">' + escapeHtml(opts.confirmText || '确认执行') + '</button>' +
      '</div>';
    overlay.classList.add('show');
    $('#modal-x').onclick = closeModal;
    $('#modal-cancel').onclick = closeModal;
    $('#modal-confirm').onclick = function () {
      closeModal();
      if (opts.onConfirm) opts.onConfirm();
    };
    overlay.onclick = function (e) { if (e.target === overlay) closeModal(); };
  }

  function closeModal() {
    const overlay = $('#modal-overlay');
    if (overlay) overlay.classList.remove('show');
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeModal();
  });

  // ==========================================================================
  // Toast
  // ==========================================================================
  function toast(kind, msg) {
    const c = document.getElementById('toast-container');
    if (!c) return;
    const el = document.createElement('div');
    el.className = 'toast ' + (kind === 'mint' ? '' : kind);
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(function () {
      el.classList.add('leaving');
      setTimeout(function () { el.remove(); }, 200);
    }, 3000);
  }

  function escapeHtml(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ==========================================================================
  // 轮询回调
  // ==========================================================================
  function onPositions(positions) {
    const next = Array.isArray(positions) ? positions : [];
    if (!_positionsChanged(state.positions, next)) {
      // 数据无变化：仅刷新 cycle 等轻量字段，不打断表单输入
      refreshCycleOnly();
      return;
    }
    state.positions = next;
    refreshCycleOnly();
    // 仅刷新持仓区，不触碰下单区，避免打断价格输入
    renderPositionsArea();
  }

  function onStatus(statusData) {
    const prevStatus = state.status;
    const prevCycle = state.cycleCounter;
    const prevConn = state.connection;
    state.status = statusData || null;
    if (statusData) {
      if (statusData.cycle != null) state.cycleCounter = statusData.cycle;
      if (statusData.connection) {
        state.connection = {
          online: !!statusData.connection.online,
          latency: statusData.connection.latency_ms != null ? statusData.connection.latency_ms : state.connection.latency,
        };
      }
    }
    if (prevCycle === state.cycleCounter &&
        _statusShallowEqual(prevStatus, state.status) &&
        _connectionEqual(prevConn, state.connection)) {
      // 数据无变化：仅刷新轻量字段
      refreshCycleOnly();
      return;
    }
    refreshCycleOnly();
    // 仅刷新状态区（含连接徽章 / PNL / 周期），不触碰下单区
    renderStatusArea();
  }

  // 简单 diff：positions 列表内容是否变化
  // 比对 token_id + state + cycle + position_size + avg_price + label + enabled
  // 加 enabled 是为了撤单/停止后 enabled=false 切换能被 diff 捕获
  function _positionsChanged(a, b) {
    if (a.length !== b.length) return true;
    const map = {};
    a.forEach(function (p) { map[p.token_id] = p; });
    for (let i = 0; i < b.length; i++) {
      const p = map[b[i].token_id];
      if (!p) return true;
      if (p.state !== b[i].state) return true;
      if (p.cycle !== b[i].cycle) return true;
      if (p.position_size !== b[i].position_size) return true;
      if (p.avg_price !== b[i].avg_price) return true;
      if (p.label !== b[i].label) return true;
      if (!!p.enabled !== !!b[i].enabled) return true;
    }
    return false;
  }
  function _statusShallowEqual(a, b) {
    if (a === b) return true;
    if (!a || !b) return false;
    const keys = ['position_count', 'total_cost_usdc', 'current_value_usdc', 'pnl_usdc', 'pnl_pct', 'bot_running'];
    for (const k of keys) {
      if (a[k] !== b[k]) return false;
    }
    return true;
  }
  function _connectionEqual(a, b) {
    if (!a || !b) return a === b;
    return a.online === b.online && a.latency === b.latency;
  }

  function onDisconnect() {
    state.connection = { online: false, latency: null };
    render();
  }

  function onReconnect() {
    state.connection.online = true;
    // 恢复后立即拉取 config（默认值可能变化）
    loadConfig();
    render();
  }

  // ==========================================================================
  // 启动初始化
  // ==========================================================================
  function loadConfig() {
    window.Api.getConfig().then(function (cfg) {
      state.globalConfig = cfg || state.globalConfig;
      // 表单未选中方向时，同步刷新默认值
      if (!state.selectedDirection) {
        state.formValues = defaultFormValues();
      }
      render();
    }).catch(function () { /* 轮询会持续重试 */ });
  }

  function checkHealth() {
    window.Api.getHealth().then(function (h) {
      state.connection = {
        online: true,
        latency: h && h.latency_ms != null ? h.latency_ms : null,
      };
      render();
    }).catch(function () {
      state.connection = { online: false, latency: null };
      render();
    });
  }

  function init() {
    // 1. 加载 bridge 地址
    window.Api.loadBaseUrl().then(function () {
      // 2. 健康检查 + 配置 + 当前 tab URL（并行）
      checkHealth();
      loadConfig();
      refreshTabUrl();
      // 3. 启动轮询
      window.Api.startPolling({
        onPositions: onPositions,
        onStatus: onStatus,
        onDisconnect: onDisconnect,
        onReconnect: onReconnect,
      });
    });

    // 监听 background.js 的 tab 更新通知
    try {
      chrome.runtime.onMessage.addListener(function (msg) {
        if (msg && msg.type === 'POLYMARKET_TAB_UPDATED') {
          // tab URL 变化：若处于 tab 模式，重新解析
          if (state.urlSource === 'tab') {
            state.currentTabUrl = msg.url || state.currentTabUrl;
            refreshTabUrl();
          }
        }
      });
    } catch (e) {
      // 忽略
    }

    // 首屏先渲染一次（显示离线骨架）
    render();
  }

  // ==========================================================================
  // 暴露 window.App（供 GlobalCommandBar.openModal 调用 openConfirm / actions）
  // ==========================================================================
  window.App = {
    openConfirm: openConfirm,
    actions: {
      onGlobalStopConfirm: onGlobalStopConfirm,
      onGlobalCloseConfirm: onGlobalCloseConfirm,
      onGlobalDeleteConfirm: onGlobalDeleteConfirm,
    },
  };

  // 注册全局快捷键（Ctrl/Cmd+Enter 下单，Esc 收起价格区）
  // 在 init 外注册一次，不随 render 重复绑定
  if (!window._polyDeskKeybound) {
    window._polyDeskKeybound = true;
    document.addEventListener('keydown', _handleGlobalKeydown);
  }

  // ==========================================================================
  // 启动
  // ==========================================================================
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
