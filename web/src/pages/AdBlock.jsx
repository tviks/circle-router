import { useEffect, useState } from 'react'
import {
  Ban,
  Check,
  CircleSlash,
  Download,
  Plus,
  RefreshCw,
  ShieldBan,
  Trash2,
  X,
} from 'lucide-react'

import { api } from '../lib/api'
import { useConfigDraft, usePoll } from '../lib/hooks'
import { bytes, dateLabel } from '../lib/format'
import {
  Card,
  Chip,
  Empty,
  Field,
  Input,
  Spinner,
  StatTile,
  TagList,
  Toggle,
  useToast,
} from '../components/ui'

export default function AdBlock() {
  const toast = useToast()
  const { data, refresh } = usePoll(api.adblock, 5000)
  const { draft, edit, dirty, settle } = useConfigDraft(
    async () => (await api.getConfig()).adblock,
  )
  const [busy, setBusy] = useState('')

  if (!draft) {
    return (
      <div className="flex justify-center py-16 text-slate-600">
        <Spinner size={22} />
      </div>
    )
  }

  const status = data?.status ?? {}
  const dns = data?.dns ?? {}

  const save = async () => {
    setBusy('save')
    try {
      await api.patchConfig({ adblock: draft })
      await api.apply()
      toast('Настройки блокировщика применены', 'ok')
      await Promise.all([settle(), refresh()])
    } catch (err) {
      toast(err.message, 'bad', 10000)
    } finally {
      setBusy('')
    }
  }

  const update = async () => {
    setBusy('update')
    try {
      const result = await api.adblockUpdate()
      toast(`Списки обновлены: ${result.domains} доменов`, 'ok')
      await refresh()
    } catch (err) {
      toast(err.message, 'bad', 12000)
    } finally {
      setBusy('')
    }
  }

  const resetStats = async () => {
    await api.adblockResetStats().catch(() => {})
    toast('Счётчики сброшены', 'ok')
    await refresh()
  }

  const setSource = (index, patch) =>
    edit((d) => ({
      ...d,
      sources: d.sources.map((s, i) => (i === index ? { ...s, ...patch } : s)),
    }))

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          icon={ShieldBan}
          label="Доменов в списке"
          value={(status.domains ?? 0).toLocaleString('ru-RU')}
          hint={status.last_update ? `обновлено ${dateLabel(status.last_update)}` : 'ещё не обновлялись'}
          tone={draft.enabled ? 'ok' : 'mute'}
        />
        <StatTile
          icon={Ban}
          label="Заблокировано"
          value={(dns.blocked ?? 0).toLocaleString('ru-RU')}
          hint={`${dns.blocked_share ?? 0}% всех запросов`}
          tone="bad"
        />
        <StatTile
          icon={CircleSlash}
          label="DNS-запросов"
          value={(dns.queries ?? 0).toLocaleString('ru-RU')}
          hint={dns.since ? `с ${dateLabel(dns.since)}` : ''}
          tone="info"
        />
        <StatTile
          icon={Download}
          label="Размер файла"
          value={bytes(status.file_size ?? 0)}
          hint={dns.connected ? 'счётчики читаются' : 'лог dnsmasq не читается'}
          tone="mute"
        />
      </div>

      <Card
        title="Блокировщик рекламы"
        subtitle="фильтрация на уровне DNS — работает для всех устройств в LAN сразу"
        actions={
          <>
            <button className="btn-ghost" onClick={update} disabled={busy === 'update'}>
              {busy === 'update' ? <Spinner /> : <RefreshCw size={15} />} Обновить списки
            </button>
            {dirty && <Chip tone="warn">не сохранено</Chip>}
            <button className="btn-primary" onClick={save} disabled={busy === 'save'}>
              {busy === 'save' && <Spinner />} Сохранить
            </button>
          </>
        }
      >
        <Toggle
          checked={draft.enabled}
          onChange={(v) => edit({ ...draft, enabled: v })}
          label="Блокировать рекламу и трекеры"
          description="запрещённые домены отвечают 0.0.0.0 — соединение не устанавливается вовсе"
        />

        <div className="mt-5 grid gap-4 border-t border-white/[0.06] pt-5 sm:grid-cols-2">
          <Field label="Автообновление, часов" hint="0 — только вручную">
            <Input
              type="number"
              min={0}
              max={720}
              value={draft.auto_update_hours}
              onChange={(e) => edit({ ...draft, auto_update_hours: Number(e.target.value) })}
            />
          </Field>
          <div className="flex items-end pb-2">
            <Toggle
              checked={draft.count_queries}
              onChange={(v) => edit({ ...draft, count_queries: v })}
              label="Считать запросы"
              description="включает log-queries в dnsmasq"
            />
          </div>
        </div>
      </Card>

      <Card
        title="Источники списков"
        actions={
          <button
            className="btn-ghost"
            onClick={() =>
              edit({ ...draft, sources: [...draft.sources, { name: '', url: '', enabled: true }] })
            }
          >
            <Plus size={15} /> Добавить
          </button>
        }
        bodyClass="p-0"
      >
        <ul className="divide-y divide-white/[0.06]">
          {draft.sources.map((source, index) => {
            const info = (status.sources ?? []).find((s) => s.url === source.url)
            return (
              <li key={index} className="px-5 py-4 sm:px-6">
                <div className="flex flex-wrap items-center gap-3">
                  <button
                    onClick={() => setSource(index, { enabled: !source.enabled })}
                    className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition ${
                      source.enabled
                        ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                        : 'border-white/10 text-slate-600 hover:border-white/25'
                    }`}
                    title={source.enabled ? 'Выключить' : 'Включить'}
                  >
                    {source.enabled ? <Check size={16} /> : <X size={16} />}
                  </button>
                  <input
                    className="input flex-1 sm:max-w-xs"
                    value={source.name}
                    placeholder="Название"
                    onChange={(e) => setSource(index, { name: e.target.value })}
                  />
                  {info && (
                    <Chip tone={info.ok ? 'ok' : 'bad'}>
                      {info.ok ? `${info.count.toLocaleString('ru-RU')} доменов` : 'ошибка'}
                    </Chip>
                  )}
                  <button
                    className="btn-danger ml-auto !px-3 !py-2"
                    onClick={() =>
                      edit({ ...draft, sources: draft.sources.filter((_, i) => i !== index) })
                    }
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
                <input
                  className="input mt-2 font-mono text-xs"
                  value={source.url}
                  placeholder="https://…"
                  onChange={(e) => setSource(index, { url: e.target.value })}
                />
                {info?.error && <p className="mt-1 text-xs text-rose-400">{info.error}</p>}
              </li>
            )
          })}
        </ul>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Свои правила">
          <div className="space-y-5">
            <Field label="Блокировать" hint="домен и все его поддомены">
              <TagList
                values={draft.blocklist}
                onChange={(v) => edit({ ...draft, blocklist: v })}
                placeholder="ads.example.com"
              />
            </Field>
            <Field label="Разрешить" hint="приоритетнее любого списка">
              <TagList
                values={draft.allowlist}
                onChange={(v) => edit({ ...draft, allowlist: v })}
                placeholder="analytics.мой-сайт.рф"
              />
            </Field>
          </div>
        </Card>

        <Card
          title="Чаще всего блокируется"
          actions={
            <button className="btn-ghost !py-1.5 !text-xs" onClick={resetStats}>
              Сбросить
            </button>
          }
          bodyClass={dns.top_blocked?.length ? 'card-pad' : 'p-0'}
        >
          {dns.top_blocked?.length ? (
            <ul className="space-y-1.5">
              {dns.top_blocked.map((item) => (
                <li key={item.domain} className="flex items-center gap-3 text-sm">
                  <span className="mono min-w-0 flex-1 truncate text-slate-300">
                    {item.domain}
                  </span>
                  <span className="mono shrink-0 text-slate-500">{item.count}</span>
                </li>
              ))}
            </ul>
          ) : (
            <Empty
              icon={Ban}
              title="Пока ничего не заблокировано"
              hint="Счётчики наполняются по мере DNS-запросов от клиентов LAN."
            />
          )}
        </Card>
      </div>
    </div>
  )
}
