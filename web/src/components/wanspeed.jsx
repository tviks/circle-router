import { useEffect, useState } from 'react'
import { ArrowDownToLine, ArrowUpFromLine, Gauge, Globe2, ShieldCheck, Timer } from 'lucide-react'

import { api } from '../lib/api'
import { dateLabel } from '../lib/format'
import { Card, Chip, Spinner, useToast } from './ui'

const PATHS = [
  ['direct', 'Напрямую', Globe2, 'через WAN, без VPN'],
  ['vpn', 'Через VPN', ShieldCheck, 'через активный сервер'],
]

/** Ниже 25 Мбит/с видео в 4K уже подтормаживает, ниже 5 — больно и HD. */
function speedTone(mbps) {
  if (mbps == null) return 'text-slate-500'
  if (mbps >= 100) return 'text-emerald-300'
  if (mbps >= 25) return 'text-sky-300'
  if (mbps >= 5) return 'text-amber-300'
  return 'text-rose-300'
}

export function WanSpeedCard() {
  const toast = useToast()
  const [data, setData] = useState(null)
  const running = Boolean(data?.job?.running)

  useEffect(() => {
    let alive = true
    let timer
    const tick = async () => {
      try {
        const next = await api.wanspeed()
        if (alive) setData(next)
      } catch {
        /* следующий тик */
      }
      if (alive) timer = setTimeout(tick, running ? 1000 : 15000)
    }
    tick()
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [running])

  const start = async () => {
    try {
      setData(await api.wanspeedStart(null, 10))
      toast('Спидтест запущен — около минуты; на гигабите съест 2–3 ГБ', 'ok')
    } catch (err) {
      toast(err.message, 'bad', 9000)
    }
  }

  const result = data?.result
  const job = data?.job

  return (
    <Card
      title="Скорость интернета"
      subtitle={
        result?.ts
          ? `замер ${dateLabel(result.ts)} · ${result.streams} потока по ${result.duration} с, с самого роутера`
          : 'замер с самого роутера: потолок канала без учёта Wi-Fi и клиентов'
      }
      actions={
        <>
          {running && (
            <Chip tone="info">
              <Spinner size={11} />
              {job.stage} · {Math.round((job.progress ?? 0) * 100)}%
            </Chip>
          )}
          {job?.error && !running && <Chip tone="bad">{job.error}</Chip>}
          <button className="btn-primary" onClick={start} disabled={running}>
            {running ? <Spinner /> : <Gauge size={15} />} Запустить тест
          </button>
        </>
      }
    >
      <div className="grid gap-4 md:grid-cols-2">
        {PATHS.map(([key, label, Icon, hint]) => {
          const r = result?.paths?.[key]
          const unavailable = key === 'vpn' && data && !data.vpn_available
          return (
            <div key={key} className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
              <div className="flex items-center gap-2 text-xs">
                <Icon size={14} className="text-slate-500" />
                <span className="font-medium uppercase tracking-wider text-slate-400">{label}</span>
                <span className="text-slate-600">{hint}</span>
                {r?.loc && (
                  <span className="mono ml-auto text-slate-500">
                    {r.loc}
                    {r.colo ? ` · ${r.colo}` : ''}
                  </span>
                )}
              </div>
              {r?.error ? (
                <p className="mt-4 text-sm text-rose-300">{r.error}</p>
              ) : r ? (
                <div className="mt-3 grid grid-cols-3 gap-3">
                  <Value icon={ArrowDownToLine} label="Скачивание" value={r.down_mbps} unit="Мбит/с" />
                  <Value icon={ArrowUpFromLine} label="Отдача" value={r.up_mbps} unit="Мбит/с" />
                  <Value
                    icon={Timer}
                    label="Отклик"
                    value={r.ping_ms}
                    unit="мс"
                    hint={r.jitter_ms != null ? `±${r.jitter_ms}` : null}
                    plain
                  />
                </div>
              ) : (
                <p className="mt-4 text-sm text-slate-600">
                  {unavailable ? 'VPN сейчас не поднят — мерить нечего.' : 'Замеров ещё не было.'}
                </p>
              )}
              {r?.ip && <div className="mono mt-3 truncate text-[11px] text-slate-600">выход: {r.ip}</div>}
            </div>
          )
        })}
      </div>
    </Card>
  )
}

function Value({ icon: Icon, label, value, unit, hint, plain }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 text-xs text-slate-500">
        <Icon size={12} className="shrink-0" />
        <span className="truncate">{label}</span>
      </div>
      <div
        className={`mt-1 text-2xl font-semibold tabular-nums ${plain ? 'text-slate-200' : speedTone(value)}`}
      >
        {value ?? '—'}
        <span className="ml-1 text-xs font-normal text-slate-500">{unit}</span>
      </div>
      {hint && <div className="mono text-[11px] text-slate-600">{hint}</div>}
    </div>
  )
}
