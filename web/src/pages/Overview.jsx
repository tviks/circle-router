import { useCallback, useState } from 'react'
import {
  ArrowDownToLine,
  ArrowUpFromLine,
  Clock,
  Cpu,
  Globe2,
  Server,
  ShieldCheck,
  Users,
} from 'lucide-react'

import { api } from '../lib/api'
import { usePoll } from '../lib/hooks'
import { bytes, duration, rate } from '../lib/format'
import { HistoryChart, SpeedChart } from '../components/charts'
import { ServerHealth } from '../components/system'
import { Card, Chip, Dot, Spinner, StatTile, Toggle, useToast } from '../components/ui'

const RANGES = [
  ['1h', '1 час'],
  ['6h', '6 часов'],
  ['24h', 'сутки'],
  ['7d', 'неделя'],
  ['30d', 'месяц'],
]

export default function Overview({ status, refreshStatus }) {
  const toast = useToast()
  const { data: live } = usePoll(() => api.live(150), 2000)
  const { data: clients } = usePoll(api.clients, 6000)
  const { data: system } = usePoll(api.system, 3000)
  const [range, setRange] = useState('24h')
  const { data: history } = usePoll(() => api.history(range), 30000, [range])
  const [busy, setBusy] = useState(false)

  const current = live?.current ?? {}
  const vpn = status?.vpn
  const vpnActive = Boolean(vpn?.routing_active && vpn?.xray_listening)

  const toggleVpn = useCallback(
    async (next) => {
      setBusy(true)
      try {
        await api.patchConfig({ vpn_enabled: next })
        await api.apply()
        toast(next ? 'VPN включён' : 'VPN выключен', 'ok')
        await refreshStatus()
      } catch (err) {
        toast(`Не получилось: ${err.message}`, 'bad', 9000)
      } finally {
        setBusy(false)
      }
    },
    [refreshStatus, toast],
  )

  const online = (clients ?? []).filter((c) => c.online).length
  const totals = history?.totals ?? {}

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatTile
          icon={ArrowDownToLine}
          label="Приём"
          value={rate(current.wan_rx_rate)}
          hint={`за период: ${bytes(totals.wan_rx ?? 0)}`}
          tone="info"
        />
        <StatTile
          icon={ArrowUpFromLine}
          label="Отдача"
          value={rate(current.wan_tx_rate)}
          hint={`за период: ${bytes(totals.wan_tx ?? 0)}`}
          tone="mute"
        />
        <StatTile
          icon={ShieldCheck}
          label="Через VPN"
          value={rate((current.proxy_down_rate ?? 0) + (current.proxy_up_rate ?? 0))}
          hint={`за период: ${bytes((totals.proxy_up ?? 0) + (totals.proxy_down ?? 0))}`}
          tone={vpnActive ? 'ok' : 'mute'}
        />
        <StatTile
          icon={Users}
          label="Клиенты"
          value={online}
          hint={`${(clients ?? []).length} в аренде DHCP`}
          tone="mute"
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        <Card
          className="lg:col-span-2"
          title="Скорость WAN"
          subtitle="последние 5 минут, обновление каждые 2 секунды"
        >
          <SpeedChart samples={live?.samples} height={220} />
        </Card>

        <Card title="Состояние">
          <div className="space-y-4">
            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
              <Toggle
                checked={Boolean(vpn?.enabled)}
                disabled={busy}
                onChange={toggleVpn}
                label="Заворачивать трафик в VPN"
                description={
                  vpn?.active_server
                    ? `активный сервер: ${vpn.active_server.name}`
                    : 'сервер не выбран'
                }
              />
              <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                <Chip tone={vpnActive ? 'ok' : vpn?.enabled ? 'warn' : 'mute'}>
                  <Dot tone={vpnActive ? 'ok' : vpn?.enabled ? 'warn' : 'mute'} />
                  {vpnActive ? 'TPROXY активен' : vpn?.enabled ? 'ожидает xray' : 'прямой роутинг'}
                </Chip>
                {busy && <Spinner size={13} className="text-slate-500" />}
              </div>
              {vpn?.enabled && !vpn?.effective && (
                <p className="mt-3 rounded-lg border border-amber-500/25 bg-amber-500/[0.08] px-3 py-2 text-xs text-amber-200">
                  xray не принял конфигурацию сервера, работает прямой роутинг.
                  {vpn.config_error ? ` ${vpn.config_error}` : ''}
                </p>
              )}
            </div>

            <ServiceRow
              icon={Server}
              name="xray"
              info={status?.services?.xray}
              extra={vpn?.xray_listening ? 'слушает TPROXY' : 'порт закрыт'}
            />
            <ServiceRow icon={Globe2} name="dnsmasq" info={status?.services?.dnsmasq} extra="DHCP и DNS" />

            <div className="grid grid-cols-2 gap-3 border-t border-white/[0.06] pt-4 text-xs">
              <Metric icon={Clock} label="Аптайм" value={duration(status?.host?.uptime)} />
              <Metric
                icon={Cpu}
                label="Load average"
                value={(status?.host?.loadavg ?? [0]).map((v) => v.toFixed(2)).join(' / ')}
              />
            </div>
          </div>
        </Card>
      </div>

      <ServerHealth system={system} />

      <Card
        title="История трафика"
        subtitle="сколько прошло через VPN и мимо него"
        actions={
          <div className="flex flex-wrap gap-1">
            {RANGES.map(([id, label]) => (
              <button
                key={id}
                onClick={() => setRange(id)}
                className={`rounded-lg px-2.5 py-1 text-xs transition ${
                  range === id
                    ? 'bg-accent/15 text-accent-soft'
                    : 'text-slate-500 hover:bg-white/[0.04] hover:text-slate-300'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        }
      >
        {history?.points?.length ? (
          <HistoryChart points={history.points} />
        ) : (
          <p className="py-10 text-center text-sm text-slate-600">
            Данных пока нет — статистика копится по минутам с момента запуска.
          </p>
        )}
      </Card>
    </div>
  )
}

function ServiceRow({ icon: Icon, name, info, extra }) {
  const running = info?.running
  const tone = running ? (info?.health === 'unhealthy' ? 'warn' : 'ok') : 'bad'
  return (
    <div className="flex items-center gap-3">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg border border-white/[0.07] bg-white/[0.02]">
        <Icon size={14} className="text-slate-400" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-sm text-slate-200">
          {name}
          <Dot tone={tone} />
        </div>
        <div className="truncate text-xs text-slate-500">
          {info?.error ? info.error : `${info?.status ?? '—'} · ${extra}`}
        </div>
      </div>
      {info?.restart_count > 0 && (
        <span className="text-xs text-slate-600">×{info.restart_count}</span>
      )}
    </div>
  )
}

function Metric({ icon: Icon, label, value }) {
  return (
    <div>
      <div className="flex items-center gap-1.5 text-slate-500">
        <Icon size={12} />
        {label}
      </div>
      <div className="mono mt-0.5 text-slate-200">{value}</div>
    </div>
  )
}
