import { useState } from 'react'
import { Save, Wifi as WifiIcon } from 'lucide-react'

import { api } from '../lib/api'
import { useConfigDraft, usePoll } from '../lib/hooks'
import {
  Card,
  Chip,
  Dot,
  Empty,
  Field,
  Input,
  Select,
  Spinner,
  Toggle,
  useToast,
} from '../components/ui'

const signalTone = (dbm) => (dbm >= -60 ? 'ok' : dbm >= -70 ? 'warn' : 'bad')

function Hint({ children }) {
  return <p className="text-xs leading-relaxed text-slate-500">{children}</p>
}

export default function WifiPage({ refreshStatus }) {
  const toast = useToast()
  const { draft, edit, dirty, settle } = useConfigDraft(api.getConfig)
  const { data: st } = usePoll(api.wifi, 5000)
  const [busy, setBusy] = useState(false)

  if (!draft) {
    return (
      <div className="flex justify-center py-16 text-slate-600">
        <Spinner size={22} />
      </div>
    )
  }

  const wifi = draft.wifi ?? {}
  const set = (path, value) =>
    edit((current) => {
      const next = structuredClone(current)
      const keys = path.split('.')
      let node = next
      for (const key of keys.slice(0, -1)) node = node[key]
      node[keys.at(-1)] = value
      return next
    })

  const save = async () => {
    setBusy(true)
    try {
      const result = await api.patchConfig({ wifi: draft.wifi })
      if (result.problems?.length) {
        toast(result.problems.join('; '), 'bad', 12000)
        return
      }
      await api.apply()
      toast('Настройки WiFi применены', 'ok')
      await Promise.all([settle(), refreshStatus?.()])
    } catch (err) {
      toast(err.message, 'bad', 12000)
    } finally {
      setBusy(false)
    }
  }

  const adapterFound = Boolean(st?.interface)
  const apUp = Boolean(st?.running && st?.ap_up)
  const clients = st?.clients ?? []

  return (
    <div className="space-y-5">
      <Card
        title="Точка доступа"
        subtitle="USB WiFi-адаптер как маленькая точка доступа"
        actions={
          st && (
            <Chip tone={apUp ? 'ok' : st.enabled ? 'bad' : 'mute'}>
              {apUp ? 'работает' : st.enabled ? 'не поднялась' : 'выключена'}
            </Chip>
          )
        }
      >
        {st?.cdrom_mode && (
          <p className="mb-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
            Адаптер виден в режиме CD-ROM (0bda:1a2b) — usb_modeswitch на хосте не
            сработал. Проверьте <span className="mono">/etc/usb_modeswitch.d/0bda:1a2b</span>
            {' '}и передёрните адаптер.
          </p>
        )}
        {!adapterFound ? (
          <Empty
            icon={WifiIcon}
            title="WiFi-адаптер не обнаружен"
            hint={
              st?.cdrom_mode
                ? 'Донгл ещё не переключился из CD-ROM режима — wlan-интерфейса нет.'
                : 'Вставьте USB WiFi-донгл — драйвер rtw88/rtl8xxxu подхватит его сам.'
            }
          />
        ) : (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
              <span className="flex items-center gap-2">
                <Dot tone={apUp ? 'ok' : 'mute'} pulse={apUp} />
                <span className="mono text-slate-200">{st.interface}</span>
              </span>
              {st.channel && (
                <span className="text-slate-500">
                  канал {st.channel} · {st.frequency} МГц
                </span>
              )}
              {st.ap_supported === false && (
                <Chip tone="warn">драйвер без AP-режима</Chip>
              )}
              <span className="text-slate-500">клиентов: {clients.length}</span>
              {st.running && st.uptime > 0 && (
                <span className="text-slate-500">работает {Math.floor(st.uptime / 60)} мин</span>
              )}
              {st.restarts > 0 && <Chip tone="warn">перезапусков: {st.restarts}</Chip>}
            </div>
            {st.last_error && (
              <p className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-xs text-red-300">
                {st.last_error}
              </p>
            )}
            {st.enabled && !st.running && !st.last_error && (
              <Hint>Точка включена, но hostapd ещё не поднялся — проверьте статус через несколько секунд.</Hint>
            )}
          </div>
        )}
      </Card>

      <Card title="Клиенты" subtitle="подключённые к точке доступа" bodyClass="p-0">
        {clients.length === 0 ? (
          <p className="px-5 py-8 text-center text-sm text-slate-500 sm:px-6">
            Никого. WiFi-клиенты появятся здесь, как только подключатся.
          </p>
        ) : (
          <ul className="divide-y divide-white/[0.06]">
            {clients.map((c) => (
              <li key={c.mac} className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6">
                <WifiIcon size={15} className={c.signal != null ? 'text-sky-400' : 'text-slate-700'} />
                <span className="mono text-slate-200">{c.mac}</span>
                {c.signal != null && (
                  <Chip tone={signalTone(c.signal)}>{c.signal} dBm</Chip>
                )}
                {c.tx_bitrate != null && (
                  <span className="mono text-xs text-slate-500">{c.tx_bitrate} MBit/s</span>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Настройки" subtitle="клиенты точки — полноценные клиенты LAN: DHCP, VPN-правила и блокировщик работают как у проводных">
        <div className="space-y-4">
          <Toggle
            checked={Boolean(wifi.enabled)}
            onChange={(v) => set('wifi.enabled', v)}
            label="Точка доступа включена"
            description="hostapd поднимется на wifi-интерфейсе, интерфейс встанет в LAN-мост"
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Имя сети (SSID)">
              <Input
                value={wifi.ssid ?? ''}
                onChange={(e) => set('wifi.ssid', e.target.value)}
                placeholder="CircleRouter"
                maxLength={32}
              />
            </Field>
            <Field label="Пароль (WPA2, 8–63 символа)" hint={wifi.password ? 'пароль задан — сотрите, чтобы сменить' : ''}>
              <Input
                type="password"
                value={wifi.password ?? ''}
                onChange={(e) => set('wifi.password', e.target.value)}
                placeholder="минимум 8 символов"
              />
            </Field>
            <Field label="Канал (2.4 ГГц)" hint="авто-выбор канала у rtw88 не работает — канал фиксированный">
              <Select
                value={String(wifi.channel ?? 6)}
                onChange={(e) => set('wifi.channel', Number(e.target.value))}
              >
                {Array.from({ length: 13 }, (_, i) => (
                  <option key={i + 1} value={i + 1}>{i + 1}</option>
                ))}
              </Select>
            </Field>
            <Field label="Режим вещания">
              <Select
                value={wifi.hidden ? '1' : '0'}
                onChange={(e) => set('wifi.hidden', e.target.value === '1')}
              >
                <option value="0">Видимая сеть</option>
                <option value="1">Скрытая (не вещать SSID)</option>
              </Select>
            </Field>
          </div>
          <Hint>
            Донгл 1×1 на 2.4 ГГц: рассчитывайте на десятки мегабит, для IoT и
            телефона — самое то. Канал по умолчанию RU: 1–13 разрешены.
          </Hint>
        </div>
      </Card>

      <div className="flex items-center justify-end gap-2">
        {dirty && (
          <>
            <Chip tone="warn">несохранённые изменения</Chip>
            <button className="btn-ghost" onClick={settle} disabled={busy}>
              Отменить
            </button>
          </>
        )}
        <button className="btn-primary" onClick={save} disabled={busy}>
          {busy ? <Spinner /> : <Save size={15} />} Сохранить и применить
        </button>
      </div>
    </div>
  )
}
