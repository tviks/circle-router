import { useCallback, useEffect, useState } from 'react'
import { Database, KeyRound, RotateCw, ScrollText } from 'lucide-react'

import { api } from '../lib/api'
import { usePoll } from '../lib/hooks'
import { dateLabel } from '../lib/format'
import { Card, Chip, Field, Input, Spinner, useToast } from '../components/ui'

const SERVICES = [
  ['xray', 'xray-core'],
  ['dnsmasq', 'dnsmasq'],
]

export default function System({ status, refreshStatus }) {
  const toast = useToast()
  const [service, setService] = useState('xray')
  const [tail, setTail] = useState(300)
  const [busy, setBusy] = useState('')

  const loadLogs = useCallback(() => api.logs(service, tail), [service, tail])
  const { data: logs, refresh: refreshLogs } = usePoll(loadLogs, 6000, [service, tail])

  const restart = async (name) => {
    setBusy(name)
    try {
      await api.restartService(name)
      toast(`${name} перезапущен`, 'ok')
      await Promise.all([refreshStatus?.(), refreshLogs()])
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setBusy('')
    }
  }

  const updateGeo = async () => {
    setBusy('geo')
    try {
      const result = await api.geoUpdate()
      toast(
        `Базы обновлены: geosite ${(result.geosite / 1048576).toFixed(1)} МБ, ` +
          `geoip ${(result.geoip / 1048576).toFixed(1)} МБ`,
        'ok',
      )
    } catch (err) {
      toast(err.message, 'bad', 12000)
    } finally {
      setBusy('')
    }
  }

  const lastApply = status?.last_apply

  return (
    <div className="space-y-5">
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Сервисы" bodyClass="p-0">
          <ul className="divide-y divide-white/[0.06]">
            {SERVICES.map(([key, label]) => {
              const info = status?.services?.[key]
              return (
                <li key={key} className="flex items-center gap-3 px-5 py-4 sm:px-6">
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-slate-200">{label}</div>
                    <div className="truncate text-xs text-slate-500">
                      {info?.image ?? '—'}
                    </div>
                  </div>
                  <Chip tone={info?.running ? 'ok' : 'bad'}>{info?.status ?? '—'}</Chip>
                  <button
                    className="btn-ghost !px-3 !py-2"
                    onClick={() => restart(key)}
                    disabled={busy === key}
                  >
                    {busy === key ? <Spinner size={14} /> : <RotateCw size={14} />}
                  </button>
                </li>
              )
            })}
          </ul>
        </Card>

        <Card title="Базы geosite / geoip" subtitle="списки доменов и подсетей от v2fly">
          <p className="text-sm text-slate-400">
            Скачивает свежие <span className="mono">dlc.dat</span> и{' '}
            <span className="mono">geoip.dat</span>, после чего перезапускает xray. Раз в месяц
            более чем достаточно.
          </p>
          <button className="btn-primary mt-4" onClick={updateGeo} disabled={busy === 'geo'}>
            {busy === 'geo' ? <Spinner /> : <Database size={15} />} Обновить базы
          </button>
        </Card>
      </div>

      <PasswordCard />

      {lastApply?.ts > 0 && (
        <Card
          title="Последнее применение"
          subtitle={dateLabel(lastApply.ts)}
          actions={<Chip tone={lastApply.ok ? 'ok' : 'bad'}>{lastApply.ok ? 'успешно' : 'с ошибками'}</Chip>}
        >
          <ol className="space-y-1.5 text-sm text-slate-400">
            {(lastApply.steps ?? []).map((step, index) => (
              <li key={index} className="flex gap-2">
                <span className="mono text-slate-700">{String(index + 1).padStart(2, '0')}</span>
                {step}
              </li>
            ))}
          </ol>
          {(lastApply.errors ?? []).length > 0 && (
            <ul className="mt-4 space-y-1.5 border-t border-rose-500/20 pt-4 text-sm text-rose-300">
              {lastApply.errors.map((error, index) => (
                <li key={index}>{error}</li>
              ))}
            </ul>
          )}
        </Card>
      )}

      <Card
        title="Логи"
        actions={
          <div className="flex items-center gap-1">
            {SERVICES.map(([key, label]) => (
              <button
                key={key}
                onClick={() => setService(key)}
                className={`rounded-lg px-2.5 py-1 text-xs transition ${
                  service === key
                    ? 'bg-accent/15 text-accent-soft'
                    : 'text-slate-500 hover:bg-white/[0.04] hover:text-slate-300'
                }`}
              >
                {label}
              </button>
            ))}
            <select
              className="ml-2 rounded-lg border border-white/[0.07] bg-ink-850 px-2 py-1 text-xs text-slate-400"
              value={tail}
              onChange={(e) => setTail(Number(e.target.value))}
            >
              {[100, 300, 1000, 2000].map((n) => (
                <option key={n} value={n}>
                  {n} строк
                </option>
              ))}
            </select>
          </div>
        }
        bodyClass="p-0"
      >
        <LogView text={logs?.log} />
      </Card>
    </div>
  )
}

function PasswordCard() {
  const toast = useToast()
  const [form, setForm] = useState({ current: '', next: '', repeat: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const mismatch = form.repeat.length > 0 && form.next !== form.repeat
  const tooShort = form.next.length > 0 && form.next.length < 8
  const ready = form.current && form.next.length >= 8 && form.next === form.repeat

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await api.changePassword(form.current, form.next)
      setForm({ current: '', next: '', repeat: '' })
      toast('Пароль изменён. Сессии на других устройствах завершены.', 'ok', 8000)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card title="Пароль панели" subtitle="меняется на лету, перезапуск не нужен">
      <form onSubmit={submit} className="grid gap-4 sm:grid-cols-3">
        <Field label="Текущий пароль">
          <Input
            type="password"
            autoComplete="current-password"
            value={form.current}
            onChange={(e) => setForm({ ...form, current: e.target.value })}
          />
        </Field>
        <Field label="Новый пароль" error={tooShort ? 'минимум 8 символов' : ''}>
          <Input
            type="password"
            autoComplete="new-password"
            value={form.next}
            onChange={(e) => setForm({ ...form, next: e.target.value })}
          />
        </Field>
        <Field label="Ещё раз" error={mismatch ? 'пароли не совпадают' : ''}>
          <Input
            type="password"
            autoComplete="new-password"
            value={form.repeat}
            onChange={(e) => setForm({ ...form, repeat: e.target.value })}
          />
        </Field>
        <div className="sm:col-span-3 flex flex-wrap items-center gap-3">
          <button type="submit" className="btn-primary" disabled={busy || !ready}>
            {busy ? <Spinner /> : <KeyRound size={15} />} Сменить пароль
          </button>
          {error && <span className="text-sm text-rose-400">{error}</span>}
          <span className="text-xs text-slate-600">
            Новый хеш ложится в <span className="mono">data/config/auth.json</span> и
            переживает пересборку образа.
          </span>
        </div>
      </form>
    </Card>
  )
}

function LogView({ text }) {
  const [pinned, setPinned] = useState(true)
  const [node, setNode] = useState(null)

  useEffect(() => {
    if (pinned && node) node.scrollTop = node.scrollHeight
  }, [text, pinned, node])

  if (!text) {
    return (
      <div className="flex items-center justify-center gap-2 py-12 text-sm text-slate-600">
        <ScrollText size={16} /> Логи пусты
      </div>
    )
  }

  return (
    <div
      ref={setNode}
      onScroll={(e) => {
        const el = e.currentTarget
        setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < 40)
      }}
      className="max-h-[28rem] overflow-auto px-5 py-4 sm:px-6"
    >
      <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-slate-400">
        {text}
      </pre>
    </div>
  )
}
