import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  CheckCircle2,
  Download,
  Gauge,
  Pencil,
  Plus,
  ServerCog,
  Trash2,
  Zap,
} from 'lucide-react'

import { api } from '../lib/api'
import { useConfigDraft, usePoll } from '../lib/hooks'
import {
  Card,
  Chip,
  Empty,
  Field,
  Input,
  Modal,
  Spinner,
  TagList,
  useToast,
} from '../components/ui'

const EMPTY_SERVER = { name: '', url: '' }

export default function Vpn({ refreshStatus }) {
  const toast = useToast()
  const { data, refresh } = usePoll(api.servers, 15000)
  const [editing, setEditing] = useState(null)
  const [importing, setImporting] = useState(false)
  const [latency, setLatency] = useState({})
  const [busyId, setBusyId] = useState(null)

  const servers = data?.servers ?? []
  const activeId = data?.active_server

  // Пока замер идёт, опрашиваем чаще: он длится по ~10 секунд на сервер.
  const [job, setJob] = useState(null)
  useEffect(() => {
    let alive = true
    const tick = async () => {
      if (!alive) return
      try {
        const next = await api.speedtestStatus()
        if (!alive) return
        setJob(next)
        if (!next.running && job?.running) refresh()
      } catch {
        /* панель переживёт */
      }
      if (alive) setTimeout(tick, job?.running ? 1500 : 8000)
    }
    tick()
    return () => {
      alive = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.running])

  const measureSpeed = async (ids) => {
    try {
      setJob(await api.speedtestStart(ids))
      toast(
        ids.length > 1
          ? `Замер запущен: ${ids.length} серверов, примерно ${ids.length * 12} с`
          : 'Замер запущен, около 12 секунд',
        'ok',
      )
    } catch (err) {
      toast(err.message, 'bad', 9000)
    }
  }

  const activate = async (id) => {
    setBusyId(id)
    try {
      await api.activateServer(id)
      await api.apply()
      toast('Сервер активирован и применён', 'ok')
      await Promise.all([refresh(), refreshStatus?.()])
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setBusyId(null)
    }
  }

  const remove = async (server) => {
    if (!window.confirm(`Удалить «${server.name}»?`)) return
    try {
      await api.deleteServer(server.id)
      toast('Сервер удалён', 'ok')
      await refresh()
    } catch (err) {
      toast(err.message, 'bad')
    }
  }

  const measure = async (id) => {
    setLatency((prev) => ({ ...prev, [id]: { pending: true } }))
    try {
      const result = await api.testServer(id)
      setLatency((prev) => ({ ...prev, [id]: result }))
    } catch (err) {
      setLatency((prev) => ({ ...prev, [id]: { ok: false, error: err.message } }))
    }
  }

  return (
    <div className="space-y-5">
      <Card
        title="Серверы"
        subtitle="активный сервер используется для всего проксируемого трафика"
        actions={
          <>
            <button
              className="btn-ghost"
              onClick={() => measureSpeed(servers.map((s) => s.id))}
              disabled={job?.running || servers.length === 0}
            >
              {job?.running ? <Spinner /> : <Zap size={15} />} Замерить все
            </button>
            <button className="btn-ghost" onClick={() => setImporting(true)}>
              <Download size={15} /> Из подписки
            </button>
            <button className="btn-primary" onClick={() => setEditing(EMPTY_SERVER)}>
              <Plus size={15} /> Добавить
            </button>
          </>
        }
        bodyClass="p-0"
      >
        {servers.length === 0 ? (
          <Empty
            icon={ServerCog}
            title="Серверов пока нет"
            hint="Добавьте ссылку vless:// / vmess:// / trojan:// / ss:// или импортируйте подписку."
          />
        ) : (
          <ul className="divide-y divide-white/[0.06]">
            {servers.map((server) => (
              <ServerRow
                key={server.id}
                server={server}
                active={server.id === activeId}
                busy={busyId === server.id}
                latency={latency[server.id]}
                speed={job?.results?.[server.id] ?? server.last_test}
                testing={job?.running && (job.current === server.id ||
                  job.queue?.includes(server.id))}
                current={job?.current === server.id}
                onActivate={() => activate(server.id)}
                onEdit={() => setEditing(server)}
                onDelete={() => remove(server)}
                onMeasure={() => measure(server.id)}
                onSpeedTest={() => measureSpeed([server.id])}
              />
            ))}
          </ul>
        )}
      </Card>

      <RoutingRules />

      <ServerModal
        server={editing}
        onClose={() => setEditing(null)}
        onSaved={async () => {
          setEditing(null)
          await refresh()
        }}
      />
      <ImportModal
        open={importing}
        onClose={() => setImporting(false)}
        onDone={async () => {
          setImporting(false)
          await refresh()
        }}
      />
    </div>
  )
}

function ServerRow({
  server,
  active,
  busy,
  latency,
  speed,
  testing,
  current,
  onActivate,
  onEdit,
  onDelete,
  onMeasure,
  onSpeedTest,
}) {
  const [open, setOpen] = useState(false)
  const summary = server.summary ?? {}

  return (
    <li className={`px-5 py-4 transition sm:px-6 ${active ? 'bg-accent/[0.05]' : ''}`}>
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={onActivate}
          disabled={busy || active}
          title={active ? 'Уже активен' : 'Сделать активным'}
          className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition ${
            active
              ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
              : 'border-white/10 text-slate-600 hover:border-accent/40 hover:text-accent'
          }`}
        >
          {busy ? <Spinner size={14} /> : <CheckCircle2 size={16} />}
        </button>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-slate-100">{server.name}</span>
            {!server.valid && <Chip tone="bad">ошибка разбора</Chip>}
            {server.outbound && !server.url && <Chip tone="info">Xray JSON</Chip>}
          </div>
          <div className="mono mt-0.5 truncate text-xs text-slate-500">
            {summary.address
              ? `${summary.protocol} · ${summary.address}:${summary.port} · ${summary.security}`
              : server.error ?? server.url}
          </div>
        </div>

        <div className="flex items-center gap-1.5">
          <SpeedChip speed={speed} testing={testing} current={current} />
          {latency?.pending ? (
            <Spinner size={14} className="text-slate-500" />
          ) : latency ? (
            <Chip tone={latency.ok ? 'ok' : 'bad'}>
              {latency.ok ? `${latency.latency_ms} мс` : 'нет связи'}
            </Chip>
          ) : null}
          <IconButton title="Замерить скорость через сервер" onClick={onSpeedTest}>
            <Zap size={15} />
          </IconButton>
          <IconButton title="Проверить задержку до хоста" onClick={onMeasure}>
            <Gauge size={15} />
          </IconButton>
          <IconButton title="Изменить" onClick={onEdit}>
            <Pencil size={15} />
          </IconButton>
          <IconButton title="Удалить" onClick={onDelete} danger>
            <Trash2 size={15} />
          </IconButton>
        </div>
      </div>

      <button
        onClick={() => setOpen((v) => !v)}
        className="mt-2 text-xs text-slate-600 transition hover:text-slate-400"
      >
        {open ? 'Скрыть параметры' : 'Параметры'}
      </button>
      {open && (
        <>
          <dl className="mt-2 grid gap-x-6 gap-y-1 text-xs sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(summary)
              .filter(([, value]) => value !== undefined && value !== null && value !== '')
              .map(([key, value]) => (
                <div key={key} className="flex gap-2">
                  <dt className="text-slate-600">{key}</dt>
                  <dd className="mono min-w-0 truncate text-slate-300">{String(value)}</dd>
                </div>
              ))}
          </dl>
          {speed?.ok && (
            <dl className="mt-2 grid gap-x-6 gap-y-1 border-t border-white/[0.06] pt-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
              <Detail label="скорость" value={`${speed.mbps} Мбит/с`} />
              <Detail label="отклик" value={`${speed.latency_ms} мс`} />
              <Detail label="выходной IP" value={speed.exit_ip} />
              <Detail label="скачано" value={`${(speed.bytes / 1048576).toFixed(1)} МБ за ${speed.seconds} с`} />
            </dl>
          )}
        </>
      )}
    </li>
  )
}

function Detail({ label, value }) {
  if (!value) return null
  return (
    <div className="flex gap-2">
      <dt className="text-slate-600">{label}</dt>
      <dd className="mono min-w-0 truncate text-slate-300">{value}</dd>
    </div>
  )
}

function SpeedChip({ speed, testing, current }) {
  if (testing) {
    return (
      <Chip tone="info">
        <Spinner size={11} />
        {current ? 'замер…' : 'в очереди'}
      </Chip>
    )
  }
  if (!speed) return null
  if (!speed.ok) {
    return <Chip tone="bad" className="max-w-[14rem] truncate">{speed.error ?? 'не вышло'}</Chip>
  }
  // Ниже 5 Мбит/с смотреть видео уже больно — подсвечиваем жёлтым.
  const tone = speed.mbps >= 25 ? 'ok' : speed.mbps >= 5 ? 'warn' : 'bad'
  return (
    <Chip tone={tone} className="gap-2">
      <span className="mono">{speed.mbps} Мбит/с</span>
      {speed.location && <span className="opacity-70">{speed.location}</span>}
    </Chip>
  )
}

function IconButton({ children, title, onClick, danger }) {
  return (
    <button
      title={title}
      onClick={onClick}
      className={`flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.07] text-slate-500 transition hover:bg-white/[0.05] ${
        danger ? 'hover:border-rose-500/40 hover:text-rose-300' : 'hover:text-slate-100'
      }`}
    >
      {children}
    </button>
  )
}

function ServerModal({ server, onClose, onSaved }) {
  const toast = useToast()
  const [form, setForm] = useState(EMPTY_SERVER)
  const [preview, setPreview] = useState(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (server) setForm({ name: server.name ?? '', url: server.url ?? '' })
    setPreview(null)
  }, [server])

  useEffect(() => {
    if (!form.url.trim()) {
      setPreview(null)
      return undefined
    }
    const timer = setTimeout(() => {
      api
        .parseServer({ url: form.url })
        .then(setPreview)
        .catch(() => setPreview({ ok: false, error: 'не удалось разобрать' }))
    }, 350)
    return () => clearTimeout(timer)
  }, [form.url])

  const save = async () => {
    setBusy(true)
    try {
      const payload = { name: form.name || 'Без имени', url: form.url.trim() }
      if (server?.id) await api.editServer(server.id, payload)
      else await api.addServer(payload)
      toast('Сохранено. Нажмите «Применить», чтобы перечитать конфиг.', 'ok')
      await onSaved()
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={Boolean(server)}
      onClose={onClose}
      title={server?.id ? 'Изменить сервер' : 'Новый сервер'}
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Отмена
          </button>
          <button className="btn-primary" onClick={save} disabled={busy}>
            {busy && <Spinner />} Сохранить
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Название">
          <Input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            placeholder="Нидерланды · Reality"
          />
        </Field>
        <Field
          label="Ссылка"
          hint="vless:// · vmess:// · trojan:// · ss://"
          error={preview && !preview.ok ? preview.error : ''}
        >
          <textarea
            className="input min-h-[100px] font-mono text-xs"
            value={form.url}
            onChange={(e) => setForm({ ...form, url: e.target.value })}
            placeholder="vless://uuid@host:443?type=tcp&security=reality&…"
          />
        </Field>
        {preview?.ok && (
          <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3 text-xs">
            <div className="mb-1.5 text-slate-500">Разобрано:</div>
            <div className="grid gap-x-5 gap-y-1 sm:grid-cols-2">
              {Object.entries(preview.summary)
                .filter(([, v]) => v)
                .map(([key, value]) => (
                  <div key={key} className="flex gap-2">
                    <span className="text-slate-600">{key}</span>
                    <span className="mono min-w-0 truncate text-slate-300">{String(value)}</span>
                  </div>
                ))}
            </div>
          </div>
        )}
      </div>
    </Modal>
  )
}

function ImportModal({ open, onClose, onDone }) {
  const toast = useToast()
  const [url, setUrl] = useState('')
  const [userAgent, setUserAgent] = useState('v2rayNG/1.9.5')
  const [replace, setReplace] = useState(false)
  const [busy, setBusy] = useState(false)

  const run = async () => {
    setBusy(true)
    try {
      const result = await api.importServers({ url, user_agent: userAgent, replace })
      toast(`Импортировано серверов: ${result.imported}`, 'ok')
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
      title="Импорт из подписки"
      footer={
        <>
          <button className="btn-ghost" onClick={onClose}>
            Отмена
          </button>
          <button className="btn-primary" onClick={run} disabled={busy || !url.trim()}>
            {busy && <Spinner />} Загрузить
          </button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Ссылка на подписку" hint="Base64-список ссылок или Xray-JSON">
          <Input
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://panel.example.com/sub/…"
          />
        </Field>
        <Field
          label="User-Agent"
          hint="Панели отдают разный формат в зависимости от клиента"
        >
          <Input value={userAgent} onChange={(e) => setUserAgent(e.target.value)} />
        </Field>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={replace}
            onChange={(e) => setReplace(e.target.checked)}
            className="h-4 w-4 rounded border-white/20 bg-ink-850 accent-sky-400"
          />
          Заменить текущий список целиком
        </label>
      </div>
    </Modal>
  )
}

function RoutingRules() {
  const toast = useToast()
  const { draft, edit, dirty, settle } = useConfigDraft(api.getConfig)
  const { data: geo } = usePoll(api.geoCategories, 600000)
  const [busy, setBusy] = useState(false)

  const geosite = useMemo(() => (geo?.geosite ?? []).map((c) => `geosite:${c}`), [geo])
  const geoip = useMemo(() => (geo?.geoip ?? []).map((c) => `geoip:${c}`), [geo])

  const save = useCallback(async () => {
    setBusy(true)
    try {
      await api.patchConfig({ bypass: draft.bypass, force_proxy: draft.force_proxy })
      await api.apply()
      toast('Правила маршрутизации применены', 'ok')
      await settle()
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setBusy(false)
    }
  }, [draft, settle, toast])

  if (!draft) return null

  const patch = (section, key, values) =>
    edit((d) => ({ ...d, [section]: { ...d[section], [key]: values } }))

  return (
    <Card
      title="Правила маршрутизации"
      subtitle="что идёт мимо VPN, а что принудительно через него"
      actions={
        <>
          {dirty && <Chip tone="warn">не сохранено</Chip>}
          <button className="btn-primary" onClick={save} disabled={busy}>
            {busy && <Spinner />} Сохранить и применить
          </button>
        </>
      }
    >
      <datalist id="geosite-options">
        {geosite.slice(0, 2000).map((c) => (
          <option key={c} value={c} />
        ))}
      </datalist>
      <datalist id="geoip-options">
        {geoip.slice(0, 2000).map((c) => (
          <option key={c} value={c} />
        ))}
      </datalist>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="space-y-4">
          <h3 className="text-sm font-medium text-emerald-300">Мимо VPN (напрямую)</h3>
          <Field label="geosite" hint={`доступно категорий: ${geosite.length}`}>
            <TagList
              values={draft.bypass.geosite}
              onChange={(v) => patch('bypass', 'geosite', v)}
              placeholder="geosite:category-ru"
            />
          </Field>
          <Field label="geoip">
            <TagList
              values={draft.bypass.geoip}
              onChange={(v) => patch('bypass', 'geoip', v)}
              placeholder="geoip:ru"
            />
          </Field>
          <Field label="Домены">
            <TagList
              values={draft.bypass.domains}
              onChange={(v) => patch('bypass', 'domains', v)}
              placeholder="domain:example.ru"
            />
          </Field>
          <Field label="IP и подсети">
            <TagList
              values={draft.bypass.ips}
              onChange={(v) => patch('bypass', 'ips', v)}
              placeholder="93.184.0.0/16"
            />
          </Field>
        </div>

        <div className="space-y-4">
          <h3 className="text-sm font-medium text-sky-300">Принудительно через VPN</h3>
          <p className="-mt-2 text-xs text-slate-500">
            Приоритетнее списка обхода: сработает, даже если домен попал в geosite выше.
          </p>
          <Field label="geosite">
            <TagList
              values={draft.force_proxy.geosite}
              onChange={(v) => patch('force_proxy', 'geosite', v)}
              placeholder="geosite:google"
            />
          </Field>
          <Field label="Домены">
            <TagList
              values={draft.force_proxy.domains}
              onChange={(v) => patch('force_proxy', 'domains', v)}
              placeholder="domain:openai.com"
            />
          </Field>
          <Field label="IP и подсети">
            <TagList
              values={draft.force_proxy.ips}
              onChange={(v) => patch('force_proxy', 'ips', v)}
              placeholder="1.1.1.1/32"
            />
          </Field>
        </div>
      </div>
    </Card>
  )
}
