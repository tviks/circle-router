import { useEffect, useState } from 'react'
import { Cable, Check, Plus, RotateCw, Save, Trash2, X } from 'lucide-react'

import { api } from '../lib/api'
import { useConfigDraft, usePoll } from '../lib/hooks'
import {
  Card,
  Chip,
  Field,
  Input,
  Select,
  Spinner,
  TagList,
  Toggle,
  useToast,
} from '../components/ui'

const WAN_MODES = [
  ['dhcp', 'DHCP — адрес выдаёт вышестоящий роутер'],
  ['pppoe', 'PPPoE — логин и пароль провайдера'],
  ['static', 'Статический адрес'],
]

export default function Network({ status, refreshStatus }) {
  const toast = useToast()
  const { draft, edit, dirty, settle } = useConfigDraft(api.getConfig)
  const { data: interfaces } = usePoll(api.interfaces, 15000)
  const [busy, setBusy] = useState(false)

  if (!draft) {
    return (
      <div className="flex justify-center py-16 text-slate-600">
        <Spinner size={22} />
      </div>
    )
  }

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
      const result = await api.patchConfig({
        wan: draft.wan,
        lan: draft.lan,
        dhcp: draft.dhcp,
        dns: draft.dns,
        firewall: draft.firewall,
        uplink_access: draft.uplink_access,
        port_forward: draft.port_forward,
      })
      if (result.problems?.length) {
        toast(result.problems.join('; '), 'bad', 12000)
        return
      }
      await api.apply()
      toast('Настройки сети применены', 'ok')
      await Promise.all([settle(), refreshStatus?.()])
    } catch (err) {
      toast(err.message, 'bad', 12000)
    } finally {
      setBusy(false)
    }
  }

  const ifaceOptions = (interfaces ?? []).map((i) => i.name)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="min-w-0 flex-1 basis-64 text-xs text-slate-500">
          Изменения вступают в силу после сохранения — контроллер перечитает конфиги и
          перезапустит нужные сервисы.
        </p>
        <div className="flex flex-wrap items-center gap-2">
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

      <Card title="Интерфейсы" subtitle="что видит система прямо сейчас" bodyClass="p-0">
        <ul className="divide-y divide-white/[0.06]">
          {(interfaces ?? []).map((iface) => (
            <li key={iface.name} className="flex flex-wrap items-center gap-3 px-5 py-3 sm:px-6">
              <Cable size={15} className={iface.carrier ? 'text-emerald-400' : 'text-slate-700'} />
              <span className="mono w-28 text-slate-200">{iface.name}</span>
              <Chip tone={iface.carrier ? 'ok' : 'mute'}>
                {iface.carrier ? 'линк есть' : 'нет кабеля'}
              </Chip>
              <span className="mono text-xs text-slate-500">
                {iface.addresses.join(', ') || 'без адреса'}
              </span>
              <span className="mono ml-auto text-xs text-slate-700">{iface.mac}</span>
              {iface.name === draft.wan.interface && <Chip tone="info">WAN</Chip>}
              {iface.name === draft.lan.interface && <Chip tone="ok">LAN</Chip>}
            </li>
          ))}
        </ul>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="WAN — вход от провайдера">
          <div className="space-y-4">
            <Field label="Физический порт">
              <Select
                value={draft.wan.interface}
                onChange={(e) => set('wan.interface', e.target.value)}
              >
                {ifaceOptions.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Тип подключения">
              <Select value={draft.wan.mode} onChange={(e) => set('wan.mode', e.target.value)}>
                {WAN_MODES.map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </Select>
            </Field>

            {draft.wan.mode === 'pppoe' && (
              <div className="space-y-4 rounded-xl border border-white/[0.06] bg-white/[0.02] p-4">
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Логин">
                    <Input
                      value={draft.wan.pppoe.username}
                      onChange={(e) => set('wan.pppoe.username', e.target.value)}
                      autoComplete="off"
                    />
                  </Field>
                  <Field label="Пароль" hint="оставьте маску, чтобы не менять">
                    <Input
                      type="password"
                      value={draft.wan.pppoe.password}
                      onChange={(e) => set('wan.pppoe.password', e.target.value)}
                      autoComplete="new-password"
                    />
                  </Field>
                </div>
                <div className="grid gap-4 sm:grid-cols-3">
                  <Field label="Service name" hint="обычно пусто">
                    <Input
                      value={draft.wan.pppoe.service}
                      onChange={(e) => set('wan.pppoe.service', e.target.value)}
                    />
                  </Field>
                  <Field label="MTU">
                    <Input
                      type="number"
                      value={draft.wan.pppoe.mtu}
                      onChange={(e) => set('wan.pppoe.mtu', Number(e.target.value))}
                    />
                  </Field>
                  <Field label="LCP echo, с">
                    <Input
                      type="number"
                      value={draft.wan.pppoe.lcp_echo_interval}
                      onChange={(e) =>
                        set('wan.pppoe.lcp_echo_interval', Number(e.target.value))
                      }
                    />
                  </Field>
                </div>
                {status?.pppoe && (
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Chip tone={status.pppoe.running ? 'ok' : 'bad'}>
                      {status.pppoe.running ? 'сессия поднята' : 'сессии нет'}
                    </Chip>
                    {status.pppoe.address && (
                      <span className="mono text-slate-400">{status.pppoe.address}</span>
                    )}
                    {status.pppoe.last_error && (
                      <span className="text-rose-400">{status.pppoe.last_error}</span>
                    )}
                    <button
                      className="btn-ghost ml-auto !py-1.5 !text-xs"
                      onClick={() =>
                        api
                          .restartPppoe()
                          .then(() => toast('PPPoE перезапущен', 'ok'))
                          .catch((e) => toast(e.message, 'bad'))
                      }
                    >
                      <RotateCw size={13} /> Переподключить
                    </button>
                  </div>
                )}
              </div>
            )}

            {draft.wan.mode === 'static' && (
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Адрес с маской" hint="например 10.0.0.2/24">
                  <Input
                    value={draft.wan.static.address}
                    onChange={(e) => set('wan.static.address', e.target.value)}
                  />
                </Field>
                <Field label="Шлюз">
                  <Input
                    value={draft.wan.static.gateway}
                    onChange={(e) => set('wan.static.gateway', e.target.value)}
                  />
                </Field>
              </div>
            )}

            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3 text-xs">
              <div className="flex justify-between">
                <span className="text-slate-500">Текущий адрес</span>
                <span className="mono text-slate-200">{status?.wan?.address || '—'}</span>
              </div>
              <div className="mt-1 flex justify-between">
                <span className="text-slate-500">Шлюз</span>
                <span className="mono text-slate-200">{status?.wan?.gateway || '—'}</span>
              </div>
            </div>
          </div>
        </Card>

        <Card title="LAN — раздача клиентам">
          <div className="space-y-4">
            <Field label="Порт">
              <Select
                value={draft.lan.interface}
                onChange={(e) => set('lan.interface', e.target.value)}
              >
                {ifaceOptions.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </Select>
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Адрес роутера">
                <Input
                  value={draft.lan.address}
                  onChange={(e) => set('lan.address', e.target.value)}
                />
              </Field>
              <Field label="Префикс">
                <Input
                  type="number"
                  min={8}
                  max={30}
                  value={draft.lan.prefix}
                  onChange={(e) => set('lan.prefix', Number(e.target.value))}
                />
              </Field>
            </div>

            <div className="border-t border-white/[0.06] pt-4">
              <Toggle
                checked={draft.dhcp.enabled}
                onChange={(v) => set('dhcp.enabled', v)}
                label="DHCP-сервер"
                description="раздаёт адреса, шлюз и DNS клиентам LAN"
              />
            </div>

            {draft.dhcp.enabled && (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Начало пула">
                    <Input
                      value={draft.dhcp.range_start}
                      onChange={(e) => set('dhcp.range_start', e.target.value)}
                    />
                  </Field>
                  <Field label="Конец пула">
                    <Input
                      value={draft.dhcp.range_end}
                      onChange={(e) => set('dhcp.range_end', e.target.value)}
                    />
                  </Field>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="Время аренды" hint="12h, 30m, infinite">
                    <Input
                      value={draft.dhcp.lease_time}
                      onChange={(e) => set('dhcp.lease_time', e.target.value)}
                    />
                  </Field>
                  <Field label="Домен сети">
                    <Input
                      value={draft.dhcp.domain}
                      onChange={(e) => set('dhcp.domain', e.target.value)}
                    />
                  </Field>
                </div>
                <Field
                  label="DNS для клиентов"
                  hint="пусто — раздаётся адрес роутера"
                >
                  <TagList
                    values={draft.dhcp.dns_servers}
                    onChange={(v) => set('dhcp.dns_servers', v)}
                    placeholder="192.168.10.1"
                  />
                </Field>
              </>
            )}
          </div>
        </Card>
      </div>

      {draft.dhcp.enabled && (
        <Reservations
          items={draft.dhcp.reservations}
          onChange={(v) => set('dhcp.reservations', v)}
        />
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="DNS">
          <div className="space-y-4">
            <Toggle
              checked={draft.dns.route_via_vpn}
              onChange={(v) => set('dns.route_via_vpn', v)}
              label="Резолвить через xray"
              description="запросы идут по тем же правилам, что и трафик, и не утекают мимо VPN"
            />
            <Field label="Upstream-серверы">
              <TagList
                values={draft.dns.upstream}
                onChange={(v) => set('dns.upstream', v)}
                placeholder="1.1.1.1"
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Размер кеша">
                <Input
                  type="number"
                  value={draft.dns.cache_size}
                  onChange={(e) => set('dns.cache_size', Number(e.target.value))}
                />
              </Field>
              <div className="flex items-end pb-2">
                <Toggle
                  checked={draft.dns.log_queries}
                  onChange={(v) => set('dns.log_queries', v)}
                  label="Логировать запросы"
                />
              </div>
            </div>
          </div>
        </Card>

        <Card title="Фаервол" subtitle="доступ к роутеру со стороны WAN">
          <div className="space-y-4">
            <Toggle
              checked={draft.firewall.allow_wan_ssh}
              onChange={(v) => set('firewall.allow_wan_ssh', v)}
              label="SSH (22) с WAN"
              description="выключайте только когда есть доступ со стороны LAN"
            />
            <Toggle
              checked={draft.firewall.allow_wan_admin}
              onChange={(v) => set('firewall.allow_wan_admin', v)}
              label={`Панель (${draft.firewall.admin_port}) с WAN`}
              description="после настройки лучше оставить панель только для LAN"
            />
            <Field label="Дополнительные TCP-порты с WAN">
              <TagList
                values={(draft.firewall.extra_wan_tcp_ports ?? []).map(String)}
                onChange={(v) =>
                  set('firewall.extra_wan_tcp_ports', v.map(Number).filter(Boolean))
                }
                placeholder="51820"
              />
            </Field>
          </div>
        </Card>
      </div>

      <PortForward
        section={draft.port_forward}
        onChange={(v) => set('port_forward', v)}
        lanNetwork={status?.lan?.network}
        wanAddress={status?.wan?.address}
      />

      <Card
        title="Доступ к сети аплинка"
        subtitle="сеть, в которую воткнут WAN-порт: NAS, принтер, камеры домашнего роутера"
      >
        <div className="grid gap-6 lg:grid-cols-2">
          <div className="space-y-4">
            <Toggle
              checked={draft.uplink_access.lan_to_uplink}
              onChange={(v) => set('uplink_access.lan_to_uplink', v)}
              label="Клиенты LAN видят сеть аплинка"
              description="работает через NAT, маршруты настраивать не нужно"
            />
            <Toggle
              checked={draft.uplink_access.uplink_to_lan}
              onChange={(v) => set('uplink_access.uplink_to_lan', v)}
              label="Сеть аплинка видит LAN"
              description="хосты за WAN-портом смогут сами открывать соединения к клиентам"
            />
            <Toggle
              checked={draft.uplink_access.no_nat_to_uplink}
              onChange={(v) => set('uplink_access.no_nat_to_uplink', v)}
              label="Без NAT в сторону аплинка"
              description="хосты аплинка увидят настоящие адреса клиентов LAN"
            />
          </div>

          <div className="space-y-4">
            <Field
              label="Подсети аплинка"
              hint="пусто — определяются автоматически по адресу WAN"
            >
              <TagList
                values={draft.uplink_access.networks}
                onChange={(v) => set('uplink_access.networks', v)}
                placeholder="192.168.31.0/24"
              />
            </Field>

            <div className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3 text-xs">
              <div className="flex justify-between gap-3">
                <span className="text-slate-500">Определено сейчас</span>
                <span className="mono text-right text-slate-200">
                  {(status?.uplink?.networks ?? []).join(', ') || '—'}
                </span>
              </div>
            </div>

            {(draft.uplink_access.uplink_to_lan || draft.uplink_access.no_nat_to_uplink) && (
              <p className="rounded-lg border border-amber-500/25 bg-amber-500/[0.08] px-3 py-2 text-xs text-amber-200">
                Обратный трафик должен знать дорогу назад. На роутере аплинка добавьте
                статический маршрут{' '}
                <span className="mono">
                  {status?.lan?.network ?? ''} → {status?.wan?.address || 'адрес WAN'}
                </span>
                . Без него хосты аплинка не достучатся до LAN.
              </p>
            )}
          </div>
        </div>
      </Card>
    </div>
  )
}

const PROTOCOL_LABELS = [
  ['tcp', 'TCP'],
  ['udp', 'UDP'],
  ['both', 'TCP+UDP'],
]

function isPrivate(address) {
  if (!address) return false
  const [a, b] = address.split('.').map(Number)
  return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) || (a === 100 && b >= 64 && b <= 127)
}

/** Подпись поля видна только на узком экране — в таблице её заменяет шапка. */
function Cell({ label, children }) {
  return (
    <label className="mt-3 block min-w-0 lg:mt-0">
      <span className="label mb-1 lg:hidden">{label}</span>
      {children}
    </label>
  )
}

function PortForward({ section, onChange, lanNetwork, wanAddress }) {
  const { data: clients } = usePoll(api.clients, 20000)
  const rules = section?.rules ?? []

  const update = (index, patch) =>
    onChange({ ...section, rules: rules.map((r, i) => (i === index ? { ...r, ...patch } : r)) })

  const add = () =>
    onChange({
      ...section,
      rules: [...rules, {
        id: Math.random().toString(36).slice(2, 10),
        name: '',
        enabled: true,
        protocol: 'tcp',
        src_port: '',
        dest_ip: '',
        dest_port: '',
        source: '',
      }],
    })

  return (
    <Card
      title="Проброс портов"
      subtitle={`входящие соединения с WAN на устройство в ${lanNetwork ?? 'LAN'}`}
      actions={
        <button className="btn-ghost" onClick={add}>
          <Plus size={15} /> Добавить
        </button>
      }
    >
      <datalist id="lan-clients">
        {(clients ?? []).map((c) => (
          <option key={c.ip} value={c.ip}>
            {c.hostname || c.mac}
          </option>
        ))}
      </datalist>

      <Toggle
        checked={section?.enabled ?? true}
        onChange={(v) => onChange({ ...section, enabled: v })}
        label="Проброс включён"
        description="снимите галку, чтобы разом отключить все правила, не удаляя их"
      />

      {isPrivate(wanAddress) && (
        <p className="mt-4 rounded-lg border border-amber-500/25 bg-amber-500/[0.08] px-3 py-2 text-xs text-amber-200">
          WAN-адрес <span className="mono">{wanAddress}</span> — приватный: роутер стоит за
          другим роутером. Правила заработают для соединений из сети аплинка, а чтобы порт
          открылся из интернета, такой же проброс нужен и на вышестоящем роутере — на{' '}
          <span className="mono">{wanAddress}</span>.
        </p>
      )}

      {rules.length === 0 ? (
        <p className="py-6 text-center text-sm text-slate-600">
          Правил нет. Например: внешний 8443 → 192.168.10.50:443 для веб-морды NAS.
        </p>
      ) : (
        <div className="mt-5 space-y-3">
          <div className="hidden gap-3 px-1 text-xs uppercase tracking-wider text-slate-600 lg:grid lg:grid-cols-[auto_1.3fr_0.8fr_0.7fr_1.2fr_0.7fr_1fr_auto]">
            <span />
            <span>Название</span>
            <span>Протокол</span>
            <span>Внешний</span>
            <span>Куда</span>
            <span>Порт</span>
            <span>Источник</span>
            <span />
          </div>
          {rules.map((rule, index) => (
            <div
              key={rule.id ?? index}
              className="rounded-xl border border-white/[0.06] bg-white/[0.02] p-3 lg:grid lg:grid-cols-[auto_1.3fr_0.8fr_0.7fr_1.2fr_0.7fr_1fr_auto] lg:items-center lg:gap-3 lg:border-0 lg:bg-transparent lg:p-1"
            >
              {/* На телефоне переключатель, имя и удаление живут одной строкой,
                  остальное уходит в сетку с подписями. */}
              <div className="flex items-center gap-3 lg:contents">
                <button
                  onClick={() => update(index, { enabled: !rule.enabled })}
                  title={rule.enabled ? 'Выключить правило' : 'Включить правило'}
                  className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border transition ${
                    rule.enabled
                      ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-300'
                      : 'border-white/10 text-slate-600 hover:border-white/25'
                  }`}
                >
                  {rule.enabled ? <Check size={16} /> : <X size={16} />}
                </button>
                <Input
                  className="min-w-0 flex-1"
                  value={rule.name}
                  placeholder="Название"
                  onChange={(e) => update(index, { name: e.target.value })}
                />
                <button
                  className="btn-danger !px-3 lg:order-last"
                  onClick={() =>
                    onChange({ ...section, rules: rules.filter((_, i) => i !== index) })
                  }
                >
                  <Trash2 size={15} />
                </button>
              </div>

              <Cell label="Протокол">
                <Select
                  value={rule.protocol}
                  onChange={(e) => update(index, { protocol: e.target.value })}
                >
                  {PROTOCOL_LABELS.map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </Select>
              </Cell>
              <Cell label="Внешний порт">
                <Input
                  value={rule.src_port}
                  placeholder="8443"
                  inputMode="numeric"
                  onChange={(e) => update(index, { src_port: e.target.value })}
                />
              </Cell>
              <Cell label="Куда">
                <Input
                  list="lan-clients"
                  value={rule.dest_ip}
                  placeholder="192.168.10.50"
                  onChange={(e) => update(index, { dest_ip: e.target.value })}
                />
              </Cell>
              <Cell label="Порт">
                <Input
                  value={rule.dest_port}
                  placeholder="443"
                  inputMode="numeric"
                  onChange={(e) => update(index, { dest_port: e.target.value })}
                />
              </Cell>
              <Cell label="Источник">
                <Input
                  value={rule.source}
                  placeholder="любой"
                  onChange={(e) => update(index, { source: e.target.value })}
                />
              </Cell>
            </div>
          ))}
          <p className="px-1 text-xs text-slate-600">
            «Источник» ограничивает, откуда пускать: IP или подсеть вроде{' '}
            <span className="mono">192.168.31.0/24</span>. Пусто — с любого адреса.
            Порты 22 и порт панели пробросить нельзя: роутер потеряет управление.
          </p>
        </div>
      )}
    </Card>
  )
}

function Reservations({ items, onChange }) {
  const rows = items ?? []
  const update = (index, key, value) => {
    const next = rows.map((row, i) => (i === index ? { ...row, [key]: value } : row))
    onChange(next)
  }

  return (
    <Card
      title="Резервации DHCP"
      subtitle="постоянный адрес для конкретного MAC"
      actions={
        <button
          className="btn-ghost"
          onClick={() => onChange([...rows, { mac: '', ip: '', hostname: '' }])}
        >
          <Plus size={15} /> Добавить
        </button>
      }
      bodyClass={rows.length ? 'card-pad' : 'p-0'}
    >
      {rows.length === 0 ? (
        <p className="px-6 py-8 text-center text-sm text-slate-600">Резерваций нет.</p>
      ) : (
        <div className="space-y-3">
          {rows.map((row, index) => (
            <div key={index} className="grid gap-3 sm:grid-cols-[1.2fr_1fr_1fr_auto]">
              <Input
                value={row.mac}
                onChange={(e) => update(index, 'mac', e.target.value)}
                placeholder="aa:bb:cc:dd:ee:ff"
              />
              <Input
                value={row.ip}
                onChange={(e) => update(index, 'ip', e.target.value)}
                placeholder="192.168.10.50"
              />
              <Input
                value={row.hostname}
                onChange={(e) => update(index, 'hostname', e.target.value)}
                placeholder="имя (необязательно)"
              />
              <button
                className="btn-danger !px-3"
                onClick={() => onChange(rows.filter((_, i) => i !== index))}
              >
                <Trash2 size={15} />
              </button>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
