/**
 * Host half of @local/quant-panel.
 *
 * Three jobs, matching the three things the panel does:
 *   1. 盯盘频率  — read/write state/watch.json, and own the timer that drives it
 *   2. 策略      — read/write state/strategies.json
 *   3. 看板      — serve state/dashboard.json (written by dashboard.py)
 *
 * Everything numeric and market-related lives in Python; this file only moves
 * JSON around and calls the scripts.  That split is deliberate: one place to
 * fix a rule, one place to fix a rendering.
 *
 * Route layer is defensive — if a DSH build has no `webServer`, the plugin
 * still loads and the sidebar entry still works, the panel just shows no data.
 */
import fs from 'node:fs'
import path from 'node:path'
import { spawn, spawnSync } from 'node:child_process'

const QUANT_ROOT = process.env.QUANT_ROOT || 'D:/dshwork/okx_quant'
const STATE_DIR = path.join(QUANT_ROOT, 'state')
const STATE_FILE = path.join(STATE_DIR, 'dashboard.json')
const WATCH_FILE = path.join(STATE_DIR, 'watch.json')
const STRATEGY_FILE = path.join(STATE_DIR, 'strategies.json')
const PYTHON = process.env.QUANT_PYTHON || 'python'

// 和 strategy.py 的 DEFAULT_* 保持一致；面板新建策略时用这套初始值
const DEFAULT_PARAMS = {
  coins: [],
  min_apr: 30.0,
  max_basis: 0.8,
  max_usd_per_coin: 200.0,
  max_total_usd: 1000.0,
  max_positions: 5,
  leverage: 1.0,
  stop_loss_pct: 5.0,
}

const DEFAULT_DIRECTIONAL = {
  coin: 'BTC',
  timeframe: '4h',
  side: 'both',
  usd: 100.0,
  leverage: 3.0,
  stop_loss_pct: 3.0,
  take_profit_pct: 9.0,
  min_confidence: 0.55,
  template: 'trend_pullback',
  mode: 'all',
  rules: [],
}

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
const STRATEGY_TYPES = ['funding_arb', 'directional']

/**
 * 模板与条件库的真源是 patterns.py。这里不复制一份定义 ——
 * 复制必然走偏（之前 snapshot.py 复刻 scan 过滤条件就踩过这个坑）。
 * 拿不到时退回内置的最小描述，只影响界面上显示的文字，不影响求值。
 */
const PATTERN_FALLBACK = {
  templates: [
    { key: 'trend_pullback', label: '上升趋势回调买', desc: '大方向向上，等回踩均线、动能降温时买，不追高', side: 'long', rules: [] },
    { key: 'trend_breakdown', label: '下降趋势跌破空', desc: '放量跌破前低，顺势跟空', side: 'short', rules: [] },
  ],
  conditions: {},
}
let patternMeta = null
let patternMetaAt = 0

function loadPatternMeta(force = false) {
  const now = Date.now()
  if (!force && patternMeta && now - patternMetaAt < 5 * 60 * 1000) return patternMeta
  try {
    const code = [
      'import json, patterns as P',
      'out = {',
      '  "templates": [',
      '    {"key": k, "label": v["label"], "desc": v["desc"], "side": v["side"],',
      '     "rules": v["rules"]} for k, v in P.TEMPLATES.items()],',
      '  "conditions": {k: {"label": v["label"], "hint": v["hint"],',
      '     "args": v.get("args") or {}} for k, v in P.CONDITIONS.items()},',
      '}',
      'print(json.dumps(out, ensure_ascii=False))',
    ].join('\n')
    const res = spawnSync(PYTHON, ['-c', code], {
      cwd: QUANT_ROOT,
      encoding: 'utf8',
      timeout: 30000,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
    })
    if (res.status === 0 && res.stdout) {
      const parsed = JSON.parse(res.stdout.trim())
      if (parsed && Array.isArray(parsed.templates) && parsed.templates.length) {
        patternMeta = parsed
        patternMetaAt = now
        return patternMeta
      }
    }
  } catch { /* 退回内置 */ }
  return patternMeta || PATTERN_FALLBACK
}

const DEFAULT_WATCH = {
  enabled: false,
  interval_min: 15,
  llm_review: true,
  last_tick: null,
  last_tick_sec: null,
  next_tick: null,
  ticks: 0,
}

let inFlight = false
let lastRun = null
let tickTimer = null
const tickState = { running: false, lastStart: null, lastEnd: null, scan: null, evaluation: null }

// ---------------------------------------------------------------- 文件读写
function readJSON(file, fallback) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return fallback
  }
}

function writeJSON(file, obj) {
  const tmp = file + '.tmp'
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(tmp, JSON.stringify(obj, null, 2), 'utf8')
  fs.renameSync(tmp, file)
}

function loadWatch() {
  const w = { ...DEFAULT_WATCH, ...readJSON(WATCH_FILE, {}) }
  w.enabled = !!w.enabled
  w.llm_review = !!w.llm_review
  w.interval_min = clampInt(w.interval_min, 1, 1440, DEFAULT_WATCH.interval_min)
  return w
}

function clampInt(v, lo, hi, dflt) {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return dflt
  return Math.min(hi, Math.max(lo, n))
}

function clampNum(v, lo, hi, dflt) {
  const n = Number(v)
  if (!Number.isFinite(n)) return dflt
  return Math.min(hi, Math.max(lo, n))
}

// 执行层有自己的硬上限（funding_arb_live.MAX_USD_PER_COIN / MAX_TOTAL_USD）。
// 策略参数必须在写入时就夹住，否则会出现「策略说可以、下单被拒」的错位。
function executorLimits() {
  const lim = { max_usd_per_coin: 200, max_total_usd: 1000 }
  try {
    const d = readJSON(STATE_FILE, null)
    if (d && d.mode) {
      if (Number.isFinite(Number(d.mode.max_usd_per_coin))) lim.max_usd_per_coin = Number(d.mode.max_usd_per_coin)
      if (Number.isFinite(Number(d.mode.max_total_usd))) lim.max_total_usd = Number(d.mode.max_total_usd)
    }
  } catch { /* 用默认上限 */ }
  return lim
}

function normalizeRules(rawRules, templateKey) {
  const meta = loadPatternMeta()
  const conds = meta.conditions || {}
  const tpl = (meta.templates || []).find((t) => t.key === templateKey)
  const source = Array.isArray(rawRules) && rawRules.length
    ? rawRules
    : (tpl && Array.isArray(tpl.rules) ? tpl.rules : [])

  const out = []
  for (const r of source) {
    if (!r || typeof r !== 'object') continue
    const cond = String(r.cond || '')
    if (!conds[cond]) continue          // 未知条件直接丢掉，不让它悄悄变全 False
    const given = r.args && typeof r.args === 'object' ? r.args : {}
    const args = {}
    const spec = conds[cond].args || {}
    for (const [k, m] of Object.entries(spec)) {
      const lo = Number.isFinite(Number(m.min)) ? Number(m.min) : -1e9
      const hi = Number.isFinite(Number(m.max)) ? Number(m.max) : 1e9
      args[k] = clampNum(k in given ? given[k] : m.default, lo, hi, m.default)
    }
    out.push({ cond, args })
  }
  return out
}

function normalizeDirectional(raw) {
  const lim = executorLimits()
  const meta = loadPatternMeta()
  const src = raw && typeof raw === 'object' ? raw : {}
  const pick = (k) => (k in src ? src[k] : DEFAULT_DIRECTIONAL[k])

  let tf = String(pick('timeframe') || '').toLowerCase()
  if (!TIMEFRAMES.includes(tf)) tf = DEFAULT_DIRECTIONAL.timeframe
  let side = String(pick('side') || '').toLowerCase()
  if (!['both', 'long', 'short'].includes(side)) side = DEFAULT_DIRECTIONAL.side
  const coin = String(pick('coin') || DEFAULT_DIRECTIONAL.coin).trim().toUpperCase() || DEFAULT_DIRECTIONAL.coin

  let template = String(pick('template') || '')
  if (!(meta.templates || []).some((t) => t.key === template)) template = DEFAULT_DIRECTIONAL.template
  let mode = String(pick('mode') || '').toLowerCase()
  if (!['all', 'any'].includes(mode)) mode = 'all'

  // 单笔只能比执行层硬限小，杠杆和止损止盈同理，AI 也就没机会越过执行层
  const usd = clampNum(pick('usd'), 10, lim.max_usd_per_coin, DEFAULT_DIRECTIONAL.usd)
  const leverage = clampNum(pick('leverage'), 1, 10, DEFAULT_DIRECTIONAL.leverage)
  const stopLoss = clampNum(pick('stop_loss_pct'), 0.3, 50, DEFAULT_DIRECTIONAL.stop_loss_pct)
  const takeProfit = clampNum(pick('take_profit_pct'), 0.3, 200, DEFAULT_DIRECTIONAL.take_profit_pct)
  const minConf = clampNum(pick('min_confidence'), 0, 1, DEFAULT_DIRECTIONAL.min_confidence)

  return {
    coin,
    timeframe: tf,
    side,
    usd,
    leverage,
    stop_loss_pct: stopLoss,
    take_profit_pct: takeProfit,
    min_confidence: minConf,
    template,
    mode,
    rules: normalizeRules(pick('rules'), template),
  }
}

function normalizeParams(raw, type) {
  if (type === 'directional') return normalizeDirectional(raw)
  const lim = executorLimits()
  const src = raw && typeof raw === 'object' ? raw : {}
  const pick = (k) => (k in src ? src[k] : DEFAULT_PARAMS[k])
  const coins = Array.isArray(pick('coins'))
    ? pick('coins').map((c) => String(c).trim().toUpperCase()).filter(Boolean)
    : String(pick('coins') || '').split(',').map((c) => c.trim().toUpperCase()).filter(Boolean)
  return {
    coins,
    min_apr: clampNum(pick('min_apr'), 0, 100000, DEFAULT_PARAMS.min_apr),
    max_basis: clampNum(pick('max_basis'), 0.05, 50, DEFAULT_PARAMS.max_basis),
    max_usd_per_coin: clampNum(pick('max_usd_per_coin'), 10, lim.max_usd_per_coin, DEFAULT_PARAMS.max_usd_per_coin),
    max_total_usd: clampNum(pick('max_total_usd'), 10, lim.max_total_usd, DEFAULT_PARAMS.max_total_usd),
    max_positions: clampInt(pick('max_positions'), 1, 20, DEFAULT_PARAMS.max_positions),
    leverage: clampNum(pick('leverage'), 1, 10, DEFAULT_PARAMS.leverage),
    stop_loss_pct: clampNum(pick('stop_loss_pct'), 0.5, 50, DEFAULT_PARAMS.stop_loss_pct),
  }
}

function loadStrategies() {
  const doc = readJSON(STRATEGY_FILE, { strategies: [] })
  return Array.isArray(doc.strategies) ? doc.strategies : []
}

function saveStrategies(items) {
  writeJSON(STRATEGY_FILE, { updated_at: new Date().toISOString(), strategies: items })
}

/**
 * 回测结果（pattern_backtest.py 写的 CSV）。面板只做展示和挑选，
 * 不在这里重算 —— 一套策略能不能用，应当是跑完历史数据之后才知道的事。
 */
function loadBacktest() {
  try {
    const file = path.join(QUANT_ROOT, 'reports', 'pattern_backtest.csv')
    const raw = fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')
    const lines = raw.trim().split(/\r?\n/)
    if (lines.length < 2) return { rows: [], at: null }
    const head = lines[0].split(',')
    const idx = {}
    head.forEach((h, i) => { idx[h.trim()] = i })
    const num = (cells, key) => {
      const v = Number(cells[idx[key]])
      return Number.isFinite(v) ? v : null
    }
    const rows = []
    for (let i = 1; i < lines.length; i += 1) {
      const cells = lines[i].split(',')
      if (cells.length !== head.length) continue
      rows.push({
        coin: cells[idx.coin],
        tf: cells[idx.tf],
        template: cells[idx.template],
        side: cells[idx.side],
        count: num(cells, 'count'),
        win_rate: num(cells, 'win_rate'),
        avg_net: num(cells, 'avg_net'),
        max_dd: num(cells, 'max_dd'),
        avg_h1: num(cells, 'avg_h1'),
        avg_h2: num(cells, 'avg_h2'),
        n_h1: num(cells, 'n_h1'),
        n_h2: num(cells, 'n_h2'),
        stable: String(cells[idx.stable]).toLowerCase() === 'true',
      })
    }
    return { rows, at: fs.statSync(file).mtime.toISOString() }
  } catch {
    return { rows: [], at: null }
  }
}

function newStrategyId() {
  const sec = Math.floor(Date.now() / 1000)
  const hex = Math.floor(Math.random() * 0xffff).toString(16).padStart(4, '0')
  return `strat-${sec}-${hex}`
}

// ---------------------------------------------------------------- 跑 Python
function runPython(args, timeoutMs) {
  return new Promise((resolve) => {
    let settled = false
    const finish = (r) => { if (!settled) { settled = true; resolve(r) } }
    // 只留最后几行 stderr：面板上没地方展示长篇 traceback，
    // 但"exit 1"三个字对排查毫无用处 —— 之前就是这么把 prepare 的
    // 正常返回误报成"执行报错"，查了半天。
    let errBuf = ''
    try {
      const child = spawn(PYTHON, args, {
        cwd: QUANT_ROOT,
        stdio: ['ignore', 'ignore', 'pipe'],
        windowsHide: true,
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
      })
      const timer = setTimeout(() => {
        try { child.kill() } catch { /* 已经退了 */ }
        finish({ ok: false, error: `超时 ${Math.round(timeoutMs / 1000)}s` })
      }, timeoutMs)
      if (child.stderr) {
        child.stderr.on('data', (buf) => {
          errBuf = (errBuf + String(buf)).slice(-1200)
        })
      }
      child.on('exit', (code) => {
        clearTimeout(timer)
        const tail = errBuf.trim().split(/\r?\n/).filter(Boolean).pop() || ''
        finish({ ok: code === 0, code, stderr: tail || undefined })
      })
      child.on('error', (err) => { clearTimeout(timer); finish({ ok: false, error: String(err && err.message) }) })
    } catch (err) {
      finish({ ok: false, error: String((err && err.message) || err) })
    }
  })
}

// 一轮跳：一个 python 进程跑完「规则评估 → 任务书 → 规则定价 → 回写面板」。
//
// 以前是五次独立 runPython，每次都要重新 import
// numpy/pandas/ccxt（光 import dashboard 就 4.5 秒），再加 OKX 的
// load_markets。合并成 tick.py 后热启动约 5 秒。
//
// 是否扫全市场费率由 watch.scan_funding 决定：
// 只做单币合约时关掉，省 40~60 秒。
async function tick() {
  if (tickState.running) return { started: false, reason: '上一轮还在跑' }
  tickState.running = true
  tickState.lastStart = new Date().toISOString()
  tickState.scan = null
  tickState.evaluation = null
  tickState.llm = null
  tickState.task = null
  tickState.auto = null
  tickState.seconds = null
  try {
    const w0 = loadWatch()
    const scanFunding = w0.scan_funding === true
    // --quote 每次都带：面板要的是实时价，一次 ticker 请求约 0.2 秒，
    // 比让用户盯着一根 4 小时前的收盘价划算得多。
    const args = ['tick.py', scanFunding ? '--scan' : '--skip-funding', '--quote']

    const r = await runPython(args, 300000)
    const errText = r.error || (r.stderr ? ('exit ' + r.code + ': ' + r.stderr) : ('exit ' + r.code))
    let parsed = null
    try {
      parsed = JSON.parse((r.stdout || '').trim().split(/\r?\n/).pop() || '')
    } catch (e) { /* 下面统一报错 */ }

    if (!r.ok || !parsed) {
      tickState.scan = errText
      tickState.evaluation = 'skipped(tick failed)'
      tickState.llm = 'skipped(tick failed)'
      tickState.task = 'skipped(tick failed)'
      tickState.auto = 'skipped(tick failed)'
    } else {
      const st = parsed.steps || {}
      const label = (k) => {
        const v = st[k]
        if (!v) return 'skipped'
        return v.ok ? 'ok' : (v.error || 'exit')
      }
      tickState.scan = scanFunding ? label('evaluate') : 'skipped(未开费率扫描)'
      tickState.evaluation = label('evaluate')
      tickState.llm = '由 DSH agent 定价，不走 API'
      tickState.task = label('task')
      tickState.auto = label('auto')
      tickState.seconds = parsed.seconds
      if (!parsed.ok) {
        const bad = Object.keys(st).filter((k) => st[k] && !st[k].ok)
          .map((k) => k + ': ' + (st[k].error || 'exit')).join(' | ')
        tickState.scan = bad || errText
      }
    }
  } finally {
    tickState.running = false
    tickState.lastEnd = new Date().toISOString()
    const w = loadWatch()
    w.last_tick = tickState.lastEnd
    w.last_tick_sec = Math.floor(Date.now() / 1000)
    w.ticks = (w.ticks || 0) + 1
    writeJSON(WATCH_FILE, w)
  }
  return { started: true, ...tickState }
}

function applyTimer() {
  if (tickTimer) {
    clearInterval(tickTimer)
    tickTimer = null
  }
  const w = loadWatch()
  if (!w.enabled) return
  const ms = w.interval_min * 60 * 1000
  tickTimer = setInterval(() => { tick().catch(() => {}) }, ms)
  if (typeof tickTimer.unref === 'function') tickTimer.unref()
}

// ---------------------------------------------------------------- 状态读取
function stateAgeMs() {
  try {
    return Date.now() - fs.statSync(STATE_FILE).mtimeMs
  } catch {
    return Infinity
  }
}

const STALE_MS = 15 * 60 * 1000

function maybeRefresh() {
  if (inFlight) return
  if (stateAgeMs() < STALE_MS) return
  startRefresh(['--quote'])
}

function readState() {
  // hasKey 只是「有没有配直连 API」。没有也不影响使用：DSH 自己就是模型，
  // 走 task 文件让 agent 定价即可，校验是同一套。
  const llmEnv = {
    hasKey: !!(process.env.DEEPSEEK_API_KEY || '').trim(),
    model: process.env.QUANT_MODEL || 'deepseek-chat',
    taskFile: path.join(QUANT_ROOT, 'state', 'llm_task.json'),
  }
  try {
    const data = JSON.parse(fs.readFileSync(STATE_FILE, 'utf8'))
    return {
      ok: true,
      stateFile: STATE_FILE,
      ageSeconds: Math.round(stateAgeMs() / 1000),
      runner: { inFlight, lastRun, tick: { ...tickState, timerOn: !!tickTimer } },
      llm: llmEnv,
      data,
    }
  } catch (err) {
    return {
      ok: false,
      stateFile: STATE_FILE,
      ageSeconds: null,
      runner: { inFlight, lastRun, tick: { ...tickState, timerOn: !!tickTimer } },
      llm: llmEnv,
      error: String((err && err.message) || err),
      hint: '先运行  python dashboard.py  生成 state/dashboard.json',
    }
  }
}

function startRefresh(extraArgs) {
  if (inFlight) return { started: false, reason: '上一轮还在跑' }
  const args = ['dashboard.py', ...(extraArgs || [])]
  const r = runPython(args, 240000)
  inFlight = true
  lastRun = { startedAt: new Date().toISOString(), args }
  r.then((res) => {
    inFlight = false
    lastRun = { ...lastRun, finishedAt: new Date().toISOString(), ...res }
  })
  return { started: true, args }
}

function sendJSON(res, code, payload) {
  const body = JSON.stringify(payload, null, 2)
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body),
  })
  res.end(body)
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = ''
    let done = false
    const finish = (v) => { if (!done) { done = true; resolve(v) } }
    req.on('data', (chunk) => {
      raw += chunk
      if (raw.length > 1e6) { finish(null); try { req.destroy() } catch { /* ignore */ } }
    })
    req.on('end', () => {
      if (!raw.trim()) return finish({})
      try { finish(JSON.parse(raw)) } catch { finish(null) }
    })
    req.on('error', () => finish(null))
    setTimeout(() => finish(null), 5000)
  })
}

function queryOf(req) {
  try {
    const u = new URL(req.url || '/', 'http://localhost')
    const out = {}
    for (const [k, v] of u.searchParams.entries()) out[k] = v
    return out
  } catch {
    return {}
  }
}

function boolish(v, dflt) {
  if (v === undefined || v === null || v === '') return dflt
  if (typeof v === 'boolean') return v
  const s = String(v).toLowerCase()
  if (['1', 'true', 'on', 'yes'].includes(s)) return true
  if (['0', 'false', 'off', 'no'].includes(s)) return false
  return dflt
}

function applyWatchPatch(patch) {
  const w = loadWatch()
  if ('enabled' in patch) w.enabled = boolish(patch.enabled, w.enabled)
  if ('llm_review' in patch) w.llm_review = boolish(patch.llm_review, w.llm_review)
  // 只做单币合约时关掉它：全市场费率扫描要 40~60 秒，是整轮里最贵的一步。
  if ('scan_funding' in patch) w.scan_funding = boolish(patch.scan_funding, w.scan_funding)
  if ('interval_min' in patch) w.interval_min = clampInt(patch.interval_min, 1, 1440, w.interval_min)
  writeJSON(WATCH_FILE, w)
  applyTimer()
  return w
}

function applyStrategyAction(body) {
  const action = String(body.action || 'list')
  const items = loadStrategies()

  if (action === 'create') {
    const type = STRATEGY_TYPES.includes(body.type) ? body.type : 'funding_arb'
    const s = {
      id: newStrategyId(),
      name: String(body.name || '').trim() || (type === 'directional' ? '单币方向策略' : '资金费率套利'),
      type,
      enabled: boolish(body.enabled, true),
      params: normalizeParams(body.params, type),
      created_at: new Date().toISOString(),
    }
    items.push(s)
    saveStrategies(items)
    return { ok: true, action, strategy: s, count: items.length }
  }

  if (action === 'update') {
    const idx = items.findIndex((s) => s.id === body.id)
    if (idx < 0) return { ok: false, error: '策略不存在' }
    const cur = items[idx]
    if (body.name !== undefined) cur.name = String(body.name || '').trim() || cur.name
    if ('enabled' in body) cur.enabled = boolish(body.enabled, cur.enabled)
    // 类型在编辑时不允许换：两套参数的键完全不同，混着改只会得到半个策略
    if (body.type && body.type !== cur.type) {
      return { ok: false, error: '策略类型不能修改，需要换类型请新建' }
    }
    if (body.params) cur.params = normalizeParams({ ...cur.params, ...body.params }, cur.type)
    cur.updated_at = new Date().toISOString()
    saveStrategies(items)
    return { ok: true, action, strategy: cur, count: items.length }
  }

  if (action === 'toggle') {
    const s = items.find((x) => x.id === body.id)
    if (!s) return { ok: false, error: '策略不存在' }
    s.enabled = 'enabled' in body ? boolish(body.enabled, !s.enabled) : !s.enabled
    saveStrategies(items)
    return { ok: true, action, strategy: s, count: items.length }
  }

  if (action === 'delete') {
    const next = items.filter((s) => s.id !== body.id)
    if (next.length === items.length) return { ok: false, error: '策略不存在' }
    saveStrategies(next)
    return { ok: true, action, count: next.length }
  }

  return { ok: false, error: `未知操作 ${action}` }
}

export const name = 'quant-panel'

export function apply(root) {
  // 定时器在 apply 里就跑起来，不依赖 webServer —— 即使面板打不开，盯盘照跑
  try {
    applyTimer()
  } catch (err) {
    console.warn('[quant-panel] 定时器启动失败', err)
  }

  try {
    root.inject(['webServer'], (ctx) => {
      const ws = ctx.webServer || (typeof ctx.get === 'function' ? ctx.get('webServer') : null)
      if (!ws || typeof ws.register !== 'function') {
        console.warn('[quant-panel] webServer 不可用，面板将没有数据接口')
        return
      }

      const disposers = []
      const add = (p, handler) => {
        try {
          disposers.push(ws.register({ kind: 'exact', path: p, handler }))
        } catch (err) {
          console.warn('[quant-panel] 路由注册失败', p, err)
        }
      }

      add('/quant/api/state', (req, res) => {
        const payload = readState()
        try { maybeRefresh() } catch { /* 刷新失败不影响读 */ }
        sendJSON(res, 200, payload)
      })

      add('/quant/api/refresh', (req, res) => {
        const q = queryOf(req)
        const extra = []
        if (q.scan === '1') extra.push('--scan')
        if (q.quote === '1') extra.push('--quote')
        sendJSON(res, 200, { ok: true, ...startRefresh(extra) })
      })

      // 手动跑一轮：扫描 + 规则评估 + 模型定价
      add('/quant/api/tick', async (req, res) => {
        try {
          const out = await tick()
          sendJSON(res, 200, { ok: true, ...out })
        } catch (err) {
          sendJSON(res, 500, { ok: false, error: String((err && err.message) || err) })
        }
      })

      // 只重跑定价这一步（规则已经算过、不想再等一轮扫描时用）。
      // 顺序：run（有 key 就自动定价、没 key 只落 prompt）→ task（生成 agent 任务书）→ 刷新面板
      add('/quant/api/llm', async (req, res) => {
        try {
          const r = await runPython(['llm_trade.py', 'run'], 180000)
          const t = await runPython(['llm_trade.py', 'task'], 60000)
          await runPython(['dashboard.py'], 90000)
          sendJSON(res, 200, { ok: r.ok && t.ok, llm: r, task: t })
        } catch (err) {
          sendJSON(res, 500, { ok: false, error: String((err && err.message) || err) })
        }
      })

      add('/quant/api/watch', async (req, res) => {
        try {
          if (req.method === 'POST') {
            const body = (await readBody(req)) || {}
            sendJSON(res, 200, { ok: true, watch: applyWatchPatch(body) })
          } else if (req.method === 'GET' && Object.keys(queryOf(req)).length) {
            sendJSON(res, 200, { ok: true, watch: applyWatchPatch(queryOf(req)) })
          } else {
            sendJSON(res, 200, { ok: true, watch: loadWatch() })
          }
        } catch (err) {
          sendJSON(res, 500, { ok: false, error: String((err && err.message) || err) })
        }
      })

      add('/quant/api/strategies', async (req, res) => {
        try {
          if (req.method === 'POST') {
            const body = (await readBody(req)) || {}
            const out = applyStrategyAction(body)
            sendJSON(res, out.ok ? 200 : 400, out)
          } else {
            const q = queryOf(req)
            if (q.action) {
              const out = applyStrategyAction(q)
              sendJSON(res, out.ok ? 200 : 400, out)
            } else {
              const meta = loadPatternMeta(Boolean(q.reload))
              sendJSON(res, 200, {
                ok: true,
                strategies: loadStrategies(),
                defaults: DEFAULT_PARAMS,
                dirDefaults: DEFAULT_DIRECTIONAL,
                timeframes: TIMEFRAMES,
                types: STRATEGY_TYPES,
                templates: meta.templates || [],
                conditions: meta.conditions || {},
                backtest: loadBacktest(),
              })
            }
          }
        } catch (err) {
          sendJSON(res, 500, { ok: false, error: String((err && err.message) || err) })
        }
      })

      // 卸载时摘掉路由。宿主不保证给 effect，缺了就靠进程退出兜底，
      // 不能因为这一句把上面的路由注册回滚掉。
      try {
        if (ctx && typeof ctx.effect === 'function') {
          ctx.effect(() => () => {
            for (const d of disposers) {
              try { d() } catch { /* ignore */ }
            }
          })
        }
      } catch { /* ignore */ }
    })
  } catch (err) {
    console.warn('[quant-panel] apply 失败（面板仍会显示，只是没有数据）', err)
  }

  // 插件卸载时把定时器收掉
  try {
    root.effect(() => () => {
      if (tickTimer) {
        clearInterval(tickTimer)
        tickTimer = null
      }
    })
  } catch { /* ignore */ }
}
