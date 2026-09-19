import { Cpu, HardDrive, MemoryStick, Thermometer } from 'lucide-react'

import { bytes, duration, rate } from '../lib/format'
import { Card, Chip } from './ui'

/** Пороги одинаковые для загрузки и заполнения: 60% — спокойно, 85% — тесно. */
function loadTone(percent) {
  if (percent >= 85) return 'bad'
  if (percent >= 60) return 'warn'
  return 'ok'
}

/** У температур шкала своя: греться до 60 нормально, за 85 уже тревожно. */
function tempTone(celsius) {
  if (celsius == null) return 'mute'
  if (celsius >= 85) return 'bad'
  if (celsius >= 70) return 'warn'
  return 'ok'
}

const BAR_COLORS = {
  ok: 'bg-emerald-400/80',
  warn: 'bg-amber-400/80',
  bad: 'bg-rose-400/80',
  mute: 'bg-slate-600',
}

function Bar({ percent, tone, className = '' }) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0))
  return (
    <div className={`h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06] ${className}`}>
      <div
        className={`h-full rounded-full transition-all duration-500 ${BAR_COLORS[tone] ?? BAR_COLORS.mute}`}
        style={{ width: `${value}%` }}
      />
    </div>
  )
}

function TempChip({ value }) {
  if (value == null) return null
  return (
    <Chip tone={tempTone(value)} className="shrink-0 tabular-nums">
      <Thermometer size={11} />
      {value.toFixed(0)}°
    </Chip>
  )
}

export function ServerHealth({ system }) {
  const cpu = system?.cpu ?? {}
  const memory = system?.memory ?? {}
  const disks = system?.disks ?? []
  const io = system?.disk_io ?? {}
  const cores = cpu.cores ?? []
  const coreSensors = (system?.sensors ?? []).filter(
    (s) => s.kind === 'cpu' && /core\s*\d+/i.test(s.label),
  )

  return (
    <div className="grid gap-4 sm:gap-5 lg:grid-cols-3">
      <Card
        title="Процессор"
        subtitle={cpu.model || '—'}
        actions={<TempChip value={cpu.temperature} />}
      >
        <div className="flex items-end justify-between gap-3">
          <div className="text-3xl font-semibold tabular-nums text-slate-100">
            {(cpu.usage ?? 0).toFixed(0)}
            <span className="ml-0.5 text-lg text-slate-500">%</span>
          </div>
          <div className="text-right text-xs text-slate-500">
            {cpu.frequency_mhz ? <div className="mono">{cpu.frequency_mhz} МГц</div> : null}
            <div className="mono">
              LA {(cpu.loadavg ?? [0, 0, 0]).map((v) => v.toFixed(2)).join(' ')}
            </div>
          </div>
        </div>
        <Bar percent={cpu.usage} tone={loadTone(cpu.usage ?? 0)} className="mt-3" />

        <div className="mt-4 space-y-2 border-t border-white/[0.06] pt-4">
          {cores.map((core) => {
            const sensor = coreSensors.find((s) =>
              new RegExp(`core\\s*${core.core}\\b`, 'i').test(s.label),
            )
            return (
              <div key={core.core} className="flex items-center gap-2.5">
                <span className="mono w-10 shrink-0 text-[11px] text-slate-600">
                  #{core.core}
                </span>
                <Bar percent={core.usage} tone={loadTone(core.usage)} />
                <span className="mono w-10 shrink-0 text-right text-[11px] text-slate-400">
                  {core.usage.toFixed(0)}%
                </span>
                <span className="mono w-9 shrink-0 text-right text-[11px] text-slate-600">
                  {sensor ? `${sensor.value.toFixed(0)}°` : ''}
                </span>
              </div>
            )
          })}
          {cores.length === 0 && (
            <p className="text-xs text-slate-600">Данные по ядрам ещё собираются…</p>
          )}
        </div>
      </Card>

      <Card
        title="Память"
        subtitle={memory.total ? `всего ${bytes(memory.total)}` : '—'}
        actions={<Cpu size={15} className="text-slate-600" />}
      >
        <div className="flex items-end justify-between gap-3">
          <div className="text-3xl font-semibold tabular-nums text-slate-100">
            {(memory.percent ?? 0).toFixed(0)}
            <span className="ml-0.5 text-lg text-slate-500">%</span>
          </div>
          <div className="mono text-right text-xs text-slate-500">
            {bytes(memory.used ?? 0)} из {bytes(memory.total ?? 0)}
          </div>
        </div>
        <Bar percent={memory.percent} tone={loadTone(memory.percent ?? 0)} className="mt-3" />

        <dl className="mt-4 grid grid-cols-2 gap-x-4 gap-y-2 border-t border-white/[0.06] pt-4 text-xs">
          <Metric icon={MemoryStick} label="Доступно" value={bytes(memory.available ?? 0)} />
          <Metric icon={MemoryStick} label="Кеш" value={bytes(memory.cached ?? 0)} />
          {memory.swap_total > 0 && (
            <>
              <Metric icon={MemoryStick} label="Swap" value={bytes(memory.swap_used ?? 0)} />
              <Metric
                icon={MemoryStick}
                label="Swap всего"
                value={bytes(memory.swap_total ?? 0)}
              />
            </>
          )}
          <Metric icon={Cpu} label="Аптайм" value={duration(system?.uptime)} />
        </dl>
      </Card>

      <Card title="Диски" subtitle={`${disks.length} шт.`} bodyClass="p-0">
        {disks.length === 0 ? (
          <p className="px-5 py-8 text-center text-sm text-slate-600 sm:px-6">
            Диски не определились.
          </p>
        ) : (
          <ul className="divide-y divide-white/[0.06]">
            {disks.map((disk) => {
              const speeds = io[disk.device] ?? io[disk.device?.replace(/p?\d+$/, '')] ?? {}
              return (
                <li key={disk.source + disk.mount} className="px-5 py-3 sm:px-6">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <HardDrive size={13} className="shrink-0 text-slate-600" />
                    <span className="mono min-w-0 flex-1 truncate text-xs text-slate-300">
                      {disk.mount || disk.source}
                    </span>
                    <TempChip value={disk.temperature} />
                  </div>
                  {disk.total > 0 && (
                    <>
                      <Bar
                        percent={disk.percent}
                        tone={loadTone(disk.percent)}
                        className="mt-2"
                      />
                      <div className="mono mt-1.5 flex flex-wrap justify-between gap-x-3 text-[11px] text-slate-500">
                        <span>
                          {bytes(disk.used)} из {bytes(disk.total)} · {disk.percent}%
                        </span>
                        <span className="text-slate-600">
                          ↓{rate(speeds.read ?? 0)} ↑{rate(speeds.write ?? 0)}
                        </span>
                      </div>
                    </>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </Card>
    </div>
  )
}

function Metric({ icon: Icon, label, value }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 text-slate-500">
        <Icon size={11} className="shrink-0" />
        <span className="truncate">{label}</span>
      </div>
      <div className="mono mt-0.5 truncate text-slate-200">{value}</div>
    </div>
  )
}
