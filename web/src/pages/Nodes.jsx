import { useCallback, useEffect, useState } from 'react'
import {
  Activity,
  ArrowDownToLine,
  ArrowUpFromLine,
  Cable,
  Cpu,
  KeyRound,
  Lightbulb,
  MemoryStick,
  Pencil,
  Plus,
  Rocket,
  RotateCw,
  Server,
  Sparkles,
  Timer,
  Trash2,
  Users,
} from 'lucide-react'

import { api } from '../lib/api'
import { usePoll } from '../lib/hooks'
import { bytes, dateLabel, duration, rate } from '../lib/format'
import { Sparkline } from '../components/charts'
import {
  Card,
  Chip,
  Dot,
  Empty,
  Field,
  Input,
  Modal,
  Spinner,
  TagList,
  Toggle,
  useToast,
} from '../components/ui'

const STATUS = {
  new: ['mute', 'новая'],
  deploying: ['info', 'разворачивается'],
  ready: ['ok', 'работает'],
  error: ['bad', 'ошибка'],
}

const DEFAULT_SNI_HINT =
  'Пусто — роутер переберёт свой список (microsoft, google, mozilla, samsung, nvidia, github) и выберет рабочий.'

export default function Nodes({ refreshStatus }) {
  const toast = useToast()
  const [fast, setFast] = useState(false)
  const { data, refresh } = usePoll(api.nodes, fast ? 1500 : 5000, [fast])
  const [deploying, setDeploying] = useState(false)
  const [editing, setEditing] = useState(null)
  const [removing, setRemoving] = useState(null)

  const nodes = data?.nodes ?? []
  const busy = Boolean(data?.busy)

  // Пока идёт задача — опрашиваем чаще, чтобы лог шёл живым.
  useEffect(() => {
    if (busy !== fast) setFast(busy)
    if (!busy && fast) refreshStatus?.()
  }, [busy, fast, refreshStatus])

  const run = useCallback(
    async (action, message) => {
      try {
        await action()
        if (message) toast(message, 'ok')
        setFast(true)
        await refresh()
      } catch (err) {
        toast(err.message, 'bad', 9000)
      }
    },
    [refresh, toast],
  )

  return (
    <div className="space-y-5">
      <Card
        title="Свои ноды"
        subtitle="VDS, на которую роутер сам ставит ядро xray и подбирает протокол под ваш канал"
        actions={
          <button className="btn-primary" onClick={() => setDeploying(true)} disabled={busy}>
            <Plus size={15} /> Развернуть ноду
          </button>
        }
        bodyClass={nodes.length ? 'p-0' : 'card-pad'}
      >
        {nodes.length === 0 ? (
          <Empty
            icon={Server}
            title="Своих нод пока нет"
            hint="Нужна чистая VDS с Linux и systemd (Debian, Ubuntu, Alma, Rocky) и доступ по SSH. На сервер ставится только ядро xray — без панелей и контейнеров; пароль используется один раз и не сохраняется."
          />
        ) : (
          <ul className="divide-y divide-white/[0.06]">
            {nodes.map((node) => (
              <NodeRow
                key={node.id}
                node={node}
                busy={busy}
                onTune={() => run(() => api.tuneNode(node.id), 'Подбор запущен, займёт пару минут')}
                onRestart={() => run(() => api.restartNode(node.id))}
                onApply={(key) =>
                  run(() => api.applyNodeProfile(node.id, key), 'Применяю профиль на ноде')
                }
                onEdit={() => setEditing(node)}
                onDelete={() => setRemoving(node)}
              />
            ))}
          </ul>
        )}
      </Card>

      <HowItWorks />

      <DeployModal
        open={deploying}
        onClose={() => setDeploying(false)}
        onDone={async () => {
          setDeploying(false)
          setFast(true)
          await refresh()
        }}
      />
      <SettingsModal
        node={editing}
        busy={busy}
        onClose={() => setEditing(null)}
        onSaved={async () => {
          setEditing(null)
          await refresh()
        }}
        onRedeploy={(credentials) =>
          run(() => api.redeployNode(editing.id, credentials), 'Переразворачиваю ноду').then(() =>
            setEditing(null),
          )
        }
      />
      <RemoveModal
        node={removing}
        onClose={() => setRemoving(null)}
        onConfirm={(wipe) =>
          run(() => api.deleteNode(removing.id, wipe), 'Удаляю ноду').then(() => setRemoving(null))
        }
      />
    </div>
  )
}

function NodeRow({ node, busy, onTune, onRestart, onApply, onEdit, onDelete }) {
  const [tone, label] = STATUS[node.status] ?? STATUS.new
  const job = node.job
  const showJob = job && (job.running || (job.finished && Date.now() / 1000 - job.finished < 600))
  const ready = node.status === 'ready'

  return (
    <li className={`px-5 py-5 sm:px-6 ${node.active ? 'bg-accent/[0.04]' : ''}`}>
      <div className="flex flex-wrap items-start gap-3">
        <span
          className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border ${
            node.active
              ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
              : 'border-white/10 bg-white/[0.02] text-slate-500'
          }`}
        >
          <Server size={16} />
        </span>
        <div className="min-w-0 flex-1 basis-56">
          <div className="flex flex-wrap items-center gap-2">
            <span className="truncate text-sm font-medium text-slate-100">{node.name}</span>
            <Chip tone={tone}>
              {node.status === 'deploying' && <Spinner size={11} />}
              {label}
            </Chip>
            {node.active && <Chip tone="ok">активный сервер</Chip>}
          </div>
          <div className="mono mt-0.5 truncate text-xs text-slate-500">
            {node.ssh_user}@{node.host}:{node.ssh_port}
            {node.facts?.os ? ` · ${node.facts.os}` : ''}
            {node.xray_version ? ` · xray ${node.xray_version}` : ''}
          </div>
          {node.profile_label && (
            <div className="mt-1.5 text-xs text-slate-400">
              <span className="text-slate-600">профиль: </span>
              {node.profile_label}
            </div>
          )}
          {node.status === 'error' && node.error && (
            <p className="mt-2 rounded-lg border border-rose-500/25 bg-rose-500/[0.08] px-3 py-2 text-xs text-rose-200">
              {node.error}
            </p>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <button className="btn-ghost !py-2" onClick={onTune} disabled={busy || !ready}>
            {job?.running && job.kind === 'tune' ? <Spinner size={14} /> : <Sparkles size={14} />}
            Подобрать
          </button>
          <IconButton title="Перезапустить ядро на ноде" onClick={onRestart} disabled={busy || !ready}>
            <RotateCw size={15} />
          </IconButton>
          <IconButton title="Настройки" onClick={onEdit}>
            <Pencil size={15} />
          </IconButton>
          <IconButton title="Удалить" onClick={onDelete} danger disabled={job?.running}>
            <Trash2 size={15} />
          </IconButton>
        </div>
      </div>

      {showJob && <JobPanel job={job} />}
      {node.recommendation && !job?.running && (
        <Recommendation rec={node.recommendation} busy={busy} onApply={() => onApply(null)} />
      )}
      {ready && <NodeMetrics node={node} />}
      {node.last_tune && <TuneResults node={node} busy={busy} onApply={onApply} />}
    </li>
  )
}

function IconButton({ children, title, onClick, danger, disabled }) {
  return (
    <button
      title={title}
      onClick={onClick}
      disabled={disabled}
      className={`flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.07] text-slate-500 transition hover:bg-white/[0.05] disabled:cursor-not-allowed disabled:opacity-40 ${
        danger ? 'hover:border-rose-500/40 hover:text-rose-300' : 'hover:text-slate-100'
      }`}
    >
      {children}
    </button>
  )
}

function JobPanel({ job }) {
  const [open, setOpen] = useState(false)
  const percent = Math.round((job.progress ?? 0) * 100)
  const tone = job.running ? 'info' : job.ok ? 'ok' : 'bad'
  const bar = { info: 'bg-accent', ok: 'bg-emerald-400/80', bad: 'bg-rose-400/80' }[tone]
  const lines = job.log ?? []

  return (
    <div className="mt-4 rounded-xl border border-white/[0.06] bg-white/[0.02] p-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Chip tone={tone}>
          {job.running && <Spinner size={11} />}
          {job.title}
        </Chip>
        <span className="min-w-0 flex-1 truncate text-slate-400">
          {job.running ? job.step : job.ok ? 'завершено' : job.error}
        </span>
        <span className="mono text-slate-500">{percent}%</span>
      </div>
      <div className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className={`h-full rounded-full transition-all duration-500 ${bar}`}
          style={{ width: `${Math.max(percent, job.running ? 3 : 0)}%` }}
        />
      </div>
      <button
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-xs text-slate-600 transition hover:text-slate-400"
      >
        {open ? 'Скрыть журнал' : `Журнал (${lines.length})`}
      </button>
      {open && (
        <ol className="mt-2 max-h-56 space-y-1 overflow-y-auto font-mono text-[11px] leading-relaxed text-slate-400">
          {lines.map((line, index) => (
            <li key={index} className="flex gap-2">
              <span className="shrink-0 text-slate-700">
                {new Date(line.ts * 1000).toLocaleTimeString('ru-RU')}
              </span>
              <span className="min-w-0 break-words">{line.text}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function Recommendation({ rec, busy, onApply }) {
  const compare = (a, b, unit) =>
    a != null && b != null ? `${b} → ${a} ${unit}` : a != null ? `${a} ${unit}` : null
  return (
    <div className="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-amber-500/25 bg-amber-500/[0.07] px-4 py-3">
      <Lightbulb size={16} className="shrink-0 text-amber-300" />
      <div className="min-w-0 flex-1 basis-60 text-xs">
        <div className="text-sm text-amber-100">Найден вариант лучше: {rec.label}</div>
        <div className="mt-0.5 text-amber-200/70">
          {rec.reason}
          {compare(rec.mbps, rec.current_mbps, 'Мбит/с') &&
            ` · скорость ${compare(rec.mbps, rec.current_mbps, 'Мбит/с')}`}
          {compare(rec.latency_ms, rec.current_latency_ms, 'мс') &&
            ` · отклик ${compare(rec.latency_ms, rec.current_latency_ms, 'мс')}`}
          {rec.seen > 1 && ` · подтверждено ${rec.seen} раза`}
        </div>
      </div>
      <button className="btn-primary !py-2" onClick={onApply} disabled={busy}>
        Применить
      </button>
    </div>
  )
}

function loadTone(value) {
  if (value == null) return 'mute'
  if (value >= 85) return 'bad'
  if (value >= 60) return 'warn'
  return 'ok'
}

function NodeMetrics({ node }) {
  const loader = useCallback(() => api.nodeMetrics(node.id), [node.id])
  const { data } = usePoll(loader, 5000, [node.id])
  const m = data?.latest ?? node.metrics ?? {}
  const history = data?.history ?? []
  const tunnel = data?.tunnel
  const throughput = data?.throughput ?? []
  const lastThroughput = throughput[throughput.length - 1]
  const tunnelSeries = data?.tunnel_series ?? []

  if (!m.online && !m.ts) {
    return (
      <p className="mt-4 flex items-center gap-2 text-xs text-slate-600">
        <Spinner size={12} /> подключаюсь к ноде за метриками…
      </p>
    )
  }

  return (
    <div className="mt-4 grid gap-4 lg:grid-cols-2">
      <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
        <div className="mb-3 flex items-center justify-between gap-2 text-xs">
          <span className="font-medium uppercase tracking-wider text-slate-500">Сервер</span>
          <span className="flex items-center gap-1.5 text-slate-500">
            <Dot tone={m.online ? 'ok' : 'bad'} />
            {m.online ? (m.via_tunnel ? 'через туннель' : 'напрямую по SSH') : m.error ?? 'нет связи'}
          </span>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 text-xs sm:grid-cols-3">
          <Metric icon={Cpu} label="CPU" value={m.cpu != null ? `${m.cpu}%` : '—'} tone={loadTone(m.cpu)} />
          <Metric
            icon={MemoryStick}
            label="Память"
            value={m.mem_pct != null ? `${m.mem_pct}%` : '—'}
            hint={m.mem_total ? `${bytes(m.mem_used)} из ${bytes(m.mem_total)}` : null}
            tone={loadTone(m.mem_pct)}
          />
          <Metric icon={Timer} label="Аптайм" value={m.uptime ? duration(m.uptime) : '—'} />
          <Metric icon={ArrowDownToLine} label={`Приём ${m.iface ?? ''}`} value={rate(m.rx_rate ?? 0)} />
          <Metric icon={ArrowUpFromLine} label="Отдача" value={rate(m.tx_rate ?? 0)} />
          <Metric icon={Users} label="Соединений" value={m.connections ?? '—'} />
          <Metric
            icon={Activity}
            label="Ядро xray"
            value={m.xray_running ? `${m.xray_cpu ?? 0}% CPU` : 'не запущено'}
            hint={m.xray_rss ? bytes(m.xray_rss) : null}
            tone={m.xray_running ? 'mute' : 'bad'}
          />
          <Metric
            icon={Cpu}
            label="Load average"
            value={(m.load ?? []).map((v) => v.toFixed(2)).join(' ') || '—'}
            hint={m.cpus ? `${m.cpus} ядер` : null}
          />
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3 border-t border-white/[0.06] pt-3">
          <div>
            <div className="text-[11px] text-slate-600">CPU, 15 мин</div>
            <Sparkline data={history} dataKey="cpu" name="CPU" color="#38bdf8" formatter={(v) => `${v}%`} />
          </div>
          <div>
            <div className="text-[11px] text-slate-600">Трафик ноды</div>
            <Sparkline data={history} dataKey="tx_rate" name="Отдача" color="#a78bfa" formatter={rate} />
          </div>
        </div>
      </div>

      <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
        <div className="mb-3 flex items-center justify-between gap-2 text-xs">
          <span className="font-medium uppercase tracking-wider text-slate-500">Линк</span>
          {tunnel && <TunnelChip tunnel={tunnel} />}
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 text-xs sm:grid-cols-3">
          <Metric
            icon={Cable}
            label="RTT до ноды"
            value={m.rtt_avg != null ? `${m.rtt_avg} мс` : '—'}
            hint={m.rtt_jitter != null ? `разброс ±${m.rtt_jitter} мс` : null}
          />
          <Metric
            icon={Cable}
            label="Потери TCP"
            value={m.rtt_loss != null ? `${(m.rtt_loss * 100).toFixed(0)}%` : '—'}
            tone={m.rtt_loss > 0.1 ? 'bad' : m.rtt_loss > 0 ? 'warn' : 'mute'}
          />
          {tunnel ? (
            <>
              <Metric
                icon={Activity}
                label="Отклик туннеля"
                value={tunnel.latency_ms != null ? `${tunnel.latency_ms} мс` : '—'}
                hint={tunnel.avg_latency_5m != null ? `среднее 5 мин: ${tunnel.avg_latency_5m} мс` : null}
              />
              <Metric
                icon={ArrowDownToLine}
                label="Через VPN ↓"
                value={rate(lastThroughput?.down ?? 0)}
              />
              <Metric icon={ArrowUpFromLine} label="Через VPN ↑" value={rate(lastThroughput?.up ?? 0)} />
              <Metric
                icon={RotateCw}
                label="Обходов за час"
                value={tunnel.failovers_hour ?? 0}
                tone={tunnel.failovers_hour >= 3 ? 'bad' : tunnel.failovers_hour > 0 ? 'warn' : 'mute'}
              />
            </>
          ) : (
            <p className="col-span-2 self-center text-slate-600 sm:col-span-1">
              Нода не активна — трафик через неё сейчас не идёт.
            </p>
          )}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3 border-t border-white/[0.06] pt-3">
          <div>
            <div className="text-[11px] text-slate-600">RTT, 15 мин</div>
            <Sparkline data={history} dataKey="rtt_ms" name="RTT" color="#34d399" formatter={(v) => `${v} мс`} />
          </div>
          <div>
            <div className="text-[11px] text-slate-600">
              {tunnel ? 'Отклик туннеля' : 'Соединения'}
            </div>
            {tunnel ? (
              <Sparkline
                data={tunnelSeries}
                dataKey="latency_ms"
                name="Отклик"
                color="#fbbf24"
                formatter={(v) => `${v} мс`}
              />
            ) : (
              <Sparkline data={history} dataKey="connections" name="Соединений" color="#fbbf24" />
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

export function TunnelChip({ tunnel }) {
  if (!tunnel || tunnel.state === 'idle') return null
  const map = {
    up: ['ok', 'туннель работает'],
    down: ['bad', 'туннель не отвечает — клиенты напрямую'],
    unknown: ['info', 'проверяю туннель…'],
  }
  const [tone, text] = map[tunnel.state] ?? map.unknown
  return (
    <Chip tone={tone}>
      <Dot tone={tone} pulse={tunnel.state === 'up'} />
      {text}
    </Chip>
  )
}

function Metric({ icon: Icon, label, value, hint, tone = 'mute' }) {
  const colors = {
    ok: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-rose-300',
    mute: 'text-slate-200',
  }
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5 text-slate-500">
        <Icon size={11} className="shrink-0" />
        <span className="truncate">{label}</span>
      </div>
      <div className={`mono mt-0.5 truncate ${colors[tone] ?? colors.mute}`}>{value}</div>
      {hint && <div className="truncate text-[11px] text-slate-600">{hint}</div>}
    </div>
  )
}

/** Тот же ключ, что считает бэкенд (node_build.cand_key). */
function candKey(c) {
  if (!c) return null
  return `${c.transport}:${c.port}:${c.sni || '-'}:${c.fp || '-'}:${c.frag ? 'frag' : 'plain'}`
}

const ROUNDS = { A: 'транспорт', B: 'SNI', C: 'стратегия', D: 'финал' }

function TuneResults({ node, busy, onApply }) {
  const [open, setOpen] = useState(false)
  const tune = node.last_tune
  const candidates = tune.candidates ?? []
  const working = candidates.filter((c) => (c.success ?? 0) >= 0.67).length

  return (
    <div className="mt-4">
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-xs text-slate-600 transition hover:text-slate-400"
      >
        {open ? 'Скрыть результаты подбора' : 'Результаты подбора'} · {dateLabel(tune.ts)} ·{' '}
        проходят {working} из {candidates.length}
        {tune.reason ? ` · ${tune.reason}` : ''}
      </button>
      {open && (
        <div className="mt-3 overflow-x-auto rounded-xl border border-white/[0.06]">
          <table className="w-full min-w-[640px] text-left text-xs">
            <thead className="border-b border-white/[0.06] text-slate-500">
              <tr>
                <th className="px-3 py-2 font-medium">Вариант</th>
                <th className="px-3 py-2 font-medium">Раунд</th>
                <th className="px-3 py-2 text-right font-medium">Успех</th>
                <th className="px-3 py-2 text-right font-medium">Отклик</th>
                <th className="px-3 py-2 text-right font-medium">Скорость</th>
                <th className="px-3 py-2 text-right font-medium">Оценка</th>
                <th className="px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-white/[0.04]">
              {candidates.map((c) => {
                const ok = (c.success ?? 0) >= 0.67 && !c.stall
                const isWinner = c.key === tune.winner_key
                const isCurrent = c.current || c.key === candKey(node.profile)
                return (
                  <tr key={c.key} className={isWinner ? 'bg-emerald-500/[0.05]' : ''}>
                    <td className="px-3 py-2">
                      <div className="flex flex-wrap items-center gap-1.5 text-slate-300">
                        <span className={ok ? '' : 'text-slate-600'}>{c.label}</span>
                        {isWinner && <Chip tone="ok">лучший</Chip>}
                        {isCurrent && <Chip tone="info">текущий</Chip>}
                      </div>
                      {c.error && !ok && <div className="mt-0.5 text-[11px] text-rose-300/80">{c.error}</div>}
                    </td>
                    <td className="px-3 py-2 text-slate-500">{ROUNDS[c.round] ?? c.round}</td>
                    <td className={`mono px-3 py-2 text-right ${ok ? 'text-slate-300' : 'text-rose-300'}`}>
                      {c.total ? `${c.ok}/${c.total}` : '—'}
                    </td>
                    <td className="mono px-3 py-2 text-right text-slate-400">
                      {c.latency_ms != null ? `${c.latency_ms} мс` : '—'}
                    </td>
                    <td className="mono px-3 py-2 text-right text-slate-300">
                      {c.mbps != null ? `${c.mbps} Мбит/с` : '—'}
                    </td>
                    <td className="mono px-3 py-2 text-right text-slate-400">{c.score ?? '—'}</td>
                    <td className="px-3 py-2 text-right">
                      {ok && !isCurrent && c.cand && (
                        <button
                          className="text-accent transition hover:text-accent-soft disabled:opacity-40"
                          onClick={() => onApply(c.key)}
                          disabled={busy}
                        >
                          применить
                        </button>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          {(tune.sni ?? []).length > 0 && (
            <div className="flex flex-wrap gap-1.5 border-t border-white/[0.06] px-3 py-2.5 text-[11px]">
              <span className="text-slate-600">домены маскировки с сервера:</span>
              {tune.sni.map((s) => (
                <Chip key={s.domain} tone={s.ok ? 'ok' : 'mute'}>
                  {s.domain}
                  {s.ms != null && <span className="opacity-60">{s.ms} мс</span>}
                </Chip>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function HowItWorks() {
  return (
    <Card title="Как это работает" subtitle="что делает роутер после нажатия «Развернуть»">
      <ol className="grid gap-4 text-sm text-slate-400 sm:grid-cols-2 lg:grid-cols-4">
        {[
          [KeyRound, 'Доступ', 'Заходит по паролю один раз, кладёт свой SSH-ключ и дальше работает только по нему. Пароль не сохраняется.'],
          [Rocket, 'Установка', 'Ставит релизный xray той же версии, что на роутере, systemd-unit, BBR и буферы TCP. Без панелей, агентов и Docker.'],
          [Sparkles, 'Подбор', 'Поднимает пробные инбаунды и меряет с роутера: Vision, XHTTP, gRPC, SS-2022, Hysteria2, домены маскировки, фрагментацию ClientHello, uTLS-отпечатки.'],
          [Lightbulb, 'Советник', 'Перепроверяет по расписанию и после частых обрывов. Нашёл лучше — предлагает или применяет сам, если включено.'],
        ].map(([Icon, title, text]) => (
          <li key={title} className="flex gap-3">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.02]">
              <Icon size={14} className="text-accent" />
            </span>
            <div>
              <div className="text-slate-200">{title}</div>
              <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{text}</p>
            </div>
          </li>
        ))}
      </ol>
    </Card>
  )
}

const EMPTY_DEPLOY = {
  name: '',
  host: '',
  ssh_port: 22,
  ssh_user: 'root',
  auth: 'password',
  password: '',
  private_key: '',
  key_passphrase: '',
  sni: [],
  activate: true,
  auto: true,
  auto_apply: false,
}

function DeployModal({ open, onClose, onDone }) {
  const toast = useToast()
  const [form, setForm] = useState(EMPTY_DEPLOY)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (open) setForm(EMPTY_DEPLOY)
  }, [open])

  const set = (patch) => setForm((f) => ({ ...f, ...patch }))
  const ready =
    form.host.trim() && (form.auth === 'password' ? form.password : form.private_key.trim())

  const submit = async () => {
    setBusy(true)
    try {
      await api.createNode({
        name: form.name.trim(),
        host: form.host.trim(),
        ssh_port: Number(form.ssh_port) || 22,
        ssh_user: form.ssh_user.trim() || 'root',
        password: form.auth === 'password' ? form.password : null,
        private_key: form.auth === 'key' ? form.private_key : null,
        key_passphrase: form.auth === 'key' ? form.key_passphrase || null : null,
        sni: form.sni,
        activate: form.activate,
        tune: { auto: form.auto, auto_apply: form.auto_apply },
      })
      toast('Развёртывание запущено — займёт 2–4 минуты', 'ok')
      await onDone()
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Развернуть свою ноду"
      wide
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Отмена
          </button>
          <button className="btn-primary" onClick={submit} disabled={busy || !ready}>
            {busy ? <Spinner /> : <Rocket size={15} />} Развернуть
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-[1fr_1fr_6rem]">
          <Field label="Название">
            <Input value={form.name} onChange={(e) => set({ name: e.target.value })} placeholder="Амстердам" />
          </Field>
          <Field label="IP или домен сервера">
            <Input
              value={form.host}
              onChange={(e) => set({ host: e.target.value })}
              placeholder="203.0.113.10"
              className="font-mono"
            />
          </Field>
          <Field label="SSH-порт">
            <Input
              type="number"
              value={form.ssh_port}
              onChange={(e) => set({ ssh_port: e.target.value })}
              className="font-mono"
            />
          </Field>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Пользователь" hint="root или пользователь с sudo без пароля">
            <Input value={form.ssh_user} onChange={(e) => set({ ssh_user: e.target.value })} className="font-mono" />
          </Field>
          <Field label="Вход">
            <div className="flex gap-1 rounded-xl border border-white/[0.07] bg-ink-850/80 p-1">
              {[
                ['password', 'Пароль'],
                ['key', 'Приватный ключ'],
              ].map(([id, text]) => (
                <button
                  key={id}
                  type="button"
                  onClick={() => set({ auth: id })}
                  className={`flex-1 rounded-lg px-3 py-1.5 text-sm transition ${
                    form.auth === id ? 'bg-accent/15 text-accent-soft' : 'text-slate-500 hover:text-slate-300'
                  }`}
                >
                  {text}
                </button>
              ))}
            </div>
          </Field>
        </div>

        {form.auth === 'password' ? (
          <Field label="Пароль" hint="Нужен один раз: роутер добавит свой ключ и пароль забудет">
            <Input
              type="password"
              autoComplete="off"
              value={form.password}
              onChange={(e) => set({ password: e.target.value })}
            />
          </Field>
        ) : (
          <>
            <Field label="Приватный ключ" hint="OpenSSH или PEM; после деплоя роутер ходит своим ключом">
              <textarea
                className="input min-h-[110px] font-mono text-xs"
                value={form.private_key}
                onChange={(e) => set({ private_key: e.target.value })}
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
              />
            </Field>
            <Field label="Пароль ключа" hint="если ключ им защищён; используется один раз и не сохраняется">
              <Input
                type="password"
                autoComplete="off"
                value={form.key_passphrase}
                onChange={(e) => set({ key_passphrase: e.target.value })}
              />
            </Field>
          </>
        )}

        <Field label="Домены маскировки Reality" hint={DEFAULT_SNI_HINT}>
          <TagList values={form.sni} onChange={(v) => set({ sni: v })} placeholder="www.example.com" />
        </Field>

        <div className="space-y-3 rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
          <Toggle
            checked={form.activate}
            onChange={(v) => set({ activate: v })}
            label="Сделать активным сервером и включить VPN"
            description="как только подбор закончится"
          />
          <Toggle
            checked={form.auto}
            onChange={(v) => set({ auto: v })}
            label="Перепроверять автоматически"
            description="раз в сутки и после частых обрывов туннеля"
          />
          <Toggle
            checked={form.auto_apply}
            onChange={(v) => set({ auto_apply: v })}
            disabled={!form.auto}
            label="Применять найденное без подтверждения"
            description="только если вариант лучше на 25% и подтвердился дважды"
          />
        </div>
      </div>
    </Modal>
  )
}

function SettingsModal({ node, busy, onClose, onSaved, onRedeploy }) {
  const toast = useToast()
  const [form, setForm] = useState(null)
  const [saving, setSaving] = useState(false)
  const [password, setPassword] = useState('')

  useEffect(() => {
    if (node) setForm({ name: node.name, sni: node.sni ?? [], ...node.tune })
    setPassword('')
  }, [node])

  if (!node || !form) return null
  const set = (patch) => setForm((f) => ({ ...f, ...patch }))

  const save = async () => {
    setSaving(true)
    try {
      await api.editNode(node.id, {
        name: form.name,
        sni: form.sni,
        tune: {
          auto: form.auto,
          auto_apply: form.auto_apply,
          interval_hours: Number(form.interval_hours),
          improve_pct: Number(form.improve_pct),
          speed_seconds: Number(form.speed_seconds),
        },
      })
      toast('Настройки ноды сохранены', 'ok')
      await onSaved()
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setSaving(false)
    }
  }

  const forgetKey = async () => {
    if (!window.confirm('Сбросить запомненный ключ сервера? Делайте это только после переустановки ОС на VDS.')) return
    try {
      await api.forgetNodeHostKey(node.id)
      toast('Ключ сервера сброшен — запомнится при следующем подключении', 'ok')
    } catch (err) {
      toast(err.message, 'bad')
    }
  }

  return (
    <Modal
      open
      onClose={onClose}
      title={`Нода «${node.name}»`}
      wide
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Отмена
          </button>
          <button className="btn-primary" onClick={save} disabled={saving}>
            {saving && <Spinner />} Сохранить
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Название">
          <Input value={form.name} onChange={(e) => set({ name: e.target.value })} />
        </Field>
        <Field label="Домены маскировки Reality" hint={DEFAULT_SNI_HINT}>
          <TagList values={form.sni} onChange={(v) => set({ sni: v })} placeholder="www.example.com" />
        </Field>

        <div className="space-y-3 rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
          <Toggle
            checked={form.auto}
            onChange={(v) => set({ auto: v })}
            label="Перепроверять автоматически"
            description="по расписанию и после частых обрывов туннеля"
          />
          <Toggle
            checked={form.auto_apply}
            onChange={(v) => set({ auto_apply: v })}
            disabled={!form.auto}
            label="Применять найденное без подтверждения"
            description="после повторного подтверждения или если текущий профиль перестал проходить"
          />
          <div className="grid gap-4 pt-1 sm:grid-cols-3">
            <Field label="Раз в, часов">
              <Input
                type="number"
                min={1}
                max={168}
                value={form.interval_hours}
                onChange={(e) => set({ interval_hours: e.target.value })}
              />
            </Field>
            <Field label="Порог выигрыша, %">
              <Input
                type="number"
                min={5}
                max={200}
                value={form.improve_pct}
                onChange={(e) => set({ improve_pct: e.target.value })}
              />
            </Field>
            <Field label="Замер скорости, с" hint="на каждого финалиста">
              <Input
                type="number"
                min={3}
                max={15}
                value={form.speed_seconds}
                onChange={(e) => set({ speed_seconds: e.target.value })}
              />
            </Field>
          </div>
        </div>

        <div className="space-y-3 rounded-xl border border-white/[0.06] p-4">
          <div className="text-sm text-slate-200">Обслуживание</div>
          <p className="text-xs text-slate-500">
            Переразвёртывание заново ставит ядро и проводит полный подбор. Обычно хватает ключа
            роутера; пароль нужен, только если ключ с сервера пропал.
          </p>
          <div className="flex flex-wrap items-end gap-2">
            <Input
              type="password"
              autoComplete="off"
              placeholder="пароль (необязательно)"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="max-w-xs"
            />
            <button
              className="btn-ghost"
              disabled={busy}
              onClick={() => onRedeploy(password ? { password } : {})}
            >
              <Rocket size={15} /> Переразвернуть
            </button>
            {node.has_host_key && (
              <button className="btn-ghost" onClick={forgetKey}>
                <KeyRound size={15} /> Сбросить ключ сервера
              </button>
            )}
          </div>
        </div>
      </div>
    </Modal>
  )
}

function RemoveModal({ node, onClose, onConfirm }) {
  const [wipe, setWipe] = useState(true)
  useEffect(() => setWipe(true), [node])
  if (!node) return null
  return (
    <Modal
      open
      onClose={onClose}
      title="Удалить ноду"
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Отмена
          </button>
          <button className="btn-danger" onClick={() => onConfirm(wipe)}>
            <Trash2 size={15} /> Удалить
          </button>
        </>
      }
    >
      <div className="space-y-4 text-sm text-slate-400">
        <p>
          «{node.name}» исчезнет из роутера вместе со своим сервером в списке VPN.
          {node.active && ' Это активный сервер — роутер переключится на следующий из списка.'}
        </p>
        <label className="flex items-center gap-2 text-slate-300">
          <input
            type="checkbox"
            checked={wipe}
            onChange={(e) => setWipe(e.target.checked)}
            className="h-4 w-4 rounded border-white/20 bg-ink-850 accent-sky-400"
          />
          Заодно снять ядро, unit и ключ роутера с самого сервера
        </label>
      </div>
    </Modal>
  )
}
