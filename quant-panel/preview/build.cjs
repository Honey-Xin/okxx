/**
 * 生成面板的静态预览，用来在真实浏览器里看 CSS 效果，不用启动 DSH。
 *
 * CSS 是构建时从 client.js 里抠出来的 —— 单一来源，所以预览看到的就是插件
 * 实际会渲染的样式。HTML 镜像了组件树，改组件时要同步改这里（这是预览，
 * 不是第二套实现）。
 */
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')

const ROOT = __dirname
const CLIENT = path.join(ROOT, '..', 'client.js')
const STATE = process.env.QUANT_STATE || 'D:/dshwork/okx_quant/state/dashboard.json'

const src = fs.readFileSync(CLIENT, 'utf8')
// client.js 里 CSS 后面可能带分号也可能不带（ASI），两种都吃
const m = src.match(/const CSS = `([\s\S]*?)`;?\s*\n\s*return \{/)
if (!m) {
  console.error('could not extract CSS from client.js')
  process.exit(1)
}
const CSS = m[1]
const d = JSON.parse(fs.readFileSync(STATE, 'utf8'))

// ---------------------------------------------------------------- 格式化
const esc = (s) =>
  String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')

const num = (v, dg = 2) => {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US', { minimumFractionDigits: dg, maximumFractionDigits: dg })
}

const priceFmt = (v) => {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  const a = Math.abs(n)
  if (a >= 1) return num(n, 2)
  if (a >= 0.01) return num(n, 4)
  return num(n, 6)
}

const signed = (v, dg = 2) => {
  const n = Number(v)
  if (!Number.isFinite(n)) return '—'
  return (n > 0 ? '+' : '') + n.toFixed(dg) + '%'
}

const toneOf = (v) => {
  const n = Number(v)
  if (!Number.isFinite(n) || n === 0) return ''
  return n > 0 ? 'tone-up' : 'tone-down'
}

const clock = (iso) => {
  if (!iso) return '—'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return '—'
  const p = (n) => String(n).padStart(2, '0')
  return p(dt.getHours()) + ':' + p(dt.getMinutes()) + ':' + p(dt.getSeconds())
}

const utcClock = (iso) => {
  if (!iso) return '—'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return String(iso).slice(0, 16)
  const p = (n) => String(n).padStart(2, '0')
  return p(dt.getUTCHours()) + ':' + p(dt.getUTCMinutes()) + ' UTC'
}

const ageHours = (iso) => {
  if (!iso) return null
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return null
  return (Date.now() - dt.getTime()) / 3600000
}

const minutesAgo = (iso) => {
  if (!iso) return '—'
  const dt = new Date(iso)
  if (Number.isNaN(dt.getTime())) return '—'
  const mins = Math.round((Date.now() - dt.getTime()) / 60000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return mins + ' 分钟前'
  const h = Math.round(mins / 60)
  if (h < 24) return h + ' 小时前'
  return Math.round(h / 24) + ' 天前'
}

const tfHours = (tf) => {
  const g = /^(\d+)([mhd])$/.exec(String(tf || '1h'))
  if (!g) return 1
  const n = Number(g[1])
  if (g[2] === 'm') return n / 60
  if (g[2] === 'd') return n * 24
  return n
}

const GRADE = {
  good: { label: '可做', cls: 'g-good' },
  marginal: { label: '勉强', cls: 'g-mid' },
  poor: { label: '不值', cls: 'g-poor' },
}

// ---------------------------------------------------------------- 小部件
const sparkline = (vals) => {
  const pts = (vals || []).map(Number).filter((v) => Number.isFinite(v))
  if (pts.length < 2) return '<div class="qp-spark-empty"></div>'
  const lo = Math.min.apply(null, pts)
  const hi = Math.max.apply(null, pts)
  const span = hi - lo || 1
  const step = 100 / (pts.length - 1)
  const path = pts
    .map((v, i) => (i ? 'L' : 'M') + (i * step).toFixed(2) + ' ' + (30 - ((v - lo) / span) * 28 - 1).toFixed(2))
    .join(' ')
  const up = pts[pts.length - 1] >= pts[0]
  return `<svg class="qp-spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">
  <path d="${path}" fill="none" stroke="${up ? 'var(--qp-up)' : 'var(--qp-down)'}" stroke-width="1.2" vector-effect="non-scaling-stroke" stroke-linejoin="round" />
</svg>`
}

const pill = (tone, text) => `<span class="qp-pill${tone ? ' qp-pill-' + tone : ''}">${esc(text)}</span>`

const card = (title, extra, body, wide) => `<section class="qp-card${wide ? ' qp-wide' : ''}">
  <header class="qp-card-head"><span class="qp-card-title">${esc(title)}</span><span class="qp-card-extra">${extra || ''}</span></header>
  <div class="qp-card-body">${body}</div>
</section>`

const chip = (k, v) => `<span class="qp-chip"><i>${k}</i><b>${v}</b></span>`

const sw = (on) => `<button type="button" role="switch" class="qp-switch${on ? ' qp-switch-on' : ''}"><span class="qp-switch-knob"></span></button>`

const stat = (label, value, sub) => `<div class="qp-stat">
  <div class="qp-stat-label">${esc(label)}</div>
  <div class="qp-stat-value">${value}</div>
  ${sub ? `<div class="qp-stat-sub">${esc(sub)}</div>` : ''}
</div>`

// ---------------------------------------------------------------- 顶部
const watch = d.watch || {}
const mode = d.mode || {}
const acc = d.account || {}
const positions = acc.positions || []
const f = d.funding || {}
const st = d.strategy || {}
const cand = st.candidates || {}
const acts = cand.actions || []
const results = cand.results || []
const dec = d.decision || {}
const au = d.audit || {}

const hero = `<div class="qp-hero">
  <div class="qp-hero-line">
    <span class="qp-live"><span class="qp-live-dot"></span>实时监控中</span>
    <div class="qp-badges">
      ${mode.dry_run ? pill('warn', '干跑模式') : pill('bad', '实盘')}
      ${pill('muted', positions.length + ' 个持仓')}
      <span class="qp-hero-time">数据 ${clock(d.as_of)}</span>
      <button type="button" class="qp-btn qp-btn-ghost">刷新</button>
    </div>
  </div>
  <h1 class="qp-h1">AI 量化监控</h1>
  <p class="qp-sub">${esc((mode.exchange || 'okx').toUpperCase())} · ${(st.strategies || []).length} 个策略 · ${st.enabled_count || 0} 个启用 · 单币 ${num(mode.max_usd_per_coin, 0)} / 总 ${num(mode.max_total_usd, 0)} USDT</p>
</div>`

const watchBar = `<section class="qp-watch">
  <div class="qp-watch-main">
    <div class="qp-watch-label"><span class="qp-watch-dot${watch.enabled ? ' on' : ''}"></span>自动盯盘</div>
    <div class="qp-watch-controls">
      ${sw(!!watch.enabled)}
      <span class="qp-watch-sep"></span>
      <select class="qp-select">${[5, 10, 15, 30, 60, 120, 240].map((m) => `<option${m === (watch.interval_min || 15) ? ' selected' : ''}>每 ${m} 分钟</option>`).join('')}</select>
      <label class="qp-watch-scan" title="关掉后不再扫全市场资金费率，只跑单币合约规则">${sw(!!watch.scan_funding)}<span>扫费率</span></label>
      <button type="button" class="qp-btn">立即跑一轮</button>
      <button type="button" class="qp-btn qp-btn-ghost">刷新数据</button>
    </div>
  </div>
  <div class="qp-watch-meta">
    ${watch.enabled ? '<span>下一轮 <b class="qp-watch-count">14:32</b></span>' : '<span class="qp-dim">已暂停</span>'}
    ${watch.ticks ? `<span class="qp-dim">· 已跑 ${watch.ticks} 轮</span>` : ''}
    ${watch.last_tick ? `<span class="qp-dim">· 上次 ${clock(watch.last_tick)}</span>` : ''}
    <span class="qp-dim">· 硬限 单币 ${num(mode.max_usd_per_coin, 0)} / 总 ${num(mode.max_total_usd, 0)} USDT</span>
  </div>
</section>`

const tabs = (active) => `<div class="qp-tabs">
  <button type="button" class="qp-tab${active === 'dir' ? ' qp-tab-on' : ''}"><b>合约方向</b><i>看标的行情与触发条件</i></button>
  <button type="button" class="qp-tab${active === 'arb' ? ' qp-tab-on' : ''}"><b>费率套利</b><i>买现货空永续</i></button>
</div>`

// ---------------------------------------------------------------- 交易标的
const gateOk = (val, th, cmp) => {
  const n = Number(val)
  const t = Number(th)
  if (!Number.isFinite(n) || !Number.isFinite(t)) return null
  if (cmp === '<') return n < t
  if (cmp === '>') return n > t
  if (cmp === '>=') return n >= t
  if (cmp === '<=') return n <= t
  return null
}

const gateRow = (label, value, suffix, cmp, th, thSuffix, ok) => `<div class="qp-gate ${ok === true ? 'qp-gate-ok' : ok === false ? 'qp-gate-no' : 'qp-gate-na'}">
  <span class="qp-gate-mark">${ok === true ? '✓' : ok === false ? '×' : '–'}</span>
  <span class="qp-gate-label">${label}</span>
  <b>${value}</b>${suffix ? `<i>${suffix}</i>` : ''}
  ${(cmp && th !== undefined && th !== null) ? `<span class="qp-gate-th">${cmp} ${th}${thSuffix || ''}</span>` : ''}
</div>`

const ruleValueText = (v) => {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (Number.isFinite(n)) return Math.abs(n) >= 100 ? num(n, 0) : num(n, 2)
  return String(v)
}

const argsText = (args) => {
  const parts = Object.entries(args || {}).map(([, v]) =>
    (typeof v === 'number' ? (Math.abs(v) >= 100 ? num(v, 0) : num(v, Math.abs(v) < 1 ? 2 : 1)) : String(v)))
  return parts.length ? '(' + parts.join('/') + ')' : ''
}

const targetCard = (t) => {
  const lim = t.limits || {}
  const age = t.bar_time ? ageHours(t.bar_time) : null
  const stale = age !== null && age > tfHours(t.timeframe) * 1.5

  // 行情取不到时优先把原因说出来，别让人对着一个空卡片猜
  if (t.error) {
    return `<div class="qp-target">
  <div class="qp-target-head">
    <div>
      <div class="qp-target-coin"><b>${esc(t.coin)}</b><span class="qp-target-tf">${esc(t.timeframe)}</span></div>
      <div class="qp-target-names">${esc((t.strategy_names || []).join(' · '))}</div>
    </div>
    <div class="qp-target-price"><b>—</b><i>USDT</i></div>
  </div>
  <div class="qp-err">行情不可用：${esc(t.error)}</div>
</div>`
  }

  const groups = results
    .filter((r) => (t.strategy_ids || []).indexOf(r.strategy_id) >= 0 && r.state && (r.state.rules || []).length)
    .map((r) => r.state)

  const ruleBlock = groups.length
    ? `<div class="qp-rule-groups">${groups.map((st) => {
      const okCount = st.rules.filter((x) => x.ok).length
      return `<div class="qp-rule-group">
        <div class="qp-rule-group-head">
          <b>${esc(st.template_label || st.template || '规则')}</b>
          <span class="qp-dim">${okCount}/${st.rules.length} 条通过</span>
          <span class="${st.fired ? 'tone-up qp-rule-fired' : 'qp-dim'}">${st.fired ? '已触发' : '未触发'}</span>
        </div>
        <div class="qp-gates qp-gates-one">
          ${st.rules.map((r) => gateRow(esc(r.label) + argsText(r.args), ruleValueText(r.value), '', '', null, '', r.ok)).join('')}
        </div>
      </div>`
    }).join('')}</div>`
    : '<div class="qp-empty-sm">还没有规则评估结果 —— 点右上角「立即跑一轮」，或者等自动盯盘到点。</div>'

  const anyFired = groups.some((s) => s.fired)
  const verdict = groups.length
    ? `<span class="${anyFired ? 'tone-up' : 'qp-dim'}">${anyFired ? '条件已全部满足（去「AI 复核」看是否成交）' : '还没有策略满足条件'}</span>`
    : ''

  return `<div class="qp-target">
  <div class="qp-target-head">
    <div>
      <div class="qp-target-coin"><b>${esc(t.coin)}</b><span class="qp-target-tf">${esc(t.timeframe)}</span></div>
      <div class="qp-target-names">${esc((t.strategy_names || []).join(' · '))}</div>
    </div>
    <div class="qp-target-price"><b>${priceFmt(t.price)}</b><i>USDT</i></div>
  </div>
  ${t.spark && t.spark.length ? `<div class="qp-target-spark">${sparkline(t.spark)}<span class="qp-target-spark-note">最近 ${t.spark.length} 根收盘</span></div>` : ''}
  ${ruleBlock}
  ${verdict ? `<div class="qp-target-verdict">${verdict}</div>` : ''}
  <div class="qp-target-limits">
    <span>单笔 ${num(lim.usd, 0)} USDT</span>
    <span>杠杆 ≤${num(lim.leverage, 0)}x</span>
    <span>止损 ≤${num(lim.stop_loss_pct, 1)}%</span>
    <span>止盈 ≤${num(lim.take_profit_pct, 1)}%</span>
    <span>置信度 ≥${num(lim.min_confidence, 2)}</span>
    <span class="qp-target-age${stale ? ' qp-target-age-stale' : ''}">${t.source === 'gate' ? 'Gate 现货' : (t.source === 'okx' ? 'OKX 永续' : '数据源未知')} · K 线 ${utcClock(t.bar_time)}${stale ? ' · 已 ' + Math.round(age) + ' 小时未更新' : ''}</span>
  </div>
</div>`
}

const targets = (d.watchlist && d.watchlist.targets) || []
const dirActs = acts.filter((a) => (a.type || 'funding_arb') === 'directional')
const HAS_KEY = !!(process.env.DEEPSEEK_API_KEY || '').trim()
const pend = st.pending || {}
const appr = st.approved || {}
const orders = appr.orders || []

const task = st.task || {}
const auto = st.auto || {}
const autoItems = (auto.items || []).filter((i) => i && i.order)

const llmCard = `<div>
  <div class="qp-fund-head">
    ${stat('待复核定价', num(auto.awaiting, 0), auto.as_of ? minutesAgo(auto.as_of) : '还没定价')}
    ${stat('已通过校验', num(appr.count, 0), appr.as_of ? minutesAgo(appr.as_of) : '')}
    ${stat('待定价候选', num(pend.count, 0), task.as_of ? '任务书 ' + minutesAgo(task.as_of) : '还没生成任务书')}
  </div>
  <div class="qp-form-note">每轮 tick 先用规则把定价算出来（ATR 定止损止盈、区间位置定仓位），标成「待复核」；复核通过才进 approved。不需要 API key —— 说一句「复核定价」就按快照重新判断，同意转正、不同意否决。</div>
  ${autoItems.length ? `<div class="qp-cand-rows">${autoItems.map((i) => {
    const o = i.order
    return `<div class="qp-cand-row qp-cand-row-pending">
      <span class="qp-cand-name">${esc(o.coin)} ${o.side === 'long' ? '做多' : '做空'}</span>
      <span class="qp-cand-nums">entry ${priceFmt(o.entry_suggested)} · SL ${priceFmt(o.stop_loss)} · TP ${priceFmt(o.take_profit)} · ${num(o.leverage, 1)}x · ${num(o.usd, 0)}U</span>
      <span class="qp-cand-note">规则定价 · 待复核 · 置信 ${num(o.confidence, 2)}</span>
    </div>`
  }).join('')}</div>` : ''}
  ${acts.length ? `<div class="qp-acts">${acts.map((a) => `<div class="qp-act qp-act-dir">
      <div class="qp-act-top">
        <span class="qp-act-side ${a.side === 'long' ? 'tone-up' : 'tone-down'}">${a.side === 'long' ? '做多' : '做空'}</span>
        <b>${esc(a.coin)}</b><span class="qp-dim"> ${esc(a.timeframe || '')}</span>
        <span class="qp-act-usd">${num(a.usd, 0)} USDT</span>
        <span class="qp-act-tag">${esc(a.pricing_label || '已复核')}</span>
      </div>
      <span class="qp-act-reason">${esc(a.reason || '')}</span>
    </div>`).join('')}</div>` : ''}
  ${orders.length ? `<div class="qp-cand-rows">${orders.map((o) => `<div class="qp-cand-row">
      <span class="qp-cand-name">${esc(o.coin)} ${o.side === 'long' ? '做多' : '做空'}</span>
      <span class="qp-cand-nums">entry ${priceFmt(o.entry_suggested)} · SL ${priceFmt(o.stop_loss)} · TP ${priceFmt(o.take_profit)} · ${num(o.leverage, 1)}x · ${num(o.usd, 0)}U</span>
      <span class="qp-cand-note">${o.model === 'rule-engine' ? '规则定价 · ' : ''}置信度 ${num(o.confidence, 2)}</span>
    </div>`).join('')}</div>` : ''}  <div class="qp-form-actions">
    <button type="button" class="qp-btn qp-btn-sm">重新定价</button>
    <span class="qp-dim">任务书：state/llm_task.json</span>
  </div>
</div>`


const targetsCard = card(
  '交易标的',
  targets.length ? targets.length + ' 个标的在盯' : '没有启用中的单币策略',
  targets.length
    ? `<div class="qp-targets">${targets.map(targetCard).join('')}</div>`
    : '<div class="qp-empty">还没有启用中的「单币合约」策略。</div>',
  true,
)

const dirCandCard = card(
  '待复核',
  dirActs.length ? dirActs.length + ' 个动作' : '无',
  dirActs.length
    ? `<div class="qp-acts">${dirActs.map((a) => `<div class="qp-act qp-act-dir">
        <div class="qp-act-top">
          <span class="qp-act-side ${a.side === 'long' ? 'tone-up' : 'tone-down'}">${a.side === 'long' ? '做多' : '做空'}</span>
          <b>${esc(a.coin)}</b><span class="qp-dim"> ${esc(a.timeframe || '')}</span>
          <span class="qp-act-usd">${num(a.usd, 0)} USDT</span>
          <span class="qp-act-tag">${esc(a.pricing_label || '已复核')}</span>
        </div>
        <div class="qp-act-metrics">RSI ${num(a.rsi, 1)} · 区间 ${num(a.pos, 1)}% · 量比 ${num(a.vol_ratio, 2)} · ATR ${num(a.atr_pct, 2)}%</div>
        <span class="qp-act-reason">${esc(a.reason || '')}</span>
      </div>`).join('')}</div>`
    : '<div class="qp-empty">没有方向策略跑出动作 —— 规则没触发，或者还没到下一轮。</div>',
  true,
)

// ---------------------------------------------------------------- 费率表
const frowsAll = f.rows || []
const frows = frowsAll.slice().sort((a, b) => (Number(b.apr) || 0) - (Number(a.apr) || 0)).slice(0, 10)
const maxApr = frowsAll.reduce((a, r) => Math.max(a, Number(r.apr) || 0), 0)
const scale = Math.max(30, maxApr)
const markPct = (30 / scale) * 100
const goodCount = frowsAll.filter((r) => r.grade === 'good').length

const funding = `<div>
  <div class="qp-fund-head">
    ${stat('候选标的', num(f.count || frowsAll.length, 0))}
    ${stat('最高年化', num(maxApr, 2) + '%')}
    ${stat('可做', num(goodCount, 0), '门槛 30%')}
    ${stat('数据', f.stale ? '已过期' : minutesAgo(f.as_of))}
  </div>
  <table class="qp-table">
    <thead><tr>
      <th class="qp-th-idx">#</th><th>币种</th><th>年化</th><th class="qp-th-bar"></th>
      <th>单期费率</th><th>结算</th><th>基差</th><th>现货价</th><th class="qp-th-grade">评级</th>
    </tr></thead>
    <tbody>${frows.map((r, i) => {
      const w = Math.min(100, (Number(r.apr) || 0) / scale * 100)
      const g = GRADE[r.grade] || GRADE.poor
      return `<tr${r.grade === 'good' ? ' class="qp-row-top"' : ''}>
        <td class="qp-idx">${String(i + 1).padStart(2, '0')}</td>
        <td class="qp-coin-name">${esc(r.coin)}</td>
        <td class="qp-apr">${num(r.apr, 2)}%</td>
        <td class="qp-td-bar"><div class="qp-bar"><div class="qp-bar-fill flat" style="width:${w}%"></div>${markPct > 0 && markPct < 100 ? `<div class="qp-bar-mark" style="left:${markPct}%"></div>` : ''}</div></td>
        <td class="qp-mono">${Number(r.rate).toFixed(6)}</td>
        <td class="qp-dim">${(r.interval_h || '—')}h</td>
        <td class="qp-mono ${toneOf(r.basis_pct)}">${signed(r.basis_pct, 3)}%</td>
        <td class="qp-mono">${priceFmt(r.spot)}</td>
        <td class="qp-grade-cell"><span class="qp-grade ${g.cls}">${g.label}</span></td>
      </tr>`
    }).join('')}</tbody>
  </table>
  ${frowsAll.length > frows.length ? `<div class="qp-table-foot">共 ${frowsAll.length} 个可对冲候选，按年化从高到低取前 ${frows.length} 个</div>` : ''}
</div>`

const arbCand = `<div>
  <div class="qp-cand-head">
    ${pill(cand.actions && cand.actions.length ? 'warn' : 'muted', cand.actions && cand.actions.length ? cand.actions.length + ' 个待复核' : '无待复核动作')}
    <span class="qp-dim">上次评估 ${minutesAgo(cand.as_of)}</span>
  </div>
  <div class="qp-cand-rows">${results
    .filter((r) => (r.type || 'funding_arb') === 'funding_arb')
    .map((r) => `<div class="qp-cand-row">
      <span class="qp-cand-name">${esc(r.name || r.strategy_id)}</span>
      <span class="qp-cand-nums"><b>命中 ${num(r.matched, 0)}</b><span class="qp-dim"> 拒绝 ${num(r.rejected, 0)}</span></span>
      ${r.note ? `<span class="qp-cand-note">${esc(r.note)}</span>` : ''}
    </div>`).join('')}</div>
</div>`

// ---------------------------------------------------------------- 账户 / 决策 / 审计
const account = `<div>
  <div class="qp-acct-head">
    ${stat('USDT 总额', num(acc.usdt_total, 4))}
    ${stat('可用', num(acc.usdt_free, 4))}
    ${stat('持仓', num(positions.length, 0), '已占保证金')}
  </div>
  ${positions.map((p) => `<div class="qp-pos">
    <div class="qp-pos-top">
      <b>${esc(p.symbol)}</b>
      <span class="qp-pos-side">${p.side === 'long' ? '多头' : '空头'} ${num(p.leverage, 0)}x</span>
    </div>
    <div class="qp-pos-pnl ${toneOf(p.upnl)}">
      <b>${(Number(p.upnl) >= 0 ? '+' : '') + num(p.upnl, 4)}</b>
      <span>${num(p.pct, 1)}%</span>
    </div>
    <div class="qp-pos-grid">
      <span>张数<b>${num(p.contracts, 4)}</b></span>
      <span>开仓<b>${num(p.entry, 6)}</b></span>
      <span>标记<b>${num(p.mark, 6)}</b></span>
      <span>名义<b>${num(p.notional, 3)}</b></span>
      <span class="qp-pos-liq">强平<b>${num(p.liq, 6)}</b></span>
    </div>
  </div>`).join('')}
</div>`

const conf = Math.max(0, Math.min(1, Number(dec.confidence) || 0))
const REGIME_LABEL = { trend_up: '趋势上行', trend_down: '趋势下行', range: '区间震荡', high_vol: '高波动', unclear: '不明' }
const decision = dec.present ? `<div class="qp-dec">
  <div class="qp-dec-head">
    <span class="qp-dec-regime">${esc(REGIME_LABEL[dec.regime] || dec.regime || '未知')}</span>
    ${dec.source === 'rule' ? pill('muted', '规则生成') : (dec.veto ? pill('warn', 'AI 已否决') : pill('good', '通过'))}
  </div>
  <div class="qp-conf">
    <span class="qp-conf-label">决策信心</span>
    <div class="qp-conf-track"><div class="qp-conf-fill" style="width:${conf * 100}%"></div></div>
    <span class="qp-conf-val">${conf.toFixed(2)}</span>
  </div>
  ${dec.veto_reason ? `<div class="qp-veto"><span class="qp-veto-label">否决理由</span>${esc(dec.veto_reason)}</div>` : ''}
  ${dec.notes ? `<div class="qp-notes">${esc(dec.notes)}</div>` : ''}
  <div class="qp-dec-foot">更新于 ${clock(dec.as_of)}</div>
</div>` : '<div class="qp-empty">还没有决策记录</div>'

const counts = au.counts || {}
const audit = au.total ? `<div>
  <div class="qp-audit-head">
    ${Object.keys(counts).map((k) => pill(k === 'execute' ? 'good' : k === 'reject' ? 'bad' : 'muted', k + ' ' + counts[k])).join('')}
    <span class="qp-dim">总计 ${num(au.total, 0)}</span>
  </div>
  <div class="qp-timeline">${(au.recent || []).map((e) => {
    const kind = e.event || e.action || 'noop'
    const text = e.text
      || (e.decision && (e.decision.veto_reason || e.decision.notes))
      || e.reason || ''
    return `<div class="qp-tl-item">
      <span class="qp-tl-dot ${esc(kind)}"></span>
      <span class="qp-tl-time">${clock(e.ts || e.at)}</span>
      <span class="qp-tl-kind">${esc(e.llm_event ? 'ai·' + e.llm_event : kind)}</span>
      <span class="qp-tl-text">${esc(text)}</span>
    </div>`
  }).join('')}</div>
</div>` : '<div class="qp-empty">还没有执行记录</div>'

// ---------------------------------------------------------------- 策略卡
const stateByStrategy = {}
results.forEach((r) => { if (r.state && r.strategy_id) stateByStrategy[r.strategy_id] = r.state })

const renderStrategy = (s) => {
  const p = s.params || {}
  const isDir = s.type === 'directional'
  const stt = stateByStrategy[s.id]
  const chips = isDir
    ? [
      chip('币种', esc((p.coin || '?') + ' · ' + (p.timeframe || '?'))),
      chip('方向', p.side === 'both' ? '跟随模板' : p.side === 'long' ? '只做多' : '只做空'),
      chip('信号', esc((stt && stt.template_label) || p.template || '—')),
      chip('单笔', num(p.usd, 0) + ' U'),
      chip('杠杆', '≤' + num(p.leverage, 0) + 'x'),
      chip('止损', '≤' + num(p.stop_loss_pct, 1) + '%'),
      chip('止盈', '≤' + num(p.take_profit_pct, 1) + '%'),
      chip('置信度', '≥' + num(p.min_confidence, 2)),
    ]
    : [
      chip('门槛', num(p.min_apr, 0) + '%'),
      chip('基差', '±' + num(p.max_basis, 1) + '%'),
      chip('单币', num(p.max_usd_per_coin, 0)),
      chip('总敞口', num(p.max_total_usd, 0)),
      chip('持仓', num(p.max_positions, 0) + ' 个'),
      chip('杠杆', num(p.leverage, 1) + 'x'),
      chip('止损', num(p.stop_loss_pct, 1) + '%'),
      chip('标的池', (p.coins && p.coins.length) ? p.coins.join(' ') : '全部可对冲'),
    ]

  const live = (isDir && stt) ? (() => {
    const rules = stt.rules || []
    const okCount = rules.filter((r) => r.ok).length
    const firstBad = rules.find((r) => !r.ok)
    return `<div class="qp-strat-live">
    <span class="qp-strat-live-tag">最新</span>
    <span>${esc(stt.coin || '')} ${esc(stt.timeframe || '')} ${priceFmt(stt.price)}</span>
    <span class="${stt.fired ? 'tone-up' : ''}">${esc(stt.template_label || stt.template || '—')}</span>
    <span>规则 <b>${okCount}/${rules.length}</b> 通过</span>
    ${stt.fired
      ? '<span class="tone-up">已触发</span>'
      : (firstBad ? `<span class="qp-strat-live-bad">卡在 ${esc(firstBad.label)}</span>` : '')}
  </div>`
  })() : ''

  return `<div class="qp-strat">
    <div class="qp-strat-top">
      <div class="qp-strat-name">
        <b>${esc(s.name || s.id)}</b>
        <span class="qp-strat-id${isDir ? ' qp-strat-id-dir' : ''}">${isDir ? '单币合约 · 方向' : '费率套利'}</span>
      </div>
      <div class="qp-strat-actions">
        ${sw(!!s.enabled)}
        <button type="button" class="qp-link">编辑</button>
        <button type="button" class="qp-link qp-link-danger">删除</button>
      </div>
    </div>
    <div class="qp-strat-chips">${chips.join('')}</div>
    ${live}
  </div>`
}

const dirStratList = (st.strategies || []).filter((x) => x.type === 'directional').map(renderStrategy)
const arbStratList = (st.strategies || []).filter((x) => (x.type || 'funding_arb') === 'funding_arb').map(renderStrategy)

const DIR_FORM_FIELDS = [
  ['币种', 'text', '例如 BTC'],
  ['周期', 'select', ['1m', '5m', '15m', '30m', '1h', '4h', '1d']],
  ['方向', 'select', ['跟随模板', '只做多', '只做空']],
  ['单笔 USDT', 'number', '开仓名义金额'],
  ['杠杆上限', 'number', 'AI 只能往低里调'],
  ['止损上限 %', 'number', '止损最多放这么远'],
  ['止盈上限 %', 'number', '止盈最远到这里'],
  ['置信度门槛', 'number', '0~1，低于就不开'],
]

// 模板与条件的真源是 patterns.py，构建时直接问它要，不在 JS 里抄一份
function loadPatternMeta() {
  const code = [
    'import json, sys',
    "sys.path.insert(0, 'D:/dshwork/okx_quant')",
    'import patterns as PT',
    'print(json.dumps({',
    "  'templates': [{'key': k, 'label': v['label'], 'desc': v['desc'], 'side': v['side'], 'rules': v['rules']} for k, v in PT.TEMPLATES.items()],",
    "  'conditions': {k: {'label': v['label'], 'hint': v['hint'], 'args': v['args']} for k, v in PT.CONDITIONS.items()},",
    '}, ensure_ascii=False))',
  ].join('\n')
  const r = spawnSync('python', ['-c', code], {
    encoding: 'utf8',
    timeout: 60000,
    windowsHide: true,
    env: Object.assign({}, process.env, { PYTHONIOENCODING: 'utf-8' }),
  })
  if (r.status !== 0 || !r.stdout) {
    console.error('[warn] read patterns.py failed:', (r.stderr || '').slice(0, 200))
    return { templates: [], conditions: {} }
  }
  return JSON.parse(r.stdout)
}

const PMETA = loadPatternMeta()

const formField = ([label, kind, hint]) => {
  const control = kind === 'select'
    ? `<select class="qp-select">${hint.map((o) => `<option>${esc(o)}</option>`).join('')}</select>`
    : `<input class="qp-input" placeholder="${esc(hint)}" />`
  return `<label class="qp-field">
    <span class="qp-field-label">${esc(label)}</span>
    ${control}
  </label>`
}

const tplCard = (t, on) => `<button type="button" class="qp-tpl${on ? ' qp-tpl-on' : ''}">
  <div class="qp-tpl-top">
    <b>${esc(t.label)}</b>
    <span class="qp-tpl-side ${t.side === 'long' ? 'tone-up' : 'tone-down'}">${t.side === 'long' ? '做多' : '做空'}</span>
  </div>
  <i>${esc(t.desc)}</i>
</button>`

const ruleFormRow = (r) => {
  const spec = (PMETA.conditions || {})[r.cond] || { label: r.cond, hint: '', args: {} }
  const args = Object.entries(spec.args || {}).map(([k, m]) => {
    const v = (r.args && r.args[k] !== undefined) ? r.args[k] : m.default
    return `<label class="qp-rule-arg"><span>${esc(m.label)}</span>
      <input class="qp-input qp-input-sm" value="${esc(String(v))}" /></label>`
  }).join('')
  return `<div class="qp-rule">
    <div class="qp-rule-main"><b>${esc(spec.label)}</b>${spec.hint ? `<span class="qp-rule-hint">${esc(spec.hint)}</span>` : ''}</div>
    <div class="qp-rule-args">${args || '<span class="qp-dim">无参数</span>'}</div>
    <button type="button" class="qp-link qp-link-danger qp-rule-del">移除</button>
  </div>`
}

const dirStrat = (st.strategies || []).filter((x) => x.type === 'directional')[0]
const curTpl = (dirStrat && dirStrat.params && dirStrat.params.template) || 'trend_pullback'
const curTplInfo = (PMETA.templates || []).find((t) => t.key === curTpl)
const curRules = (curTplInfo && curTplInfo.rules) || []

const strategyForm = `<div class="qp-form">
  <div class="qp-form-type">
    <span class="qp-field-label">策略类型</span>
    <div class="qp-type-row">
      <button type="button" class="qp-type"><b>费率套利</b><i>买现货 + 空永续，赚资金费率</i></button>
      <button type="button" class="qp-type qp-type-on"><b>单币合约</b><i>一个币做多或做空，规则触发后由 AI 定价</i></button>
    </div>
  </div>
  <label class="qp-field qp-field-wide">
    <span class="qp-field-label">策略名称</span>
    <input class="qp-input" placeholder="例如：BTC 4h 跌破空" value="${esc((dirStrat && dirStrat.name) || '')}" />
  </label>

  <div class="qp-form-block">
    <div class="qp-block-head">
      <span class="qp-field-label">信号模板</span>
      <span class="qp-dim">决定「什么位置算入场机会」</span>
    </div>
    <div class="qp-tpl-list">${(PMETA.templates || []).map((t) => tplCard(t, t.key === curTpl)).join('')}</div>
    ${curTplInfo ? `<div class="qp-tpl-desc">${esc(curTplInfo.desc)}</div>` : ''}
  </div>

  <div class="qp-form-block">
    <div class="qp-block-head">
      <span class="qp-field-label">触发规则</span>
      <span class="qp-dim">全部满足才动手</span>
    </div>
    <div class="qp-rule-list">${curRules.map(ruleFormRow).join('')}</div>
    <div class="qp-rule-add">
      <select class="qp-select qp-select-sm"><option>+ 加一条条件</option>${Object.values(PMETA.conditions || {}).map((c) => `<option>${esc(c.label)}</option>`).join('')}</select>
      <span class="qp-dim">所有规则同时满足才触发；想更宽松就减少条件</span>
    </div>
  </div>

  <div class="qp-form-grid">${DIR_FORM_FIELDS.map(formField).join('')}</div>
  <div class="qp-form-note">上面这些是「动手时最多能有多大」的上限（单笔、杠杆、止损止盈）。真正决定"该不该买"的是信号模板里的规则。触发后交给 AI 定具体买点和止损价，AI 只能在上限内取值。</div>
  <div class="qp-form-actions">
    <button type="button" class="qp-btn qp-btn-primary">创建策略</button>
    <button type="button" class="qp-btn qp-btn-ghost">取消</button>
  </div>
</div>`

const stratSection = (items, emptyText) => `${items.length
  ? `<div class="qp-strat-list">${items.join('')}</div>`
  : `<div class="qp-empty">${emptyText}</div>`}
${strategyForm}`

// ---------------------------------------------------------------- 组装
const themeVars = `:root {
  --dsw-alias-bg-base: #16171b;
  --dsw-alias-bg-layer-1: #1d1f24;
  --dsw-alias-bg-layer-2: #24262c;
  --dsw-alias-label-primary: #e9ebee;
  --dsw-alias-label-secondary: #979daa;
  --dsw-alias-border-l1: #2d3038;
  --dsw-alias-border-l2: #3d414b;
  --dsw-alias-brand-primary: #5b7cfa;
  --dsw-alias-state-error-primary: #dc2626;
}`
const pageHead = (title) => `<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>${title}</title>
<style>
${themeVars}
html, body { margin: 0; background: var(--dsw-alias-bg-base); }
body { font-family: "Segoe UI", -apple-system, "Microsoft YaHei", sans-serif; }
.preview-frame { max-width: 1100px; margin: 0 auto; }
${CSS}
</style></head>
<body><div class="preview-frame qp-root">`

const commonTail = `  <div class="qp-row2">
    ${card('账户与持仓', (acc.balance_error ? `<span class="qp-warn-text">${acc.stale ? '上次读数 · ' : ''}${esc(acc.balance_error)}</span>` : ''), account, false)}
    ${card('行情判断', '', decision, false)}
  </div>
  ${card('AI 复核', HAS_KEY ? (process.env.QUANT_MODEL || 'deepseek-chat') : ((st.auto && st.auto.awaiting) ? st.auto.awaiting + ' 条定价待复核' : '规则定价 + agent 复核'), llmCard, true)}
  ${card('执行日志', '事件流来自 crypto-quant skill 的审计日志', audit, true)}`

const htmlDir = `${pageHead('quant-panel preview')}
  ${hero}
  ${watchBar}
  ${tabs('dir')}
  ${targetsCard}
${commonTail}
  ${card('单币合约策略', '<button type="button" class="qp-btn qp-btn-sm">新建策略</button>', stratSection(dirStratList, '还没有单币合约策略。'), true)}
</div></body></html>`

const htmlArb = `${pageHead('quant-panel preview (arb)')}
  ${hero}
  ${watchBar}
  ${tabs('arb')}
  ${card('资金费率机会', f.stale ? '数据已过期' : minutesAgo(f.as_of), funding, true)}
  ${card('套利策略执行', (st.enabled_count || 0) + ' 个策略启用中', arbCand, true)}
${commonTail}
  ${card('资金费率套利策略', '<button type="button" class="qp-btn qp-btn-sm">新建策略</button>', stratSection(arbStratList, '还没有费率套利策略。'), true)}
</div></body></html>`

const outDir = path.join(ROOT, 'preview.html')
fs.writeFileSync(outDir, htmlDir, 'utf8')
fs.writeFileSync(path.join(ROOT, 'preview-arb.html'), htmlArb, 'utf8')
console.log('wrote', outDir, '(' + htmlDir.length + ' bytes, CSS ' + CSS.length + ' chars)')
