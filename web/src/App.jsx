import { useCallback, useEffect, useState } from 'react'
import {
  Activity,
  Cable,
  Globe2,
  LayoutDashboard,
  LogOut,
  Menu,
  RefreshCw,
  Server,
  ShieldBan,
  ShieldCheck,
  Terminal,
  Users,
  X,
} from 'lucide-react'

import { api } from './lib/api'
import { usePoll } from './lib/hooks'
import { Dot, Spinner, useToast } from './components/ui'
import Login from './pages/Login'
import Overview from './pages/Overview'
import Vpn from './pages/Vpn'
import Nodes from './pages/Nodes'
import Network from './pages/Network'
import Clients from './pages/Clients'
import AdBlock from './pages/AdBlock'
import System from './pages/System'

const PAGES = [
  { id: 'overview', label: 'Обзор', icon: LayoutDashboard, component: Overview },
  { id: 'vpn', label: 'VPN', icon: ShieldCheck, component: Vpn },
  { id: 'nodes', label: 'Свои ноды', icon: Server, component: Nodes },
  { id: 'network', label: 'Сеть', icon: Cable, component: Network },
  { id: 'adblock', label: 'Блокировщик', icon: ShieldBan, component: AdBlock },
  { id: 'clients', label: 'Клиенты', icon: Users, component: Clients },
  { id: 'system', label: 'Система', icon: Terminal, component: System },
]

function routeFromHash() {
  const id = window.location.hash.replace('#', '')
  return PAGES.some((p) => p.id === id) ? id : 'overview'
}

export default function App() {
  const [user, setUser] = useState(undefined)
  const [page, setPage] = useState(routeFromHash)
  const [navOpen, setNavOpen] = useState(false)

  useEffect(() => {
    api
      .me()
      .then((r) => setUser(r.authenticated ? r.user : null))
      .catch(() => setUser(null))
  }, [])

  useEffect(() => {
    const onHash = () => setPage(routeFromHash())
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])

  const navigate = useCallback((id) => {
    window.location.hash = id
    setPage(id)
    setNavOpen(false)
  }, [])

  if (user === undefined) {
    return (
      <div className="flex h-full items-center justify-center text-slate-500">
        <Spinner size={24} />
      </div>
    )
  }

  if (user === null) return <Login onSuccess={setUser} />

  return <Shell page={page} navigate={navigate} navOpen={navOpen} setNavOpen={setNavOpen} onLogout={() => setUser(null)} />
}

function Shell({ page, navigate, navOpen, setNavOpen, onLogout }) {
  const toast = useToast()
  const { data: status, refresh } = usePoll(api.status, 4000)
  const [applying, setApplying] = useState(false)

  const applyChanges = useCallback(async () => {
    setApplying(true)
    try {
      const result = await api.apply()
      toast(`Применено: ${result.steps?.length ?? 0} шагов`, 'ok')
      await refresh()
    } catch (err) {
      toast(`Не удалось применить: ${err.message}`, 'bad', 9000)
    } finally {
      setApplying(false)
    }
  }, [refresh, toast])

  const logout = async () => {
    await api.logout().catch(() => {})
    onLogout()
  }

  const Current = PAGES.find((p) => p.id === page)?.component ?? Overview

  return (
    <div className="flex h-full">
      {navOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/60 backdrop-blur-sm lg:hidden"
          onClick={() => setNavOpen(false)}
        />
      )}

      <aside
        className={`fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-white/[0.06] bg-ink-900/95 backdrop-blur transition-transform lg:static lg:translate-x-0 ${
          navOpen ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-center gap-2.5 px-5 py-5">
          <span className="relative flex h-8 w-8 items-center justify-center rounded-xl border border-accent/40 bg-accent/10">
            <Globe2 size={16} className="text-accent" />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight text-slate-100">circle-router</div>
            <div className="truncate text-[11px] text-slate-500">
              {status?.lan?.address ?? '—'}
            </div>
          </div>
          <button
            className="ml-auto text-slate-500 lg:hidden"
            onClick={() => setNavOpen(false)}
          >
            <X size={18} />
          </button>
        </div>

        <nav className="flex-1 space-y-1 px-3">
          {PAGES.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              onClick={() => navigate(id)}
              className={`flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition ${
                page === id
                  ? 'bg-accent/[0.14] text-accent-soft shadow-[inset_0_0_0_1px_rgba(56,189,248,0.2)]'
                  : 'text-slate-400 hover:bg-white/[0.04] hover:text-slate-100'
              }`}
            >
              <Icon size={16} className="shrink-0" />
              {label}
            </button>
          ))}
        </nav>

        <div className="space-y-3 border-t border-white/[0.06] px-5 py-4">
          <VpnBadge status={status} />
          <button onClick={logout} className="flex items-center gap-2 text-xs text-slate-500 transition hover:text-rose-300">
            <LogOut size={14} /> Выйти
          </button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center gap-3 border-b border-white/[0.06] bg-ink-950/80 px-4 py-3 backdrop-blur sm:px-6">
          <button className="text-slate-400 lg:hidden" onClick={() => setNavOpen(true)}>
            <Menu size={20} />
          </button>
          <h1 className="text-sm font-semibold text-slate-200">
            {PAGES.find((p) => p.id === page)?.label}
          </h1>
          <div className="ml-auto flex items-center gap-2">
            <WanBadge status={status} />
            <button className="btn-primary" onClick={applyChanges} disabled={applying}>
              {applying ? <Spinner /> : <RefreshCw size={15} />}
              Применить
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6">
          <div className="mx-auto w-full max-w-6xl">
            <Current status={status} refreshStatus={refresh} />
          </div>
        </div>
      </main>
    </div>
  )
}

function VpnBadge({ status }) {
  const vpn = status?.vpn
  const active = vpn?.routing_active && vpn?.xray_listening
  // xray работает, а туннель молчит — LAN временно ушёл напрямую.
  const failover = vpn?.watchdog_state === 'failover' && vpn?.enabled && !active
  const tone = active ? 'ok' : failover ? 'bad' : vpn?.enabled ? 'warn' : 'mute'
  const text = active
    ? 'VPN активен'
    : failover
      ? 'Туннель недоступен — напрямую'
      : vpn?.enabled
        ? 'VPN включён, но не поднят'
        : 'VPN выключен'
  return (
    <div className="flex items-center gap-2 text-xs text-slate-400">
      <Dot tone={tone} pulse={active} />
      <span className="truncate">{text}</span>
    </div>
  )
}

function WanBadge({ status }) {
  const wan = status?.wan
  if (!wan) return null
  const ok = Boolean(wan.address)
  return (
    <div className="hidden items-center gap-2 rounded-xl border border-white/[0.07] bg-white/[0.02] px-3 py-2 text-xs sm:flex">
      <Activity size={13} className={ok ? 'text-emerald-400' : 'text-slate-600'} />
      <span className="text-slate-500">WAN</span>
      <span className="mono text-slate-200">{wan.address || 'нет адреса'}</span>
    </div>
  )
}
