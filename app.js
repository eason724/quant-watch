/* 观澜量化 · 自选决策台前端引擎
   口径与后端 Python 引擎一致：T根K线收盘信号 → 下一根开盘成交、滑点、佣金、整手、涨跌停约束。
   支持 日线/60分钟/30分钟 周期，全部计算在浏览器本地完成，数据来自东方财富公开接口。 */

const KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get";
const RT_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get";
const CAP_KEY = "qw_watchlist_v1";
const PARAM_KEY = "qw_params_v1";
const VIEW_KEY = "qw_view_v1";

/* ---------- 数据层 ---------- */

function guessSecids(sym) {
  const s = sym.trim().toUpperCase();
  if (/^\d{6}$/.test(s)) return [("569".includes(s[0]) ? "1." : "0.") + s];
  if (/^\d{1,3}\.[A-Z0-9]+$/.test(s)) return [s];
  return ["105.", "106.", "107."].map(m => m + s);   // 美股: 纳斯达克/纽交所/美交所
}

/* klt: 101=日线, 60/30=分钟线; lmt=根数(含预热缓冲) */
async function fetchKlines(secid, klt, lmt) {
  const url = `${KLINE_URL}?secid=${encodeURIComponent(secid)}` +
    `&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57&klt=${klt}&fqt=1&lmt=${lmt}&end=20500101`;
  const j = await (await fetch(url, { cache: "no-store" })).json();
  const lines = (j && j.data && j.data.klines) || [];
  return lines.map(ln => {
    const [d, o, c, h, l] = ln.split(",");
    return { date: d, open: +o, close: +c, high: +h, low: +l };
  });
}

async function fetchRT(secids) {
  if (!secids.length) return [];
  const j = await (await fetch(`${RT_URL}?fltt=2&fields=f2,f3,f12,f14&secids=${secids.join(",")}`,
    { cache: "no-store" })).json();
  return ((j.data && j.data.diff) || []).map(d => ({
    symbol: d.f12, name: d.f14,
    price: typeof d.f2 === "number" ? d.f2 : null,
    pct: typeof d.f3 === "number" ? d.f3 : null,
  }));
}

/* ---------- 视图状态（周期 × 跨度） ---------- */

const SPANS = { "1m": "近1月", "3m": "近3月", "1y": "近1年", "3y": "近3年" };
const KLTS = { 101: "日线", 60: "60分钟", 30: "30分钟" };
/* 各组合需要显示的根数 */
const SPAN_BARS = {
  101: { "1m": 35, "3m": 75, "1y": 290, "3y": 830 },
  60:  { "1m": 140, "3m": 400, "1y": 1620, "3y": 3100 },
  30:  { "1m": 280, "3m": 800, "1y": 3240, "3y": 5000 },
};
const BUF = 260;   // 指标预热缓冲

function loadView() {
  try {
    const v = JSON.parse(localStorage.getItem(VIEW_KEY));
    if (v && v.klt && v.span) return v;
  } catch (e) { /* 忽略 */ }
  return { klt: 60, span: "3m" };   // 默认：60分钟线 · 近3月（够细、又不失真）
}
function saveView(v) { localStorage.setItem(VIEW_KEY, JSON.stringify(v)); }
let VIEW = loadView();

/* ---------- 市场状态 ---------- */

function marketStatus() {
  const now = new Date();
  const day = now.getDay(), h = now.getHours() + now.getMinutes() / 60;
  const isWkday = day >= 1 && day <= 5;
  const cn = isWkday && h >= 9.25 && h <= 15.05;
  // 美股（北京时间）：夏令时 21:30-04:05，冬令时 22:30-05:05
  const m = now.getMonth() + 1;
  const dst = m >= 3 && m <= 10 || (m === 11 && day > 1 && day <= 7 && h < 22); // 粗略
  const usOpenH = dst ? 21.5 : 22.5, usCloseH = dst ? 4.1 : 5.1;
  const us = usOpenH < usCloseH ? (isWkday && h >= usOpenH && h <= usCloseH)
    : (isWkday && h >= usOpenH) || ((day === 0 || day === 6) && h <= usCloseH) || (day === 6 && h >= usOpenH);
  return { cn: cn ? "开盘中" : "已收盘", us: us ? "开盘中" : "已收盘" };
}

/* ---------- 指标（与 Python 口径一致） ---------- */

function sma(arr, n) {
  const out = new Array(arr.length).fill(null);
  let sum = 0;
  for (let i = 0; i < arr.length; i++) {
    sum += arr[i];
    if (i >= n) sum -= arr[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

function rsiEwm(closes, n) {
  const out = new Array(closes.length).fill(null);
  let gain = 0, loss = 0;
  for (let i = 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = Math.max(d, 0), l = Math.max(-d, 0);
    if (i === 1) { gain = g; loss = l; }
    else { gain += (g - gain) / n; loss += (l - loss) / n; }
    out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  }
  return out;
}

function momentum(closes, i, lb) {
  return i >= lb ? closes[i] / closes[i - lb] - 1 : null;
}

/* ---------- 策略目标 + 人话建议 ---------- */

function makeTargets(type, closes, kl, p) {
  const N = closes.length;
  if (type === "ma") {
    const f = sma(closes, p.maF), s = sma(closes, p.maS);
    return {
      at: i => (f[i] == null || s[i] == null) ? 0 : (f[i] > s[i] ? 1 : 0),
      reason: i => f[i] == null || s[i] == null ? "K线数量不足，无法计算均线" :
        `MA${p.maF}=${f[i].toFixed(2)} ${f[i] > s[i] ? "↑" : "↓"} MA${p.maS}=${s[i].toFixed(2)}`,
      advice: (i, pos) => pos > 0.5
        ? `趋势向上：快线在慢线上方，持股待涨；跌破 MA${p.maS} 或死叉再离场。`
        : `趋势未确认：快线仍在慢线下方，空仓等待金叉再进，不抢反弹。`
    };
  }
  if (type === "rsi") {
    const r = rsiEwm(closes, p.rsiN);
    const pos = new Array(N).fill(0);
    let hold = 0;
    for (let i = 0; i < N; i++) {
      if (r[i] != null) {
        if (r[i] < p.rsiBuy) hold = 1;
        else if (r[i] > p.rsiSell) hold = 0;
      }
      pos[i] = hold;
    }
    return {
      at: i => pos[i],
      reason: i => r[i] == null ? "K线数量不足" :
        `RSI${p.rsiN}=${r[i].toFixed(1)}（${r[i] < p.rsiBuy ? "超卖区" : r[i] > p.rsiSell ? "超买区" : "中性区"}）`,
      advice: (i, cur) => {
        const v = r[i];
        if (v == null) return "K线数量不足。";
        if (cur > 0.5) return `超卖修复中：持有博反弹；RSI 回到 ${p.rsiSell} 以上就止盈离场。`;
        if (v < p.rsiBuy) return `已进入超卖区：跌破 ${p.rsiBuy} 触发买入信号，可以分批留意。`;
        if (v > p.rsiSell) return `短期过热：RSI ${v.toFixed(1)} 超买，此前若有持仓应止盈，现在不建议追。`;
        return `中性区域（${v.toFixed(1)}）：无操作信号，观望为主。`;
      }
    };
  }
  if (type === "grid") {
    const step = p.gridStep / 100, lv = p.gridLv, center = closes[0];
    const unitsAt = i => {
      let u = lv;
      for (let k = 1; k <= lv; k++) {
        if (closes[i] < center * (1 - step * k)) u++;
        if (closes[i] > center * (1 + step * k)) u--;
      }
      return u;
    };
    return {
      at: i => unitsAt(i) / (2 * lv),
      reason: i => `价格 ${closes[i].toFixed(2)}，网格 ${unitsAt(i)}/${2 * lv} 格（中心 ${center.toFixed(2)}）`,
      advice: (i, cur) => {
        const u = unitsAt(i);
        const off = (closes[i] / center - 1) * 100;
        return `网格第 ${u}/${2 * lv} 格（价距中心 ${off >= 0 ? "+" : ""}${off.toFixed(1)}%）：` +
          (u > lv ? `已在中心下方 ${u - lv} 格，越跌越买，每跌 ${p.gridStep}% 补一格。`
            : u < lv ? `在中心上方 ${lv - u} 格，每涨 ${p.gridStep}% 减一格，落袋为安。`
              : `正好在中心，持有现仓即可。`);
      }
    };
  }
  return {
    at: i => { const m = momentum(closes, i, p.momLb); return m == null ? 0 : (m > 0 ? 1 : 0); },
    reason: i => { const m = momentum(closes, i, p.momLb); return m == null ? "K线数量不足" :
      `近${p.momLb}根K线 ${m >= 0 ? "+" : ""}${(m * 100).toFixed(1)}%（${m > 0 ? "动量向上" : "动量走弱"}）`; },
    advice: (i, pos) => {
      const m = momentum(closes, i, p.momLb);
      if (m == null) return "K线数量不足。";
      return pos > 0.5
        ? `动量向上（近${p.momLb}根 +${(m * 100).toFixed(1)}%）：强者恒强，继续持有；动量转负即走。`
        : `动量走弱（近${p.momLb}根 ${(m * 100).toFixed(1)}%）：回避，等动量转正再回来。`;
    }
  };
}

/* ---------- 迷你回测（与 Python 引擎同规则） ---------- */

function limitPct(symbol) {
  const code = symbol.split(".")[1] || "";
  if (!/^\d{6}$/.test(code)) return null;
  return /^(300|301|159|588|688|689)/.test(code) ? 20.0 : 10.0;
}

function miniBacktest(kl, symbol, target) {
  const isCN = /^\d{6}/.test(symbol.split(".")[1] || "");
  const fee = isCN
    ? { buy: 0.00005, sell: 0.00005, tax: 0, minFee: 0.5, slip: 0.0005, lot: 100 }
    : { buy: 0, sell: 0, tax: 0, minFee: 0, slip: 0.0002, lot: 1 };
  const cap = 100000;
  let cash = cap, shares = 0, avgCost = 0;
  const equity = [];
  let pending = null;
  const N = kl.length;
  for (let i = 0; i < N; i++) {
    if (pending != null) {
      const totalEq = cash + shares * kl[i].open;
      const curW = totalEq > 0 ? shares * kl[i].open / totalEq : 0;
      const dw = pending - curW;
      if (Math.abs(dw) >= 0.005) {
        const limit = limitPct(symbol);
        let blockedBuy = false, blockedSell = false;
        if (limit && i > 0) {
          const chg = (kl[i].open / kl[i - 1].close - 1) * 100;
          blockedBuy = chg >= limit - 0.3;
          blockedSell = chg <= -(limit - 0.3);
        }
        if (dw > 0 && !blockedBuy) {
          const px = kl[i].open * (1 + fee.slip);
          const n = Math.floor(Math.floor(Math.min(dw * totalEq, cash) / (px * (1 + fee.buy))) / fee.lot) * fee.lot;
          if (n > 0) {
            const v = n * px, f = Math.max(fee.minFee, v * fee.buy);
            cash -= v + f;
            avgCost = (avgCost * shares + v) / (shares + n);
            shares += n;
          }
        } else if (dw < 0 && shares > 0 && !blockedSell) {
          const px = kl[i].open * (1 - fee.slip);
          let n = pending <= 1e-9 ? shares
            : Math.floor((shares * kl[i].open - pending * totalEq) / px);
          n = Math.floor(Math.min(Math.floor(n), shares) / fee.lot) * fee.lot;
          if (n > 0) {
            const v = n * px;
            const f = Math.max(fee.minFee, v * fee.sell) + v * fee.tax;
            cash += v - f;
            shares -= n;
          }
          if (shares === 0) avgCost = 0;
        }
      }
      pending = null;
    }
    equity.push(cash + shares * kl[i].close);
    const tgt = target.at(i);
    const cur = shares * kl[i].close / (cash + shares * kl[i].close);
    pending = Math.abs(tgt - cur) > 0.005 ? tgt : null;
  }
  const last = kl[N - 1];
  const finalW = cash + shares * last.close > 0 ? shares * last.close / (cash + shares * last.close) : 0;
  return { equity, posNow: finalW };
}

/* ---------- 自选池管理 ---------- */

function loadList() {
  try {
    const v = JSON.parse(localStorage.getItem(CAP_KEY));
    if (Array.isArray(v) && v.length) return v;
  } catch (e) { /* 忽略 */ }
  return [
    { secid: "1.510300", symbol: "510300", name: "沪深300ETF" },
    { secid: "107.SPY", symbol: "SPY", name: "标普500ETF-SPDR" },
  ];
}
function saveList(v) { localStorage.setItem(CAP_KEY, JSON.stringify(v)); }

function loadParams() {
  try { return JSON.parse(localStorage.getItem(PARAM_KEY)) || {}; } catch (e) { return {}; }
}
function saveParams(p) { localStorage.setItem(PARAM_KEY, JSON.stringify(p)); }

function defaultParams() {
  return { maF: 10, maS: 30, rsiN: 14, rsiBuy: 30, rsiSell: 70, gridStep: 3, gridLv: 6, momLb: 20 };
}

/* ---------- 渲染 ---------- */

const STRATS = [["ma", "双均线"], ["mom", "动量"], ["rsi", "均值回归"], ["grid", "网格"]];
const STRAT_COLOR = { ma: "#1F4E79", mom: "#c8281e", rsi: "#7a4ea3", grid: "#0a8f4d" };
const charts = {};

function cssId(s) { return s.replace(/[^A-Za-z0-9]/g, "_"); }

async function addSymbol(input) {
  const sym = input.trim();
  if (!sym) return;
  for (const secid of guessSecids(sym)) {
    try {
      const kl = await fetchKlines(secid, 101, 80);
      if (kl.length > 10) {
        const rt = await fetchRT([secid]);
        return { secid, symbol: sym.toUpperCase(), name: (rt[0] && rt[0].name) || sym.toUpperCase() };
      }
    } catch (e) { /* 试下一个市场 */ }
  }
  return null;
}

function cardHTML(item) {
  const p = Object.assign(defaultParams(), loadParams()[item.secid] || {});
  return `
  <div class="watch-card" id="wc-${cssId(item.secid)}">
    <div class="wc-head">
      <div class="wc-title"><b>${item.name}</b>
        <span class="muted" style="font-size:12px">${item.symbol} · ${/^\d/.test(item.symbol) ? "A股" : "美股"}</span></div>
      <div class="wc-price"><span class="wc-px" id="wcpx-${cssId(item.secid)}">—</span>
        <span class="wc-pct" id="wcpct-${cssId(item.secid)}"></span></div>
      <button class="wc-del" data-del="${item.secid}">移除</button>
    </div>
    <div class="wc-params">
      均线 <input data-p="maF" value="${p.maF}">/<input data-p="maS" value="${p.maS}">
      · RSI <input data-p="rsiN" value="${p.rsiN}">/<input data-p="rsiBuy" value="${p.rsiBuy}">/<input data-p="rsiSell" value="${p.rsiSell}">
      · 网格 <input data-p="gridStep" value="${p.gridStep}">%×<input data-p="gridLv" value="${p.gridLv}">
      <button class="wc-re" data-re="${item.secid}">按此参数重算</button>
    </div>
    <div class="wc-overall" id="wcoa-${cssId(item.secid)}">计算中…</div>
    <div class="wc-sigs" id="wcsig-${cssId(item.secid)}"></div>
    <div class="wc-chart" id="wcch-${cssId(item.secid)}"></div>
    <div class="muted wc-note" id="wcnote-${cssId(item.secid)}"></div>
  </div>`;
}

function paramsOf(item) {
  return Object.assign(defaultParams(), loadParams()[item.secid] || {});
}

function overallAdvice(rows) {
  const avg = rows.reduce((s, r) => s + r.posNow, 0) / rows.length;
  const bulls = rows.filter(r => r.posNow > 0.5).length;
  const bears = rows.filter(r => r.posNow < 0.02).length;
  let verdict, tone;
  if (avg >= 0.75) { verdict = "偏多 —— 多数策略看多，倾向持有/逢低加仓"; tone = "up"; }
  else if (avg >= 0.25) { verdict = "中性分歧 —— 多空不一，轻仓或观望，等方向明朗"; tone = "muted"; }
  else if (avg > 0.001) { verdict = "偏空 —— 多数策略已离场，仅剩少数仓位"; tone = "down"; }
  else { verdict = "一致看空 —— 四策略均空仓，此时入场胜率不高"; tone = "down"; }
  return `<div class="wc-oa-line"><b class="${tone}">综合建议：${verdict}</b>
    <span class="muted">（看多 ${bulls}/4 · 空仓 ${bears}/4）</span></div>`;
}

async function renderCard(item) {
  const holder = document.getElementById("watchCards");
  const divId = `wc-${cssId(item.secid)}`;
  let card = document.getElementById(divId);
  if (!card) { holder.insertAdjacentHTML("beforeend", cardHTML(item)); card = document.getElementById(divId); }
  const sigBox = document.getElementById(`wcsig-${cssId(item.secid)}`);
  sigBox.innerHTML = '<div class="empty">拉取行情并计算中…</div>';

  const bars = SPAN_BARS[VIEW.klt][VIEW.span];
  let kl;
  try { kl = await fetchKlines(item.secid, VIEW.klt, Math.min(bars + BUF, 5000)); } catch (e) { kl = []; }
  if (!kl.length) {
    sigBox.innerHTML = '<div class="empty">该标的取不到数据（检查代码/市场）</div>';
    return;
  }
  const closes = kl.map(k => k.close);
  const p = paramsOf(item);
  const showFrom = Math.max(0, kl.length - bars);   // 显示窗口（缓冲用于指标预热）
  const rows = [];
  for (const [type, cn] of STRATS) {
    const t = makeTargets(type, closes, kl, p);
    const bt = miniBacktest(kl, item.symbol, t);
    const i = closes.length - 1;
    const posNow = bt.posNow;
    const posTxt = posNow > 0.98 ? "满仓持有" : posNow > 0.02 ? `仓位 ${Math.round(posNow * 100)}%` : "空仓观望";
    const eq = bt.equity.slice(showFrom);
    const ret = eq[0] > 0 ? eq[eq.length - 1] / eq[0] - 1 : 0;
    rows.push({ type, cn, bt, reason: t.reason(i), advice: t.advice(i, posNow),
      posTxt, posNow, ret, eqShow: eq });
  }
  document.getElementById(`wcoa-${cssId(item.secid)}`).innerHTML = overallAdvice(rows);
  sigBox.innerHTML = rows.map(r => `
    <div class="wc-sig">
      <span class="wc-tag" style="background:${STRAT_COLOR[r.type]}1a;color:${STRAT_COLOR[r.type]}">${r.cn}</span>
      <b>${r.posTxt}</b>
      <span class="muted" style="font-size:12px">${r.reason}</span>
      <span class="wc-ret ${r.ret >= 0 ? "up" : "down"}">${SPANS[VIEW.span]} ${r.ret >= 0 ? "+" : ""}${(r.ret * 100).toFixed(1)}%</span>
      <div class="wc-adv">${r.advice}</div>
    </div>`).join("");

  const chKey = cssId(item.secid);
  if (!charts[chKey]) charts[chKey] = echarts.init(document.getElementById(`wcch-${chKey}`));
  charts[chKey].setOption({
    grid: { left: 46, right: 8, top: 26, bottom: 22 },
    tooltip: { trigger: "axis", valueFormatter: v => v.toFixed(3) },
    legend: { top: 0, itemWidth: 12, textStyle: { fontSize: 10.5 } },
    xAxis: { type: "category", data: kl.slice(showFrom).map(k => k.date),
      axisLabel: { fontSize: 10, formatter: v => v.slice(VIEW.klt === 101 ? 0 : 5, VIEW.klt === 101 ? 7 : 10) } },
    yAxis: { type: "value", scale: true, axisLabel: { fontSize: 10 } },
    series: rows.map(r => ({
      name: r.cn, type: "line", showSymbol: false,
      data: r.eqShow.map(v => +(v / 100000).toFixed(4)),
      lineStyle: { width: 1.5, color: STRAT_COLOR[r.type] }, itemStyle: { color: STRAT_COLOR[r.type] },
    }))
  }, true);
  window.addEventListener("resize", () => charts[chKey] && charts[chKey].resize());

  document.getElementById(`wcnote-${cssId(item.secid)}`).textContent =
    `迷你回测：${KLTS[VIEW.klt]} · ${SPANS[VIEW.span]} · 初始 ¥100,000 · 信号收盘出、下一根开盘成交（含费用滑点与A股涨跌停约束）· 仅供参考，非投资建议`;
}

async function renderWatchlist() {
  const list = loadList();
  const holder = document.getElementById("watchCards");
  document.getElementById("watchEmpty").style.display = list.length ? "none" : "";
  const existing = new Set(list.map(i => `wc-${cssId(i.secid)}`));
  holder.querySelectorAll(".watch-card").forEach(c => { if (!existing.has(c.id)) c.remove(); });
  await Promise.all(list.map(renderCard));
  renderPoolMomentum();
}

/* 自选池动量排序横幅（固定日线20日口径） */
async function renderPoolMomentum() {
  const list = loadList();
  if (list.length < 2) { document.getElementById("poolMom").innerHTML = ""; return; }
  const rows = [];
  await Promise.all(list.map(async item => {
    try {
      const kl = await fetchKlines(item.secid, 101, 120);
      const closes = kl.map(k => k.close);
      if (closes.length > 21) {
        const m = closes[closes.length - 1] / closes[closes.length - 21] - 1;
        rows.push({ name: item.name, symbol: item.symbol, m });
      }
    } catch (e) { /* 跳过 */ }
  }));
  rows.sort((a, b) => b.m - a.m);
  if (!rows.length) { document.getElementById("poolMom").innerHTML = ""; return; }
  document.getElementById("poolMom").innerHTML =
    `自选池近20日动量（日线口径）：` + rows.map(r =>
      `<span class="pm ${r.m >= 0 ? "up" : "down"}">${r.name} ${r.m >= 0 ? "+" : ""}${(r.m * 100).toFixed(1)}%</span>`
    ).join("　");
}

/* 实时价刷新（自选卡片）+ 倒计时 */
let rtCountdown = 30;
async function refreshWatchRT() {
  rtCountdown = 30;
  const list = loadList();
  if (!list.length) return;
  try {
    const rt = await fetchRT(list.map(i => i.secid));
    const map = {}; rt.forEach(r => map[r.symbol] = r);
    list.forEach(item => {
      const q = map[item.symbol];
      if (!q) return;
      const px = document.getElementById(`wcpx-${cssId(item.secid)}`);
      const pct = document.getElementById(`wcpct-${cssId(item.secid)}`);
      if (px && q.price != null) px.textContent = q.price.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
      if (pct) {
        pct.textContent = q.pct == null ? "" : (q.pct > 0 ? "+" : "") + q.pct.toFixed(2) + "%";
        pct.className = "wc-pct " + (q.pct > 0 ? "up" : q.pct < 0 ? "down" : "muted");
      }
    });
  } catch (e) { /* 静默 */ }
}

setInterval(() => {
  rtCountdown -= 1;
  const el = document.getElementById("rtTimer");
  if (el) {
    const st = marketStatus();
    el.innerHTML = `A股${st.cn} · 美股${st.us} · 实时价格 ${rtCountdown}s 后刷新` +
      `（休市时价格停在最后成交价，属正常）`;
  }
}, 1000);

/* ---------- 事件绑定 ---------- */

function bindWatchEvents() {
  const addBtn = document.getElementById("addBtn");
  const addInput = document.getElementById("addInput");
  async function doAdd() {
    const v = addInput.value.trim();
    if (!v) return;
    addBtn.disabled = true; addBtn.textContent = "识别中…";
    const item = await addSymbol(v);
    addBtn.disabled = false; addBtn.textContent = "添加";
    addInput.value = "";
    if (!item) { alert("没找到该标的：请检查代码（A股6位数字 / 美股如 SPY、AAPL）"); return; }
    const list = loadList();
    if (list.some(x => x.secid === item.secid)) { alert("已在自选中"); return; }
    list.push(item); saveList(list);
    renderWatchlist();
  }
  addBtn.addEventListener("click", doAdd);
  addInput.addEventListener("keydown", e => { if (e.key === "Enter") doAdd(); });

  document.getElementById("watchCards").addEventListener("click", e => {
    const del = e.target.closest("[data-del]");
    if (del) {
      if (!confirm("从自选中移除？")) return;
      saveList(loadList().filter(x => x.secid !== del.dataset.del));
      renderWatchlist();
      return;
    }
    const re = e.target.closest("[data-re]");
    if (re) {
      const p = paramsOf({ secid: re.dataset.re });
      document.querySelectorAll(`#wc-${cssId(re.dataset.re)} [data-p]`).forEach(inp => {
        const v = parseFloat(inp.value);
        if (!isNaN(v) && v > 0) p[inp.dataset.p] = v;
      });
      const all = loadParams(); all[re.dataset.re] = p; saveParams(all);
      renderWatchlist();
    }
  });

  // 周期/跨度切换
  const kltSel = document.getElementById("kltSel"), spanSel = document.getElementById("spanSel");
  kltSel.value = String(VIEW.klt); spanSel.value = VIEW.span;
  const onViewChange = () => {
    VIEW = { klt: +kltSel.value, span: spanSel.value };
    saveView(VIEW);
    Object.keys(charts).forEach(k => { charts[k].dispose(); delete charts[k]; });
    document.getElementById("watchCards").innerHTML = "";
    renderWatchlist();
  };
  kltSel.addEventListener("change", onViewChange);
  spanSel.addEventListener("change", onViewChange);
}

function initWatchTab() {
  bindWatchEvents();
  renderWatchlist();
  refreshWatchRT();
  setInterval(refreshWatchRT, 30000);
}
