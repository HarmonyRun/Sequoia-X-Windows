/* =========================================================================
 * Sequoia-X 工作台 —— 前端逻辑（纯原生 JS，零依赖，无框架 / 无 CDN）
 * -------------------------------------------------------------------------
 * 分区：
 *   1. 数字格式化 fmt / 通用工具
 *   2. 全局状态 + API 封装（fetch 失败统一走错误条）
 *   3. 路由与 Tab 切换（URL hash 懒加载）
 *   4. 通用 SVG 图表（环形图 / 横向柱状图）
 *   5. Tab1 总览
 *   6. Tab2 选股结果
 *   7. Tab3 个股（K 线 + 成交量 + 均线 + 悬停 tooltip）
 *   8. Tab4 数据覆盖（筛选 / 搜索 / 分页）
 *   9. Tab5 运行与日志（提交任务 / 1.5s 轮询 / 日志文件）
 *  10. Tab6 设置（接口驱动的表单 / 数据操作 / 计划任务 / 数据库与缓存）
 *  11. 初始化
 * ========================================================================= */
'use strict';

/* =======================================================================
 * 1. 数字格式化与通用工具
 * ======================================================================= */

/** 统一的数字格式化工具 */
const fmt = {
  /**
   * 千分位格式化。3411846 -> "3,411,846"
   * @param {*} v 原始值
   * @param {number} [digits=0] 小数位
   */
  num: function (v, digits) {
    if (v === null || v === undefined || v === '' || !isFinite(Number(v))) return '--';
    const n = Number(v);
    const d = digits === undefined ? 0 : digits;
    const parts = Math.abs(n).toFixed(d).split('.');
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (n < 0 ? '-' : '') + parts.join('.');
  },

  /** 价格：固定两位小数 + 千分位 */
  price: function (v) {
    return fmt.num(v, 2);
  },

  /** 大数字缩写：3411846 -> "341.2 万"；4453223189 -> "44.53 亿" */
  money: function (v) {
    if (v === null || v === undefined || v === '' || !isFinite(Number(v))) return '--';
    const n = Number(v);
    const a = Math.abs(n);
    if (a >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
    if (a >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
    return fmt.num(n, 0);
  },

  /** 百分比：1.23 -> "+1.23%"；-4.56 -> "-4.56%" */
  pct: function (v, digits) {
    if (v === null || v === undefined || v === '' || !isFinite(Number(v))) return '--';
    const n = Number(v);
    const d = digits === undefined ? 2 : digits;
    return (n > 0 ? '+' : '') + n.toFixed(d) + '%';
  },

  /** 日期：'2026/10/12 19:15:15' -> "2026-10-12" */
  date: function (s) {
    if (s === null || s === undefined || s === '') return '--';
    const m = String(s).match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
    if (!m) return String(s);
    return m[1] + '-' + pad2(m[2]) + '-' + pad2(m[3]);
  },

  /** 日期时间：'2026/10/12 19:15:15' -> "2026-10-12 19:15:15" */
  dt: function (s) {
    if (s === null || s === undefined || s === '') return '--';
    const m = String(s).match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?/);
    if (!m) return String(s);
    let out = m[1] + '-' + pad2(m[2]) + '-' + pad2(m[3]);
    if (m[4] !== undefined) out += ' ' + pad2(m[4]) + ':' + m[5] + (m[6] ? ':' + m[6] : '');
    return out;
  },

  /** 数据库大小（入参单位 MB）：128.4 -> "128.4 MB" */
  size: function (mb) {
    if (mb === null || mb === undefined || !isFinite(Number(mb))) return '--';
    const n = Number(mb);
    if (n >= 1024) return (n / 1024).toFixed(2) + ' GB';
    return n.toFixed(1) + ' MB';
  },

  /** 字节数格式化（日志文件 size）：12345 -> "12.1 KB" */
  bytes: function (v) {
    if (v === null || v === undefined || !isFinite(Number(v))) return '--';
    const n = Number(v);
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' GB';
    if (n >= 1048576) return (n / 1048576).toFixed(2) + ' MB';
    if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
    return n + ' B';
  },

  /** 涨跌方向 -> CSS class（A 股：红涨绿跌） */
  dirClass: function (v) {
    const n = Number(v);
    if (!isFinite(n) || n === 0) return 'flat';
    return n > 0 ? 'up' : 'down';
  },

  /** 秒数 -> "01:23:45" */
  duration: function (ms) {
    if (!isFinite(ms) || ms < 0) ms = 0;
    const total = Math.floor(ms / 1000);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return pad2(h) + ':' + pad2(m) + ':' + pad2(s);
  }
};

/** 两位补零 */
function pad2(v) {
  return String(v).length < 2 ? '0' + v : String(v);
}

/** HTML 转义，防止注入 */
function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

/** querySelector 简写 */
function $(sel, root) {
  return (root || document).querySelector(sel);
}

/** 解析后端时间字符串为 Date（兼容 'YYYY-MM-DD HH:MM:SS' 与 'YYYY/MM/DD HH:MM:SS'） */
function parseTs(s) {
  if (!s) return null;
  const str = String(s);
  const m = str.match(/(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?/);
  if (!m) return null;
  return new Date(
    Number(m[1]), Number(m[2]) - 1, Number(m[3]),
    Number(m[4] || 0), Number(m[5] || 0), Number(m[6] || 0)
  );
}

/** 空状态块 */
function emptyBox(msg, small) {
  return '<div class="empty' + (small ? ' sm' : '') + '">' + esc(msg) + '</div>';
}

/** 环形图 / 柱状图配色 */
const PALETTE = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff',
                 '#39c5cf', '#ff7b72', '#7ee787', '#ffa657', '#a5d6ff',
                 '#79c0ff', '#56d364'];

/** 策略 key -> 中文名（/api/strategies 未加载时的兜底） */
const STRATEGY_FALLBACK = {
  ma_volume:         { name: 'MaVolumeStrategy',        label: '均线放量' },
  turtle:            { name: 'TurtleTradeStrategy',     label: '海龟交易' },
  flag:              { name: 'HighTightFlagStrategy',   label: '高旗形整理' },
  shakeout:          { name: 'LimitUpShakeoutStrategy', label: '涨停洗盘' },
  limit_down:        { name: 'UptrendLimitDownStrategy',label: '上升趋势跌停' },
  rps:               { name: 'RpsBreakoutStrategy',     label: 'RPS极强动量' },
  private_placement: { name: 'PrivatePlacementStrategy',label: '定增监控' }
};

/** 任务类型 -> 中文名 */
const JOB_KIND_LABEL = {
  daily:    '日常选股（含飞书推送）',
  signals:  '只计算信号（不推送）',
  backfill: '回填数据',
  sync:     '同步当日最新数据',
  reload:   '整只重拉（删后重拉）'
};

/** 任务类型 -> 中文短名 */
const JOB_KIND_SHORT = {
  daily:    '日常选股',
  signals:  '信号计算',
  backfill: '数据回填',
  sync:     '当日同步',
  reload:   '整只重拉'
};

/* =======================================================================
 * 2. 全局状态 + API 封装
 * ======================================================================= */

const state = {
  tab: 'overview',
  /** 各 tab 是否已懒加载过 */
  loaded: { overview: false, signals: false, coverage: false, jobs: false, settings: false },
  overview: null,
  strategies: null,          // /api/strategies 的 strategies 数组
  strategiesPromise: null,
  signals: null,
  coverage: { filter: 'all', q: '', limit: 100, offset: 0, total: 0, busy: false },
  stock: { symbol: null, days: 250, data: null },
  jobs: {},                  // id -> job 对象
  poll: {},                  // id -> interval 句柄
  pollFail: {},              // id -> 连续失败次数
  inflight: {},              // 请求去重（如 overview）
  logFiles: [],
  logCurrent: null,
  suggestItems: [],
  suggestIndex: -1,
  /* ---- Tab6 设置 ---- */
  settings: {
    groups: [],              // /api/settings 的 groups
    config: {},              // /api/settings 的 config
    envPath: '',
    values: {},              // KEY -> 已保存的值（用于 diff / 放弃改动）
    dirty: {},               // KEY -> true（只记录改动过的 key）
    busy: false,
    probe: null,             // 数据源探测结果
    backups: [],             // /api/db/backups 的文件列表
    lastJob: {}              // 操作名 -> 最近一次提交的任务 {id,kind,status}
  }
};

/** 简易 API 封装：统一解析 JSON、统一抛错（调用方负责 showError） */
async function api(path, options) {
  const opt = Object.assign({ headers: {} }, options || {});
  if (opt.body !== undefined && typeof opt.body !== 'string') {
    opt.body = JSON.stringify(opt.body);
    opt.headers['Content-Type'] = 'application/json';
  }

  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    throw new Error('网络请求失败：' + path);
  }

  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = null; }
  }
  if (data === null || typeof data !== 'object') {
    throw new Error('接口 ' + path + ' 返回了非 JSON 内容（HTTP ' + res.status + '）');
  }
  if (data.ok === false) {
    throw new Error(data.error || ('接口 ' + path + ' 返回失败'));
  }
  if (!res.ok) {
    throw new Error('接口 ' + path + ' 请求失败（HTTP ' + res.status + '）');
  }
  return data;
}

/* -------- 错误条 / 轻提示 -------- */
let errorTimer = null;
/** 显示红色错误条（不白屏） */
function showError(msg) {
  const bar = $('#error-bar');
  if (!bar) return;
  bar.innerHTML = '<b>请求失败：</b>' + esc(msg);
  bar.hidden = false;
  if (errorTimer) clearTimeout(errorTimer);
  errorTimer = setTimeout(function () { bar.hidden = true; }, 8000);
}

let toastTimer = null;
/** 右下角轻提示 */
function showToast(msg, kind) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () { t.hidden = true; }, 3500);
}

/** 加载策略元数据（只请求一次） */
function ensureStrategies() {
  if (state.strategies) return Promise.resolve(state.strategies);
  if (state.strategiesPromise) return state.strategiesPromise;
  state.strategiesPromise = api('/api/strategies').then(function (data) {
    state.strategies = data.strategies || [];
    state.strategiesPromise = null;
    return state.strategies;
  }).catch(function (err) {
    state.strategiesPromise = null;
    // 兜底：用内置的 7 个策略，保证界面仍可用
    state.strategies = Object.keys(STRATEGY_FALLBACK).map(function (k) {
      return { key: k, name: STRATEGY_FALLBACK[k].name, label: STRATEGY_FALLBACK[k].label, desc: '' };
    });
    throw err;
  });
  return state.strategiesPromise;
}

/** 取策略显示名（key -> {name,label}） */
function strategyMeta(key) {
  const list = state.strategies || [];
  for (let i = 0; i < list.length; i++) {
    if (list[i].key === key) return list[i];
  }
  if (STRATEGY_FALLBACK[key]) {
    return { key: key, name: STRATEGY_FALLBACK[key].name, label: STRATEGY_FALLBACK[key].label, desc: '' };
  }
  return { key: key, name: key, label: key, desc: '' };
}

/* =======================================================================
 * 3. 路由与 Tab 切换（URL hash，刷新保持当前 tab，懒加载）
 * ======================================================================= */

const TABS = ['overview', 'signals', 'stock', 'coverage', 'jobs', 'settings'];

/** 从 location.hash 解析当前 tab（支持 #stock/600519 这种带股票代码的深链接） */
function tabFromHash() {
  const h = String(location.hash || '').replace(/^#/, '').split('/')[0];
  return TABS.indexOf(h) >= 0 ? h : 'overview';
}

/** 从 location.hash 解析深链接里的股票代码，例如 #stock/600519 → '600519' */
function symbolFromHash() {
  const parts = String(location.hash || '').replace(/^#/, '').split('/');
  return parts.length > 1 && /^\d{6}$/.test(parts[1]) ? parts[1] : '';
}

/** 切换 tab（同时更新导航高亮与视图显隐，并触发懒加载） */
function switchTab(name) {
  if (TABS.indexOf(name) < 0) name = 'overview';
  state.tab = name;

  TABS.forEach(function (t) {
    const view = document.querySelector('[data-view="' + t + '"]');
    if (view) view.hidden = (t !== name);
    const link = document.querySelector('.tab[data-tab="' + t + '"]');
    if (link) link.classList.toggle('active', t === name);
  });

  // 懒加载：其它 tab 首次进入时才拉数据
  if (name === 'overview' && !state.loaded.overview) {
    loadOverview().catch(function (e) { showError(e.message); });
  } else if (name === 'signals' && !state.loaded.signals) {
    loadSignals().catch(function (e) { showError(e.message); });
  } else if (name === 'coverage') {
    if (!state.loaded.coverage) {
      loadCoverage().catch(function (e) { showError(e.message); });
    }
    // 深链接 #coverage/check：进来就自动跑一次停牌核查
    if (String(location.hash).split('/')[1] === 'check' && !state.coverage.checked) {
      state.coverage.checked = true;
      checkSuspended().catch(function (e) { showError(e.message); });
    }
  } else if (name === 'jobs' && !state.loaded.jobs) {
    loadJobs().catch(function (e) { showError(e.message); });
  } else if (name === 'settings' && !state.loaded.settings) {
    loadSettings().catch(function (e) { showError(e.message); });
  } else if (name === 'stock') {
    // 支持 #stock/600519 深链接：只在代码变化时加载，避免重复请求
    const wanted = symbolFromHash();
    if (wanted && wanted !== state.stock.symbol) {
      loadStock(wanted).catch(function (e) { showError(e.message); });
    }
  }
}

/** 全局函数：从「选股结果」等位置跳转到「个股」tab 并加载该股票 */
function openStock(symbol) {
  if (!symbol) return;
  const target = '#stock/' + symbol;
  if (location.hash !== target) location.hash = target;
  switchTab('stock');
  loadStock(symbol).catch(function (e) { showError(e.message); });
}
window.openStock = openStock;

/* =======================================================================
 * 4. 通用 SVG 图表（环形图 / 横向柱状图）
 * ======================================================================= */

/**
 * SVG 环形图（circle + stroke-dasharray），中心显示总数
 * @param {Array<{label:string,value:number}>} items
 * @returns {string} HTML 片段（含图例）
 */
function donutChart(items) {
  const list = (items || []).filter(function (d) { return Number(d.value) > 0; });
  const total = list.reduce(function (s, d) { return s + Number(d.value); }, 0);
  if (!list.length || total <= 0) return emptyBox('暂无数据');

  const S = 190, C = S / 2, R = 66, SW = 24;
  const CIRC = 2 * Math.PI * R;

  let offset = 0;
  let segs = '';
  list.forEach(function (d, i) {
    const val = Number(d.value);
    const len = (val / total) * CIRC;
    const color = PALETTE[i % PALETTE.length];
    segs += '<circle cx="' + C + '" cy="' + C + '" r="' + R + '" fill="none"'
          + ' stroke="' + color + '" stroke-width="' + SW + '"'
          + ' stroke-dasharray="' + len.toFixed(3) + ' ' + (CIRC - len).toFixed(3) + '"'
          + ' stroke-dashoffset="' + (-offset).toFixed(3) + '"'
          + ' transform="rotate(-90 ' + C + ' ' + C + ')"></circle>';
    offset += len;
  });

  let legend = '';
  list.forEach(function (d, i) {
    const val = Number(d.value);
    const pct = (val / total * 100).toFixed(1);
    legend += '<div class="legend-row">'
            + '<span class="legend-dot" style="background:' + PALETTE[i % PALETTE.length] + '"></span>'
            + '<span class="legend-name" title="' + esc(d.label) + '">' + esc(d.label) + '</span>'
            + '<span class="legend-val">' + esc(fmt.num(val)) + '</span>'
            + '<span class="legend-pct">' + pct + '%</span>'
            + '</div>';
  });

  const svg = '<svg class="donut-svg" viewBox="0 0 ' + S + ' ' + S + '" role="img">'
            + '<circle cx="' + C + '" cy="' + C + '" r="' + R + '" fill="none" stroke="#21262d" stroke-width="' + SW + '"></circle>'
            + segs
            + '<text x="' + C + '" y="' + (C - 2) + '" text-anchor="middle" fill="#e6edf3"'
            + ' style="font-family:var(--mono);font-size:22px;font-weight:600">' + esc(fmt.num(total)) + '</text>'
            + '<text x="' + C + '" y="' + (C + 16) + '" text-anchor="middle" fill="#8b949e"'
            + ' style="font-family:var(--sans);font-size:11px">合计</text>'
            + '</svg>';

  return '<div class="donut-block">' + svg + '<div class="donut-legend">' + legend + '</div></div>';
}

/**
 * SVG 横向柱状图
 * @param {Array<{label:string,value:number}>} items
 * @param {{labelW?:number,max?:number}} [opts]
 */
function hbarChart(items, opts) {
  const list = (items || []).slice();
  if (!list.length) return emptyBox('暂无数据');
  opts = opts || {};

  const W = 660;
  const rowH = 22, gap = 9;
  const labelW = opts.labelW || 120;
  const valueW = 92;
  const H = list.length * (rowH + gap) + 6;
  const barX = labelW + 8;
  const barMax = W - barX - valueW;
  const max = Math.max(1, list.reduce(function (m, d) { return Math.max(m, Number(d.value) || 0); }, 0));

  let body = '';
  list.forEach(function (d, i) {
    const val = Number(d.value) || 0;
    const w = Math.max(1, (val / max) * barMax);
    const y = i * (rowH + gap) + 3;
    const color = PALETTE[i % PALETTE.length];
    body += '<text class="hbar-txt" x="' + (labelW) + '" y="' + (y + rowH / 2 + 4) + '" text-anchor="end"'
          + ' fill="#8b949e">' + esc(truncate(d.label, 12)) + '</text>';
    body += '<rect x="' + barX + '" y="' + y + '" width="' + barMax + '" height="' + rowH + '" rx="3" fill="#21262d"></rect>';
    body += '<rect x="' + barX + '" y="' + y + '" width="' + w.toFixed(1) + '" height="' + rowH + '" rx="3" fill="' + color + '"></rect>';
    body += '<text class="hbar-txt" x="' + (barX + w + 8) + '" y="' + (y + rowH / 2 + 4) + '"'
          + ' fill="#e6edf3">' + esc(fmt.num(val)) + '</text>';
  });

  return '<svg class="hbar-svg" viewBox="0 0 ' + W + ' ' + H + '" role="img">' + body + '</svg>';
}

/** 字符串截断（用于图表标签） */
function truncate(s, n) {
  const str = String(s === null || s === undefined ? '' : s);
  return str.length > n ? str.slice(0, n - 1) + '…' : str;
}

/* =======================================================================
 * 5. Tab1 总览
 * ======================================================================= */

/** 加载并渲染总览（首次进入或手动刷新） */
async function loadOverview(force) {
  if (state.loaded.overview && !force) return;
  if (state.inflight.overview) return state.inflight.overview;   // 防重复请求
  state.inflight.overview = (async function () {
    const data = await api('/api/overview');
    state.overview = data;
    state.loaded.overview = true;
    renderOverview(data);

    // 同步顶部 Header
    const db = data.db || {};
    const hdDate = $('#hd-date');
    const hdSym = $('#hd-symbols');
    if (hdDate) hdDate.textContent = fmt.date(db.max_date);
    if (hdSym) hdSym.textContent = fmt.num(db.symbols);
  })();
  try {
    await state.inflight.overview;
  } finally {
    state.inflight.overview = null;
  }
}

/** 渲染总览页全部内容 */
function renderOverview(d) {
  const db = d.db || {};
  const cfg = d.config || {};
  const sources = d.sources || [];
  const lastDates = d.last_dates || [];
  const boards = d.boards || [];
  const tasks = d.tasks || [];
  const sig = d.signals || {};

  /* ---- 统计卡片 ---- */
  const cards = [
    {
      label: '股票总数',
      value: fmt.num(db.symbols),
      sub: db.target_symbols !== undefined && db.target_symbols !== null
        ? ('目标 ' + fmt.num(db.target_symbols) + ' 只') : ''
    },
    {
      label: '总行数',
      value: fmt.money(db.rows),
      sub: fmt.num(db.rows) + ' 行'
    },
    {
      label: '数据区间',
      value: fmt.date(db.min_date),
      sub: '至 ' + fmt.date(db.max_date),
      small: true
    },
    {
      label: '数据库大小',
      value: fmt.size(db.size_mb),
      sub: db.path || ''
    },
    {
      label: '数据源数量',
      value: fmt.num(sources.length),
      sub: sources.map(function (s) { return s.name; }).join(' / ')
    },
    {
      label: '最近选股信号数',
      value: fmt.num(sig.total || 0),
      sub: sig.data_date ? ('数据日期 ' + fmt.date(sig.data_date)) : '尚未计算'
    }
  ];

  const statsEl = $('#ov-stats');
  if (statsEl) {
    statsEl.innerHTML = cards.map(function (c) {
      return '<div class="stat-card">'
           +   '<div class="stat-label">' + esc(c.label) + '</div>'
           +   '<div class="stat-value' + (c.small ? ' small' : '') + '" title="' + esc(c.value) + '">' + esc(c.value) + '</div>'
           +   '<div class="stat-sub" title="' + esc(c.sub) + '">' + esc(c.sub) + '</div>'
           + '</div>';
    }).join('');
  }

  /* ---- 数据源分布：环形图 ---- */
  const srcEl = $('#ov-sources');
  if (srcEl) {
    srcEl.innerHTML = donutChart(sources.map(function (s) {
      return { label: s.name, value: Number(s.count) || 0 };
    }));
  }

  /* ---- 最新数据日期分布：横向柱状图（取前 10，其余归入「其他」） ---- */
  const dateEl = $('#ov-lastdates');
  if (dateEl) {
    dateEl.innerHTML = hbarChart(topN(lastDates.map(function (x) {
      return { label: fmt.date(x.date), value: Number(x.count) || 0 };
    }), 10, '其他日期'), { labelW: 110 });
  }

  /* ---- 板块分布：横向柱状图（取前 12，其余归入「其他」） ---- */
  const boardEl = $('#ov-boards');
  if (boardEl) {
    boardEl.innerHTML = hbarChart(topN(boards.map(function (x) {
      return { label: x.name, value: Number(x.count) || 0 };
    }), 12, '其他板块'), { labelW: 120 });
  }

  /* ---- 计划任务表格 ---- */
  const taskEl = $('#ov-tasks');
  if (taskEl) {
    if (!tasks.length) {
      taskEl.innerHTML = emptyBox('暂无计划任务');
    } else {
      taskEl.innerHTML = '<div class="table-wrap"><table class="tbl">'
        + '<thead><tr><th>任务名</th><th>状态</th><th>下次运行</th><th>上次运行</th><th>上次结果</th></tr></thead><tbody>'
        + tasks.map(function (t) {
            return '<tr>'
              + '<td>' + esc(t.name) + '</td>'
              + '<td>' + taskStateBadge(t.state) + '</td>'
              + '<td class="num">' + esc(fmt.dt(t.next_run)) + '</td>'
              + '<td class="num">' + esc(fmt.dt(t.last_run)) + '</td>'
              + '<td>' + taskResultCell(t.last_result, t.last_result_text) + '</td>'
              + '</tr>';
          }).join('')
        + '</tbody></table></div>';
    }
  }

  /* ---- 当前配置 ---- */
  const cfgEl = $('#ov-config');
  if (cfgEl) {
    const rows = [
      ['数据源', cfg.data_source],
      ['auto 顺序', cfg.auto_source_order],
      ['并发', cfg.workers],
      ['间隔', cfg.delay !== undefined && cfg.delay !== null ? cfg.delay + ' 秒' : null],
      ['起始日期', cfg.start_date],
      ['回填落后天数', cfg.rebase_stale_days]
    ];
    cfgEl.innerHTML = '<div class="kv">' + rows.map(function (r) {
      const v = (r[1] === undefined || r[1] === null || r[1] === '') ? '--' : String(r[1]);
      return '<div class="kv-row">'
           +   '<span class="kv-k">' + esc(r[0]) + '</span>'
           +   '<span class="kv-v mono" title="' + esc(v) + '">' + esc(v) + '</span>'
           + '</div>';
    }).join('') + '</div>';
  }
}

/** 取前 n 项，其余合并为「其他」 */
function topN(items, n, otherLabel) {
  const sorted = items.slice().sort(function (a, b) { return b.value - a.value; });
  if (sorted.length <= n) return sorted;
  const head = sorted.slice(0, n);
  const restSum = sorted.slice(n).reduce(function (s, d) { return s + d.value; }, 0);
  if (restSum > 0) head.push({ label: otherLabel || '其他', value: restSum });
  return head;
}

/** 计划任务状态徽章 */
function taskStateBadge(s) {
  const map = {
    'ready':    ['就绪', 'badge-green'],
    'running':  ['运行中', 'badge-blue'],
    'disabled': ['已禁用', 'badge-grey'],
    'queued':   ['排队中', 'badge-yellow'],
    'unknown':  ['未知', 'badge-grey']
  };
  const key = String(s === null || s === undefined ? '' : s).toLowerCase();
  const hit = map[key];
  if (hit) return '<span class="badge ' + hit[1] + '">' + esc(hit[0]) + '</span>';
  return '<span class="badge badge-grey">' + esc(s === null || s === undefined ? '--' : s) + '</span>';
}

/** 计划任务上次结果：0 显示绿色「成功」；后端给的 last_result_text 优先 */
function taskResultCell(v, text) {
  if (text) {
    // 「尚未运行」「已禁用」等中性状态不要标红
    if (text === '成功' || text === '正在运行') {
      return '<span class="ok-text">' + esc(text) + '</span>';
    }
    if (text === '尚未运行' || text === '已禁用' || text === '未计划' || text === '无更多运行') {
      return '<span class="muted">' + esc(text) + '</span>';
    }
    return '<span class="err-text">' + esc(text) + '</span>';
  }
  if (v === null || v === undefined || v === '') return '<span class="muted">--</span>';
  const n = Number(v);
  if (n === 0) return '<span class="ok-text">成功</span>';
  return '<span class="err-text">失败（' + esc(String(v)) + '）</span>';
}

/* =======================================================================
 * 6. Tab2 选股结果
 * ======================================================================= */

/** 加载选股结果（策略元数据 + 信号） */
async function loadSignals(force) {
  if (state.loaded.signals && !force) return;

  // 策略元数据失败不阻塞信号渲染
  let strategies = null;
  try {
    strategies = await ensureStrategies();
  } catch (e) {
    strategies = state.strategies || [];
    showError('策略列表加载失败：' + e.message);
  }

  const sg = await api('/api/signals');
  state.signals = sg;
  state.loaded.signals = true;
  renderSignals(sg, strategies || []);
}

/** 渲染选股结果：按策略分组卡片 */
function renderSignals(sg, strategies) {
  const metaEl = $('#sig-meta');
  if (metaEl) {
    if (sg.generated_at) {
      metaEl.textContent = '生成时间 ' + fmt.dt(sg.generated_at)
        + ' · 数据日期 ' + fmt.date(sg.data_date)
        + ' · 共 ' + fmt.num(sg.total || 0) + ' 只';
    } else {
      metaEl.textContent = '尚未计算过信号（点击右侧按钮重新计算）';
    }
  }

  // 以 /api/strategies 的 7 个策略为基准，再合并信号里出现的额外分组
  const groups = sg.groups || [];
  const byKey = {};
  groups.forEach(function (g) { byKey[g.key] = g; });

  const keys = [];
  (strategies || []).forEach(function (s) {
    if (s && s.key && keys.indexOf(s.key) < 0) keys.push(s.key);
  });
  groups.forEach(function (g) {
    if (g && g.key && keys.indexOf(g.key) < 0) keys.push(g.key);
  });

  const wrap = $('#sig-groups');
  if (!wrap) return;

  if (!keys.length) {
    wrap.innerHTML = emptyBox('暂无策略数据');
    return;
  }

  wrap.innerHTML = keys.map(function (key) {
    const g = byKey[key];
    const meta = (function () {
      for (let i = 0; i < (strategies || []).length; i++) {
        if (strategies[i].key === key) return strategies[i];
      }
      return strategyMeta(key);
    })();

    const symbols = (g && g.symbols) ? g.symbols : [];
    const label = (g && g.label) || meta.label || key;
    const cls = (g && g.name) || meta.name || key;
    const desc = meta.desc || '';
    const err = g && g.error ? String(g.error) : '';

    let body;
    if (err) {
      body = '<div class="err-inline">策略执行错误：' + esc(err) + '</div>';
    } else if (!symbols.length) {
      body = '<div class="muted" style="font-size:13px">无信号</div>';
    } else {
      body = '<div class="sym-list">' + symbols.map(function (s) {
        return '<button type="button" class="sym-tag" data-symbol="' + esc(s.symbol) + '">'
             +   '<span class="code">' + esc(s.symbol) + '</span>'
             +   '<span class="nm">' + esc(s.name || '') + '</span>'
             + '</button>';
      }).join('') + '</div>';
    }

    return '<div class="card strat-card" data-key="' + esc(key) + '">'
         +   '<div class="card-head" data-toggle="1">'
         +     '<div class="strat-title">'
         +       '<span class="fold">▼</span>'
         +       '<h3>' + esc(label) + '</h3>'
         +       '<code class="cls">' + esc(cls) + '</code>'
         +       '<span class="badge ' + (symbols.length ? 'badge-blue' : 'badge-grey') + '">'
         +         fmt.num(symbols.length) + ' 个信号</span>'
         +       (desc ? '<span class="strat-desc">' + esc(desc) + '</span>' : '')
         +     '</div>'
         +   '</div>'
         +   '<div class="card-body">' + body + '</div>'
         + '</div>';
  }).join('');

  // 折叠：点击卡片头部
  wrap.querySelectorAll('.strat-card .card-head').forEach(function (head) {
    head.addEventListener('click', function () {
      const card = head.parentNode;
      if (card) card.classList.toggle('collapsed');
    });
  });

  // 点击股票标签 -> 跳转「个股」tab 并加载
  wrap.querySelectorAll('.sym-tag').forEach(function (btn) {
    btn.addEventListener('click', function (ev) {
      ev.stopPropagation();
      openStock(btn.getAttribute('data-symbol'));
    });
  });
}

/* =======================================================================
 * 7. Tab3 个股
 * ======================================================================= */

/** 加载个股详情 */
async function loadStock(symbol, days) {
  const d = days || state.stock.days || 250;
  const data = await api('/api/stock/' + encodeURIComponent(symbol) + '?days=' + encodeURIComponent(d));

  // 数值统一转 number，便于绘图
  const bars = (data.bars || []).map(function (b) {
    return {
      date: b.date,
      open: Number(b.open), high: Number(b.high), low: Number(b.low), close: Number(b.close),
      volume: Number(b.volume) || 0,
      turnover: Number(b.turnover) || 0
    };
  });

  state.stock = { symbol: data.symbol || symbol, days: d, data: data, bars: bars };

  const input = $('#stock-q');
  if (input) input.value = (data.symbol || symbol) + ' ' + (data.name || '');

  const range = $('#stock-range');
  if (range) {
    range.querySelectorAll('button').forEach(function (b) {
      b.classList.toggle('active', Number(b.getAttribute('data-days')) === Number(d));
    });
  }

  renderStock(data, bars);
}

/** 渲染个股详情（头部 / 统计 / K 线 / 最近 30 日表格） */
function renderStock(d, bars) {
  const wrap = $('#stock-detail');
  if (!wrap) return;

  const st = d.stats || {};
  const hitSignals = d.signals || [];

  const sigTags = hitSignals.length
    ? hitSignals.map(function (k) {
        const m = strategyMeta(k);
        return '<span class="badge badge-blue" title="' + esc(m.name) + '">' + esc(m.label) + '</span>';
      }).join('')
    : '<span class="badge badge-grey">未命中任何策略</span>';

  const metaBits = [
    ['数据源', d.source],
    ['所属板块', d.board],
    ['最新日期', fmt.date(d.last_date)]
  ];

  let html = '';

  /* ---- 头部卡片：名称 / 代码 / 元信息 / 命中策略 ---- */
  html += '<div class="card"><div class="card-body">'
        +   '<div class="stock-head">'
        +     '<span class="stock-name">' + esc(d.name || '未知股票') + '</span>'
        +     '<span class="stock-code">' + esc(d.symbol || '') + '</span>'
        +     metaBits.map(function (m) {
              return '<span class="badge badge-grey">' + esc(m[0]) + '：' + esc(m[1] === null || m[1] === undefined || m[1] === '' ? '--' : m[1]) + '</span>';
            }).join('')
        +     '<span class="stock-tags">' + sigTags + '</span>'
        +   '</div>'
        + '</div></div>';

  /* ---- 一行统计：最新收盘 / 当日涨跌幅 / 20日涨跌幅 / 量比 ---- */
  const statCards = [
    { label: '最新收盘', value: fmt.price(st.close), cls: '' },
    { label: '当日涨跌幅', value: fmt.pct(st.chg_1d), cls: fmt.dirClass(st.chg_1d) },
    { label: '20 日涨跌幅', value: fmt.pct(st.chg_20d), cls: fmt.dirClass(st.chg_20d) },
    { label: '量比', value: st.vol_ratio === null || st.vol_ratio === undefined ? '--' : fmt.num(st.vol_ratio, 2), cls: '' }
  ];
  html += '<div class="stat-row">' + statCards.map(function (c) {
    return '<div class="stat-card">'
         +   '<div class="stat-label">' + esc(c.label) + '</div>'
         +   '<div class="stat-value ' + c.cls + '">' + esc(c.value) + '</div>'
         + '</div>';
  }).join('') + '</div>';

  /* ---- 主图：后复权日 K 线 + 成交量 ---- */
  html += '<div class="card">'
        +   '<div class="card-head">'
        +     '<h2>后复权日 K 线</h2>'
        +     '<div class="chart-legend">'
        +       '<span><i style="background:#f85149"></i>涨（收 ≥ 开）</span>'
        +       '<span><i style="background:#3fb950"></i>跌</span>'
        +       '<span><i style="background:#e3b341"></i>MA5</span>'
        +       '<span><i style="background:#58a6ff"></i>MA10</span>'
        +       '<span><i style="background:#bc8cff"></i>MA20</span>'
        +     '</div>'
        +   '</div>'
        +   '<div class="card-body"><div class="chart-wrap" id="kline-wrap">'
        +     (bars.length ? '' : emptyBox('暂无 K 线数据'))
        +   '</div></div>'
        + '</div>';

  /* ---- 最近 30 个交易日表格 ---- */
  const recent = bars.slice(-30).reverse();
  html += '<div class="card">'
        +   '<div class="card-head"><h2>最近 30 个交易日</h2>'
        +     '<span class="hint">最新在前</span></div>'
        +   '<div class="card-body">';
  if (!recent.length) {
    html += emptyBox('暂无数据');
  } else {
    html += '<div class="table-wrap"><table class="tbl">'
          + '<thead><tr><th>日期</th><th class="num">开盘</th><th class="num">最高</th>'
          + '<th class="num">最低</th><th class="num">收盘</th><th class="num">涨跌幅</th>'
          + '<th class="num">成交量</th><th class="num">成交额</th></tr></thead><tbody>'
          + recent.map(function (b) {
              const pct = b.open ? ((b.close - b.open) / b.open * 100) : 0;
              return '<tr>'
                + '<td class="num">' + esc(fmt.date(b.date)) + '</td>'
                + '<td class="num">' + esc(fmt.price(b.open)) + '</td>'
                + '<td class="num">' + esc(fmt.price(b.high)) + '</td>'
                + '<td class="num">' + esc(fmt.price(b.low)) + '</td>'
                + '<td class="num ' + fmt.dirClass(b.close - b.open) + '">' + esc(fmt.price(b.close)) + '</td>'
                + '<td class="num ' + fmt.dirClass(pct) + '">' + esc(fmt.pct(pct)) + '</td>'
                + '<td class="num">' + esc(fmt.num(b.volume)) + '</td>'
                + '<td class="num">' + esc(fmt.money(b.turnover)) + '</td>'
                + '</tr>';
            }).join('')
          + '</tbody></table></div>';
  }
  html += '</div></div>';

  wrap.innerHTML = html;

  // 绘制 K 线并绑定悬停
  if (bars.length) {
    const holder = $('#kline-wrap');
    if (holder) mountKline(holder, bars);
  }
}

/**
 * 手写 SVG K 线图：主图（价格 + MA5/10/20）+ 下方 1/4 高度成交量柱状子图
 * x 轴按交易日等距排布，y 轴按价格范围自适应，红涨绿跌
 */
function mountKline(holder, bars) {
  const W = 1200;                 // SVG 内部坐标系宽度（响应式缩放）
  const ML = 70, MR = 18, MT = 16;
  const priceH = 288;
  const volH = 92;
  const gapY = 24;
  const priceTop = MT;
  const priceBottom = priceTop + priceH;
  const volTop = priceBottom + gapY;
  const volBottom = volTop + volH;
  const H = volBottom + 28;
  const plotW = W - ML - MR;

  const n = bars.length;
  const step = plotW / n;
  const bw = Math.max(1, Math.min(step * 0.68, 16));
  const xC = function (i) { return ML + (i + 0.5) * step; };

  // ---- 价格区间（含 4% 留白）----
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < n; i++) {
    if (isFinite(bars[i].low)) lo = Math.min(lo, bars[i].low);
    if (isFinite(bars[i].high)) hi = Math.max(hi, bars[i].high);
  }
  if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
  if (hi <= lo) hi = lo + 1;
  const padP = (hi - lo) * 0.04;
  lo -= padP; hi += padP;

  const yP = function (v) { return priceBottom - (v - lo) / (hi - lo) * priceH; };

  // ---- 成交量区间 ----
  let volMax = 0;
  for (let i = 0; i < n; i++) volMax = Math.max(volMax, bars[i].volume);
  if (volMax <= 0) volMax = 1;
  const yV = function (v) { return volBottom - (v / volMax) * volH; };

  // ---- 均线 ----
  const ma5 = maSeries(bars, 5);
  const ma10 = maSeries(bars, 10);
  const ma20 = maSeries(bars, 20);

  let s = '';
  s += '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="K 线图">';

  // 价格网格 + 左侧刻度
  for (let k = 0; k <= 4; k++) {
    const v = lo + (hi - lo) * (k / 4);
    const y = yP(v);
    s += '<line class="k-grid" x1="' + ML + '" y1="' + y.toFixed(1) + '" x2="' + (W - MR) + '" y2="' + y.toFixed(1) + '"></line>';
    s += '<text class="k-label k-label-r" x="' + (ML - 8) + '" y="' + (y + 4).toFixed(1) + '">' + esc(fmt.price(v)) + '</text>';
  }

  // 成交量区域：底色 + 顶部分隔线 + 最大量刻度
  s += '<rect x="' + ML + '" y="' + volTop + '" width="' + plotW + '" height="' + volH + '" fill="rgba(255,255,255,.015)"></rect>';
  s += '<line class="k-axis" x1="' + ML + '" y1="' + volTop + '" x2="' + (W - MR) + '" y2="' + volTop + '"></line>';
  s += '<text class="k-label" x="' + (ML - 8) + '" y="' + (volTop + 11) + '" text-anchor="end">' + esc(fmt.money(volMax)) + '</text>';
  s += '<text class="k-label" x="' + (ML - 8) + '" y="' + (volBottom) + '" text-anchor="end">0</text>';
  s += '<text class="k-label" x="' + (ML + 4) + '" y="' + (volTop - 6) + '">成交量</text>';

  // ---- 蜡烛 + 成交量柱 ----
  let candles = '';
  let vols = '';
  for (let i = 0; i < n; i++) {
    const b = bars[i];
    const isUp = b.close >= b.open;          // A 股：收 >= 开 用红
    const color = isUp ? '#f85149' : '#3fb950';
    const x = xC(i);
    const yHigh = yP(b.high);
    const yLow = yP(b.low);
    const yOpen = yP(b.open);
    const yClose = yP(b.close);

    // 影线
    candles += '<line x1="' + x.toFixed(1) + '" y1="' + yHigh.toFixed(1) + '" x2="' + x.toFixed(1)
             + '" y2="' + yLow.toFixed(1) + '" stroke="' + color + '" stroke-width="1"></line>';

    // 实体（开==收 时画 1px 细线）
    const top = Math.min(yOpen, yClose);
    const bodyH = Math.max(1, Math.abs(yClose - yOpen));
    candles += '<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + bw.toFixed(1)
             + '" height="' + bodyH.toFixed(1) + '" fill="' + color + '"></rect>';

    // 成交量柱
    const vh = Math.max(0.6, (b.volume / volMax) * volH);
    vols += '<rect x="' + (x - bw / 2).toFixed(1) + '" y="' + (volBottom - vh).toFixed(1) + '" width="' + bw.toFixed(1)
          + '" height="' + vh.toFixed(1) + '" fill="' + color + '" opacity="0.75"></rect>';
  }
  s += vols + candles;

  // ---- 均线（细线）----
  s += maPath(ma5, xC, yP, '#e3b341');
  s += maPath(ma10, xC, yP, '#58a6ff');
  s += maPath(ma20, xC, yP, '#bc8cff');

  // ---- x 轴日期刻度（按交易日等距，不按自然日）----
  const tickCount = Math.min(8, n);
  for (let k = 0; k < tickCount; k++) {
    const i = tickCount === 1 ? 0 : Math.round(k * (n - 1) / (tickCount - 1));
    const label = (k === 0 ? fmt.date(bars[i].date) : fmt.date(bars[i].date).slice(5));
    // 首尾刻度贴边对齐，避免文字超出绘图区
    let anchor = 'middle';
    let tx = xC(i);
    if (k === 0) { anchor = 'start'; tx = ML; }
    else if (k === tickCount - 1 && tickCount > 1) { anchor = 'end'; tx = W - MR; }
    s += '<text class="k-label" x="' + tx.toFixed(1) + '" y="' + (H - 8) + '" text-anchor="' + anchor + '">'
       + esc(label) + '</text>';
  }

  // ---- 悬停层：竖线 + 高亮块 + 命中区 ----
  s += '<rect class="k-hl" x="0" y="' + priceTop + '" width="0" height="' + (volBottom - priceTop) + '" style="display:none"></rect>';
  s += '<line class="k-crosshair" x1="0" y1="' + priceTop + '" x2="0" y2="' + volBottom + '" style="display:none"></line>';
  s += '<rect class="k-hit" x="' + ML + '" y="' + priceTop + '" width="' + plotW + '" height="' + (volBottom - priceTop) + '"></rect>';
  s += '</svg>';

  holder.innerHTML = s;

  // ---- tooltip（跟随鼠标的 div）----
  const tip = document.createElement('div');
  tip.className = 'chart-tip';
  tip.hidden = true;
  holder.appendChild(tip);

  const svg = holder.querySelector('svg');
  const hit = holder.querySelector('.k-hit');
  const cross = holder.querySelector('.k-crosshair');
  const hl = holder.querySelector('.k-hl');
  if (!svg || !hit) return;

  function hide() {
    tip.hidden = true;
    cross.style.display = 'none';
    hl.style.display = 'none';
  }

  hit.addEventListener('mousemove', function (ev) {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const scale = rect.width / W;              // 等比缩放系数
    const px = (ev.clientX - rect.left) / scale;
    let i = Math.floor((px - ML) / step);
    if (i < 0) i = 0;
    if (i > n - 1) i = n - 1;

    const b = bars[i];
    const x = xC(i);
    cross.setAttribute('x1', x.toFixed(1));
    cross.setAttribute('x2', x.toFixed(1));
    cross.style.display = '';
    hl.setAttribute('x', (x - step / 2).toFixed(1));
    hl.setAttribute('width', step.toFixed(1));
    hl.style.display = '';

    const chg = b.open ? (b.close - b.open) / b.open * 100 : 0;
    tip.innerHTML =
        '<div class="tip-date">' + esc(fmt.date(b.date)) + '</div>'
      + '<div class="tip-row"><span>开盘</span><b>' + esc(fmt.price(b.open)) + '</b></div>'
      + '<div class="tip-row"><span>最高</span><b>' + esc(fmt.price(b.high)) + '</b></div>'
      + '<div class="tip-row"><span>最低</span><b>' + esc(fmt.price(b.low)) + '</b></div>'
      + '<div class="tip-row"><span>收盘</span><b class="' + fmt.dirClass(b.close - b.open) + '">' + esc(fmt.price(b.close)) + '</b></div>'
      + '<div class="tip-row"><span>涨跌</span><b class="' + fmt.dirClass(chg) + '">' + esc(fmt.pct(chg)) + '</b></div>'
      + '<div class="tip-row"><span>成交量</span><b>' + esc(fmt.num(b.volume)) + '</b></div>'
      + '<div class="tip-row"><span>成交额</span><b>' + esc(fmt.money(b.turnover)) + '</b></div>';
    tip.hidden = false;

    // 定位：默认在鼠标右侧，越界则翻到左侧
    const wrapRect = holder.getBoundingClientRect();
    const tw = tip.offsetWidth;
    const th = tip.offsetHeight;
    let left = ev.clientX - wrapRect.left + 16;
    if (left + tw > wrapRect.width - 4) left = ev.clientX - wrapRect.left - tw - 16;
    if (left < 4) left = 4;
    let top = ev.clientY - wrapRect.top + 14;
    if (top + th > wrapRect.height - 4) top = Math.max(4, wrapRect.height - th - 4);
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
  });

  hit.addEventListener('mouseleave', hide);
  svg.addEventListener('mouseleave', hide);
}

/** 计算简单移动平均序列（前 period-1 个点为 null） */
function maSeries(bars, period) {
  const out = new Array(bars.length).fill(null);
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

/** 生成一条均线的 SVG path */
function maPath(series, xC, yP, color) {
  let d = '';
  let started = false;
  for (let i = 0; i < series.length; i++) {
    const v = series[i];
    if (v === null || !isFinite(v)) { started = false; continue; }
    d += (started ? ' L ' : ' M ') + xC(i).toFixed(1) + ' ' + yP(v).toFixed(1);
    started = true;
  }
  if (!d) return '';
  return '<path d="' + d + '" fill="none" stroke="' + color + '" stroke-width="1.1" opacity="0.9"></path>';
}

/* =======================================================================
 * 8. Tab4 数据覆盖
 * ======================================================================= */

/** 加载数据覆盖表格（filter / q / limit / offset） */
/**
 * 核查「数据落后」的股票到底是真停牌还是我们漏了数据。
 * 用新浪实时行情：状态码 00 = 正常交易，03 = 停牌；停牌股成交量恒为 0。
 */
async function checkSuspended() {
  const btn = $('#cov-check');
  const box = $('#cov-check-result');
  if (!box) return;

  if (btn) { btn.disabled = true; btn.textContent = '核查中…'; }
  box.hidden = false;
  box.innerHTML = '<div class="muted">正在用新浪实时行情核查…</div>';

  try {
    const data = await api('/api/suspended-check');
    const rows = data.rows || [];

    if (!rows.length) {
      box.innerHTML = '<div class="ok-text">所有股票都是最新的，没有需要核查的股票。</div>';
      return;
    }

    let html = '<div class="check-summary">'
             +   '核查 <b>' + fmt.num(data.checked) + '</b> 只：'
             +   '<span class="ok-text">停牌 ' + fmt.num(data.suspended) + ' 只（数据没问题）</span>'
             +   (data.missing > 0
                    ? '，<span class="err-text">有成交 ' + fmt.num(data.missing) + ' 只（库里可能漏数据！）</span>'
                    : '')
             +   (data.unknown > 0 ? '，<span class="muted">无法判断 ' + fmt.num(data.unknown) + ' 只</span>' : '')
             + '</div>';

    html += '<div class="table-wrap"><table class="tbl"><thead><tr>'
          +   '<th>代码</th><th>名称</th><th class="num">行情日期</th>'
          +   '<th class="num">最新价</th><th class="num">昨收</th>'
          +   '<th class="num">成交量</th><th>状态码</th><th>结论</th>'
          + '</tr></thead><tbody>';

    rows.forEach(function (r) {
      const ok = r.suspended === true;
      const bad = r.suspended === false;
      html += '<tr' + (bad ? ' class="row-warn"' : '') + '>'
            +   '<td class="mono">' + esc(r.symbol) + '</td>'
            +   '<td>' + esc(r.name || '--') + '</td>'
            +   '<td class="num mono">' + esc(r.quote_date || '--') + '</td>'
            +   '<td class="num mono">' + (r.price === undefined ? '--' : esc(String(r.price))) + '</td>'
            +   '<td class="num mono">' + (r.prev_close === undefined ? '--' : esc(String(r.prev_close))) + '</td>'
            +   '<td class="num mono">' + (r.volume === undefined ? '--' : esc(fmt.num(r.volume))) + '</td>'
            +   '<td class="mono">' + esc(r.flag === undefined ? '--' : String(r.flag)) + '</td>'
            +   '<td>' + (ok ? '<span class="ok-text">停牌</span>'
                          : bad ? '<span class="err-text">有成交，需回填</span>'
                                : '<span class="muted">' + esc(r.note || '未知') + '</span>') + '</td>'
            + '</tr>';
    });

    html += '</tbody></table></div>';
    box.innerHTML = html;
  } catch (e) {
    box.innerHTML = '<div class="err-text">核查失败：' + esc(e.message) + '</div>';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '核查停牌'; }
  }
}

async function loadCoverage() {
  const c = state.coverage;
  if (c.busy) return;
  c.busy = true;

  const url = '/api/coverage?filter=' + encodeURIComponent(c.filter)
            + '&q=' + encodeURIComponent(c.q)
            + '&limit=' + encodeURIComponent(c.limit)
            + '&offset=' + encodeURIComponent(c.offset);

  try {
    const data = await api(url);
    c.total = Number(data.total) || 0;
    renderCoverage(data);
    state.loaded.coverage = true;
  } finally {
    c.busy = false;
  }
}

/** 渲染数据覆盖表格 + 分页 */
function renderCoverage(data) {
  const c = state.coverage;
  const rows = data.rows || [];
  const totalEl = $('#cov-total');
  if (totalEl) totalEl.textContent = '共 ' + fmt.num(c.total) + ' 只';

  const box = $('#cov-table');
  if (!box) return;

  if (!rows.length) {
    box.innerHTML = emptyBox('没有符合条件的数据');
    return;
  }

  let html = '<div class="table-wrap"><table class="tbl">'
           + '<thead><tr>'
           +   '<th>代码</th><th>名称</th><th class="num">最新日期</th>'
           +   '<th class="num">落后交易日数</th><th class="num">行数</th>'
           +   '<th>数据源</th><th>板块</th>'
           + '</tr></thead><tbody>';

  rows.forEach(function (r) {
    const stale = (r.stale_days === null || r.stale_days === undefined) ? null : Number(r.stale_days);
    const isWarn = stale === null || stale > 0;      // 落后或缺失 -> 黄色标记
    let staleCell;
    if (stale === null) {
      staleCell = '<span class="warn-text">缺失</span>';
    } else if (stale > 0) {
      staleCell = '<span class="warn-text">' + esc(fmt.num(stale)) + '</span>';
    } else {
      staleCell = '<span class="muted">0</span>';
    }

    html += '<tr' + (isWarn ? ' class="row-warn"' : '') + '>'
          +   '<td class="num">' + esc(r.symbol) + '</td>'
          +   '<td>' + esc(r.name || '') + '</td>'
          +   '<td class="num">' + esc(fmt.date(r.last_date)) + '</td>'
          +   '<td class="num">' + staleCell + '</td>'
          +   '<td class="num">' + esc(fmt.num(r.rows)) + '</td>'
          +   '<td>' + esc(r.source || '--') + '</td>'
          +   '<td>' + esc(r.board || '--') + '</td>'
          + '</tr>';
  });

  html += '</tbody></table></div>';

  // ---- 分页 ----
  const limit = c.limit || 100;
  const page = Math.floor(c.offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(c.total / limit));
  const from = c.total === 0 ? 0 : c.offset + 1;
  const to = Math.min(c.offset + limit, c.total);

  html += '<div class="pager">'
        +   '<span class="pager-info">第 <b>' + page + '</b> / <b>' + pages + '</b> 页 · '
        +     '显示 <b>' + fmt.num(from) + '</b> - <b>' + fmt.num(to) + '</b> 条 · '
        +     '共 <b>' + fmt.num(c.total) + '</b> 只</span>'
        +   '<span class="pager-actions">'
        +     '<button type="button" class="btn" id="cov-prev"' + (c.offset <= 0 ? ' disabled' : '') + '>上一页</button>'
        +     '<button type="button" class="btn" id="cov-next"' + (c.offset + limit >= c.total ? ' disabled' : '') + '>下一页</button>'
        +   '</span>'
        + '</div>';

  box.innerHTML = html;

  const prev = $('#cov-prev');
  const next = $('#cov-next');
  if (prev) prev.addEventListener('click', function () {
    c.offset = Math.max(0, c.offset - limit);
    loadCoverage().catch(function (e) { showError(e.message); });
  });
  if (next) next.addEventListener('click', function () {
    if (c.offset + limit >= c.total) return;
    c.offset += limit;
    loadCoverage().catch(function (e) { showError(e.message); });
  });
}

/* =======================================================================
 * 9. Tab5 运行与日志
 * ======================================================================= */

/** 加载任务列表（并接管仍在运行的任务轮询） */
async function loadJobs() {
  const data = await api('/api/jobs');
  const jobs = data.jobs || [];
  jobs.forEach(function (j) {
    if (!j || !j.id) return;
    const prev = state.jobs[j.id];
    // 保留已经渲染过的日志长度标记，避免重复追加
    if (prev && prev._renderedLen !== undefined) j._renderedLen = prev._renderedLen;
    state.jobs[j.id] = j;
    if (j.status === 'running') startPoll(j.id);
  });

  state.loaded.jobs = true;
  renderJobs();

  // 日志文件列表（失败不影响任务列表）
  try {
    await loadLogFiles();
  } catch (e) {
    const panel = $('#logs-panel');
    if (panel) panel.innerHTML = emptyBox('日志列表加载失败：' + e.message);
  }
}

/** 开始轮询某个任务（每 1.5 秒） */
function startPoll(id) {
  if (state.poll[id]) return;
  state.pollFail[id] = 0;
  state.poll[id] = setInterval(function () { pollJob(id); }, 1500);
  pollJob(id);
}

/** 停止轮询 */
function stopPoll(id) {
  if (state.poll[id]) {
    clearInterval(state.poll[id]);
    delete state.poll[id];
  }
}

/** 轮询单个任务 */
async function pollJob(id) {
  try {
    const data = await api('/api/jobs/' + encodeURIComponent(id));
    const job = data.job;
    if (!job) return;

    const prev = state.jobs[id];
    if (prev && prev._renderedLen !== undefined) job._renderedLen = prev._renderedLen;
    state.jobs[id] = job;
    state.pollFail[id] = 0;

    updateJobCard(job);

    if (job.status !== 'running') {
      stopPoll(id);
      onJobFinished(job);
    }
  } catch (e) {
    state.pollFail[id] = (state.pollFail[id] || 0) + 1;
    if (state.pollFail[id] >= 3) {
      stopPoll(id);
      showError('任务 ' + id + ' 轮询失败：' + e.message);
    }
  }
}

/** 任务结束后：刷新历史列表与相关数据 */
function onJobFinished(job) {
  renderJobs();
  const kind = job.kind;
  // 信号相关的任务结束后刷新选股结果与总览
  if (kind === 'signals' || kind === 'daily') {
    state.loaded.signals = false;
    if (state.tab === 'signals') {
      loadSignals(true).catch(function (e) { showError(e.message); });
    }
  }
  if (kind === 'backfill' || kind === 'daily') {
    state.loaded.coverage = false;
    if (state.tab === 'coverage') {
      loadCoverage().catch(function (e) { showError(e.message); });
    }
  }
  loadOverview(true).catch(function () { /* 静默 */ });
  showToast('任务 ' + (job.id || '') + ' 已' + (job.status === 'done' ? '完成' : '结束'),
            job.status === 'done' ? 'ok' : 'err');
}

/** 渲染任务区（正在运行 + 历史） */
function renderJobs() {
  const all = Object.keys(state.jobs).map(function (k) { return state.jobs[k]; });
  const running = all.filter(function (j) { return j.status === 'running'; });

  /* ---- 正在运行 ---- */
  const runBox = $('#jobs-running');
  const runHint = $('#jobs-running-hint');
  if (runHint) runHint.textContent = running.length ? (running.length + ' 个任务运行中 · 每 1.5 秒刷新') : '';
  if (runBox) {
    if (!running.length) {
      runBox.innerHTML = emptyBox('当前没有正在运行的任务');
    } else {
      runBox.innerHTML = running.map(function (j) { return jobCardHTML(j); }).join('');
      running.forEach(function (j) { syncJobCard(j); });
    }
  }

  /* ---- 历史任务 ---- */
  const histBox = $('#jobs-history');
  if (histBox) {
    if (!all.length) {
      histBox.innerHTML = emptyBox('暂无任务记录');
    } else {
      const sorted = all.slice().sort(function (a, b) {
        const ta = parseTs(a.started); const tb = parseTs(b.started);
        return (tb ? tb.getTime() : 0) - (ta ? ta.getTime() : 0);
      });
      histBox.innerHTML = '<div class="table-wrap"><table class="tbl">'
        + '<thead><tr><th>任务 ID</th><th>类型</th><th>状态</th><th>开始时间</th>'
        + '<th>结束时间</th><th class="num">退出码</th><th>进度</th></tr></thead><tbody>'
        + sorted.map(function (j) {
            return '<tr>'
              + '<td class="num">' + esc(j.id) + '</td>'
              + '<td>' + esc(JOB_KIND_LABEL[j.kind] || j.kind || '--') + '</td>'
              + '<td>' + jobStatusBadge(j.status) + '</td>'
              + '<td class="num">' + esc(fmt.dt(j.started)) + '</td>'
              + '<td class="num">' + esc(j.ended ? fmt.dt(j.ended) : '--') + '</td>'
              + '<td class="num">' + esc(j.exit === null || j.exit === undefined ? '--' : j.exit) + '</td>'
              + '<td>' + esc((j.progress && j.progress.text) ? j.progress.text : '--') + '</td>'
              + '</tr>';
          }).join('')
        + '</tbody></table></div>';
    }
  }
}

/** 单个运行中任务的卡片 HTML */
function jobCardHTML(j) {
  const startMs = parseTs(j.started);
  const elapsed = startMs ? (Date.now() - startMs.getTime()) : 0;
  return '<div class="job-card" data-job="' + esc(j.id) + '">'
       +   '<div class="job-head">'
       +     jobStatusBadge(j.status)
       +     '<span class="job-kind">' + esc(JOB_KIND_LABEL[j.kind] || j.kind || '任务') + '</span>'
       +     '<span class="job-id">#' + esc(j.id) + '</span>'
       +     '<span class="job-elapsed" data-start="' + (startMs ? startMs.getTime() : '') + '">已运行 '
       +       esc(fmt.duration(elapsed)) + '</span>'
       +     '<span class="job-progress-text" data-progress></span>'
       +   '</div>'
       +   '<div class="progress-bar" data-bar><i style="width:0%"></i></div>'
       +   '<pre class="log-window" data-log data-len="0"></pre>'
       + '</div>';
}

/** 就地更新运行中任务卡片（进度 / 日志），避免整块重绘导致滚动位置丢失 */
function syncJobCard(j) {
  const card = document.querySelector('.job-card[data-job="' + j.id + '"]');
  if (!card) return;

  // 进度文字
  const ptext = card.querySelector('[data-progress]');
  if (ptext) ptext.textContent = (j.progress && j.progress.text) ? j.progress.text : '';

  // 进度条
  const bar = card.querySelector('[data-bar]');
  if (bar) {
    const inner = bar.querySelector('i');
    const done = j.progress && j.progress.done !== undefined && j.progress.done !== null ? Number(j.progress.done) : null;
    const total = j.progress && j.progress.total ? Number(j.progress.total) : null;
    if (inner) {
      if (done !== null && total) {
        bar.classList.remove('indet');
        inner.style.width = Math.max(0, Math.min(100, done / total * 100)).toFixed(1) + '%';
      } else {
        bar.classList.add('indet');   // 无总数时用不确定进度动画
      }
    }
  }

  // 实时日志（增量追加，自动滚到底部）
  const pre = card.querySelector('[data-log]');
  if (pre) {
    const lines = j.log || [];
    const len = Number(pre.getAttribute('data-len') || 0);
    if (len === 0) {
      pre.textContent = lines.join('\n');
      pre.setAttribute('data-len', String(lines.length));
    } else if (lines.length > len) {
      pre.textContent += (pre.textContent ? '\n' : '') + lines.slice(len).join('\n');
      pre.setAttribute('data-len', String(lines.length));
    } else if (lines.length < len) {
      // 后端只保留最后 2000 行，长度变短说明被截断，整体重建
      pre.textContent = lines.join('\n');
      pre.setAttribute('data-len', String(lines.length));
    }
    pre.scrollTop = pre.scrollHeight;
  }
}

/** 更新运行中卡片（若卡片不存在则整块重绘） */
function updateJobCard(j) {
  if (!document.querySelector('.job-card[data-job="' + j.id + '"]')) {
    renderJobs();
    return;
  }
  syncJobCard(j);
  if (j.status !== 'running') renderJobs();
}

/** 任务状态徽章 */
function jobStatusBadge(status) {
  if (status === 'running') return '<span class="badge badge-blue">运行中</span>';
  if (status === 'done') return '<span class="badge badge-green">已完成</span>';
  if (status === 'failed') return '<span class="badge badge-red">失败</span>';
  return '<span class="badge badge-grey">' + esc(status || '未知') + '</span>';
}

/** 提交一个任务（POST /api/jobs） */
async function submitJob(kind, btn) {
  if (btn) btn.disabled = true;
  try {
    const data = await api('/api/jobs', { method: 'POST', body: { kind: kind } });
    const job = data.job;
    if (job && job.id) {
      state.jobs[job.id] = job;
      startPoll(job.id);
      state.loaded.jobs = true;
      renderJobs();
      showToast('已提交任务 #' + job.id + '（' + (JOB_KIND_SHORT[kind] || kind) + '）', 'ok');
    } else {
      showToast('任务已提交', 'ok');
    }
  } catch (e) {
    showError('提交任务失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* -------- 日志文件 -------- */

/** 拉取日志文件列表 */
async function loadLogFiles() {
  const data = await api('/api/logs');
  state.logFiles = data.files || [];
  renderLogFiles();
}

/** 渲染日志文件列表 + 内容区 */
function renderLogFiles() {
  const box = $('#logs-panel');
  if (!box) return;

  if (!state.logFiles.length) {
    box.innerHTML = emptyBox('暂无日志文件');
    return;
  }

  const list = state.logFiles.map(function (f) {
    const active = state.logCurrent === f.name ? ' active' : '';
    const tip = '最后修改：' + fmt.dt(f.mtime) + ' · 大小：' + fmt.bytes(f.size);
    return '<button type="button" class="log-file' + active + '" data-name="' + esc(f.name) + '"'
         +   ' title="' + esc(tip) + '">'
         +   '<span class="fn">' + esc(f.name) + '</span>'
         +   '<span class="sz">' + esc(fmt.bytes(f.size)) + '</span>'
         + '</button>';
  }).join('');

  const title = state.logCurrent ? state.logCurrent : '（点击左侧文件名查看内容，默认显示最后 500 行）';

  box.innerHTML = '<div class="logs-layout">'
    + '<div class="log-file-list">' + list + '</div>'
    + '<div class="log-view-wrap">'
    +   '<div class="card-head" style="padding:6px 0;border-bottom:0">'
    +     '<span class="hint">' + esc(title) + '</span>'
    +   '</div>'
    +   '<pre class="log-view" id="log-view">' + (state.logCurrent ? '加载中…' : '') + '</pre>'
    + '</div>'
    + '</div>';

  box.querySelectorAll('.log-file').forEach(function (btn) {
    btn.addEventListener('click', function () {
      loadLogContent(btn.getAttribute('data-name')).catch(function (e) { showError(e.message); });
    });
  });

  // 列表重绘后恢复已选中的日志内容
  if (state.logCurrent && state.logContent !== undefined) {
    const view = $('#log-view');
    if (view) {
      view.textContent = state.logContent;
      view.scrollTop = view.scrollHeight;
    }
  }
}

/** 加载单个日志文件内容（最后 500 行） */
async function loadLogContent(name) {
  const data = await api('/api/logs/' + encodeURIComponent(name) + '?tail=500');
  state.logCurrent = name;
  state.logContent = (data.lines || []).join('\n') || '（空文件）';
  renderLogFiles();
  const view = $('#log-view');
  if (view) view.scrollTop = view.scrollHeight;
}

/* =======================================================================
 * 10. Tab6 设置
 * -----------------------------------------------------------------------
 * 卡片 1/2：/api/settings 的 groups 接口驱动渲染，改动进 dirty 跟踪，
 *           由吸底保存栏统一 POST /api/settings（只发改动过的 key）。
 * 卡片 3：数据操作（POST /api/jobs），不入 dirty 跟踪。
 * 卡片 4：计划任务（GET /api/tasks + POST /api/tasks/action）。
 * 卡片 5：数据库与缓存（/api/db/backup、/api/db/backups、/api/cache/clear）。
 * ======================================================================= */

/** 未保存改动数量（按 key 去重） */
function dirtyCount() {
  return Object.keys(state.settings.dirty).length;
}

/** 卡片内提示条 HTML（ok 绿 / err 红 / warn 黄 / info 蓝） */
function noteHTML(kind, text) {
  return '<div class="set-note ' + kind + '">' + esc(text) + '</div>';
}

/** 最近一次数据操作的状态行（job id / 状态 / 类型） */
function renderOpState(kind) {
  const st = state.settings.lastJob[kind];
  if (!st) return '';
  const cls = st.status === 'failed' ? ' st-err' : (st.status === 'done' ? ' st-ok' : '');
  return '最近任务 <span class="' + cls + '">#' + esc(st.id) + '</span>'
       + ' · ' + esc(JOB_KIND_SHORT[st.kind] || st.kind || '--')
       + ' · ' + esc(jobStatusText(st.status));
}

/** 任务状态 -> 纯文字 */
function jobStatusText(s) {
  if (s === 'running') return '运行中';
  if (s === 'done') return '已完成';
  if (s === 'failed') return '失败';
  return s || '未知';
}

/* -------- 日期 / 代码输入解析 -------- */

/** 今天往前 n 天的 YYYY-MM-DD（默认 30） */
function isoDaysAgo(n) {
  const d = new Date();
  d.setDate(d.getDate() - (n === undefined ? 30 : n));
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/** 今天的 YYYY-MM-DD */
function isoToday() {
  return isoDaysAgo(0);
}

/** '600519,000001 600519' -> ['600519','000001']（去重、保持顺序） */
function parseSymbols(text) {
  const out = [];
  String(text || '').split(/[\s,，、;；]+/).forEach(function (s) {
    const t = s.trim().toUpperCase();
    if (t && out.indexOf(t) < 0) out.push(t);
  });
  return out;
}

/**
 * 把接口 item.type 归一化成实际的 <input type>。
 * secret: true 的字段默认用密码框遮住。
 */
function inputTypeOf(item) {
  if (item.type === 'select') return 'select';
  if (item.type === 'int' || item.type === 'float') return 'number';
  if (item.type === 'date') return 'date';
  if (item.type === 'password') return 'password';
  return item.secret ? 'password' : 'text';
}

/** 渲染单个 item 的输入控件（text / password / int / float / date / select） */
function itemControlHTML(item) {
  const key = String(item.key);
  const id = 'set-f-' + key;
  const value = item.value === null || item.value === undefined ? '' : String(item.value);
  const ph = item.placeholder ? ' placeholder="' + esc(item.placeholder) + '"' : '';
  const cls = 'input set-input' + (inputTypeOf(item) === 'password' ? ' password' : '');
  const common = ' id="' + esc(id) + '" class="' + cls + '" data-key="' + esc(key) + '"'
               + ' autocomplete="off" spellcheck="false"' + ph;

  if (item.type === 'select') {
    const choices = item.choices || [];
    let opts = '';
    let matched = false;
    choices.forEach(function (c) {
      const v = String(c.value);
      if (v === value) matched = true;
      opts += '<option value="' + esc(v) + '"' + (v === value ? ' selected' : '') + '>'
            + esc(c.label === undefined || c.label === null ? v : c.label) + '</option>';
    });
    // 当前值不在 choices 里时补一个，避免显示错乱
    if (value && !matched) {
      opts = '<option value="' + esc(value) + '" selected>' + esc(value) + '（当前值）</option>' + opts;
    }
    if (!value) opts = '<option value="" selected>--</option>' + opts;
    return '<select' + common + '>' + opts + '</select>';
  }

  if (item.type === 'int' || item.type === 'float') {
    const min = (item.min === null || item.min === undefined) ? '' : ' min="' + esc(item.min) + '"';
    const max = (item.max === null || item.max === undefined) ? '' : ' max="' + esc(item.max) + '"';
    const step = (item.step !== null && item.step !== undefined && item.step !== '')
      ? item.step
      : (item.type === 'int' ? '1' : 'any');
    return '<input type="number"' + common + min + max + ' step="' + esc(step) + '" value="' + esc(value) + '">';
  }

  const type = inputTypeOf(item);
  const input = '<input type="' + type + '"' + common + ' value="' + esc(value) + '">';
  const toggle = type === 'password'
    ? '<button type="button" class="btn" data-pw="' + esc(id) + '">显示</button>'
    : '';
  if (!toggle) return input;
  return '<div class="set-input-row">' + input + toggle + '</div>';
}

/** 渲染一个设置项行 */
function renderItem(item) {
  const key = String(item.key);
  const help = item.help ? '<div class="set-help">' + esc(item.help) + '</div>' : '';
  const envHint = item.in_file === false
    ? '<span class="set-env-hint" title="该值来自环境变量，写入 .env 可能不生效">来自环境变量</span>'
    : '';
  return '<div class="set-row" data-row="' + esc(key) + '">'
       +   '<div class="set-label">' + esc(item.label || key)
       +     (item.required ? '<span class="set-req" title="必填">*</span>' : '')
       +     envHint
       +     (item.restart ? '<span class="badge badge-yellow">需重启</span>' : '')
       +   '</div>'
       +   '<div class="set-field">'
       +     itemControlHTML(item)
       +     help
       +   '</div>'
       + '</div>';
}

/** 渲染设置分组卡片（卡片 1 / 2；meta 里带按钮，所以放卡片头） */
function renderSettingGroup(g, meta) {
  meta = meta || {};
  const items = g.items || [];
  const actions = meta.actions
    ? '<div class="head-actions">' + meta.actions + '</div>'
    : '<span class="hint">' + esc(g.key || '') + '</span>';
  return '<div class="card" data-group="' + esc(g.key || '') + '">'
       +   '<div class="card-head">'
       +     '<h2>' + esc(g.title || g.key || '设置') + '</h2>'
       +     actions
       +   '</div>'
       +   '<div class="card-body">'
       +     (g.desc ? '<p class="set-desc">' + esc(g.desc) + '</p>' : '')
       +     (items.length
               ? '<div class="set-form">' + items.map(renderItem).join('') + '</div>'
               : emptyBox('该分组暂无配置项'))
       +     (meta.extra || '')
       +   '</div>'
       + '</div>';
}

/** 设置页吸底保存栏（JS 生成，视图里不放静态节点，避免与其它页面互相干扰） */
function saveBarHTML() {
  return '<div id="set-savebar" class="savebar" hidden>'
       +   '<span class="savebar-text" id="set-savebar-text"></span>'
       +   '<span class="savebar-actions">'
       +     '<button type="button" class="btn" id="set-btn-discard">放弃改动</button>'
       +     '<button type="button" class="btn btn-primary" id="set-btn-save">保存</button>'
       +   '</span>'
       + '</div>';
}

/** 保存栏显隐与文案（只有存在未保存改动时才显示） */
function updateSaveBar() {
  const bar = $('#set-savebar');
  if (!bar) return;
  const n = dirtyCount();
  const text = $('#set-savebar-text');
  const save = $('#set-btn-save');
  const discard = $('#set-btn-discard');
  bar.hidden = n === 0;
  if (text) text.textContent = '有 ' + n + ' 项未保存';
  if (save) save.disabled = n === 0 || state.settings.busy;
  if (discard) discard.disabled = state.settings.busy;
}

/** 从当前 DOM 读取控件值（密码框也是明文 value） */
function readControlValue(key) {
  const el = $('#set-f-' + key);
  if (el) return el.value === null || el.value === undefined ? '' : String(el.value);
  // 控件不在页面上（例如表单正在重渲染）时退回已记录的值
  const v = state.settings.values[key];
  return v === undefined || v === null ? '' : String(v);
}

/**
 * 读取表单当前值（只读，不改动 state.settings.values 这个「已保存值」基线）。
 * 返回 {key: 字符串值}
 */
function readFormValues() {
  const out = {};
  state.settings.groups.forEach(function (g) {
    (g.items || []).forEach(function (it) {
      if (!it || !it.key) return;
      out[String(it.key)] = readControlValue(String(it.key));
    });
  });
  return out;
}

/** 提交前校验 + 归一化（返回 {ok, values} 或 {ok:false, error}） */
function collectFormValues() {
  const s = state.settings;
  const raw = readFormValues();
  const values = {};
  let error = null;

  s.groups.forEach(function (g) {
    (g.items || []).forEach(function (it) {
      if (!it || !it.key) return;
      const key = String(it.key);
      const str = raw[key] === undefined || raw[key] === null ? '' : String(raw[key]).trim();

      if (it.type === 'int') {
        if (str === '') {
          if (it.required) error = error || (it.label || key) + ' 不能为空';
          values[key] = '';
          return;
        }
        if (!/^-?\d+$/.test(str)) { error = error || (it.label || key) + ' 必须是整数'; return; }
        const n = parseInt(str, 10);
        if (it.min !== null && it.min !== undefined && n < Number(it.min)) {
          error = error || (it.label || key) + ' 不能小于 ' + it.min; return;
        }
        if (it.max !== null && it.max !== undefined && n > Number(it.max)) {
          error = error || (it.label || key) + ' 不能大于 ' + it.max; return;
        }
        values[key] = String(n);
        return;
      }

      if (it.type === 'float') {
        if (str === '') {
          if (it.required) error = error || (it.label || key) + ' 不能为空';
          values[key] = '';
          return;
        }
        if (!isFinite(Number(str))) { error = error || (it.label || key) + ' 必须是数字'; return; }
        const n = Number(str);
        if (it.min !== null && it.min !== undefined && n < Number(it.min)) {
          error = error || (it.label || key) + ' 不能小于 ' + it.min; return;
        }
        if (it.max !== null && it.max !== undefined && n > Number(it.max)) {
          error = error || (it.label || key) + ' 不能大于 ' + it.max; return;
        }
        values[key] = str;                 // 原样提交（省得 0.50 被改写成 0.5 而误判为已改动）
        return;
      }

      // text / password / date / select：原样提交，只校验必填
      if (str === '' && it.required) error = error || (it.label || key) + ' 不能为空';
      values[key] = str;
    });
  });

  if (error) return { ok: false, error: error };
  return { ok: true, values: values };
}

/** 比较 DOM 当前值与已保存值，刷新 dirty 集合与保存栏 */
function recomputeDirty() {
  const s = state.settings;
  const dirty = {};
  const byKey = {};
  s.groups.forEach(function (g) {
    (g.items || []).forEach(function (it) { if (it && it.key) byKey[it.key] = it; });
  });

  Object.keys(s.values).forEach(function (key) {
    if (!byKey[key]) return;
    const el = $('#set-f-' + key);
    if (!el) return;                                   // 控件不在页面上（如正在重渲染）则忽略
    if (String(el.value) !== String(s.values[key])) dirty[key] = true;
  });

  s.dirty = dirty;
  Object.keys(byKey).forEach(function (key) {
    const row = document.querySelector('.set-row[data-row="' + key + '"]');
    if (row) row.classList.toggle('is-dirty', !!dirty[key]);
  });
  updateSaveBar();
}

/** 单个控件变更（300ms 防抖，不重渲染整表，避免丢焦点） */
let settingsDirtyTimer = null;
function onSettingChanged(key) {
  if (settingsDirtyTimer) clearTimeout(settingsDirtyTimer);
  settingsDirtyTimer = setTimeout(function () {
    settingsDirtyTimer = null;
    recomputeDirty();
  }, 300);
}

/** 立即刷新 dirty（blur / change 时用） */
function onSettingChangedNow() {
  if (settingsDirtyTimer) { clearTimeout(settingsDirtyTimer); settingsDirtyTimer = null; }
  recomputeDirty();
}

/** 应用 /api/settings 返回的 groups + config，并重置 dirty */
function applySettingsData(data) {
  const s = state.settings;
  s.groups = data.groups || [];
  s.config = data.config || {};
  s.envPath = data.env_path || '';
  s.values = {};
  s.groups.forEach(function (g) {
    (g.items || []).forEach(function (it) {
      if (!it || !it.key) return;
      s.values[it.key] = it.value === null || it.value === undefined ? '' : String(it.value);
    });
  });
  s.dirty = {};
}

/**
 * 渲染设置页的接口驱动部分（卡片 1 飞书推送 / 卡片 2 数据源与回填参数）
 * @param {boolean} [reloadTasks] 是否顺带刷新计划任务（首次加载时）
 */
function renderSettings(reloadTasks) {
  const box = $('#set-groups');
  if (!box) return;

  const s = state.settings;
  let html;
  if (!s.groups.length) {
    html = '<div class="card"><div class="card-body">' + emptyBox('暂无配置项') + '</div></div>';
  } else {
    html = s.groups.map(function (g) {
      if (g.key === 'feishu') {
        return renderSettingGroup(g, {
          actions: '<button type="button" class="btn" id="set-btn-test-feishu">测试推送</button>'
                 + '<span class="hint">POST /api/settings/test-feishu</span>',
          extra: '<div id="set-feishu-result"></div>'
        });
      }
      if (g.key === 'datasource') {
        return renderSettingGroup(g, {
          actions: '<button type="button" class="btn" id="set-btn-probe">探测数据源健康度</button>'
                 + '<span class="hint">POST /api/settings/probe-sources</span>',
          extra: '<div id="set-probe-result"></div>'
        });
      }
      if (g.key === 'llm') {
        // 和「测试推送」同理：地址/模型名写错不会在选股时报错，只会静默分析不出来，
        // 所以必须有个按钮让用户当场验证。测的是输入框里的值，不必先保存。
        return renderSettingGroup(g, {
          actions: '<button type="button" class="btn" id="set-btn-test-llm">测试大模型</button>'
                 + '<span class="hint">POST /api/settings/test-llm</span>',
          extra: '<div id="set-llm-result"></div>'
        });
      }
      if (g.key === 'strategies') {
        // 策略勾选不走通用表单渲染。
        // 通用表单只会渲染成「一个文本框，让你手填逗号分隔的 key」——那是给用户添麻烦。
        // 这里画成分组勾选面板（中文名 + 说明 + 已启用计数）。
        // 底层仍是同一个 ENABLED_STRATEGIES 配置项，所以面板自己负责往
        // #set-f-ENABLED_STRATEGIES 这个隐藏 input 里同步值，这样
        // recomputeDirty() / 保存栏 / applySettingsData() 一行都不用改就能生效。
        //
        // 同组下方还挂「自定义策略」区块：两者是同一件事的两半
        // （勾选决定用哪些内置策略，自定义决定再建哪些自己的策略），
        // 放一起用户不用来回翻。
        return renderSettingGroup(g, {
          actions: '<span class="hint">勾选后点右下角保存生效</span>',
          extra: '<div id="set-strategy-picker" class="strategy-picker">'
               +   '<div class="set-note info">正在读取策略列表…</div>'
               + '</div>'
               + '<div id="custom-strategy-area" class="custom-area"></div>'
        });
      }
      return renderSettingGroup(g, {});
    }).join('') + (s.envPath ? '<div class="set-help">配置文件：' + esc(s.envPath) + '</div>' : '');
  }

  // 吸底保存栏始终跟着分组一起重建，保证引用一直有效
  box.innerHTML = html + saveBarHTML();

  bindSettingsForm();
  renderProbeResult();

  if (reloadTasks) {
    loadTasks().catch(function (e) { showError(e.message); });
    loadDbInfo();
    loadBackups().catch(function (e) { showError(e.message); });
  }
}

/** 绑定卡片 1/2 表单的输入事件（每次重渲染后都要重绑） */
function bindSettingsForm() {
  const box = $('#set-groups');
  if (!box) return;

  // 文本 / 数字：input 事件 + 300ms 防抖（不重渲染，不丢焦点）
  box.querySelectorAll('input[data-key]').forEach(function (el) {
    const key = el.getAttribute('data-key');
    // ⚠️ 这里**不能**把 el.value 写回 state.settings.values：
    //    那个对象是「已保存值」基线，recomputeDirty() 正是拿 DOM 当前值和它比较
    //    来判断有没有改动的。一旦在输入时就同步过去，两边永远相等，
    //    dirty 恒为 0 —— 保存栏不出现、点保存也只会提示「没有需要保存的改动」。
    //    基线的更新只发生在 applySettingsData()（加载和保存成功后）。
    el.addEventListener('input', function () {
      onSettingChanged(key);
    });
    // select / date / 失焦：立即更新
    el.addEventListener('change', function () {
      onSettingChangedNow();
    });
    el.addEventListener('blur', onSettingChangedNow);
    // 回车不提交表单（防止误保存），保存必须点「保存」按钮
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') ev.preventDefault();
    });
  });

  // 密码框的「显示 / 隐藏」
  box.querySelectorAll('[data-pw]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const input = document.getElementById(btn.getAttribute('data-pw'));
      if (!input) return;
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.textContent = show ? '隐藏' : '显示';
    });
  });

  // 测试推送
  const tbtn = $('#set-btn-test-feishu');
  if (tbtn) {
    tbtn.addEventListener('click', function () { testFeishu(tbtn); });
  }

  // 探测数据源
  const pbtn = $('#set-btn-probe');
  if (pbtn) {
    pbtn.addEventListener('click', function () { probeSources(pbtn); });
  }

  // 测试大模型连通性
  const lbtn = $('#set-btn-test-llm');
  if (lbtn) {
    lbtn.addEventListener('click', function () { testLLM(lbtn); });
  }

  // 策略勾选面板
  renderStrategyPicker();
  // 自定义策略区块
  renderCustomArea();
}

/* ═══════════════════════════════════════════════════════════════
   自定义策略：自然语言描述 → 大模型翻译成规则 → 预览确认 → 保存
   ═══════════════════════════════════════════════════════════════ */

/** 自定义策略区的状态 */
const customState = {
  items: [],
  loaded: false,
  busy: false,      // 正在生成 / 保存
  draft: null,      // 待确认的草稿 {rule, warnings, attempts, dry_run}
  draftError: '',
};

const CUSTOM_EXAMPLES = [
  '找连续三天缩量回调、今天放量收阳的票',
  '5日线上穿20日线并且放量的',
  '创业板里 RSI 超卖后开始回升的',
  '突破20日新高并且成交量放大1.5倍的',
];

async function renderCustomArea(force) {
  const box = $('#custom-strategy-area');
  if (!box) return;

  if (!customState.loaded || force) {
    try {
      const data = await api('/api/custom-strategies');
      customState.items = data.items || [];
      customState.loaded = true;
    } catch (e) {
      // 接口可能还没上线（后端在并行开发），不要因此把整个设置页搞崩
      customState.items = [];
      customState.loaded = true;
      customState.loadError = e.message;
    }
  }
  renderCustomAreaBody();
}

function renderCustomAreaBody() {
  const box = $('#custom-strategy-area');
  if (!box) return;
  const st = customState;

  let html = '<div class="cs-head">'
           +   '<h3>自定义策略</h3>'
           +   '<p class="cs-desc">用中文描述你想要什么样的股票，大模型会把它翻译成选股规则。'
           +   '生成后<b>先预览确认再保存</b>——大模型偶尔会理解错，规则是给人看懂的格式，你可以直接改。</p>'
           + '</div>';

  // ── 输入区 ──
  html += '<div class="cs-input-row">'
        +   '<input type="text" id="cs-desc" class="input cs-input" '
        +     'placeholder="例如：找连续三天缩量回调、今天放量收阳的票" '
        +     (st.busy ? 'disabled' : '') + '>'
        +   '<button type="button" class="btn btn-primary" id="cs-generate" '
        +     (st.busy ? 'disabled' : '') + '>'
        +     (st.busy ? '生成中…' : '生成规则')
        +   '</button>'
        + '</div>';

  if (!st.busy) {
    html += '<div class="cs-examples">试试：'
          + CUSTOM_EXAMPLES.map(function (t) {
              return '<button type="button" class="cs-example" data-example="' + esc(t) + '">'
                   + esc(t) + '</button>';
            }).join('')
          + '</div>';
  }

  if (st.busy) {
    html += noteHTML('info',
      '正在调用大模型翻译，并试跑一批股票验证效果，通常需要 30~90 秒…');
  }

  if (st.draftError) {
    html += noteHTML('err', '生成失败：' + esc(st.draftError));
  }

  // ── 草稿预览区 ──
  if (st.draft) {
    html += renderDraft(st.draft);
  }

  // ── 已保存列表 ──
  html += '<div class="cs-list-head">已保存的自定义策略（'
        + st.items.length + ' 条）</div>';

  if (!st.items.length) {
    html += emptyBox('还没有自定义策略。上面输入一句话就能建一个。', true);
  } else {
    html += '<div class="cs-list">';
    st.items.forEach(function (it) {
      const bad = (it.warnings || []).length > 0;
      html += '<div class="cs-item' + (bad ? ' is-bad' : '') + '">'
            +   '<div class="cs-item-main">'
            +     '<div class="cs-item-name">' + esc(it.name)
            +       '<span class="cs-badge">' + esc(it.source === 'llm' ? '大模型生成' : '手动') + '</span>'
            +     '</div>'
            +     (it.description ? '<div class="cs-item-desc">' + esc(it.description) + '</div>' : '')
            +     '<div class="cs-item-meta">更新于 ' + esc(it.updated_at || '--') + '</div>'
            +     (bad ? '<div class="cs-item-warn">' + esc(it.warnings.join('；')) + '</div>' : '')
            +   '</div>'
            +   '<div class="cs-item-ops">'
            +     '<button type="button" class="btn btn-sm" data-cs-view="' + esc(it.name) + '">看规则</button>'
            +     '<button type="button" class="btn btn-sm btn-danger" data-cs-del="' + esc(it.name) + '">删除</button>'
            +   '</div>'
            +   '<pre class="cs-json" id="cs-json-' + esc(cssId(it.name)) + '" hidden></pre>'
            + '</div>';
    });
    html += '</div>';
  }

  box.innerHTML = html;
  bindCustomArea();

  // 「看规则」点击后把 JSON 填进对应的 pre
  st.items.forEach(function (it) {
    const pre = $('#cs-json-' + cssId(it.name));
    if (pre) pre.textContent = JSON.stringify(it.conditions, null, 2);
  });
}

/** 把名字转成能安全用作 id 的字符串（中文名不能直接进 querySelector） */
function cssId(name) {
  return String(name).replace(/[^0-9a-zA-Z]/g, function (c) {
    return 'u' + c.charCodeAt(0).toString(16);
  });
}

function renderDraft(d) {
  const rule = d.rule || {};
  const dry = d.dry_run || {};
  let html = '<div class="cs-draft">';
  html += '<div class="cs-draft-head">规则已生成，请确认</div>';

  html += '<div class="cs-draft-meta">'
        +   '<span>策略名：<b>' + esc(rule.name || '(未命名)') + '</b></span>'
        +   (d.attempts > 1 ? '<span class="cs-retry">重试了 ' + d.attempts + ' 次才通过校验</span>' : '')
        + '</div>';

  if (rule.description) {
    html += '<div class="cs-draft-desc">' + esc(rule.description) + '</div>';
  }

  // 试跑结果：让用户有个「大概能选出多少只」的预期
  if (dry.checked) {
    const rate = dry.checked ? (dry.hits / dry.checked * 100) : 0;
    let kind = 'ok';
    let msg = '试跑 ' + dry.checked + ' 只股票，命中 <b>' + dry.hits + '</b> 只'
            + '（约 ' + rate.toFixed(1) + '%）';
    if (dry.hits === 0) {
      kind = 'warn';
      msg += '，全市场大概也选不出多少只。条件可能过严，建议放宽某个数值后重试。';
    } else if (rate > 30) {
      kind = 'warn';
      msg += '，比例偏高，条件可能偏松。';
    }
    html += noteHTML(kind, msg);
    if (dry.samples && dry.samples.length) {
      html += '<div class="cs-samples">命中样例：'
            + dry.samples.map(function (c) { return '<code>' + esc(c) + '</code>'; }).join(' ')
            + '</div>';
    }
  }

  if ((d.warnings || []).length) {
    html += noteHTML('warn', d.warnings.map(esc).join('；'));
  }

  html += '<details class="cs-details"><summary>查看生成的规则 JSON（可复制去改）</summary>'
        +   '<pre class="cs-json">' + esc(JSON.stringify(rule, null, 2)) + '</pre>'
        + '</details>';

  html += '<div class="cs-draft-ops">'
        +   '<button type="button" class="btn btn-primary" id="cs-save">保存这个策略</button>'
        +   '<button type="button" class="btn" id="cs-discard">放弃</button>'
        + '</div>';
  html += '</div>';
  return html;
}

function bindCustomArea() {
  const input = $('#cs-desc');
  if (input) {
    input.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); doGenerateRule(); }
    });
  }

  const gen = $('#cs-generate');
  if (gen) gen.addEventListener('click', doGenerateRule);

  document.querySelectorAll('[data-example]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const el = $('#cs-desc');
      if (el) { el.value = btn.getAttribute('data-example'); el.focus(); }
    });
  });

  const save = $('#cs-save');
  if (save) save.addEventListener('click', doSaveDraft);

  const discard = $('#cs-discard');
  if (discard) discard.addEventListener('click', function () {
    customState.draft = null;
    customState.draftError = '';
    renderCustomAreaBody();
  });

  document.querySelectorAll('[data-cs-view]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const pre = $('#cs-json-' + cssId(btn.getAttribute('data-cs-view')));
      if (pre) pre.hidden = !pre.hidden;
    });
  });

  document.querySelectorAll('[data-cs-del]').forEach(function (btn) {
    btn.addEventListener('click', function () { doDeleteRule(btn.getAttribute('data-cs-del')); });
  });
}

async function doGenerateRule() {
  const input = $('#cs-desc');
  const desc = input ? String(input.value || '').trim() : '';
  if (!desc) {
    showToast('先用中文描述一下你想要什么样的股票', 'err');
    return;
  }
  customState.busy = true;
  customState.draftError = '';
  customState.draft = null;
  renderCustomAreaBody();

  try {
    // 注意：api() 直接透传 options 给 fetch，没有 timeoutMs 这种东西。
    // 这个接口要调大模型 + 试跑 120 只股票，30~90 秒很正常；
    // fetch 默认不超时，所以这里不需要额外配置，只要界面上把「生成中」显示出来。
    const data = await api('/api/custom-strategies/generate', {
      method: 'POST', body: { description: desc },
    });
    customState.draft = data;
    showToast('规则生成成功，请确认后保存', 'ok');
  } catch (e) {
    customState.draftError = e.message;
  } finally {
    customState.busy = false;
    renderCustomAreaBody();
  }
}

async function doSaveDraft() {
  const d = customState.draft;
  if (!d || !d.rule) return;
  try {
    await api('/api/custom-strategies', {
      method: 'POST', body: { rule: d.rule, source: 'llm' },
    });
    customState.draft = null;
    customState.loaded = false;
    showToast('已保存。记得在上面勾选「自定义策略」并保存设置，它才会参与选股', 'ok');
    await renderCustomArea(true);
    // 新策略可能改变策略总数，刷新勾选面板的计数
    strategyState.loaded = false;
    await renderStrategyPicker(true);
  } catch (e) {
    showError('保存失败：' + e.message);
  }
}

async function doDeleteRule(name) {
  if (!window.confirm('确定删除自定义策略「' + name + '」？此操作不可撤销。')) return;
  try {
    await api('/api/custom-strategies/' + encodeURIComponent(name), { method: 'DELETE' });
    showToast('已删除「' + name + '」', 'ok');
    await renderCustomArea(true);
    strategyState.loaded = false;
    await renderStrategyPicker(true);
  } catch (e) {
    showError('删除失败：' + e.message);
  }
}

/* -------- 设置卡片：策略勾选 -------- */

/** 策略勾选面板的状态（与后端 /api/strategies 同步） */
const strategyState = { groups: [], total: 0, raw: '', loaded: false, error: '' };

/**
 * 读取 /api/strategies 并渲染勾选面板。
 *
 * 面板与 #set-f-ENABLED_STRATEGIES 隐藏 input 双向同步：
 * 勾选框变化时把选中的 key 拼成逗号分隔字符串写进那个 input，
 * 于是现有的 recomputeDirty() 就会把 ENABLED_STRATEGIES 标记为脏，
 * 右下角保存栏自动出现。不需要给保存流程加任何特例。
 */
async function renderStrategyPicker(force) {
  const box = $('#set-strategy-picker');
  if (!box) return;

  if (!strategyState.loaded || force) {
    try {
      const data = await api('/api/strategies');
      strategyState.groups = data.groups || [];
      strategyState.total = data.total || 0;
      strategyState.raw = data.raw == null ? '' : String(data.raw);
      strategyState.error = '';
      strategyState.loaded = true;
    } catch (e) {
      strategyState.error = e.message;
      box.innerHTML = noteHTML('err', '读取策略列表失败：' + e.message);
      return;
    }
  }

  renderStrategyPickerBody();
}

function renderStrategyPickerBody() {
  const box = $('#set-strategy-picker');
  if (!box) return;

  const st = strategyState;
  const enabledSet = {};
  let enabledCount = 0;
  st.groups.forEach(function (g) {
    (g.items || []).forEach(function (it) {
      if (it.enabled) { enabledSet[it.key] = true; enabledCount++; }
    });
  });

  // 「全选/全不选」按钮 + 计数
  let html = '<div class="sp-toolbar">'
           +   '<span class="sp-count">已启用 <b>' + enabledCount + '</b> / ' + st.total + ' 个策略</span>'
           +   '<span class="sp-actions">'
           +     '<button type="button" class="btn btn-sm" id="sp-all">全选</button>'
           +     '<button type="button" class="btn btn-sm" id="sp-none">全不选</button>'
           +     '<button type="button" class="btn btn-sm" id="sp-reset">恢复默认（全部启用）</button>'
           +   '</span>'
           + '</div>';

  if (enabledCount === 0) {
    // 这个提示很重要：用户可能以为「一个都不勾 = 什么都不跑」，
    // 实际上空配置的语义是「全部启用」（向后兼容老 .env）。
    html += noteHTML('info',
      '当前没有任何自定义：<b>留空 = 全部启用</b>。若真的一个策略都不想跑，请至少勾选一个。');
  }

  st.groups.forEach(function (g) {
    html += '<div class="sp-group">'
          +   '<div class="sp-group-title">' + esc(g.title) + '</div>'
          +   '<div class="sp-grid">';
    (g.items || []).forEach(function (it) {
      html += '<label class="sp-item' + (it.enabled ? ' is-on' : '') + '" title="' + esc(it.desc || '') + '">'
            +   '<input type="checkbox" data-sp="' + esc(it.key) + '"'
            +     (it.enabled ? ' checked' : '') + '>'
            +   '<span class="sp-label">' + esc(it.label) + '</span>'
            +   '<code class="sp-key">' + esc(it.key) + '</code>'
            + '</label>';
    });
    html += '</div></div>';
  });

  // 隐藏 input：让通用的脏检测/保存流程能读到值
  html += '<input type="hidden" id="set-f-ENABLED_STRATEGIES" data-key="ENABLED_STRATEGIES" value="">';

  box.innerHTML = html;

  // 初始同步一次隐藏 input（此时还没改动，值等于已保存值，不会误报 dirty）
  syncStrategyInput(false);

  box.querySelectorAll('input[data-sp]').forEach(function (cb) {
    cb.addEventListener('change', function () {
      cb.closest('.sp-item').classList.toggle('is-on', cb.checked);
      syncStrategyInput(true);
    });
  });

  const all = $('#sp-all');
  if (all) all.addEventListener('click', function () { setAllStrategies(true); });
  const none = $('#sp-none');
  if (none) none.addEventListener('click', function () { setAllStrategies(false); });
  const reset = $('#sp-reset');
  if (reset) reset.addEventListener('click', function () { setAllStrategies(true); });
}

/**
 * 把勾选状态写进隐藏 input。
 *
 * @param {boolean} markDirty 是否触发脏检测。
 *   初始化时必须传 false：那时 input 的值会被设成「用户上次保存的原值」，
 *   而拿它去和基线比较正是我们想要的（相等 -> 不脏）。
 *   但如果是用户点出来的，就必须触发 recomputeDirty() 让保存栏出现。
 */
function syncStrategyInput(markDirty) {
  const input = $('#set-f-ENABLED_STRATEGIES');
  if (!input) return;

  const picked = [];
  document.querySelectorAll('input[data-sp]').forEach(function (cb) {
    if (cb.checked) picked.push(cb.getAttribute('data-sp'));
  });

  // 全部勾选时写空字符串——语义等价（空=全开），而且这样
  // .env 里不会存一长串 15 个 key，将来新增策略也能自动生效。
  input.value = picked.length === strategyState.total ? '' : picked.join(',');

  // 更新计数与提示
  const cnt = document.querySelector('.sp-count b');
  if (cnt) cnt.textContent = picked.length;

  if (markDirty) onSettingChangedNow();
}

function setAllStrategies(on) {
  document.querySelectorAll('input[data-sp]').forEach(function (cb) {
    cb.checked = on;
    cb.closest('.sp-item').classList.toggle('is-on', on);
  });
  syncStrategyInput(true);
}

/* -------- 设置卡片：大模型连通性测试 -------- */

/** POST /api/settings/test-llm（测输入框里的值，不必先保存） */
async function testLLM(btn) {
  const box = $('#set-llm-result');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '测试中…'; }
  if (box) box.innerHTML = '<div class="set-note info">正在调用大模型，最长可能要 1 分钟…</div>';

  try {
    const body = {};
    [['LLM_BASE_URL', 'base_url'], ['LLM_MODEL', 'model'], ['LLM_API_KEY', 'api_key']]
      .forEach(function (pair) {
        const el = $('#set-f-' + pair[0]);
        const v = el ? String(el.value || '').trim() : '';
        if (v) body[pair[1]] = v;
      });
    const data = await api('/api/settings/test-llm', { method: 'POST', body: body });
    const msg = data.message || '连接成功';
    if (box) box.innerHTML = noteHTML('ok', msg);
    showToast('大模型连接成功', 'ok');
  } catch (e) {
    if (box) box.innerHTML = noteHTML('err', '连接失败：' + e.message);
    showError('大模型连接失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || '测试大模型'; }
  }
}

/* -------- 设置卡片 1：飞书测试推送 -------- */

/** POST /api/settings/test-feishu（输入框里没填就交给后端用已保存的地址） */
async function testFeishu(btn) {
  const box = $('#set-feishu-result');
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '发送中…'; }
  if (box) box.innerHTML = '<div class="set-note info">正在发送测试卡片…</div>';

  try {
    const body = {};
    const el = $('#set-f-FEISHU_WEBHOOK_URL');
    const url = el ? String(el.value || '').trim() : '';
    if (url) body.webhook_url = url;
    const data = await api('/api/settings/test-feishu', { method: 'POST', body: body });
    const msg = data.message || '测试卡片已发送';
    if (box) box.innerHTML = noteHTML('ok', msg + (data.webhook ? '（' + data.webhook + '）' : ''));
    showToast(msg, 'ok');
  } catch (e) {
    if (box) box.innerHTML = noteHTML('err', '测试推送失败：' + e.message);
    showError('测试推送失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || '测试推送'; }
  }
}

/* -------- 设置卡片 2：数据源健康度探测 -------- */

/** POST /api/settings/probe-sources（可能要 5~20 秒，按钮显示「探测中…」并禁用） */
async function probeSources(btn) {
  const box = $('#set-probe-result');
  if (btn) { btn.disabled = true; btn.textContent = '探测中…'; }
  if (box) box.innerHTML = '<div class="set-note info">正在逐个探测数据源，最多可能要 20 秒，请稍候…</div>';

  try {
    const data = await api('/api/settings/probe-sources', { method: 'POST', body: {} });
    state.settings.probe = data;
    renderProbeResult();
    const healthy = data.healthy || [];
    showToast('探测完成，可用 ' + healthy.length + ' 个数据源', healthy.length ? 'ok' : 'err');
  } catch (e) {
    state.settings.probe = null;
    if (box) box.innerHTML = noteHTML('err', '探测失败：' + e.message);
    showError('探测失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '探测数据源健康度'; }
  }
}

/** 渲染数据源探测结果（源名 / 可用与否 / 耗时 / 错误） */
function renderProbeResult() {
  const box = $('#set-probe-result');
  if (!box) return;
  const data = state.settings.probe;
  if (!data) { box.innerHTML = ''; return; }

  const rows = data.results || [];
  const healthy = data.healthy || [];
  const okN = rows.filter(function (r) { return r && r.ok; }).length;

  const head = '<div class="set-note ' + (okN ? 'ok' : 'err') + '">'
             +   '健康度：可用 ' + fmt.num(okN) + ' / ' + fmt.num(rows.length)
             +   (healthy.length ? '（' + esc(healthy.join(' / ')) + '）' : '（当前没有可用的数据源）')
             + '</div>';

  const tags = rows.length
    ? '<div class="probe-map">' + rows.map(function (r) {
        const ok = r && r.ok === true;
        const ms = (r && r.ms !== null && r.ms !== undefined) ? (fmt.num(r.ms) + ' ms') : '';
        const err = (r && r.error) ? ' · ' + esc(String(r.error)) : '';
        return '<span class="probe-tag ' + (ok ? 'ok' : 'bad') + '" title="' + esc(String((r && r.source) || '') + err) + '">'
             +   '<span class="src">' + esc((r && (r.label || r.source)) || '--') + '</span>'
             +   '<span class="badge ' + (ok ? 'badge-green' : 'badge-red') + '">' + (ok ? '可用' : '不可用') + '</span>'
             +   (ms ? '<span class="ms">' + esc(ms) + '</span>' : '')
             +   (r && r.error ? '<span class="err">' + esc(String(r.error)) + '</span>' : '')
             + '</span>';
      }).join('') + '</div>'
    : emptyBox('没有探测到任何数据源', true);

  box.innerHTML = head + tags;
}

/* -------- 设置卡片 3：数据操作 -------- */

/** 统一的 /api/jobs 提交 + 状态展示 */
async function submitJobPayload(payload, btn, opKey) {
  if (btn) btn.disabled = true;
  try {
    const data = await api('/api/jobs', { method: 'POST', body: payload });
    const job = data.job;
    if (job && job.id) {
      state.jobs[job.id] = job;
      if (job.status === 'running') startPoll(job.id);
      state.loaded.jobs = true;
      renderJobs();
      state.settings.lastJob[opKey] = { id: job.id, kind: job.kind || payload.kind, status: job.status };
      showToast('任务已启动：' + job.id + '，进度请到「运行与日志」页查看', 'ok');
    } else {
      state.settings.lastJob[opKey] = { id: '(未知)', kind: payload.kind, status: 'running' };
      showToast('任务已启动，进度请到「运行与日志」页查看', 'ok');
    }
    renderOpStates();
  } catch (e) {
    showError('提交任务失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 刷新 4 个操作块下方的「最近任务」状态行 */
function renderOpStates() {
  document.querySelectorAll('#view-settings .op-block[data-op]').forEach(function (blk) {
    const box = blk.querySelector('[data-op-state]');
    if (box) box.innerHTML = renderOpState(blk.getAttribute('data-op'));
  });
}

/** 数据操作 1：回填当日最新数据（增量，最快） */
function opSync(btn) {
  return submitJobPayload({ kind: 'sync' }, btn, 'sync');
}

/** 数据操作 2：补齐全市场缺口 */
function opBackfill(btn) {
  return submitJobPayload({ kind: 'backfill' }, btn, 'backfill');
}

/** 数据操作 3：指定股票整只重拉（会先删历史，必须二次确认） */
function opReload(btn) {
  const el = $('#set-reload-symbols');
  const symbols = parseSymbols(el ? el.value : '');
  if (!symbols.length) {
    showToast('请先填写股票代码，如 600519,000001', 'err');
    if (el) el.focus();
    return Promise.resolve();
  }
  if (!window.confirm('「整只重拉」会先删除这 ' + symbols.length + ' 只股票的全部历史数据（'
      + symbols.join('、') + '），再重新抓取。\n\n确定继续吗？')) {
    return Promise.resolve();
  }
  return submitJobPayload({ kind: 'reload', symbols: symbols }, btn, 'reload');
}

/** 数据操作 4：指定日期区间补数据（股票留空 = 全市场） */
function opRange(btn) {
  const sEl = $('#set-range-start');
  const eEl = $('#set-range-end');
  const start = sEl ? String(sEl.value || '').trim() : '';
  const end = eEl ? String(eEl.value || '').trim() : '';
  if (!start || !end) {
    showToast('请选择开始日期和结束日期', 'err');
    return Promise.resolve();
  }
  if (start > end) {
    showToast('开始日期不能晚于结束日期', 'err');
    return Promise.resolve();
  }
  const symbols = parseSymbols($('#set-range-symbols') ? $('#set-range-symbols').value : '');
  const payload = { kind: 'backfill', start: start, end: end };
  if (symbols.length) payload.symbols = symbols;
  return submitJobPayload(payload, btn, 'range');
}

/* -------- 设置卡片 4：计划任务 -------- */

/** GET /api/tasks 并渲染表格 */
async function loadTasks() {
  const data = await api('/api/tasks');
  renderTasks(data.tasks || []);
}

/** 渲染计划任务表格（任务名 / 状态 / 下次运行 / 上次运行 / 上次结果 / 操作） */
function renderTasks(tasks) {
  const box = $('#set-tasks-body');
  if (!box) return;

  if (!tasks.length) {
    box.innerHTML = emptyBox('暂无计划任务');
    return;
  }

  box.innerHTML = '<div class="table-wrap"><table class="tbl"><thead><tr>'
    +   '<th>任务名</th><th>状态</th><th>下次运行</th><th>上次运行</th><th>上次结果</th><th>操作</th>'
    + '</tr></thead><tbody>'
    + tasks.map(function (t) {
        const name = String(t.name || '');
        return '<tr>'
          + '<td>' + esc(name) + '</td>'
          + '<td>' + taskStateBadge(t.state) + '</td>'
          + '<td class="num">' + esc(fmt.dt(t.next_run)) + '</td>'
          + '<td class="num">' + esc(fmt.dt(t.last_run)) + '</td>'
          + '<td>' + taskResultCell(t.last_result, t.last_result_text) + '</td>'
          + '<td><span class="head-actions">'
          +   '<button type="button" class="btn" data-task="' + esc(name) + '" data-taction="enable">启用</button>'
          +   '<button type="button" class="btn" data-task="' + esc(name) + '" data-taction="disable">禁用</button>'
          +   '<button type="button" class="btn" data-task="' + esc(name) + '" data-taction="run">立即运行</button>'
          + '</span></td>'
          + '</tr>';
      }).join('')
    + '</tbody></table></div>';
}

/** POST /api/tasks/action：启用 / 禁用 / 立即运行 */
async function taskAction(name, action, btn) {
  if (!name) return;
  if (action === 'run' && !window.confirm('「立即运行」会真的完整跑一遍任务，并把选股结果推送到飞书。\n\n确定现在运行「' + name + '」吗？')) {
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const data = await api('/api/tasks/action', { method: 'POST', body: { name: name, action: action } });
    if (data.tasks) renderTasks(data.tasks);
    showToast(data.message || '操作已完成', 'ok');
    if (action === 'run') {
      // 任务刚被触发，顺带刷新一次任务列表（状态会变成「运行中」）
      setTimeout(function () { loadTasks().catch(function () { /* 静默 */ }); }, 1200);
    }
  } catch (e) {
    showError('任务操作失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* -------- 设置卡片 5：数据库与缓存 -------- */

/** 数据库信息：优先用 /api/overview 的 db，其次用 settings 的 config.db_path */
function loadDbInfo() {
  const box = $('#set-db-info');
  if (!box) return;

  const db = (state.overview && state.overview.db) || {};
  const cfg = state.settings.config || {};
  const path = db.path || cfg.db_path || '--';

  const rows = [
    ['数据库路径', path],
    ['数据库大小', db.size_mb === undefined || db.size_mb === null ? '--（点右上角刷新）' : fmt.size(db.size_mb)],
    ['股票数', db.symbols === undefined || db.symbols === null ? '--' : fmt.num(db.symbols)],
    ['总行数', db.rows === undefined || db.rows === null ? '--' : fmt.num(db.rows)],
    ['数据区间', (db.min_date || db.max_date)
        ? (fmt.date(db.min_date) + ' ~ ' + fmt.date(db.max_date)) : '--'],
    ['数据源顺序', cfg.auto_source_order || '--']
  ];

  box.innerHTML = rows.map(function (r) {
    return '<div class="kv-row">'
         +   '<span class="kv-k">' + esc(r[0]) + '</span>'
         +   '<span class="kv-v mono" title="' + esc(String(r[1])) + '">' + esc(String(r[1])) + '</span>'
         + '</div>';
  }).join('');

  // overview 里没有数据库信息时补拉一次（失败不影响其它卡片）。
  // 注意不能写 `&& !state.inflight.overview`：header 也会发同一个请求，
  // 一旦它正在飞就直接跳过补拉，这几行就永远停在「--（点右上角刷新）」了。
  // loadOverview() 内部会复用在飞的 promise，所以这里无脑调 + then 重渲染即可。
  if (!state.overview) {
    loadOverview()
      .then(function () { loadDbInfo(); })
      .catch(function () { /* 静默 */ });
  }
}

/** POST /api/db/backup，成功后提示文件名与大小并刷新备份列表 */
async function backupDb(btn) {
  const box = $('#set-db-result');
  if (btn) { btn.disabled = true; btn.textContent = '备份中…'; }
  if (box) box.innerHTML = '<div class="set-note info">正在备份数据库，大库可能要一会儿…</div>';

  try {
    const data = await api('/api/db/backup', { method: 'POST', body: {} });
    const name = data.file || data.name || '(未知文件)';
    const size = (data.size_mb === null || data.size_mb === undefined) ? '' : ('，' + fmt.size(data.size_mb));
    const kept = (data.kept === null || data.kept === undefined) ? '' : ('，保留最近 ' + fmt.num(data.kept) + ' 份');
    if (box) {
      box.innerHTML = noteHTML('ok', '备份完成：' + name + size + kept
        + (data.path ? '（' + data.path + '）' : ''));
    }
    showToast('备份完成：' + name + size, 'ok');
    await loadBackups();
  } catch (e) {
    if (box) box.innerHTML = noteHTML('err', '备份失败：' + e.message);
    showError('备份失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '备份数据库'; }
  }
}

/** GET /api/db/backups 并渲染备份文件列表 */
async function loadBackups() {
  const data = await api('/api/db/backups');
  state.settings.backups = data.files || [];
  renderBackups();
}

/** 渲染备份文件列表 */
function renderBackups() {
  const box = $('#set-backups');
  if (!box) return;
  const files = state.settings.backups || [];

  if (!files.length) {
    box.innerHTML = emptyBox('暂无备份文件', true);
    return;
  }

  box.innerHTML = '<div class="backup-list">' + files.map(function (f) {
    return '<div class="backup-row" title="' + esc(String(f.path || '')) + '">'
         +   '<span class="bk-name">' + esc(f.name || '--') + '</span>'
         +   '<span class="bk-time">' + esc(fmt.dt(f.mtime)) + '</span>'
         +   '<span class="bk-size">' + esc(fmt.size(f.size_mb)) + '</span>'
         + '</div>';
  }).join('') + '</div>';
}

/** POST /api/cache/clear：清理 4 类内存缓存 */
async function clearCache(btn) {
  const box = $('#set-db-result');
  const targets = ['signals', 'jobs', 'names', 'coverage'];
  if (btn) { btn.disabled = true; btn.textContent = '清理中…'; }
  if (box) box.innerHTML = '<div class="set-note info">正在清理缓存…</div>';

  try {
    const data = await api('/api/cache/clear', { method: 'POST', body: { targets: targets } });
    const cleared = data.cleared || [];
    const msg = data.message || ('已清理 ' + cleared.length + ' 项缓存');
    if (box) {
      box.innerHTML = noteHTML('ok', msg + (cleared.length ? '：' + cleared.join('、') : '（没有需要清理的缓存）'));
    }
    showToast(msg, 'ok');
    // 缓存清掉后，已加载的数据下次进入要重新拉
    cleared.forEach(function (t) {
      if (t === 'signals') state.loaded.signals = false;
      if (t === 'coverage') state.loaded.coverage = false;
      if (t === 'jobs') state.loaded.jobs = false;
    });
  } catch (e) {
    if (box) box.innerHTML = noteHTML('err', '清理缓存失败：' + e.message);
    showError('清理缓存失败：' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '清理缓存'; }
  }
}

/* -------- 设置页加载 / 保存 / 放弃 -------- */

/** 加载 /api/settings 并渲染（首次进入 tab 或放弃改动时调用） */
async function loadSettings(force) {
  if (state.loaded.settings && !force) return;
  if (state.settings.busy) return;
  state.settings.busy = true;
  try {
    const data = await api('/api/settings');
    applySettingsData(data);
    state.loaded.settings = true;
    renderSettings(true);
    updateSaveBar();
  } finally {
    state.settings.busy = false;
  }
}

/** 保存：只提交改动过的 key */
async function saveSettings(btn) {
  const s = state.settings;
  if (s.busy) return;
  if (!dirtyCount()) { showToast('没有需要保存的改动'); return; }

  const res = collectFormValues();
  if (!res.ok) { showError(res.error); return; }

  const values = {};
  Object.keys(s.dirty).forEach(function (key) {
    if (res.values[key] !== undefined) values[key] = res.values[key];
  });
  if (!Object.keys(values).length) { showToast('没有需要保存的改动'); return; }

  s.busy = true;
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  const fbox = $('#set-feishu-result');

  try {
    const data = await api('/api/settings', { method: 'POST', body: { values: values } });

    // 用响应里的 groups / config 重新渲染（相当于重新加载当前值），清掉 dirty
    applySettingsData(data);
    state.loaded.settings = true;
    renderSettings(false);
    updateSaveBar();
    showToast('已保存', 'ok');

    const warnings = data.warnings || [];
    const applied = data.applied || [];
    if (warnings.length) {
      // 黄色提示条 + 逐条 toast
      const box = $('#set-feishu-result');
      if (box) {
        box.innerHTML = noteHTML('warn', '已保存，但有 ' + warnings.length + ' 条提醒：'
          + '<br>· ' + warnings.map(esc).join('<br>· '));
      }
      warnings.forEach(function (w) { showToast(w); });
    } else if (applied.length && fbox) {
      fbox.innerHTML = noteHTML('ok', '已更新：' + applied.join('、'));
    }
    if (data.restart_required) showToast('部分改动需要重启服务后生效');
  } catch (e) {
    showError('保存失败：' + e.message);
  } finally {
    s.busy = false;
    if (btn) btn.textContent = '保存';
    updateSaveBar();
  }
}

/** 放弃改动：重新拉一次 /api/settings 覆盖当前值 */
async function discardSettings(btn) {
  if (state.settings.busy) return;
  if (btn) btn.disabled = true;
  try {
    state.loaded.settings = false;
    await loadSettings(true);
    showToast('已放弃未保存的改动');
  } catch (e) {
    showError('放弃改动失败：' + e.message);
  } finally {
    if (btn) btn.disabled = false;
    updateSaveBar();
  }
}

/** 绑定设置页所有事件（入口调用一次；动态部分用事件委托） */
function bindSettingsEvents() {
  /* ---- 卡片 3：4 个数据操作（事件委托，便于后续调整 DOM） ---- */
  const ops = $('#view-settings');
  if (ops) {
    ops.addEventListener('click', function (ev) {
      const btn = ev.target && ev.target.closest ? ev.target.closest('[data-action]') : null;
      if (!btn) return;
      const act = btn.getAttribute('data-action');
      if (act === 'op-sync') opSync(btn).catch(function (e) { showError(e.message); });
      else if (act === 'op-backfill') opBackfill(btn).catch(function (e) { showError(e.message); });
      else if (act === 'op-reload') opReload(btn).catch(function (e) { showError(e.message); });
      else if (act === 'op-range') opRange(btn).catch(function (e) { showError(e.message); });
    });
  }

  /* ---- 卡片 3：日期区间默认值（今天往前 30 天 ~ 今天） ---- */
  const startEl = $('#set-range-start');
  const endEl = $('#set-range-end');
  if (startEl && !startEl.value) startEl.value = isoDaysAgo(30);
  if (endEl && !endEl.value) endEl.value = isoToday();

  /* ---- 卡片 3：操作块里的输入框回车不要误提交任务 ---- */
  ['#set-reload-symbols', '#set-range-start', '#set-range-end', '#set-range-symbols'].forEach(function (sel) {
    const el = $(sel);
    if (!el) return;
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        if (sel === '#set-reload-symbols') opReload($('[data-action="op-reload"]'));
        else if (sel === '#set-range-symbols') opRange($('[data-action="op-range"]'));
      }
    });
  });

  /* ---- 卡片 4：计划任务（事件委托） ---- */
  const tasksBody = $('#set-tasks-body');
  if (tasksBody) {
    tasksBody.addEventListener('click', function (ev) {
      const btn = ev.target && ev.target.closest ? ev.target.closest('[data-taction]') : null;
      if (!btn) return;
      taskAction(btn.getAttribute('data-task'), btn.getAttribute('data-taction'), btn)
        .catch(function (e) { showError(e.message); });
    });
  }
  const tasksRefresh = $('#set-tasks-refresh');
  if (tasksRefresh) {
    tasksRefresh.addEventListener('click', function () {
      loadTasks().catch(function (e) { showError(e.message); });
    });
  }

  /* ---- 卡片 5：数据库与缓存 ---- */
  const btnBackup = $('#set-btn-backup');
  if (btnBackup) btnBackup.addEventListener('click', function () { backupDb(btnBackup); });

  const btnClear = $('#set-btn-clear-cache');
  if (btnClear) btnClear.addEventListener('click', function () { clearCache(btnClear); });

  const btnBkRefresh = $('#set-btn-backups-refresh');
  if (btnBkRefresh) {
    btnBkRefresh.addEventListener('click', function () {
      loadBackups().catch(function (e) { showError(e.message); });
    });
  }

  /* ---- 吸底保存栏（节点由 renderSettings 生成，用委托保证一直可用）---- */
  const setView = $('#view-settings');
  if (setView) {
    setView.addEventListener('click', function (ev) {
      const t = ev.target;
      if (!t || !t.id) return;
      if (t.id === 'set-btn-save') saveSettings(t);
      else if (t.id === 'set-btn-discard') discardSettings(t);
    });
  }

  updateSaveBar();
}

/* =======================================================================
 * 11. 初始化与事件绑定
 * ======================================================================= */

/** 刷新当前 tab（顶部「刷新」按钮） */
function refreshCurrent() {
  const t = state.tab;
  const fail = function (e) { showError(e.message); };

  if (t === 'overview') {
    state.loaded.overview = false;
    loadOverview(true).catch(fail);
  } else if (t === 'signals') {
    state.loaded.signals = false;
    loadSignals(true).catch(fail);
  } else if (t === 'stock') {
    const wanted = symbolFromHash();
    if (wanted && wanted !== state.stock.symbol) {
      // 深链接 #stock/600519：直接加载
      loadStock(wanted).catch(fail);
    } else if (state.stock.symbol) {
      loadStock(state.stock.symbol).catch(fail);
    } else {
      showToast('请先搜索并选择一只股票', 'err');
    }
  } else if (t === 'coverage') {
    loadCoverage().catch(fail);
  } else if (t === 'jobs') {
    loadJobs().catch(fail);
  } else if (t === 'settings') {
    // 刷新 = 重新拉配置 + 计划任务（未保存的改动会被覆盖）
    state.loaded.settings = false;
    loadSettings(true).catch(fail);
    loadTasks().catch(fail);
    loadBackups().catch(fail);
    loadDbInfo();
  }
  // 顶部 Header 的日期/股票数始终以 overview 为准
  if (t !== 'overview') loadOverview(true).catch(function () { /* 静默 */ });
}

/** 绑定所有事件 */
function bindEvents() {
  /* ---- Tab 导航（hashchange 驱动，保证刷新保持当前 tab）---- */
  window.addEventListener('hashchange', function () { switchTab(tabFromHash()); });

  /* ---- 顶部刷新 ---- */
  const btnRefresh = $('#btn-refresh');
  if (btnRefresh) btnRefresh.addEventListener('click', refreshCurrent);

  /* ---- Tab2：重新计算信号 ---- */
  const btnRecalc = $('#btn-recalc');
  if (btnRecalc) {
    btnRecalc.addEventListener('click', function () { submitJob('signals', btnRecalc); });
  }

  /* ---- Tab3：搜索（回车触发 / 输入防抖）+ 下拉候选 ---- */
  const q = $('#stock-q');
  const suggest = $('#stock-suggest');
  let searchTimer = null;

  function closeSuggest() {
    if (suggest) { suggest.hidden = true; suggest.innerHTML = ''; }
    state.suggestItems = [];
    state.suggestIndex = -1;
  }

  async function doSearch() {
    const text = (q.value || '').trim();
    if (!text) { closeSuggest(); return; }
    try {
      const data = await api('/api/search?q=' + encodeURIComponent(text));
      const results = data.results || [];
      state.suggestItems = results;
      state.suggestIndex = results.length ? 0 : -1;
      if (!suggest) return;
      if (!results.length) {
        suggest.innerHTML = '<div class="suggest-empty">没有匹配的股票</div>';
        suggest.hidden = false;
        return;
      }
      suggest.innerHTML = results.map(function (r, i) {
        return '<div class="suggest-item' + (i === 0 ? ' active' : '') + '" data-symbol="' + esc(r.symbol) + '">'
             +   '<span class="code">' + esc(r.symbol) + '</span>'
             +   '<span class="nm">' + esc(r.name || '') + '</span>'
             + '</div>';
      }).join('');
      suggest.hidden = false;
      suggest.querySelectorAll('.suggest-item').forEach(function (item) {
        item.addEventListener('mousedown', function (ev) {
          ev.preventDefault();
          closeSuggest();
          openStock(item.getAttribute('data-symbol'));
        });
      });
    } catch (e) {
      showError('搜索失败：' + e.message);
    }
  }

  if (q) {
    q.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        if (state.suggestIndex >= 0 && state.suggestItems[state.suggestIndex]) {
          const pick = state.suggestItems[state.suggestIndex];
          closeSuggest();
          openStock(pick.symbol);
        } else {
          doSearch();
        }
      } else if (ev.key === 'ArrowDown' && !suggest.hidden && state.suggestItems.length) {
        ev.preventDefault();
        state.suggestIndex = Math.min(state.suggestItems.length - 1, state.suggestIndex + 1);
        highlightSuggest();
      } else if (ev.key === 'ArrowUp' && !suggest.hidden && state.suggestItems.length) {
        ev.preventDefault();
        state.suggestIndex = Math.max(0, state.suggestIndex - 1);
        highlightSuggest();
      } else if (ev.key === 'Escape') {
        closeSuggest();
      }
    });

    q.addEventListener('input', function () {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(doSearch, 320);
    });

    q.addEventListener('blur', function () {
      setTimeout(closeSuggest, 150);
    });
  }

  function highlightSuggest() {
    if (!suggest) return;
    suggest.querySelectorAll('.suggest-item').forEach(function (el, i) {
      el.classList.toggle('active', i === state.suggestIndex);
    });
  }

  /* ---- Tab3：时间范围切换 60 / 120 / 250 日 ---- */
  const range = $('#stock-range');
  if (range) {
    range.querySelectorAll('button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const days = Number(btn.getAttribute('data-days'));
        state.stock.days = days;
        range.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        if (state.stock.symbol) {
          loadStock(state.stock.symbol, days).catch(function (e) { showError(e.message); });
        } else {
          showToast('请先搜索并选择一只股票', 'err');
        }
      });
    });
  }

  /* ---- Tab4：筛选 / 搜索 / 分页 ---- */
  const filterBox = $('#cov-filter');
  if (filterBox) {
    filterBox.querySelectorAll('button').forEach(function (btn) {
      btn.addEventListener('click', function () {
        filterBox.querySelectorAll('button').forEach(function (b) { b.classList.remove('active'); });
        btn.classList.add('active');
        state.coverage.filter = btn.getAttribute('data-filter') || 'all';
        state.coverage.offset = 0;
        loadCoverage().catch(function (e) { showError(e.message); });
      });
    });
  }

  const covQ = $('#cov-q');
  if (covQ) {
    let covTimer = null;
    covQ.addEventListener('input', function () {
      if (covTimer) clearTimeout(covTimer);
      covTimer = setTimeout(function () {
        state.coverage.q = (covQ.value || '').trim();
        state.coverage.offset = 0;
        loadCoverage().catch(function (e) { showError(e.message); });
      }, 320);
    });
    covQ.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        if (covTimer) clearTimeout(covTimer);
        state.coverage.q = (covQ.value || '').trim();
        state.coverage.offset = 0;
        loadCoverage().catch(function (e) { showError(e.message); });
      }
    });
  }

  /* ---- Tab4：核查停牌（区分「真停牌」和「我们漏了数据」）---- */
  const btnCovCheck = $('#cov-check');
  if (btnCovCheck) {
    btnCovCheck.addEventListener('click', function () {
      checkSuspended().catch(function (e) { showError(e.message); });
    });
  }

  /* ---- Tab5：三个操作卡片 ---- */
  document.querySelectorAll('.action-card').forEach(function (btn) {
    btn.addEventListener('click', function () {
      submitJob(btn.getAttribute('data-kind'), btn);
    });
  });

  const btnJobsRefresh = $('#btn-jobs-refresh');
  if (btnJobsRefresh) {
    btnJobsRefresh.addEventListener('click', function () {
      loadJobs().catch(function (e) { showError(e.message); });
    });
  }

  const btnLogsRefresh = $('#btn-logs-refresh');
  if (btnLogsRefresh) {
    btnLogsRefresh.addEventListener('click', function () {
      loadLogFiles().catch(function (e) { showError(e.message); });
    });
  }

  /* ---- 运行中任务的「已运行时长」每秒刷新 ---- */
  setInterval(function () {
    document.querySelectorAll('.job-elapsed[data-start]').forEach(function (el) {
      const start = Number(el.getAttribute('data-start'));
      if (!start) return;
      el.textContent = '已运行 ' + fmt.duration(Date.now() - start);
    });
  }, 1000);

  /* ---- 页面重新可见时立即补一次轮询 ---- */
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    Object.keys(state.poll).forEach(function (id) { pollJob(id); });
  });
}

/** 入口 */
function init() {
  bindEvents();
  bindSettingsEvents();
  const t = tabFromHash();
  // 首次加载：switchTab 会按 tab 懒加载（总览 tab 时即拉 /api/overview）
  switchTab(t);
  // 若当前不是「总览」，额外拉一次 /api/overview 以填充顶部 Header 的日期 / 股票数
  if (t !== 'overview') {
    loadOverview().catch(function (e) { showError(e.message); });
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
