import { useMemo, useState } from 'react'
import { Pin, Users } from 'lucide-react'

import { api } from '../lib/api'
import { usePoll } from '../lib/hooks'
import { bytes, duration, rate } from '../lib/format'
import { Card, Chip, Dot, Empty, Spinner, useToast } from '../components/ui'

export default function Clients() {
  const toast = useToast()
  const { data: clients } = usePoll(api.clients, 3000)
  // Резервации нужны, чтобы показать «статический» вместо кнопки-булавки.
  const { data: config, refresh: refreshConfig } = usePoll(api.getConfig, 30000)
  const [busy, setBusy] = useState('')
  const rows = clients ?? []

  const reservedMacs = useMemo(() => {
    const list = (config?.dhcp?.reservations ?? [])
      .map((r) => (r.mac || '').toLowerCase())
      .filter(Boolean)
    return new Set(list)
  }, [config])

  const pin = async (client) => {
    if (!client.mac) {
      toast('MAC неизвестен — устройство без аренды DHCP закрепить нельзя', 'bad')
      return
    }
    setBusy(client.ip)
    try {
      const cfg = await api.getConfig()
      const dhcp = {
        ...cfg.dhcp,
        reservations: [
          ...(cfg.dhcp?.reservations ?? []),
          { mac: client.mac.toLowerCase(), ip: client.ip, hostname: client.hostname || '' },
        ],
      }
      const result = await api.patchConfig({ dhcp })
      if (result?.ok === false) {
        toast(`Не закреплено: ${result.problems.join('; ')}`, 'bad', 12000)
        return
      }
      await api.apply()
      await refreshConfig()
      toast(`IP закреплён; вступит в силу при продлении аренды`, 'ok', 8000)
    } catch (err) {
      toast(err.message, 'bad', 9000)
    } finally {
      setBusy('')
    }
  }

  const leaseCell = (client) => {
    if (client.mac && reservedMacs.has(client.mac.toLowerCase())) {
      return <Chip tone="ok">статический</Chip>
    }
    return (
      <button
        className="btn-ghost !px-2 !py-1.5"
        title={client.mac ? 'Закрепить этот IP за устройством' : 'Нет аренды DHCP — MAC неизвестен'}
        onClick={() => pin(client)}
        disabled={busy === client.ip || !client.mac}
      >
        {busy === client.ip ? <Spinner size={13} /> : <Pin size={13} />}
      </button>
    )
  }

  return (
    <Card
      title="Клиенты LAN"
      subtitle="аренды DHCP и учёт трафика по nftables"
      bodyClass="p-0"
    >
      {rows.length === 0 ? (
        <Empty
          icon={Users}
          title="Пока никого"
          hint="Воткните кабель в LAN-порт — устройства появятся здесь после получения адреса."
        />
      ) : (
        <>
          {/* На телефоне таблица из семи колонок нечитаема — показываем карточки. */}
          <ul className="divide-y divide-white/[0.06] lg:hidden">
            {rows.map((client) => (
              <li key={client.ip} className="px-4 py-3">
                <div className="flex items-center gap-2">
                  <Dot tone={client.online ? 'ok' : 'mute'} pulse={client.online} />
                  <span className="min-w-0 flex-1 truncate text-sm text-slate-200">
                    {client.hostname || 'без имени'}
                  </span>
                  <span className="mono shrink-0 text-xs text-slate-400">{client.ip}</span>
                </div>
                <div className="mono mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
                  <span className="text-sky-300">↓ {rate(client.down_rate)}</span>
                  <span className="text-violet-300">↑ {rate(client.up_rate)}</span>
                  <span className="text-slate-500">
                    за сутки {bytes((client.down_day ?? 0) + (client.up_day ?? 0))}
                  </span>
                </div>
                <div className="mono mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-slate-600">
                  <span>{client.mac || '—'}</span>
                  {client.expires_in ? <span>аренда {duration(client.expires_in)}</span> : null}
                  {leaseCell(client)}
                </div>
              </li>
            ))}
          </ul>

          <div className="hidden overflow-x-auto lg:block">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/[0.06] text-left text-xs uppercase tracking-wider text-slate-500">
                  <th className="px-5 py-3 font-medium sm:px-6">Устройство</th>
                  <th className="px-4 py-3 font-medium">IP</th>
                  <th className="px-4 py-3 font-medium">MAC</th>
                  <th className="px-4 py-3 text-right font-medium">Приём</th>
                  <th className="px-4 py-3 text-right font-medium">Отдача</th>
                  <th className="px-4 py-3 text-right font-medium">За сутки</th>
                  <th className="px-4 py-3 text-right font-medium sm:px-6">Аренда</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-white/[0.04]">
                {rows.map((client) => (
                  <tr key={client.ip} className="transition hover:bg-white/[0.02]">
                    <td className="px-5 py-3 sm:px-6">
                      <div className="flex items-center gap-2">
                        <Dot tone={client.online ? 'ok' : 'mute'} pulse={client.online} />
                        <span className="truncate text-slate-200">
                          {client.hostname || 'без имени'}
                        </span>
                      </div>
                    </td>
                    <td className="mono px-4 py-3 text-slate-300">{client.ip}</td>
                    <td className="mono px-4 py-3 text-xs text-slate-500">{client.mac || '—'}</td>
                    <td className="mono px-4 py-3 text-right text-sky-300">
                      {rate(client.down_rate)}
                    </td>
                    <td className="mono px-4 py-3 text-right text-violet-300">
                      {rate(client.up_rate)}
                    </td>
                    <td className="mono px-4 py-3 text-right text-slate-400">
                      {bytes((client.down_day ?? 0) + (client.up_day ?? 0))}
                    </td>
                    <td className="mono px-4 py-3 text-right text-xs text-slate-500 sm:px-6">
                      <div className="flex items-center justify-end gap-2">
                        {client.expires_in ? duration(client.expires_in) : '—'}
                        {leaseCell(client)}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  )
}
