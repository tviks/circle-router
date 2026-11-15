import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { bytes, clockLabel, dateLabel, rate } from '../lib/format'

const AXIS = { stroke: '#475569', fontSize: 10 }
const GRID = '#1e293b'

function ChartTooltip({ active, payload, label, formatter, labelFormatter }) {
  if (!active || !payload?.length) return null
  return (
    <div className="rounded-xl border border-white/10 bg-ink-900/95 px-3 py-2 text-xs shadow-card backdrop-blur">
      <div className="mb-1 text-slate-500">{labelFormatter(label)}</div>
      {payload.map((entry) => (
        <div key={entry.dataKey} className="flex items-center gap-2 py-0.5">
          <span
            className="inline-block h-2 w-2 rounded-full"
            style={{ background: entry.color }}
          />
          <span className="text-slate-400">{entry.name}</span>
          <span className="mono ml-auto pl-3 text-slate-100">{formatter(entry.value)}</span>
        </div>
      ))}
    </div>
  )
}

export function SpeedChart({ samples, height = 200 }) {
  const data = (samples ?? []).map((s) => ({
    ts: s.ts,
    down: s.wan_rx_rate ?? 0,
    up: s.wan_tx_rate ?? 0,
  }))

  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: -12 }}>
          <defs>
            <linearGradient id="gradDown" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.45} />
              <stop offset="100%" stopColor="#38bdf8" stopOpacity={0.02} />
            </linearGradient>
            <linearGradient id="gradUp" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#a78bfa" stopOpacity={0.4} />
              <stop offset="100%" stopColor="#a78bfa" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={GRID} strokeDasharray="3 4" vertical={false} />
          <XAxis dataKey="ts" tickFormatter={clockLabel} {...AXIS} tickLine={false} axisLine={false} minTickGap={52} />
          <YAxis tickFormatter={(v) => rate(v)} {...AXIS} tickLine={false} axisLine={false} width={54} />
          <Tooltip
            content={<ChartTooltip formatter={rate} labelFormatter={clockLabel} />}
            cursor={{ stroke: '#334155', strokeWidth: 1 }}
          />
          <Area
            type="monotone"
            dataKey="down"
            name="Приём"
            stroke="#38bdf8"
            strokeWidth={2}
            fill="url(#gradDown)"
            isAnimationActive={false}
            dot={false}
          />
          <Area
            type="monotone"
            dataKey="up"
            name="Отдача"
            stroke="#a78bfa"
            strokeWidth={2}
            fill="url(#gradUp)"
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

export function HistoryChart({ points, height = 240 }) {
  const data = (points ?? []).map((p) => ({
    ts: p.ts,
    proxy: (p.proxy_up ?? 0) + (p.proxy_down ?? 0),
    direct: (p.direct_up ?? 0) + (p.direct_down ?? 0),
  }))

  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 6, right: 6, bottom: 0, left: -8 }}>
          <CartesianGrid stroke={GRID} strokeDasharray="3 4" vertical={false} />
          <XAxis dataKey="ts" tickFormatter={dateLabel} {...AXIS} tickLine={false} axisLine={false} minTickGap={64} />
          <YAxis tickFormatter={(v) => bytes(v)} {...AXIS} tickLine={false} axisLine={false} width={54} />
          <Tooltip
            content={<ChartTooltip formatter={bytes} labelFormatter={dateLabel} />}
            cursor={{ fill: 'rgba(148,163,184,0.06)' }}
          />
          <Legend
            wrapperStyle={{ fontSize: 12, color: '#94a3b8', paddingTop: 6 }}
            iconType="circle"
            iconSize={8}
          />
          <Bar dataKey="proxy" name="Через VPN" stackId="a" fill="#38bdf8" radius={[0, 0, 0, 0]} />
          <Bar dataKey="direct" name="Напрямую" stackId="a" fill="#475569" radius={[3, 3, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

/** Маленький график без осей — для карточек нод. */
export function Sparkline({ data, dataKey, color = '#38bdf8', formatter = (v) => v, height = 44, name }) {
  const points = (data ?? []).filter((p) => p[dataKey] !== null && p[dataKey] !== undefined)
  const id = `spark-${dataKey}-${color.replace('#', '')}`
  if (points.length < 2) {
    return <div style={{ height }} className="flex items-center text-[11px] text-slate-700">копим данные…</div>
  }
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 4, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <Tooltip
            content={<ChartTooltip formatter={formatter} labelFormatter={clockLabel} />}
            cursor={{ stroke: '#334155', strokeWidth: 1 }}
          />
          <XAxis dataKey="ts" hide />
          <YAxis hide domain={[0, 'auto']} />
          <Area
            type="monotone"
            dataKey={dataKey}
            name={name}
            stroke={color}
            strokeWidth={1.5}
            fill={`url(#${id})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
