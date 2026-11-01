/**
 * Client half of @local/quant-panel.
 *
 * Three sections, matching the three things the panel does:
 *   1. 盯盘  — 频率设置 + 开关 + 立即跑一轮
 *   2. 策略  — 参数化的资金费率套利，可多套并存/启停/删除
 *   3. 看板  — 行情、费率机会、策略候选、账户、执行日志
 *
 * No build step: this file is loaded as-is by the Harness module loader.
 * Styling uses --dsw-alias-* theme tokens; the only literal colours are the
 * up/down pair, which the theme has no semantic token for.
 */
window.__ModuleLoader__.load({
  id: '@local/quant-panel',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useState, useEffect, useRef, useCallback } = React

    const PANEL_KEY = 'quant-panel'
    const POLL_MS = 8000
    const INTERVALS = [5, 10, 15, 30, 60, 120, 240]
    const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
    const STRATEGY_TYPES = [
      ['funding_arb', '费率套利', '买现货 + 空永续，赚资金费率'],
      ['directional', '单币合约', '一个币做多或做空，规则触发后由 AI 定价'],
    ]

    // 面板一直只显示一个视图，避免信息打架；套利是另一条腿，按需切过去看
    const VIEWS = [
      ['directional', '合约方向', '看要交易的标的与触发条件'],
      ['funding_arb', '费率套利', '买现货空永续吃资金费率'],
    ]

    const SIDE_LABEL = { long: '做多', short: '做空', both: '多空双向' }

    const GRADE = {
      good: { label: '可做', cls: 'g-good' },
      marginal: { label: '勉强', cls: 'g-mid' },
      poor: { label: '不值', cls: 'g-poor' },
    }

    const REGIME = {
      trend_up: '趋势上行',
      trend_down: '趋势下行',
      range: '区间震荡',
      high_vol: '高波动',
      unclear: '不明',
    }

    // ------------------------------------------------------------ 格式化
    function priceFmt(v) {
      const n = Number(v)
      if (!Number.isFinite(n)) return '—'
      const a = Math.abs(n)
      const digits = a >= 1 ? 2 : a >= 0.01 ? 4 : 6
      return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    }

    function num(v, digits) {
      const n = Number(v)
      if (!Number.isFinite(n)) return '—'
      return n.toLocaleString('en-US', {
        minimumFractionDigits: digits === undefined ? 2 : digits,
        maximumFractionDigits: digits === undefined ? 2 : digits,
      })
    }

    function pct(v, digits) {
      const n = Number(v)
      if (!Number.isFinite(n)) return '—'
      return n.toFixed(digits === undefined ? 2 : digits) + '%'
    }

    function signed(v, digits) {
      const n = Number(v)
      if (!Number.isFinite(n)) return '—'
      const s = n >= 0 ? '+' : ''
      return s + n.toFixed(digits === undefined ? 2 : digits)
    }

    function toneOf(v) {
      const n = Number(v)
      if (!Number.isFinite(n) || n === 0) return ''
      return n > 0 ? 'tone-up' : 'tone-down'
    }

    function clock(iso) {
      if (!iso) return '—'
      const d = new Date(iso)
      if (Number.isNaN(d.getTime())) return String(iso)
      const p = (n) => String(n).padStart(2, '0')
      return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds())
    }

    function utcClock(iso) {
      if (!iso) return '—'
      const d = new Date(iso)
      if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 16)
      const p = (n) => String(n).padStart(2, '0')
      return p(d.getUTCHours()) + ':' + p(d.getUTCMinutes()) + ' UTC'
    }

    function ageHours(iso) {
      if (!iso) return null
      const d = new Date(iso)
      if (Number.isNaN(d.getTime())) return null
      return (Date.now() - d.getTime()) / 3600000
    }

    // 一根 K 线该有多长（小时）。用来判断「这根 K 线是不是该更新了」，
    // 1h 周期停在 14 小时前是真告警，4h 周期停在 5 小时前很正常。
    function tfHours(tf) {
      const m = /^(\d+)([mhd])$/.exec(String(tf || '1h'))
      if (!m) return 1
      const n = Number(m[1])
      if (m[2] === 'm') return n / 60
      if (m[2] === 'd') return n * 24
      return n
    }

    function minutesAgo(iso) {
      if (!iso) return '—'
      const d = new Date(iso)
      if (Number.isNaN(d.getTime())) return '—'
      const m = Math.round((Date.now() - d.getTime()) / 60000)
      if (m < 1) return '刚刚'
      if (m < 60) return m + ' 分钟前'
      const hr = Math.floor(m / 60)
      if (hr < 24) return hr + ' 小时前'
      return Math.floor(hr / 24) + ' 天前'
    }

    function dur(sec) {
      const n = Math.max(0, Math.round(Number(sec) || 0))
      const m = Math.floor(n / 60)
      const s = n % 60
      return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0')
    }

    async function api(path, opts) {
      const res = await fetch(path, { cache: 'no-store', ...(opts || {}) })
      let body = null
      try { body = await res.json() } catch { body = null }
      if (!res.ok) {
        const msg = (body && (body.error || body.hint)) || ('HTTP ' + res.status)
        throw new Error(msg)
      }
      return body
    }

    function postJSON(path, payload) {
      return api(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {}),
      })
    }

    // ------------------------------------------------------------ 图标
    function QuantIcon() {
      return h('svg', { width: 18, height: 18, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': 'true' },
        h('path', { d: 'M4 20V9M4 9l4 3 4-7 4 5 4-3', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', strokeLinejoin: 'round' }),
        h('circle', { cx: 20, cy: 7, r: 1.6, fill: 'currentColor' }),
      )
    }

    function RefreshIcon({ spinning }) {
      return h('svg', { className: spinning ? 'qp-spin' : undefined, width: 13, height: 13, viewBox: '0 0 24 24', fill: 'none', 'aria-hidden': 'true' },
        h('path', { d: 'M20 11a8 8 0 1 0-2.3 5.7', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round' }),
        h('path', { d: 'M20 4v6h-6', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' }),
      )
    }

    function Sparkline({ points }) {
      const vals = Array.isArray(points) ? points.map(Number).filter(Number.isFinite) : []
      if (vals.length < 2) return h('div', { className: 'qp-spark-empty' })
      const w = 100
      const hgt = 30
      const lo = Math.min(...vals)
      const hi = Math.max(...vals)
      const span = hi - lo || 1
      const step = w / (vals.length - 1)
      const d = vals.map((v, i) => {
        const x = i * step
        const y = hgt - ((v - lo) / span) * (hgt - 3) - 1.5
        return (i === 0 ? 'M' : 'L') + x.toFixed(2) + ' ' + y.toFixed(2)
      }).join(' ')
      const up = vals[vals.length - 1] >= vals[0]
      return h('svg', { className: 'qp-spark', viewBox: '0 0 ' + w + ' ' + hgt, preserveAspectRatio: 'none', 'aria-hidden': 'true' },
        h('path', { d: d, fill: 'none', stroke: up ? 'var(--qp-up)' : 'var(--qp-down)', strokeWidth: 1.4, strokeLinejoin: 'round', vectorEffect: 'non-scaling-stroke' }),
      )
    }

    function Switch({ on, onChange, disabled }) {
      return h('button', {
        type: 'button',
        role: 'switch',
        'aria-checked': !!on,
        disabled: !!disabled,
        className: 'qp-switch' + (on ? ' qp-switch-on' : '') + (disabled ? ' qp-switch-off' : ''),
        onClick: () => { if (!disabled && onChange) onChange(!on) },
      }, h('span', { className: 'qp-switch-knob' }))
    }

    // ------------------------------------------------------------ 基础组件
    function Card({ title, extra, wide, children, className }) {
      return h('section', { className: 'qp-card' + (wide ? ' qp-wide' : '') + (className ? ' ' + className : '') },
        h('header', { className: 'qp-card-head' },
          h('span', { className: 'qp-card-title' }, title),
          extra ? h('span', { className: 'qp-card-extra' }, extra) : null,
        ),
        h('div', { className: 'qp-card-body' }, children),
      )
    }

    function Stat({ label, value, tone, sub }) {
      return h('div', { className: 'qp-stat' },
        h('div', { className: 'qp-stat-label' }, label),
        h('div', { className: 'qp-stat-value' + (tone ? ' ' + tone : '') }, value),
        sub ? h('div', { className: 'qp-stat-sub' }, sub) : null,
      )
    }

    function Pill({ tone, children }) {
      return h('span', { className: 'qp-pill' + (tone ? ' qp-pill-' + tone : '') }, children)
    }

    function Bar({ value, max, toneClass, mark }) {
      const v = Number(value) || 0
      const m = Number(max) || 1
      const w = Math.max(0, Math.min(100, (v / m) * 100))
      const mk = Number(mark)
      return h('div', { className: 'qp-bar' },
        h('div', { className: 'qp-bar-fill ' + (toneClass || 'flat'), style: { width: w + '%' } }),
        Number.isFinite(mk) && mk > 0 && mk < 100
          ? h('div', { className: 'qp-bar-mark', style: { left: mk + '%' } })
          : null,
      )
    }

    // ------------------------------------------------------------ 盯盘控制条
    function WatchBar({ watch, onPatch, onTick, busy, limits, onRefresh }) {
      const [left, setLeft] = useState(null)
      const baseRef = useRef({ at: 0, sec: null })

      useEffect(() => {
        baseRef.current = { at: Date.now(), sec: watch && watch.seconds_to_next }
      }, [watch && watch.seconds_to_next, watch && watch.last_tick])

      useEffect(() => {
        if (!watch || !watch.enabled || baseRef.current.sec === null || baseRef.current.sec === undefined) {
          setLeft(null)
          return undefined
        }
        const id = setInterval(() => {
          const base = baseRef.current
          if (base.sec === null || base.sec === undefined) return
          const elapsed = (Date.now() - base.at) / 1000
          setLeft(Math.max(0, base.sec - elapsed))
        }, 1000)
        return () => clearInterval(id)
      }, [watch && watch.enabled, watch && watch.seconds_to_next])

      const enabled = !!(watch && watch.enabled)
      const interval = (watch && watch.interval_min) || 15

      return h('section', { className: 'qp-watch' },
        h('div', { className: 'qp-watch-main' },
          h('div', { className: 'qp-watch-label' },
            h('span', { className: 'qp-watch-dot' + (enabled ? ' on' : '') }),
            '自动盯盘',
          ),
          h('div', { className: 'qp-watch-controls' },
            h(Switch, { on: enabled, onChange: (v) => onPatch({ enabled: v }) }),
            h('span', { className: 'qp-watch-sep' }),
            h('select', {
              className: 'qp-select',
              value: interval,
              disabled: false,
              title: '每轮之间等多久',
              onChange: (e) => onPatch({ interval_min: Number(e.target.value) }),
            }, INTERVALS.map((m) => h('option', { key: m, value: m }, '每 ' + m + ' 分钟'))),
            // 全市场费率扫描是整轮里最贵的一步（40~60 秒）。只做单币合约时关掉它，
            // 一轮从 50 秒降到 5 秒。
            h('label', {
              className: 'qp-watch-scan',
              title: '关掉后不再扫全市场资金费率，只跑单币合约规则',
            },
              h(Switch, {
                on: !!(watch && watch.scan_funding),
                onChange: (v) => onPatch({ scan_funding: v }),
              }),
              h('span', null, '扫费率'),
            ),
            h('button', {
              type: 'button',
              className: 'qp-btn',
              disabled: !!busy,
              onClick: onTick,
              title: '立刻扫描一次并跑规则',
            }, '立即跑一轮'),
            h('button', {
              type: 'button',
              className: 'qp-btn qp-btn-ghost',
              disabled: !!busy,
              onClick: onRefresh,
              title: '只刷新数据，不跑规则',
            }, '刷新数据'),
          ),
        ),
        h('div', { className: 'qp-watch-meta' },
          enabled
            ? (left !== null
              ? h('span', null, '下一轮 ', h('b', { className: 'qp-watch-count' }, dur(left)))
              : h('span', null, '定时器已启动'))
            : h('span', { className: 'qp-dim' }, '已暂停'),
          watch && watch.ticks ? h('span', { className: 'qp-dim' }, '· 已跑 ' + watch.ticks + ' 轮') : null,
          watch && watch.last_tick ? h('span', { className: 'qp-dim' }, '· 上次 ' + clock(watch.last_tick)) : null,
          limits
            ? h('span', { className: 'qp-dim' }, '· 硬限 单币 ' + num(limits.max_usd_per_coin, 0) + ' / 总 ' + num(limits.max_total_usd, 0) + ' USDT')
            : null,
        ),
      )
    }

    // ------------------------------------------------------------ 行情
    function CoinCard({ c }) {
      const age = ageHours(c.bar_time)
      const stale = age !== null && age > 8
      const pos = Number(c.range_pos)
      return h('div', { className: 'qp-coin' },
        h('div', { className: 'qp-coin-top' },
          h('span', { className: 'qp-coin-sym' }, c.symbol),
          h('span', { className: 'qp-coin-chg ' + toneOf(c.ret_24h) }, signed(c.ret_24h) + '%'),
        ),
        h('div', { className: 'qp-coin-price' },
          h('b', null, priceFmt(c.live_price != null ? c.live_price : c.price)),
          h('span', { className: 'qp-coin-unit' }, 'USDT'),
        ),
        h('div', { className: 'qp-coin-spark' }, h(Sparkline, { points: c.spark })),
        h('div', { className: 'qp-coin-row' },
          h('span', null, 'RSI ', h('b', null, num(c.rsi14, 1))),
          h('span', null, 'ATR ', h('b', null, pct(c.atr_pct))),
          h('span', null, '量比 ', h('b', null, num(c.vol_ratio, 2))),
        ),
        h('div', { className: 'qp-coin-pos' },
          h('span', { className: 'qp-coin-pos-label' }, '24h 区间位置'),
          h('div', { className: 'qp-pos-track' },
            h('div', { className: 'qp-pos-fill', style: { width: Math.max(0, Math.min(100, pos)) + '%' } }),
          ),
          h('span', { className: 'qp-coin-pos-val' }, pct(pos, 1)),
        ),
        h('div', { className: 'qp-coin-foot' + (stale ? ' qp-coin-foot-stale' : '') },
          'K 线 ' + utcClock(c.bar_time) + (stale ? ' · 已 ' + Math.round(age) + ' 小时未更新' : ''),
        ),
      )
    }

    function MarketGrid({ market }) {
      const items = Object.values(market || {})
      if (!items.length) return h('div', { className: 'qp-empty' }, '没有行情数据')
      return h('div', { className: 'qp-coins' }, items.map((c) => h(CoinCard, { key: c.symbol, c })))
    }

    // ------------------------------------------------------------ 费率表
    const FUND_TOP_N = 10

    function FundingTable({ f }) {
      if (!f || f.error) {
        return h('div', { className: 'qp-empty' }, '费率扫描失败：' + ((f && f.error) || '未知错误'))
      }
      const all = f.rows || []
      const rows = all
        .slice()
        .sort((a, b) => (Number(b.apr) || 0) - (Number(a.apr) || 0))
        .slice(0, FUND_TOP_N)
      const maxApr = all.reduce((m, r) => Math.max(m, Number(r.apr) || 0), 0)
      const scale = Math.max(30, maxApr)
      const markPct = (30 / scale) * 100
      return h('div', null,
        h('div', { className: 'qp-fund-head' },
          h(Stat, { label: '候选标的', value: num(f.count || all.length, 0) }),
          h(Stat, { label: '最高年化', value: pct(f.best_apr), tone: f.best_apr >= 30 ? 'tone-up' : '' }),
          h(Stat, { label: '可做', value: num(f.actionable_count || 0, 0), sub: '门槛 30%' }),
          h(Stat, { label: '数据', value: f.stale ? '已过期' : minutesAgo(f.as_of), tone: f.stale ? 'tone-down' : '' }),
        ),
        h('table', { className: 'qp-table' },
          h('thead', null, h('tr', null,
            h('th', { className: 'qp-th-idx' }, '#'),
            h('th', null, '币种'),
            h('th', null, '年化'),
            h('th', { className: 'qp-th-bar' }, ''),
            h('th', null, '单期费率'),
            h('th', null, '结算'),
            h('th', null, '基差'),
            h('th', null, '现货价'),
            h('th', { className: 'qp-th-grade' }, '评级'),
          )),
          h('tbody', null, rows.map((r, i) => {
            const g = GRADE[r.grade] || GRADE.poor
            return h('tr', { key: r.coin + i, className: r.grade === 'good' ? 'qp-row-top' : undefined },
              h('td', { className: 'qp-idx' }, String(i + 1).padStart(2, '0')),
              h('td', { className: 'qp-coin-name' }, r.coin),
              h('td', { className: 'qp-apr' }, pct(r.apr)),
              h('td', { className: 'qp-td-bar' },
                h(Bar, { value: r.apr, max: scale, toneClass: r.grade === 'good' ? 'good' : 'flat', mark: markPct })),
              h('td', { className: 'qp-mono' }, Number(r.rate).toFixed(6)),
              h('td', { className: 'qp-dim' }, (r.interval_h || '—') + 'h'),
              h('td', { className: 'qp-mono ' + toneOf(r.basis_pct) }, signed(r.basis_pct, 3) + '%'),
              h('td', { className: 'qp-mono' }, priceFmt(r.spot)),
              h('td', { className: 'qp-grade-cell' }, h('span', { className: 'qp-grade ' + g.cls }, g.label)),
            )
          })),
        ),
        all.length > rows.length
          ? h('div', { className: 'qp-table-foot' },
            '共 ' + all.length + ' 个可对冲候选，按年化从高到低取前 ' + rows.length + ' 个')
          : null,
      )
    }

    // ------------------------------------------------------------ 策略
    function ParamChip({ k, v }) {
      return h('span', { className: 'qp-chip' }, h('i', null, k), h('b', null, v))
    }

    function StrategyCard({ s, onToggle, onDelete, onEdit, state }) {
      const p = s.params || {}
      const isDir = s.type === 'directional'
      const chips = isDir
        ? [
          ['币种', (p.coin || '?') + ' · ' + (p.timeframe || '?')],
          ['方向', p.side === 'both' ? '跟随模板' : (p.side === 'long' ? '只做多' : '只做空')],
          ['信号', (state && state.template_label) || p.template || '—'],
          ['单笔', num(p.usd, 0) + ' U'],
          ['杠杆', '≤' + num(p.leverage, 0) + 'x'],
          ['止损', '≤' + num(p.stop_loss_pct, 1) + '%'],
          ['止盈', '≤' + num(p.take_profit_pct, 1) + '%'],
          ['置信度', '≥' + num(p.min_confidence, 2)],
        ]
        : [
          ['门槛', pct(p.min_apr, 0)],
          ['基差', '±' + pct(p.max_basis, 1)],
          ['单币', num(p.max_usd_per_coin, 0)],
          ['总敞口', num(p.max_total_usd, 0)],
          ['持仓', num(p.max_positions, 0) + ' 个'],
          ['杠杆', num(p.leverage, 0) + 'x'],
          ['止损', pct(p.stop_loss_pct, 0)],
          ['标的池', (p.coins && p.coins.length) ? p.coins.join(',') : '全部可对冲'],
        ]

      // 方向策略把"规则通过了几条、卡在哪一条"摊在卡上
      const live = isDir && state
        ? (() => {
          const rules = state.rules || []
          const okCount = rules.filter((r) => r.ok).length
          const firstBad = rules.find((r) => !r.ok)
          return h('div', { className: 'qp-strat-live' },
            h('span', { className: 'qp-strat-live-tag' }, '最新'),
            h('span', null, (state.coin || '') + ' ' + (state.timeframe || '') + ' ' + priceFmt(state.price)),
            h('span', { className: state.fired ? 'tone-up' : '' },
              state.template_label || state.template || '—'),
            h('span', null, '规则 ', h('b', null, okCount + '/' + rules.length), ' 通过'),
            (state.fired)
              ? h('span', { className: 'tone-up' }, '已触发')
              : (firstBad ? h('span', { className: 'qp-strat-live-bad' }, '卡在 ' + firstBad.label) : null),
          )
        })()
        : null

      return h('div', { className: 'qp-strat' + (s.enabled ? '' : ' qp-strat-off') },
        h('div', { className: 'qp-strat-top' },
          h('div', { className: 'qp-strat-name' },
            h('b', null, s.name),
            h('span', { className: 'qp-strat-id' + (isDir ? ' qp-strat-id-dir' : '') },
              isDir ? '单币合约 · 方向' : '费率套利'),
          ),
          h('div', { className: 'qp-strat-actions' },
            h(Switch, { on: !!s.enabled, onChange: () => onToggle(s) }),
            h('button', { type: 'button', className: 'qp-link', onClick: () => onEdit(s) }, '编辑'),
            h('button', { type: 'button', className: 'qp-link qp-link-danger', onClick: () => onDelete(s) }, '删除'),
          ),
        ),
        h('div', { className: 'qp-strat-chips' }, chips.map(([k, v]) =>
          h(ParamChip, { key: k, k, v }),
        )),
        live,
      )
    }

    const DIR_FIELDS = [
      ['coin', '币种', 'text', '例如 BTC'],
      ['timeframe', '周期', 'select', TIMEFRAMES],
      ['side', '方向', 'select', [['both', '跟随模板'], ['long', '只做多'], ['short', '只做空']]],
      ['usd', '单笔 USDT', 'number', '开仓名义金额'],
      ['leverage', '杠杆上限', 'number', 'AI 只能往低里调'],
      ['stop_loss_pct', '止损上限 %', 'number', '止损最多放这么远'],
      ['take_profit_pct', '止盈上限 %', 'number', '止盈最远到这里'],
      ['min_confidence', '置信度门槛', 'number', '0~1，低于就不开'],
    ]

    const ARB_FIELDS = [
      ['coins', '标的池', 'text', '逗号分隔，留空=全部可对冲'],
      ['min_apr', '最低年化 %', 'number', '低于这个值不动手'],
      ['max_basis', '最大基差 %', 'number', '基差太大说明两腿不同步'],
      ['max_usd_per_coin', '单币上限 USDT', 'number', ''],
      ['max_total_usd', '总敞口 USDT', 'number', ''],
      ['max_positions', '最大持仓数', 'number', '同时持有几个币'],
      ['leverage', '杠杆', 'number', '永续那腿的杠杆'],
      ['stop_loss_pct', '止损 %', 'number', '单腿浮亏超过就平'],
    ]

    function cloneRules(rules) {
      return (Array.isArray(rules) ? rules : []).map((r) => ({
        cond: r.cond,
        args: Object.assign({}, r.args || {}),
      }))
    }

    function StrategyForm({ initial, defaults, dirDefaults, limits, templates, conditions, backtest, onSubmit, onCancel, busy }) {
      const initType = (initial && initial.type) || 'funding_arb'
      const [type, setType] = useState(initType)
      const tplList = Array.isArray(templates) ? templates : []
      const condMap = conditions || {}

      const pickSrc = (t) => {
        if (initial && initial.type === t && initial.params) return initial.params
        return (t === 'directional' ? dirDefaults : defaults) || {}
      }

      const tplOf = (key) => tplList.find((x) => x.key === key) || null

      const toForm = (t, src) => {
        const name = (initial && initial.name) || ''
        if (t === 'directional') {
          const tpl = src.template || (tplList[0] && tplList[0].key) || 'trend_pullback'
          const tplDef = tplOf(tpl)
          const rules = cloneRules(
            Array.isArray(src.rules) && src.rules.length
              ? src.rules
              : (tplDef ? tplDef.rules : []),
          )
          return {
            name,
            coin: src.coin, timeframe: src.timeframe, side: src.side,
            usd: src.usd, leverage: src.leverage,
            stop_loss_pct: src.stop_loss_pct, take_profit_pct: src.take_profit_pct,
            min_confidence: src.min_confidence,
            template: tpl,
            mode: src.mode || 'all',
            rules,
          }
        }
        return {
          name,
          coins: (src.coins || []).join(','),
          min_apr: src.min_apr, max_basis: src.max_basis,
          max_usd_per_coin: src.max_usd_per_coin, max_total_usd: src.max_total_usd,
          max_positions: src.max_positions, leverage: src.leverage,
          stop_loss_pct: src.stop_loss_pct,
        }
      }

      const [f, setF] = useState(() => toForm(initType, pickSrc(initType)))
      const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

      const switchType = (t) => {
        if (t === type) return
        setType(t)
        // 切类型时保住已填的名字，其余按新类型的默认值重来
        setF({ ...toForm(t, pickSrc(t)), name: f.name })
      }

      // 换模板 = 整套规则换掉。已经在规则上手工调过的参数会被覆盖，
      // 这是有意的：模板就是「一组规则」，混着用没人知道实际在跑什么。
      const pickTemplate = (key) => {
        const def = tplOf(key)
        setF({
          ...f,
          template: key,
          rules: cloneRules(def ? def.rules : []),
          side: 'both',
        })
      }

      const setRuleArg = (idx, argKey, val) => {
        const rules = cloneRules(f.rules)
        if (!rules[idx]) return
        rules[idx].args = Object.assign({}, rules[idx].args, { [argKey]: val })
        setF({ ...f, rules })
      }

      const dropRule = (idx) => {
        const rules = cloneRules(f.rules)
        rules.splice(idx, 1)
        setF({ ...f, rules })
      }

      const addRule = (condKey) => {
        if (!condKey) return
        const spec = condMap[condKey] || {}
        const args = {}
        Object.entries(spec.args || {}).forEach(([k, m]) => { args[k] = m.default })
        setF({ ...f, rules: cloneRules(f.rules).concat([{ cond: condKey, args }]) })
      }

      const fields = type === 'directional' ? DIR_FIELDS : (type === 'funding_arb' ? ARB_FIELDS : ARB_FIELDS)
      const isDir = type === 'directional'
      const tplNow = tplOf(f.template)

      const renderField = ([k, label, kind, hint]) => {
        let hintText = hint || ''
        if (!isDir && k === 'max_usd_per_coin' && limits) hintText = '执行层硬限 ' + num(limits.max_usd_per_coin, 0)
        if (!isDir && k === 'max_total_usd' && limits) hintText = '执行层硬限 ' + num(limits.max_total_usd, 0)

        const control = kind === 'select'
          ? h('select', { className: 'qp-select', value: f[k] === undefined ? '' : f[k], onChange: set(k) },
            hint.map((o) => Array.isArray(o)
              ? h('option', { key: o[0], value: o[0] }, o[1])
              : h('option', { key: o, value: o }, o)))
          : h('input', {
            className: 'qp-input',
            type: kind,
            value: f[k] === undefined || f[k] === null ? '' : f[k],
            placeholder: hintText,
            step: kind === 'number' ? 'any' : undefined,
            onChange: set(k),
          })

        return h('label', { key: k, className: 'qp-field' },
          h('span', { className: 'qp-field-label' }, label),
          control,
        )
      }

      const renderRule = (r, idx) => {
        const spec = condMap[r.cond] || { label: r.cond, hint: '', args: {} }
        const argSpec = spec.args || {}
        return h('div', { className: 'qp-rule', key: (r.cond || '') + idx },
          h('div', { className: 'qp-rule-main' },
            h('b', null, spec.label || r.cond),
            spec.hint ? h('span', { className: 'qp-rule-hint' }, spec.hint) : null,
          ),
          h('div', { className: 'qp-rule-args' },
            Object.entries(argSpec).map(([k, m]) => h('label', { className: 'qp-rule-arg', key: k },
              h('span', null, m.label),
              h('input', {
                className: 'qp-input qp-input-sm',
                type: 'number',
                step: 'any',
                value: (r.args && r.args[k] !== undefined) ? r.args[k] : m.default,
                onChange: (e) => setRuleArg(idx, k, e.target.value),
              }),
            )),
            Object.keys(argSpec).length ? null : h('span', { className: 'qp-dim' }, '无参数'),
          ),
          h('button', {
            type: 'button',
            className: 'qp-link qp-link-danger qp-rule-del',
            onClick: () => dropRule(idx),
          }, '移除'),
        )
      }

      // 该模板在回测里表现如何 —— 选之前就该看到，而不是选完亏了才知道
      const tplStats = (() => {
        const rows = (backtest && backtest.rows) || []
        const mine = rows.filter((x) => x.template === f.template && (x.count || 0) >= 10)
        if (!mine.length) return null
        const pos = mine.filter((x) => (x.avg_net || 0) > 0).length
        const stable = mine.filter((x) => x.stable).length
        return { total: mine.length, pos, stable }
      })()

      const buildParams = () => (isDir
        ? {
          coin: String(f.coin || '').trim().toUpperCase(),
          timeframe: f.timeframe,
          side: f.side,
          usd: Number(f.usd),
          leverage: Number(f.leverage),
          stop_loss_pct: Number(f.stop_loss_pct),
          take_profit_pct: Number(f.take_profit_pct),
          min_confidence: Number(f.min_confidence),
          template: f.template,
          mode: f.mode,
          rules: cloneRules(f.rules).map((r) => ({
            cond: r.cond,
            args: Object.fromEntries(Object.entries(r.args || {}).map(([k, v]) => [k, Number(v)])),
          })),
        }
        : {
          coins: f.coins,
          min_apr: Number(f.min_apr),
          max_basis: Number(f.max_basis),
          max_usd_per_coin: Number(f.max_usd_per_coin),
          max_total_usd: Number(f.max_total_usd),
          max_positions: Number(f.max_positions),
          leverage: Number(f.leverage),
          stop_loss_pct: Number(f.stop_loss_pct),
        })

      return h('div', { className: 'qp-form' },
        h('div', { className: 'qp-form-type' },
          h('span', { className: 'qp-field-label' }, '策略类型'),
          h('div', { className: 'qp-type-row' }, STRATEGY_TYPES.map(([val, label, desc]) =>
            h('button', {
              key: val,
              type: 'button',
              className: 'qp-type' + (type === val ? ' qp-type-on' : ''),
              disabled: !!initial,
              onClick: () => switchType(val),
            },
              h('b', null, label),
              h('i', null, desc),
            ),
          )),
          initial ? h('span', { className: 'qp-dim qp-type-note' }, '编辑时不能改类型，需要换类型请新建') : null,
        ),
        h('label', { key: 'name', className: 'qp-field qp-field-wide' },
          h('span', { className: 'qp-field-label' }, '策略名称'),
          h('input', {
            className: 'qp-input',
            type: 'text',
            value: f.name || '',
            placeholder: isDir ? '例如：BTC 4h 跌破空' : '例如：稳健费率套利',
            onChange: set('name'),
          }),
        ),

        // ---- 信号模板：这是「什么位置算机会」的定义，跟仓位是两件事 ----
        isDir ? h('div', { className: 'qp-form-block' },
          h('div', { className: 'qp-block-head' },
            h('span', { className: 'qp-field-label' }, '信号模板'),
            h('span', { className: 'qp-dim' }, '决定「什么位置算入场机会」'),
          ),
          h('div', { className: 'qp-tpl-list' }, tplList.map((t) => h('button', {
            key: t.key,
            type: 'button',
            className: 'qp-tpl' + (f.template === t.key ? ' qp-tpl-on' : ''),
            onClick: () => pickTemplate(t.key),
          },
            h('div', { className: 'qp-tpl-top' },
              h('b', null, t.label),
              h('span', { className: 'qp-tpl-side ' + (t.side === 'long' ? 'tone-up' : 'tone-down') },
                t.side === 'long' ? '做多' : '做空'),
            ),
            h('i', null, t.desc),
          ))),
          tplNow ? h('div', { className: 'qp-tpl-desc' }, tplNow.desc) : null,
          tplStats ? h('div', { className: 'qp-tpl-stats' },
            '历史回测：', tplStats.total, ' 个市场里有 ',
            h('b', null, String(tplStats.pos)), ' 个为正，',
            h('b', { className: tplStats.stable ? 'tone-up' : 'tone-down' }, String(tplStats.stable)),
            ' 个前后半段都为正',
            tplStats.stable === 0 ? '（说明它只是赶上了某段行情）' : '',
          ) : null,
        ) : null,

        // ---- 规则列表：模板展开后逐条可改 ----
        isDir ? h('div', { className: 'qp-form-block' },
          h('div', { className: 'qp-block-head' },
            h('span', { className: 'qp-field-label' }, '触发规则'),
            h('span', { className: 'qp-dim' }, '全部满足才动手'),
          ),
          (f.rules || []).length
            ? h('div', { className: 'qp-rule-list' }, (f.rules || []).map(renderRule))
            : h('div', { className: 'qp-empty-sm' }, '还没有规则 —— 这条策略永远不会触发'),
          h('div', { className: 'qp-rule-add' },
            h('select', {
              className: 'qp-select qp-select-sm',
              value: '',
              onChange: (e) => { addRule(e.target.value); e.target.value = '' },
            },
              h('option', { value: '' }, '+ 加一条条件'),
              Object.entries(condMap).map(([k, v]) => h('option', { key: k, value: k }, v.label)),
            ),
            h('span', { className: 'qp-dim' }, '所有规则同时满足才触发；想更宽松就减少条件'),
          ),
        ) : null,

        h('div', { className: 'qp-form-grid' }, fields.map(renderField)),
        isDir
          ? h('div', { className: 'qp-form-note' },
            '上面这些是「动手时最多能有多大」的上限（单笔、杠杆、止损止盈）。真正决定"该不该买"的是信号模板里的规则。触发后交给 AI 定具体买点和止损价，AI 只能在上限内取值。')
          : null,
        h('div', { className: 'qp-form-actions' },
          h('button', {
            type: 'button',
            className: 'qp-btn qp-btn-primary',
            disabled: !!busy,
            onClick: () => onSubmit({ name: f.name, type, params: buildParams() }),
          }, busy ? '保存中…' : (initial ? '保存修改' : '创建策略')),
          h('button', { type: 'button', className: 'qp-btn qp-btn-ghost', onClick: onCancel }, '取消'),
        ),
      )
    }

    // ------------------------------------------------------------ 方向标的卡
    function gateOk(val, threshold, cmp) {
      const n = Number(val)
      const m = Number(threshold)
      if (!Number.isFinite(n) || !Number.isFinite(m)) return null
      if (cmp === '<') return n < m
      if (cmp === '>') return n > m
      if (cmp === '>=') return n >= m
      if (cmp === '<=') return n <= m
      return null
    }

    function GateRow({ label, value, suffix, cmp, threshold, suffix2, ok }) {
      return h('div', { className: 'qp-gate' + (ok === true ? ' qp-gate-ok' : ok === false ? ' qp-gate-no' : ' qp-gate-na') },
        h('span', { className: 'qp-gate-mark' }, ok === true ? '✓' : ok === false ? '×' : '–'),
        h('span', { className: 'qp-gate-label' }, label),
        h('b', null, value),
        suffix ? h('i', null, suffix) : null,
        (cmp && threshold !== undefined && threshold !== null)
          ? h('span', { className: 'qp-gate-th' }, cmp + ' ' + threshold + (suffix2 || ''))
          : null,
      )
    }

    function ruleValueText(v) {
      if (v === null || v === undefined) return '—'
      const n = Number(v)
      if (Number.isFinite(n)) return Math.abs(n) >= 100 ? num(n, 0) : num(n, 2)
      return String(v)
    }

    function argsText(args) {
      const parts = Object.entries(args || {}).map(([, v]) =>
        (typeof v === 'number' ? (Math.abs(v) >= 100 ? num(v, 0) : num(v, Math.abs(v) < 1 ? 2 : 1)) : String(v)))
      return parts.length ? '(' + parts.join('/') + ')' : ''
    }

    function TargetCard({ t, states }) {
      const lim = t.limits || {}
      const age = t.bar_time ? ageHours(t.bar_time) : null
      const stale = age !== null && age > tfHours(t.timeframe) * 1.5

      // 一个标的可能挂多条策略（不同规则），分组显示，不然看不出是哪条在等
      const groups = (states || []).filter((s) => s && Array.isArray(s.rules) && s.rules.length)

      const ruleBlock = groups.length
        ? h('div', { className: 'qp-rule-groups' }, groups.map((st, gi) => {
          const okCount = st.rules.filter((r) => r.ok).length
          return h('div', { key: (st.template || '') + gi, className: 'qp-rule-group' },
            h('div', { className: 'qp-rule-group-head' },
              h('b', null, st.template_label || st.template || '规则'),
              h('span', { className: 'qp-dim' }, okCount + '/' + st.rules.length + ' 条通过'),
              h('span', { className: st.fired ? 'tone-up qp-rule-fired' : 'qp-dim' },
                st.fired ? '已触发' : '未触发'),
            ),
            h('div', { className: 'qp-gates qp-gates-one' },
              st.rules.map((r, i) => h(GateRow, {
                key: (r.cond || '') + i,
                label: r.label + argsText(r.args),
                value: ruleValueText(r.value),
                ok: r.ok,
              })),
            ),
          )
        }))
        : h('div', { className: 'qp-empty-sm' },
          '还没有规则评估结果 —— 点右上角「立即跑一轮」，或者等自动盯盘到点。')

      // 「已触发」是规则层的事实，「要不要下单」由 AI 复核卡说了算。
      // 这里别写成"等待 AI 定价"—— 复核完就没人再改这句话，
      // 于是条件明明已经判过了，卡片还挂着"等待"，看起来像卡住。
      const verdict = groups.length
        ? (groups.some((s) => s.fired)
          ? { cls: 'tone-up', text: '条件已全部满足（去「AI 复核」看是否成交）' }
          : { cls: 'qp-dim', text: '还没有策略满足条件' })
        : null

      return h('div', { className: 'qp-target' },
        h('div', { className: 'qp-target-head' },
          h('div', null,
            h('div', { className: 'qp-target-coin' },
              h('b', null, t.coin),
              h('span', { className: 'qp-target-tf' }, t.timeframe),
            ),
            h('div', { className: 'qp-target-names' }, (t.strategy_names || []).join(' · ')),
          ),
          h('div', { className: 'qp-target-price' },
            h('b', null, priceFmt(t.price)),
            h('i', null, 'USDT'),
          ),
        ),
        t.error
          ? h('div', { className: 'qp-err' }, '取行情失败：' + t.error)
          : h('div', null,
            t.spark && t.spark.length
              ? h('div', { className: 'qp-target-spark' },
                h(Sparkline, { points: t.spark }),
                h('span', { className: 'qp-target-spark-note' }, '最近 ' + t.spark.length + ' 根收盘'))
              : null,
            ruleBlock,
            verdict
              ? h('div', { className: 'qp-target-verdict' }, h('span', { className: verdict.cls }, verdict.text))
              : null,
            h('div', { className: 'qp-target-limits' },
              h('span', null, '单笔 ' + num(lim.usd, 0) + ' USDT'),
              h('span', null, '杠杆 ≤' + num(lim.leverage, 0) + 'x'),
              h('span', null, '止损 ≤' + num(lim.stop_loss_pct, 1) + '%'),
              h('span', null, '止盈 ≤' + num(lim.take_profit_pct, 1) + '%'),
              h('span', null, '置信度 ≥' + num(lim.min_confidence, 2)),
              h('span', { className: 'qp-target-age' + (stale ? ' qp-target-age-stale' : '') },
                // 数据源必须写在脸上：OKX 永续 = 与下单同一个市场，
                // Gate 现货 = 回退（跨所价差会让止损价算歪）
                (t.source === 'gate' ? 'Gate 现货' : (t.source === 'okx' ? 'OKX 永续' : '数据源未知')) +
                ' · K 线 ' + utcClock(t.bar_time) +
                (stale ? ' · 已 ' + Math.round(age) + ' 小时未更新' : '')),
            ),
          ),
      )
    }

    // ------------------------------------------------------------ AI 定价
    function LlmCard({ strat, llm, onRun, busy }) {
      const pend = strat.pending || {}
      const appr = strat.approved || {}
      const task = strat.task || {}
      const auto = strat.auto || {}
      const autoItems = (auto.items || []).filter((i) => i && i.order)
      const autoWaiting = auto.awaiting || 0
      const orders = appr.orders || []
      const acts = ((strat.candidates && strat.candidates.actions) || [])
        .filter((a) => (a.type || '') === 'directional')
      const hasKey = !!(llm && llm.hasKey)

      return h('div', null,
        h('div', { className: 'qp-fund-head' },
          h(Stat, {
            label: '待复核定价',
            value: num(autoWaiting, 0),
            tone: autoWaiting ? 'tone-warn' : '',
            sub: auto.as_of ? minutesAgo(auto.as_of) : '还没定价',
          }),
          h(Stat, {
            label: '已通过校验',
            value: num(appr.count || 0, 0),
            tone: (appr.count || 0) ? 'tone-up' : '',
            sub: appr.as_of ? minutesAgo(appr.as_of) : '',
          }),
          h(Stat, {
            label: '待定价候选',
            value: num(pend.count || 0, 0),
            sub: task.as_of ? '任务书 ' + minutesAgo(task.as_of) : '还没生成任务书',
          }),
        ),

        h('div', { className: 'qp-form-note' },
          hasKey
            ? '已配置 DEEPSEEK_API_KEY：每轮会直接调模型定价并校验。'
            : '每轮 tick 先用规则把定价算出来（ATR 定止损止盈、区间位置定仓位），'
              + '标成「待复核」；复核通过才进 approved。不需要 API key —— '
              + '你说一句「复核定价」我就按快照重新判断，同意就转正、不同意就否决。'),

        autoItems.length
          ? h('div', { className: 'qp-cand-rows' }, autoItems.map((i, k) => {
            const o = i.order
            return h('div', { key: (i.strategy_id || '') + k, className: 'qp-cand-row qp-cand-row-pending' },
              h('span', { className: 'qp-cand-name' },
                (o.coin || '') + ' ' + (o.side === 'long' ? '做多' : '做空')),
              h('span', { className: 'qp-cand-nums' },
                'entry ' + priceFmt(o.entry_suggested) +
                ' · SL ' + priceFmt(o.stop_loss) +
                ' · TP ' + priceFmt(o.take_profit) +
                ' · ' + num(o.leverage, 1) + 'x · ' + num(o.usd, 0) + 'U'),
              h('span', { className: 'qp-cand-note' },
                '规则定价 · 待复核 · 置信 ' + num(o.confidence, 2)),
            )
          }))
          : null,

        orders.length
          ? h('div', { className: 'qp-cand-rows' }, orders.map((o, i) => h('div', {
            key: (o.strategy_id || '') + i,
            className: 'qp-cand-row',
          },
            h('span', { className: 'qp-cand-name' }, (o.coin || '') + ' ' + (o.side === 'long' ? '做多' : '做空')),
            h('span', { className: 'qp-cand-nums' },
              'entry ' + priceFmt(o.entry_suggested) +
              ' · SL ' + priceFmt(o.stop_loss) +
              ' · TP ' + priceFmt(o.take_profit) +
              ' · ' + num(o.leverage, 1) + 'x · ' + num(o.usd, 0) + 'U'),
            h('span', { className: 'qp-cand-note' },
              (o.model === 'rule-engine' ? '规则定价 · ' : '') + '置信度 ' + num(o.confidence, 2)),
          )))
          : null,

        acts.length
          ? h('div', { className: 'qp-acts' }, acts.map((a, i) => h('div', {
            key: (a.strategy_id || '') + i,
            className: 'qp-act qp-act-dir',
          },
            h('div', { className: 'qp-act-top' },
              h('span', { className: 'qp-act-side ' + (a.side === 'long' ? 'tone-up' : 'tone-down') },
                a.side === 'long' ? '做多' : '做空'),
              h('b', null, a.coin),
              h('span', { className: 'qp-dim' }, ' ' + (a.timeframe || '')),
              h('span', { className: 'qp-act-usd' }, num(a.usd, 0) + ' USDT'),
              h('span', { className: 'qp-act-tag' }, a.pricing_label || '已复核'),
            ),
            h('div', { className: 'qp-act-reason' }, a.reason || ''),
          )))
          : null,

        h('div', { className: 'qp-form-actions' },
          h('button', {
            type: 'button',
            className: 'qp-btn qp-btn-sm',
            disabled: !!busy,
            onClick: onRun,
          }, busy ? '生成中…' : '重新定价'),
          h('span', { className: 'qp-dim' },
            task.file ? '任务书：' + task.file.replace(/^.*[\\/]state[\\/]/, 'state/') : ''),
        ),
      )
    }

    // ------------------------------------------------------------ 候选与执行
    function Candidates({ cand, only }) {
      if (!cand) return h('div', { className: 'qp-empty' }, '还没有跑过规则')
      const pick = (x) => !only || (x.type || 'funding_arb') === only
      const acts = (cand.actions || []).filter(pick)
      const results = (cand.results || []).filter(pick)
      if (only && !acts.length && !results.length) {
        return h('div', { className: 'qp-empty' },
          only === 'directional'
            ? '没有方向策略跑出动作 —— 规则没触发，或者还没到下一轮。'
            : '没有套利策略跑出动作。')
      }
      return h('div', null,
        h('div', { className: 'qp-cand-head' },
          h(Pill, { tone: acts.length ? 'warn' : 'muted' }, acts.length ? acts.length + ' 个待复核' : '无待复核动作'),
          h('span', { className: 'qp-dim' }, '上次评估 ' + minutesAgo(cand.as_of)),
          cand.error ? h('span', { className: 'qp-err' }, cand.error) : null,
        ),
        acts.length
          ? h('div', { className: 'qp-acts' }, acts.map((a, i) => {
            const isDir = a.type === 'directional'
            const sideLabel = a.side === 'long' ? '做多' : '做空'
            return h('div', { key: (a.strategy_id || a.coin || '') + i, className: 'qp-act' + (isDir ? ' qp-act-dir' : '') },
              h('div', { className: 'qp-act-top' },
                isDir ? h('span', { className: 'qp-act-side ' + (a.side === 'long' ? 'tone-up' : 'tone-down') }, sideLabel) : null,
                h('b', null, a.coin),
                a.timeframe ? h('span', { className: 'qp-dim' }, ' ' + a.timeframe) : null,
                h('span', { className: 'qp-act-usd' }, num(a.usd, 0) + ' USDT'),
                h('span', { className: 'qp-act-tag' }, a.pricing_label || '已复核'),
              ),
              isDir
                ? h('div', { className: 'qp-act-metrics' },
                  'RSI ' + num(a.rsi, 1) + ' · 区间 ' + num(a.pos, 1) + '% · 量比 ' + num(a.vol_ratio, 2) + ' · ATR ' + num(a.atr_pct, 2) + '%')
                : null,
              h('span', { className: 'qp-act-reason' }, a.reason || ''),
            )
          }))
          : null,
        results.length
          ? h('div', { className: 'qp-cand-rows' }, results.map((r, i) =>
            h('div', { key: (r.strategy_id || '') + i, className: 'qp-cand-row' },
              h('span', { className: 'qp-cand-name' }, r.name || r.strategy_id),
              h('span', { className: 'qp-cand-nums' },
                h('b', { className: r.matched ? 'tone-up' : '' }, '命中 ' + num(r.matched, 0)),
                h('span', { className: 'qp-dim' }, ' 拒绝 ' + num(r.rejected, 0)),
              ),
              r.note ? h('span', { className: 'qp-cand-note' }, r.note) : null,
            ),
          ))
          : null,
      )
    }

    function AccountCard({ a }) {
      if (!a) return h('div', { className: 'qp-empty' }, '没有账户数据')
      const pos = a.positions || []
      return h('div', null,
        h('div', { className: 'qp-acct-head' },
          h(Stat, { label: 'USDT 总额', value: num(a.usdt_total, 4) }),
          h(Stat, { label: '可用', value: num(a.usdt_free, 4) }),
          h(Stat, { label: '持仓', value: num(pos.length, 0), sub: '已占保证金' }),
        ),
        a.positions_error ? h('div', { className: 'qp-err' }, a.positions_error) : null,
        pos.length
          ? pos.map((p) => h('div', { key: p.symbol, className: 'qp-pos' },
            h('div', { className: 'qp-pos-top' },
              h('b', null, p.symbol),
              h('span', { className: 'qp-pos-side' }, (p.side === 'long' ? '多头' : '空头') + ' ' + num(p.leverage, 0) + 'x'),
            ),
            h('div', { className: 'qp-pos-pnl ' + toneOf(p.upnl) },
              h('b', null, (Number(p.upnl) >= 0 ? '+' : '') + num(p.upnl, 4)),
              h('span', null, num(p.pct, 1) + '%'),
            ),
            h('div', { className: 'qp-pos-grid' },
              h('span', null, '张数', h('b', null, num(p.contracts, 4))),
              h('span', null, '开仓', h('b', null, num(p.entry, 6))),
              h('span', null, '标记', h('b', null, num(p.mark, 6))),
              h('span', null, '名义', h('b', null, num(p.notional, 3))),
              h('span', { className: 'qp-pos-liq' }, '强平', h('b', null, num(p.liq, 6))),
            ),
          ))
          : h('div', { className: 'qp-empty qp-empty-sm' }, '空仓'),
      )
    }

    function DecisionCard({ d, regime }) {
      if (!d || !d.present) return h('div', { className: 'qp-empty' }, '还没有决策记录')
      const conf = Math.max(0, Math.min(1, Number(d.confidence) || 0))
      return h('div', { className: 'qp-dec' },
        h('div', { className: 'qp-dec-head' },
          h('span', { className: 'qp-dec-regime' }, REGIME[d.regime] || d.regime || '未知'),
          // 规则兜底版不是"通过"，别让人以为模型放行了
          d.source === 'rule'
            ? h(Pill, { tone: 'muted' }, '规则生成')
            : (d.veto ? h(Pill, { tone: 'warn' }, 'AI 已否决') : h(Pill, { tone: 'good' }, '通过')),
        ),
        h('div', { className: 'qp-conf' },
          h('span', { className: 'qp-conf-label' }, '决策信心'),
          h('div', { className: 'qp-conf-track' }, h('div', { className: 'qp-conf-fill', style: { width: conf * 100 + '%' } })),
          h('span', { className: 'qp-conf-val' }, conf.toFixed(2)),
        ),
        d.veto_reason ? h('div', { className: 'qp-veto' }, h('span', { className: 'qp-veto-label' }, '否决理由'), d.veto_reason) : null,
        d.notes ? h('div', { className: 'qp-notes' }, d.notes) : null,
        h('div', { className: 'qp-dec-foot' }, '更新于 ' + clock(d.as_of)),
      )
    }

    function AuditList({ a }) {
      if (!a || !a.total) return h('div', { className: 'qp-empty' }, '还没有执行记录')
      // recent 已经是「最新在前」，不再 reverse —— 面板高度有限，
      // 让人一眼看到刚发生的事比顺着时间往下读更重要
      const rows = a.recent || []
      return h('div', null,
        h('div', { className: 'qp-audit-head' },
          Object.entries(a.counts || {}).map(([k, v]) => h(Pill, { key: k, tone: k === 'execute' ? 'good' : k === 'reject' ? 'bad' : 'muted' }, k + ' ' + v)),
          h('span', { className: 'qp-dim' }, '总计 ' + a.total),
        ),
        h('div', { className: 'qp-timeline' }, rows.map((e, i) => {
          const kind = e.event || e.action || 'noop'
          // llm_trade 的事件在后端已经归一成 execute/reject/noop，
          // 原文放在 text 里；skill 的事件仍从 decision 里取理由
          const text = e.text
            || (e.decision && (e.decision.veto_reason || e.decision.notes))
            || e.reason || ''
          return h('div', { key: i, className: 'qp-tl-item' },
            h('span', { className: 'qp-tl-dot ' + kind }),
            h('span', { className: 'qp-tl-time' }, clock(e.ts || e.at)),
            h('span', { className: 'qp-tl-kind' }, e.llm_event ? 'ai·' + e.llm_event : kind),
            h('span', { className: 'qp-tl-text' }, text),
          )
        })),
      )
    }

    // ------------------------------------------------------------ 主面板
    function QuantPanel() {
      const [payload, setPayload] = useState(null)
      const [netErr, setNetErr] = useState(null)
      const [busy, setBusy] = useState(false)
      const [msg, setMsg] = useState(null)
      const [editing, setEditing] = useState(null)
      const [creating, setCreating] = useState(false)
      const [strategies, setStrategies] = useState(null)
      const [view, setView] = useState('directional')
      const timerRef = useRef(null)

      const load = useCallback(async () => {
        try {
          const body = await api('/quant/api/state')
          setPayload(body)
          setNetErr(null)
          return body
        } catch (err) {
          setNetErr(String((err && err.message) || err))
          return null
        }
      }, [])

      const loadStrategies = useCallback(async () => {
        try {
          const body = await api('/quant/api/strategies')
          setStrategies(body)
        } catch (err) {
          setMsg({ kind: 'err', text: '读策略失败：' + String((err && err.message) || err) })
        }
      }, [])

      useEffect(() => {
        load()
        loadStrategies()
        timerRef.current = setInterval(load, POLL_MS)
        return () => { if (timerRef.current) clearInterval(timerRef.current) }
      }, [load, loadStrategies])

      const data = (payload && payload.data) || {}
      const watch = data.watch || {}
      const mode = data.mode || {}
      const limits = { max_usd_per_coin: mode.max_usd_per_coin, max_total_usd: mode.max_total_usd }
      const strat = data.strategy || {}
      const defaults = (strategies && strategies.defaults) || {}
      const dirDefaults = (strategies && strategies.dirDefaults) || {}
      const templates = (strategies && strategies.templates) || []
      const conditions = (strategies && strategies.conditions) || {}
      const backtest = (strategies && strategies.backtest) || null
      // 规则层算好的「当前值 vs 阈值」按策略 id 索引，卡片直接摊开显示，
      // 前端不做触发判断，否则就和 strategy.py 里的那份逻辑分家了
      const stateById = {}
      for (const r of ((strat.candidates && strat.candidates.results) || [])) {
        if (r && r.state && r.strategy_id) stateById[r.strategy_id] = r.state
      }

      // 合约方向是主视图：标的来自 watchlist（后端已按启用中的 directional 策略算好）
      const targets = (data.watchlist && data.watchlist.targets) || []
      const dirActs = ((strat.candidates && strat.candidates.actions) || [])
        .filter((a) => (a.type || 'funding_arb') === 'directional')
      const autoPriced = (strat.auto && strat.auto.items) || []
      const listAll = (strategies && strategies.strategies) || []
      const arbCount = listAll.filter((s) => s.enabled && (s.type || 'funding_arb') === 'funding_arb').length
      const visibleStrategies = listAll.filter(
        (s) => ((s.type || 'funding_arb') === 'directional') === (view === 'directional'))

      // 一个标的可能挂多条策略：把每条策略的规则评估结果都收过来，
      // 前端只负责显示，判断全部在 patterns.py 里做
      const statesForTarget = (t) => {
        const ids = t.strategy_ids || []
        const out = []
        for (const r of ((strat.candidates && strat.candidates.results) || [])) {
          if (!ids.includes(r.strategy_id)) continue
          if (r.state) out.push(r.state)
        }
        return out
      }

      const flash = (kind, text) => {
        setMsg({ kind, text })
        setTimeout(() => setMsg(null), 6000)
      }

      const patchWatch = async (patch) => {
        try {
          await postJSON('/quant/api/watch', patch)
          await load()
        } catch (err) {
          flash('err', '保存失败：' + String((err && err.message) || err))
        }
      }

      const runTick = async () => {
        setBusy(true)
        setMsg({ kind: 'info', text: '扫描市场 → 跑规则 → 让模型定价，大约 1~2 分钟…' })
        try {
          const r = await api('/quant/api/tick')
          const bad = []
          if (r.scan && r.scan !== 'ok') bad.push('扫描：' + r.scan)
          if (r.evaluation && r.evaluation !== 'ok') bad.push('规则：' + r.evaluation)
          if (r.llm && r.llm !== 'ok') bad.push('定价：' + r.llm)
          flash(bad.length ? 'err' : 'ok', bad.length ? '跑完但有异常 → ' + bad.join(' / ') : '跑完一轮，数据已更新')
          await load()
        } catch (err) {
          flash('err', '跑失败：' + String((err && err.message) || err))
        } finally {
          setBusy(false)
        }
      }

      const runLlm = async () => {
        setBusy(true)
        setMsg({ kind: 'info', text: '正在让模型给已触发的候选定价…' })
        try {
          const r = await api('/quant/api/llm')
          flash(r.ok ? 'ok' : 'err', r.ok ? '定价跑完了' : '定价失败：' + (r.error || ('exit ' + r.code)))
          await load()
        } catch (err) {
          flash('err', '定价失败：' + String((err && err.message) || err))
        } finally {
          setBusy(false)
        }
      }

      const refreshData = async () => {
        setBusy(true)
        try {
          await api('/quant/api/refresh?quote=1')
          let tries = 0
          const before = payload && payload.data && payload.data.as_of
          const watchStart = setInterval(async () => {
            tries += 1
            const body = await load()
            const now = body && body.data && body.data.as_of
            if ((now && now !== before) || tries > 20) {
              clearInterval(watchStart)
              setBusy(false)
            }
          }, 2500)
        } catch (err) {
          flash('err', '刷新失败：' + String((err && err.message) || err))
          setBusy(false)
        }
      }

      const createStrategy = async ({ name, type, params }) => {
        setBusy(true)
        try {
          await postJSON('/quant/api/strategies', { action: 'create', name, type, params })
          setCreating(false)
          flash('ok', '策略已创建')
          await loadStrategies()
          await load()
        } catch (err) {
          flash('err', '创建失败：' + String((err && err.message) || err))
        } finally {
          setBusy(false)
        }
      }

      const updateStrategy = async ({ name, type, params }) => {
        if (!editing) return
        setBusy(true)
        try {
          await postJSON('/quant/api/strategies', { action: 'update', id: editing.id, name, type, params })
          setEditing(null)
          flash('ok', '策略已更新')
          await loadStrategies()
          await load()
        } catch (err) {
          flash('err', '更新失败：' + String((err && err.message) || err))
        } finally {
          setBusy(false)
        }
      }

      const toggleStrategy = async (s) => {
        try {
          await postJSON('/quant/api/strategies', { action: 'toggle', id: s.id })
          await loadStrategies()
          await load()
        } catch (err) {
          flash('err', '切换失败：' + String((err && err.message) || err))
        }
      }

      const deleteStrategy = async (s) => {
        try {
          await postJSON('/quant/api/strategies', { action: 'delete', id: s.id })
          flash('ok', '已删除「' + s.name + '」')
          await loadStrategies()
          await load()
        } catch (err) {
          flash('err', '删除失败：' + String((err && err.message) || err))
        }
      }

      const list = (strategies && strategies.strategies) || []
      const dry = mode.dry_run
      const livePositions = ((data.account && data.account.positions) || []).length

      return h('div', { className: 'qp-root' },
        h('div', { className: 'qp-hero' },
          h('div', { className: 'qp-hero-line' },
            h('span', { className: 'qp-live' }, h('span', { className: 'qp-live-dot' }), netErr ? '连接中断' : '实时监控中'),
            h('div', { className: 'qp-badges' },
              dry ? h(Pill, { tone: 'warn' }, '干跑模式') : h(Pill, { tone: 'bad' }, '实盘'),
              h(Pill, { tone: 'muted' }, livePositions + ' 个持仓'),
              h('span', { className: 'qp-hero-time' }, '数据 ' + clock(data.as_of)),
              h('button', { type: 'button', className: 'qp-btn qp-btn-ghost', onClick: refreshData, disabled: busy },
                h(RefreshIcon, { spinning: busy }), '刷新'),
            ),
          ),
          h('h1', { className: 'qp-h1' }, 'AI 量化监控'),
          h('p', { className: 'qp-sub' },
            (mode.exchange || 'okx').toUpperCase() + ' · ' +
            (list.length ? list.length + ' 个策略 · ' + (strat.enabled_count || 0) + ' 个启用' : '还没有策略') +
            ' · 单币 ' + num(mode.max_usd_per_coin, 0) +
            ' / 总 ' + num(mode.max_total_usd, 0) + ' USDT'),
        ),

        msg ? h('div', { className: 'qp-toast qp-toast-' + msg.kind }, msg.text) : null,
        netErr ? h('div', { className: 'qp-toast qp-toast-err' }, '接口不可用：' + netErr) : null,

        h(WatchBar, { watch, onPatch: patchWatch, onTick: runTick, onRefresh: refreshData, busy, limits }),

        // 主视图切换：合约方向是主战场，套利折到另一个 tab 里
        h('div', { className: 'qp-tabs' }, VIEWS.map(([val, label, hint]) =>
          h('button', {
            key: val,
            type: 'button',
            className: 'qp-tab' + (view === val ? ' qp-tab-on' : ''),
            onClick: () => setView(val),
          },
            h('b', null, label),
            h('i', null, hint),
          ),
        )),

        view === 'directional'
          ? h('div', null,
            h(Card, {
              title: '交易标的',
              extra: targets.length ? targets.length + ' 个标的在盯' : '没有启用中的单币策略',
              wide: true,
            },
              targets.length
                ? h('div', { className: 'qp-targets' }, targets.map((t) =>
                  h(TargetCard, { key: t.key, t, states: statesForTarget(t) })))
                : h('div', { className: 'qp-empty' },
                  h('span', null, '还没有启用中的「单币合约」策略。'),
                  h('button', {
                    type: 'button',
                    className: 'qp-btn qp-btn-sm',
                    onClick: () => { setView('directional'); setCreating(true); setEditing(null) },
                  }, '新建一个')),
            ),
          )
          : h('div', null,
            h(Card, {
              title: '资金费率机会',
              extra: (data.funding && data.funding.stale) ? '数据已过期' : minutesAgo(data.funding && data.funding.as_of),
              wide: true,
            }, h(FundingTable, { f: data.funding })),
            h(Card, {
              title: '套利策略执行',
              extra: arbCount + ' 个套利策略启用中',
              wide: true,
            }, h(Candidates, { cand: strat.candidates, only: 'funding_arb' })),
          ),

        h('div', { className: 'qp-row2' },
          h(Card, {
            title: '账户与持仓',
            // 余额读不到时必须写在卡头上，否则一堆旧数字看着像实时值
            extra: (data.account && data.account.balance_error)
              ? h('span', { className: 'qp-warn-text' },
                (data.account.stale ? '上次读数 · ' : '') + data.account.balance_error)
              : null,
          }, h(AccountCard, { a: data.account })),
          h(Card, {
            title: '行情判断',
            extra: (() => {
              const d = data.decision || {}
              if (!d.present) return '还没有判断'
              const age = Number(d.age_minutes)
              const when = Number.isFinite(age) ? minutesAgo(d.as_of) : ''
              // 超过 90 分钟标黄：这份判断由每小时定时任务生成，
              // 不是「立即跑一轮」的产物，拖太久就该有人看一眼
              if (Number.isFinite(age) && age > 90) {
                return h('span', { className: 'qp-warn-text' }, when + ' · 定时任务该跑了')
              }
              return when + ' · 定时任务生成'
            })(),
          }, h(DecisionCard, { d: data.decision })),
        ),

        // AI 复核：把「市场判断的最终结论」与「每笔候选的定价」放在一张卡里。
        // 分成两张会让人以为是两件不相干的事，其实是同一个判断链条的两段。
        h(Card, {
          title: 'AI 复核',
          extra: (payload && payload.llm && payload.llm.hasKey)
            ? (payload.llm.model || '')
            : (autoPriced.length ? autoPriced.length + ' 条定价待复核' : '规则定价 + agent 复核'),
          wide: true,
        }, h(LlmCard, {
          strat,
          llm: payload && payload.llm,
          onRun: runLlm,
          busy,
        })),

        h(Card, { title: '执行日志', extra: '事件流来自 crypto-quant skill 的审计日志', wide: true },
          h(AuditList, { a: data.audit })),

        // ---- 策略
        h(Card, {
          title: view === 'directional' ? '单币合约策略' : '资金费率套利策略',
          extra: h('button', {
            type: 'button',
            className: 'qp-btn qp-btn-sm',
            onClick: () => { setCreating(!creating); setEditing(null) },
          }, creating ? '收起' : '新建策略'),
          wide: true,
        },
          h('div', { className: 'qp-strat-list' },
            visibleStrategies.length
              ? visibleStrategies.map((s) => h(StrategyCard, {
                key: s.id,
                s,
                state: stateById[s.id],
                onToggle: toggleStrategy,
                onDelete: deleteStrategy,
                onEdit: (x) => { setEditing(x); setCreating(false) },
              }))
              : h('div', { className: 'qp-empty' },
                view === 'directional'
                  ? '还没有单币合约策略。点「新建策略」，类型选「单币合约」。'
                  : '还没有费率套利策略。点「新建策略」，类型选「费率套利」。'),
          ),
          (creating || editing)
            ? h(StrategyForm, {
              key: editing ? editing.id : 'new',
              initial: editing,
              defaults,
              dirDefaults,
              limits,
              templates,
              conditions,
              backtest,
              busy,
              onSubmit: editing ? updateStrategy : createStrategy,
              onCancel: () => { setCreating(false); setEditing(null) },
            })
            : null,
        ),
      )
    }

    const CSS = `
.qp-root {
  --qp-up: #16a34a;
  --qp-down: #dc2626;
  --qp-warn-c: #b7791f;
  --qp-radius: 12px;
  height: 100%;
  overflow-y: auto;
  padding: 20px 22px 44px;
  font-size: 13px;
  color: var(--dsw-alias-label-primary);
  background: var(--dsw-alias-bg-base);
}

/* ---------- hero ---------- */
.qp-hero { margin-bottom: 14px; }
.qp-hero-line {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 6px;
}
.qp-live {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11.5px; color: var(--dsw-alias-label-secondary);
  letter-spacing: 0.02em;
}
.qp-live-dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--qp-up); box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.5);
  animation: qp-pulse 2.4s ease-out infinite;
}
@keyframes qp-pulse {
  0% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0.45); }
  70% { box-shadow: 0 0 0 7px rgba(22, 163, 74, 0); }
  100% { box-shadow: 0 0 0 0 rgba(22, 163, 74, 0); }
}
.qp-badges { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.qp-hero-time { font-size: 11.5px; color: var(--dsw-alias-label-secondary); font-variant-numeric: tabular-nums; }
.qp-h1 { margin: 0 0 4px; font-size: 21px; font-weight: 600; letter-spacing: -0.01em; }
.qp-sub { margin: 0; font-size: 12px; color: var(--dsw-alias-label-secondary); }
.qp-hero::after {
  content: ''; display: block; height: 1px; margin-top: 14px;
  background: linear-gradient(90deg, var(--dsw-alias-brand-primary), transparent 132px);
  opacity: 0.45;
}

/* ---------- toast ---------- */
.qp-toast {
  margin: 10px 0 0; padding: 8px 12px; border-radius: 9px;
  font-size: 12px; border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
}
.qp-toast-ok { border-color: rgba(22, 163, 74, 0.4); color: var(--qp-up); }
.qp-toast-err { border-color: rgba(220, 38, 38, 0.4); color: var(--qp-down); }
.qp-toast-info { color: var(--dsw-alias-label-secondary); }

/* ---------- watch bar ---------- */
.qp-watch {
  margin-top: 12px; padding: 12px 14px; border-radius: var(--qp-radius);
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
  display: flex; flex-direction: column; gap: 9px;
}
.qp-watch-main {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
}
.qp-watch-label {
  display: inline-flex; align-items: center; gap: 8px;
  font-size: 13px; font-weight: 550;
}
.qp-watch-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--dsw-alias-label-secondary); opacity: 0.5;
}
.qp-watch-dot.on { background: var(--qp-up); opacity: 1; }
.qp-watch-controls { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.qp-watch-scan {
  display: inline-flex; align-items: center; gap: 6px;
  font-size: 11.5px; color: var(--dsw-alias-label-secondary);
  cursor: pointer; user-select: none;
}
.qp-watch-sep { width: 1px; height: 18px; background: var(--dsw-alias-border-l1); }
.qp-watch-meta {
  display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  font-size: 11.5px; color: var(--dsw-alias-label-secondary);
}
.qp-watch-count { font-variant-numeric: tabular-nums; color: var(--dsw-alias-label-primary); }

/* ---------- controls ---------- */
.qp-switch {
  position: relative; width: 38px; height: 21px; flex: none;
  border-radius: 999px; border: 1px solid var(--dsw-alias-border-l2);
  background: var(--dsw-alias-bg-layer-2); cursor: pointer; padding: 0;
  transition: background 160ms ease, border-color 160ms ease;
}
.qp-switch-on { background: var(--qp-up); border-color: transparent; }
.qp-switch-knob {
  position: absolute; top: 2px; left: 2px; width: 15px; height: 15px;
  border-radius: 50%; background: #fff; transition: transform 160ms ease;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
}
.qp-switch-on .qp-switch-knob { transform: translateX(17px); }
.qp-switch-off { opacity: 0.5; cursor: not-allowed; }

.qp-select, .qp-input {
  font: inherit; font-size: 12px; color: var(--dsw-alias-label-primary);
  background: var(--dsw-alias-bg-layer-2);
  border: 1px solid var(--dsw-alias-border-l2);
  border-radius: 8px; padding: 5px 8px; outline: none;
}
.qp-input::placeholder { color: var(--dsw-alias-label-secondary); opacity: 0.55; }
.qp-select:focus, .qp-input:focus { border-color: var(--dsw-alias-brand-primary); }
.qp-btn {
  display: inline-flex; align-items: center; gap: 5px;
  font: inherit; font-size: 12px; cursor: pointer;
  padding: 5px 11px; border-radius: 8px;
  border: 1px solid var(--dsw-alias-border-l2);
  background: var(--dsw-alias-bg-layer-2);
  color: var(--dsw-alias-label-primary);
  transition: background 140ms ease, border-color 140ms ease;
}
.qp-btn:hover:not(:disabled) { border-color: var(--dsw-alias-brand-primary); }
.qp-btn:disabled { opacity: 0.55; cursor: not-allowed; }
.qp-btn-ghost { background: transparent; }
.qp-btn-primary {
  background: var(--dsw-alias-brand-primary); border-color: transparent; color: #fff;
}
.qp-btn-sm { padding: 3px 9px; font-size: 11.5px; }
.qp-link {
  background: none; border: none; padding: 0 2px; cursor: pointer;
  font: inherit; font-size: 11.5px; color: var(--dsw-alias-label-secondary);
}
.qp-link:hover { color: var(--dsw-alias-brand-primary); }
.qp-link-danger:hover { color: var(--qp-down); }

/* ---------- cards ---------- */
.qp-card {
  margin-top: 12px; border-radius: var(--qp-radius);
  border: 1px solid var(--dsw-alias-border-l1);
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.022), rgba(255, 255, 255, 0) 92px),
    var(--dsw-alias-bg-layer-1);
  overflow: hidden;
}
.qp-card-head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 10px; padding: 11px 14px 0;
}
.qp-card-title {
  font-size: 12.5px; font-weight: 600; letter-spacing: 0.01em;
  display: inline-flex; align-items: center; gap: 7px;
}
.qp-card-title::before {
  content: ''; width: 2px; height: 11px; border-radius: 2px;
  background: var(--dsw-alias-brand-primary);
}
.qp-card-extra { font-size: 11px; color: var(--dsw-alias-label-secondary); }
.qp-card-body { padding: 11px 14px 14px; }
.qp-wide { grid-column: 1 / -1; }
.qp-row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) { .qp-row2 { grid-template-columns: 1fr; } }

/* ---------- coins ---------- */
.qp-coins { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 11px; align-items: stretch; }
@media (max-width: 860px) { .qp-coins { grid-template-columns: 1fr; } }
.qp-coin {
  border: 1px solid var(--dsw-alias-border-l1); border-radius: 10px;
  padding: 11px 12px; background: var(--dsw-alias-bg-layer-2);
  display: flex; flex-direction: column; gap: 7px;
}
.qp-coin-top { display: flex; align-items: center; justify-content: space-between; }
.qp-coin-sym { font-size: 12px; font-weight: 600; letter-spacing: 0.03em; }
.qp-coin-chg { font-size: 11.5px; font-variant-numeric: tabular-nums; }
.qp-coin-price { display: flex; align-items: baseline; gap: 5px; }
.qp-coin-price b { font-size: 19px; font-weight: 600; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
.qp-coin-unit { font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.qp-coin-spark { height: 30px; }
.qp-spark { width: 100%; height: 30px; display: block; }
.qp-spark-empty { height: 30px; }
.qp-coin-row {
  display: flex; gap: 10px; flex-wrap: wrap;
  font-size: 11px; color: var(--dsw-alias-label-secondary);
}
.qp-coin-row b { color: var(--dsw-alias-label-primary); font-weight: 550; font-variant-numeric: tabular-nums; }
.qp-coin-pos { display: flex; align-items: center; gap: 7px; font-size: 10.5px; }
.qp-coin-pos-label { color: var(--dsw-alias-label-secondary); white-space: nowrap; }
.qp-pos-track {
  flex: 1; height: 3px; border-radius: 2px; overflow: hidden;
  background: var(--dsw-alias-border-l1);
}
.qp-pos-fill {
  height: 100%; border-radius: 2px;
  background: linear-gradient(90deg, var(--qp-down), var(--qp-warn-c) 50%, var(--qp-up));
}
.qp-coin-pos-val { color: var(--dsw-alias-label-secondary); font-variant-numeric: tabular-nums; min-width: 34px; text-align: right; }
.qp-coin-foot {
  font-size: 10.5px; color: var(--dsw-alias-label-secondary);
  padding-top: 6px; border-top: 1px dashed var(--dsw-alias-border-l1);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.qp-coin-foot-stale { color: var(--qp-warn-c); opacity: 1; }

/* ---------- funding table ---------- */
.qp-fund-head {
  display: grid; grid-template-columns: repeat(4, 1fr);
  gap: 10px; padding-bottom: 11px; margin-bottom: 4px;
  border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.qp-stat-label { font-size: 10.5px; color: var(--dsw-alias-label-secondary); margin-bottom: 2px; }
.qp-stat-value { font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
.qp-stat-sub { font-size: 10px; color: var(--dsw-alias-label-secondary); margin-top: 1px; }

.qp-table { width: 100%; border-collapse: collapse; font-size: 11.5px; }
.qp-table th {
  text-align: left; font-weight: 500; font-size: 10.5px;
  color: var(--dsw-alias-label-secondary);
  padding: 6px 8px; border-bottom: 1px solid var(--dsw-alias-border-l1);
  white-space: nowrap;
}
.qp-table td {
  padding: 6px 8px; border-bottom: 1px solid var(--dsw-alias-border-l1);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}
.qp-table tr:last-child td { border-bottom: none; }
.qp-table tbody tr:hover { background: rgba(255, 255, 255, 0.025); }
.qp-row-top { background: rgba(22, 163, 74, 0.05); }
.qp-th-idx, .qp-idx { width: 26px; color: var(--dsw-alias-label-secondary); font-size: 10px; }
.qp-coin-name { font-weight: 550; }
.qp-apr { color: var(--dsw-alias-label-primary); }
.qp-th-bar, .qp-td-bar { width: 84px; }
.qp-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; }
.qp-dim { color: var(--dsw-alias-label-secondary); }
.qp-th-grade, .qp-grade-cell { text-align: right; width: 52px; }
.qp-table-foot {
  margin-top: 9px;
  font-size: 10.5px;
  color: var(--dsw-alias-label-secondary);
  opacity: 0.7;
}
.qp-cand-row-pending {
  border-left: 2px solid var(--qp-warn-c);
  padding-left: 9px;
}
.qp-cand-row-pending .qp-cand-note { color: var(--qp-warn-c); }
.qp-warn-text { color: var(--qp-warn-c); }

.qp-bar {
  position: relative; height: 5px; border-radius: 3px;
  background: var(--dsw-alias-border-l1);
}
.qp-bar-fill { position: absolute; left: 0; top: 0; bottom: 0; border-radius: 3px; background: var(--dsw-alias-label-secondary); opacity: 0.45; }
.qp-bar-fill.good { background: var(--qp-up); opacity: 0.75; }
.qp-bar-mark { position: absolute; top: -2px; bottom: -2px; width: 1px; background: var(--dsw-alias-label-primary); opacity: 0.26; }

.qp-grade { font-size: 10.5px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--dsw-alias-border-l1); }
.qp-grade.g-good { color: var(--qp-up); border-color: rgba(22, 163, 74, 0.4); }
.qp-grade.g-mid { color: var(--qp-warn-c); border-color: rgba(183, 121, 31, 0.4); }
.qp-grade.g-poor { color: var(--dsw-alias-label-secondary); }

/* ---------- strategy ---------- */
.qp-strat-list { display: flex; flex-direction: column; gap: 9px; }
.qp-strat {
  border: 1px solid var(--dsw-alias-border-l1); border-radius: 10px;
  padding: 10px 12px; background: var(--dsw-alias-bg-layer-2);
}
.qp-strat-off { opacity: 0.55; }
.qp-strat-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.qp-strat-name { display: flex; align-items: baseline; gap: 8px; }
.qp-strat-name b { font-size: 12.5px; font-weight: 600; }
.qp-strat-id { font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.qp-strat-actions { display: flex; align-items: center; gap: 8px; }
.qp-strat-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
.qp-strat-id-dir { color: var(--dsw-alias-brand-primary); }
.qp-strat-live {
  display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 14px;
  margin-top: 9px; padding-top: 9px;
  border-top: 1px solid var(--dsw-alias-border-l1);
  font-size: 11px; font-variant-numeric: tabular-nums;
  color: var(--dsw-alias-label-secondary);
}
.qp-strat-live b { color: var(--dsw-alias-label-primary); font-weight: 600; }
.qp-strat-live > span { white-space: nowrap; }
.qp-strat-live i { font-style: normal; opacity: 0.72; font-size: 10.5px; }
.qp-strat-live-tag {
  font-size: 9.5px; letter-spacing: 0.06em; text-transform: uppercase;
  color: var(--dsw-alias-brand-primary);
  padding: 1px 6px; border-radius: 4px;
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 12%, transparent);
}
.qp-strat-live-bad { color: var(--qp-warn-c); }
.qp-chip {
  display: inline-flex; align-items: baseline; gap: 4px;
  font-size: 10.5px; padding: 2px 7px; border-radius: 6px;
  background: var(--dsw-alias-bg-layer-1); border: 1px solid var(--dsw-alias-border-l1);
}
.qp-chip i { font-style: normal; color: var(--dsw-alias-label-secondary); }
.qp-chip b { font-weight: 550; font-variant-numeric: tabular-nums; }

/* ---- 视图切换 */
.qp-tabs {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  margin: 12px 0;
}
.qp-tab {
  appearance: none; text-align: left; cursor: pointer; font: inherit;
  display: flex; flex-direction: column; gap: 2px;
  padding: 10px 13px; border-radius: 10px;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
  color: var(--dsw-alias-label-secondary);
  transition: border-color 0.15s, background 0.15s;
}
.qp-tab b { font-size: 12.5px; font-weight: 600; color: var(--dsw-alias-label-primary); }
.qp-tab i { font-style: normal; font-size: 10.5px; line-height: 1.4; opacity: 0.8; }
.qp-tab:hover { border-color: var(--dsw-alias-border-l2); }
.qp-tab-on {
  border-color: var(--dsw-alias-brand-primary);
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 8%, var(--dsw-alias-bg-layer-1));
}
.qp-tab-on b { color: var(--dsw-alias-brand-primary); }

/* ---- 交易标的卡 */
.qp-targets { display: grid; gap: 12px; }
.qp-target {
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 11px;
  padding: 13px 14px 12px;
  background: var(--dsw-alias-bg-layer-1);
}
.qp-target-head {
  display: flex; align-items: flex-start; justify-content: space-between; gap: 12px;
}
.qp-target-coin { display: flex; align-items: baseline; gap: 7px; }
.qp-target-coin b { font-size: 15px; font-weight: 650; letter-spacing: 0.02em; }
.qp-target-tf {
  font-size: 10px; padding: 1px 6px; border-radius: 5px;
  background: var(--dsw-alias-bg-layer-2); color: var(--dsw-alias-label-secondary);
  font-variant-numeric: tabular-nums;
}
.qp-target-names { margin-top: 3px; font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.qp-target-price { text-align: right; line-height: 1.15; }
.qp-target-price b { font-size: 21px; font-weight: 600; font-variant-numeric: tabular-nums; }
.qp-target-price i { font-style: normal; font-size: 10px; margin-left: 4px; color: var(--dsw-alias-label-secondary); }
.qp-target-spark { margin: 10px 0 2px; display: flex; align-items: center; gap: 10px; }
.qp-target-spark .qp-spark { flex: 1 1 auto; min-width: 0; max-width: 460px; height: 34px; }
.qp-target-spark-note { flex: 0 0 auto; font-size: 10px; color: var(--dsw-alias-label-secondary); opacity: 0.75; white-space: nowrap; }

.qp-gates {
  display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  margin-top: 11px;
}
@media (max-width: 720px) { .qp-gates { grid-template-columns: 1fr; } }
.qp-gate-group {
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 9px; padding: 9px 10px;
  background: color-mix(in srgb, var(--dsw-alias-bg-base) 55%, transparent);
}
.qp-gate-title { font-size: 10.5px; font-weight: 600; margin-bottom: 6px; letter-spacing: 0.03em; }
.qp-gate {
  display: grid; grid-template-columns: 14px auto 1fr auto auto;
  align-items: baseline; gap: 6px;
  font-size: 11px; padding: 3px 0;
  font-variant-numeric: tabular-nums;
}
.qp-gate + .qp-gate { border-top: 1px dashed var(--dsw-alias-border-l1); }
.qp-gate-mark { font-size: 10px; text-align: center; }
.qp-gate-label { color: var(--dsw-alias-label-secondary); }
.qp-gate b { font-weight: 600; color: var(--dsw-alias-label-primary); }
.qp-gate i { font-style: normal; font-size: 10px; color: var(--dsw-alias-label-secondary); }
.qp-gate-th { font-size: 10px; color: var(--dsw-alias-label-secondary); opacity: 0.8; white-space: nowrap; }
.qp-gate-ok .qp-gate-mark { color: var(--qp-up); }
.qp-gate-no .qp-gate-mark { color: var(--qp-down); }
.qp-gate-no b { color: var(--qp-down); }
.qp-gate-na .qp-gate-mark { opacity: 0.4; }
.qp-gates-one { grid-template-columns: 1fr; }
.qp-gates-one .qp-gate { grid-template-columns: 14px 1fr auto; }
.qp-gates-one .qp-gate-label {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.qp-rule-groups { display: grid; gap: 9px; margin-top: 11px; }
.qp-rule-group {
  border: 1px solid var(--dsw-alias-border-l1);
  border-radius: 9px; padding: 9px 11px 6px;
  background: color-mix(in srgb, var(--dsw-alias-bg-base) 55%, transparent);
}
.qp-rule-group-head {
  display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap;
  font-size: 11px; margin-bottom: 4px;
}
.qp-rule-group-head b { font-weight: 600; }
.qp-rule-fired { font-size: 10.5px; }

.qp-target-verdict { margin-top: 10px; font-size: 11.5px; }
.qp-target-note {
  margin-top: 7px; font-size: 10.5px; line-height: 1.5;
  color: var(--dsw-alias-label-secondary);
  padding-left: 9px; border-left: 2px solid var(--dsw-alias-border-l2);
}
.qp-target-limits {
  display: flex; flex-wrap: wrap; gap: 5px 14px;
  margin-top: 10px; padding-top: 9px;
  border-top: 1px solid var(--dsw-alias-border-l1);
  font-size: 10.5px; color: var(--dsw-alias-label-secondary);
  font-variant-numeric: tabular-nums;
}
.qp-target-age { margin-left: auto; }
.qp-target-age-stale { color: var(--qp-warn-c); opacity: 1; }

/* ---------- form ---------- */
.qp-form {
  margin-top: 11px; padding: 12px; border-radius: 10px;
  border: 1px dashed var(--dsw-alias-border-l2);
  background: var(--dsw-alias-bg-layer-2);
}
.qp-form-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 9px 11px; }
@media (max-width: 900px) { .qp-form-grid { grid-template-columns: 1fr 1fr; } }
.qp-form-type { margin-bottom: 11px; }
.qp-type-row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 6px; }
@media (max-width: 900px) { .qp-type-row { grid-template-columns: 1fr; } }
.qp-type {
  display: flex; flex-direction: column; gap: 2px; align-items: flex-start;
  padding: 8px 11px; border-radius: 8px; cursor: pointer; text-align: left;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
  color: var(--dsw-alias-label-primary); font: inherit;
}
.qp-type b { font-size: 12px; font-weight: 600; }
.qp-type i { font-style: normal; font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.qp-type:hover:not(:disabled) { border-color: var(--dsw-alias-border-l2); }
.qp-type-on {
  border-color: color-mix(in srgb, var(--dsw-alias-brand-primary) 55%, transparent);
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 9%, var(--dsw-alias-bg-layer-1));
}
.qp-type:disabled { cursor: default; opacity: 0.6; }
.qp-type-note { display: block; margin-top: 6px; }
.qp-field-wide { margin-bottom: 11px; }
.qp-form-note {
  margin-top: 10px; padding: 8px 10px; border-radius: 8px;
  font-size: 11px; line-height: 1.6;
  color: var(--dsw-alias-label-secondary);
  background: var(--dsw-alias-bg-layer-1);
  border-left: 2px solid color-mix(in srgb, var(--dsw-alias-brand-primary) 45%, transparent);
}
.qp-field { display: flex; flex-direction: column; gap: 3px; }
.qp-field-label { font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.qp-input { width: 100%; box-sizing: border-box; }
.qp-form-actions { display: flex; gap: 8px; margin-top: 11px; }

/* ---------- 信号模板 / 规则编辑 ---------- */
.qp-form-block { margin-bottom: 13px; }
.qp-block-head {
  display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap;
  margin-bottom: 7px;
}
.qp-tpl-list {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 7px;
}
.qp-tpl {
  display: flex; flex-direction: column; gap: 3px; align-items: stretch;
  padding: 8px 10px; border-radius: 8px; cursor: pointer; text-align: left;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
  color: var(--dsw-alias-label-primary); font: inherit;
}
.qp-tpl:hover { border-color: var(--dsw-alias-border-l2); }
.qp-tpl-on {
  border-color: color-mix(in srgb, var(--dsw-alias-brand-primary) 55%, transparent);
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 9%, var(--dsw-alias-bg-layer-1));
}
.qp-tpl-top { display: flex; align-items: baseline; gap: 7px; }
.qp-tpl-top b { font-size: 11.5px; font-weight: 600; }
.qp-tpl-side {
  font-size: 9.5px; font-weight: 600; padding: 1px 5px; border-radius: 4px;
  background: color-mix(in srgb, currentColor 13%, transparent);
}
.qp-tpl i { font-style: normal; font-size: 10px; line-height: 1.45; color: var(--dsw-alias-label-secondary); }
.qp-tpl-desc {
  margin-top: 8px; font-size: 10.5px; line-height: 1.55;
  color: var(--dsw-alias-label-secondary);
}
.qp-tpl-stats {
  margin-top: 6px; font-size: 10.5px;
  color: var(--dsw-alias-label-secondary);
  padding: 6px 9px; border-radius: 7px;
  background: var(--dsw-alias-bg-layer-1);
  border-left: 2px solid var(--dsw-alias-border-l2);
}
.qp-tpl-stats b { font-variant-numeric: tabular-nums; }

.qp-rule-list { display: flex; flex-direction: column; gap: 6px; }
.qp-rule {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 7px 10px; border-radius: 8px;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-1);
}
.qp-rule-main { display: flex; flex-direction: column; gap: 1px; min-width: 130px; }
.qp-rule-main b { font-size: 11.5px; font-weight: 600; }
.qp-rule-hint { font-size: 10px; color: var(--dsw-alias-label-secondary); }
.qp-rule-args { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-left: auto; }
.qp-rule-arg { display: flex; align-items: center; gap: 4px; font-size: 10px; color: var(--dsw-alias-label-secondary); }
.qp-rule-del { flex: 0 0 auto; }
.qp-rule-add { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-top: 8px; }
.qp-input-sm, .qp-select-sm { padding: 3px 6px; font-size: 11px; width: 74px; }
.qp-select-sm { width: auto; min-width: 150px; }
.qp-empty-sm {
  font-size: 11px; padding: 9px 11px; border-radius: 8px;
  color: var(--dsw-alias-label-secondary);
  background: var(--dsw-alias-bg-layer-1);
  border: 1px dashed var(--dsw-alias-border-l2);
}

/* ---------- candidates ---------- */
.qp-cand-head { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 9px; }
.qp-acts { display: flex; flex-direction: column; gap: 6px; margin-bottom: 9px; }
.qp-act {
  display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap;
  font-size: 11.5px; padding: 7px 10px; border-radius: 8px;
  border: 1px solid rgba(183, 121, 31, 0.35);
  background: rgba(183, 121, 31, 0.07);
}
.qp-act b { font-weight: 600; }
.qp-act-dir {
  flex-direction: column; align-items: stretch; gap: 4px;
  border-color: color-mix(in srgb, var(--dsw-alias-brand-primary) 32%, transparent);
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 6%, transparent);
}
.qp-act-top { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.qp-act-side {
  font-size: 10.5px; font-weight: 600; padding: 1px 6px; border-radius: 4px;
  background: color-mix(in srgb, currentColor 12%, transparent);
}
.qp-act-tag {
  font-size: 10px; padding: 1px 6px; border-radius: 4px;
  color: var(--dsw-alias-brand-primary);
  background: color-mix(in srgb, var(--dsw-alias-brand-primary) 11%, transparent);
}
.qp-act-metrics { font-size: 10.5px; color: var(--dsw-alias-label-secondary); font-variant-numeric: tabular-nums; }
.qp-act-usd { font-variant-numeric: tabular-nums; color: var(--qp-warn-c); }
.qp-act-reason { color: var(--dsw-alias-label-secondary); font-size: 11px; }
.qp-cand-rows { display: flex; flex-direction: column; gap: 4px; }
.qp-cand-row {
  display: flex; align-items: baseline; gap: 10px;
  font-size: 11.5px; padding: 4px 0;
  border-top: 1px solid var(--dsw-alias-border-l1);
}
.qp-cand-name { min-width: 120px; font-weight: 550; }
.qp-cand-nums { font-variant-numeric: tabular-nums; }
.qp-cand-note { color: var(--dsw-alias-label-secondary); font-size: 10.5px; }

/* ---------- account ---------- */
.qp-acct-head {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
  padding-bottom: 10px; border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.qp-pos {
  margin-top: 9px; border-radius: 9px; overflow: hidden;
  border: 1px solid var(--dsw-alias-border-l1);
  background: var(--dsw-alias-bg-layer-2);
}
.qp-pos-top {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 11px 4px; font-size: 12px;
}
.qp-pos-side { font-size: 10.5px; color: var(--dsw-alias-label-secondary); }
.qp-pos-pnl {
  display: flex; align-items: baseline; gap: 7px;
  padding: 5px 11px 7px; font-variant-numeric: tabular-nums;
  border-left: 2px solid currentColor; margin: 0 11px 6px; border-radius: 2px;
  background: linear-gradient(90deg, rgba(220, 38, 38, 0.13), transparent);
}
.qp-pos-pnl.tone-up { background: linear-gradient(90deg, rgba(22, 163, 74, 0.13), transparent); }
.qp-pos-pnl b { font-size: 15px; font-weight: 600; }
.qp-pos-pnl span { font-size: 11px; }
.qp-pos-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 4px 10px;
  padding: 0 11px 10px; font-size: 10.5px;
  color: var(--dsw-alias-label-secondary);
}
.qp-pos-grid b { display: block; color: var(--dsw-alias-label-primary); font-weight: 550; font-variant-numeric: tabular-nums; }
.qp-pos-liq b { color: var(--qp-warn-c); }

/* ---------- decision ---------- */
.qp-dec-head { display: flex; align-items: center; justify-content: space-between; gap: 9px; margin-bottom: 9px; }
.qp-dec-regime { font-size: 14px; font-weight: 600; }
.qp-conf { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.qp-conf-label { font-size: 10.5px; color: var(--dsw-alias-label-secondary); white-space: nowrap; }
.qp-conf-track { flex: 1; height: 4px; border-radius: 2px; background: var(--dsw-alias-border-l1); overflow: hidden; }
.qp-conf-fill { height: 100%; border-radius: 2px; background: var(--dsw-alias-brand-primary); }
.qp-conf-val { font-size: 10.5px; color: var(--dsw-alias-label-secondary); font-variant-numeric: tabular-nums; }
.qp-veto {
  font-size: 11.5px; line-height: 1.6; padding: 9px 11px; border-radius: 8px;
  border-left: 2px solid var(--qp-warn-c);
  background: rgba(183, 121, 31, 0.07);
  color: var(--dsw-alias-label-primary);
}
.qp-veto-label {
  display: block; font-size: 10px; color: var(--qp-warn-c);
  letter-spacing: 0.04em; margin-bottom: 3px;
}
.qp-notes { font-size: 11px; line-height: 1.65; color: var(--dsw-alias-label-secondary); margin-top: 9px; }
.qp-dec-foot { font-size: 10.5px; color: var(--dsw-alias-label-secondary); margin-top: 9px; }

/* ---------- audit ---------- */
.qp-audit-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; }
.qp-timeline { position: relative; padding-left: 4px; }
.qp-timeline::before {
  content: ''; position: absolute; left: 8px; top: 6px; bottom: 6px;
  width: 1px; background: var(--dsw-alias-border-l1);
}
.qp-tl-item {
  position: relative; display: grid;
  grid-template-columns: 58px 52px 1fr; align-items: center;
  gap: 10px; font-size: 11.5px;
  color: var(--dsw-alias-label-secondary);
  padding: 7px 0 7px 16px;
}
.qp-tl-dot {
  position: absolute; left: 5px; top: 50%; transform: translateY(-50%);
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--dsw-alias-label-secondary);
}
.qp-tl-dot.execute { background: var(--qp-up); }
.qp-tl-dot.reject { background: var(--qp-down); }
.qp-tl-dot.noop { background: var(--dsw-alias-border-l2); }
.qp-tl-time { font-variant-numeric: tabular-nums; font-size: 10.5px; }
.qp-tl-kind { font-size: 10.5px; }
.qp-tl-text {
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-size: 11px;
}

/* ---------- misc ---------- */
.qp-pill {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 10.5px; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--dsw-alias-border-l1);
  color: var(--dsw-alias-label-secondary); white-space: nowrap;
}
.qp-pill-good { color: var(--qp-up); border-color: rgba(22, 163, 74, 0.4); }
.qp-pill-warn { color: var(--qp-warn-c); border-color: rgba(183, 121, 31, 0.4); }
.qp-pill-bad { color: var(--qp-down); border-color: rgba(220, 38, 38, 0.4); }
.qp-pill-muted { color: var(--dsw-alias-label-secondary); }
.qp-empty { font-size: 11.5px; color: var(--dsw-alias-label-secondary); padding: 10px 0; }
.qp-empty-sm { padding: 6px 0; }
.qp-err { font-size: 11px; color: var(--qp-down); }
.tone-up { color: var(--qp-up); }
.tone-down { color: var(--qp-down); }
.qp-spin { animation: qp-turn 0.9s linear infinite; }
@keyframes qp-turn { to { transform: rotate(360deg); } }
`

    return {
      inject: ['slots'],
      apply(ctx) {
        try {
          const style = document.createElement('style')
          style.setAttribute('data-quant-panel', '')
          style.textContent = CSS
          document.head.appendChild(style)
          ctx.effect(() => () => {
            try { style.remove() } catch { /* already gone */ }
          })
        } catch { /* 样式失败不影响功能 */ }

        try {
          ctx.slots.inject('sidebar.panellist', () =>
            ctx.slots.register(
              { name: 'sidebar.panellist', id: PANEL_KEY, order: 35, label: () => '量化监控' },
              QuantIcon,
            ),
          )
        } catch { /* slot 不存在就不挂入口 */ }

        try {
          ctx.slots.inject('main', () =>
            ctx.slots.register({ name: 'main', key: PANEL_KEY }, QuantPanel),
          )
        } catch { /* 同上 */ }
      },
    }
  },
})
