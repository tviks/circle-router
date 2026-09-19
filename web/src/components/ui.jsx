import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Info, Loader2, X } from 'lucide-react'

export function Card({ title, subtitle, actions, children, className = '', bodyClass = 'card-pad' }) {
  return (
    <section className={`card animate-fade-up ${className}`}>
      {(title || actions) && (
        <header className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3 border-b border-white/[0.06] px-4 py-4 sm:px-6">
          <div className="min-w-0 flex-1 basis-48">
            {title && <h2 className="text-[15px] font-semibold text-slate-100">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p>}
          </div>
          {actions && (
            <div className="flex flex-wrap items-center gap-2 max-sm:w-full">{actions}</div>
          )}
        </header>
      )}
      <div className={bodyClass}>{children}</div>
    </section>
  )
}

const TONES = {
  ok: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-300',
  warn: 'border-amber-500/25 bg-amber-500/10 text-amber-300',
  bad: 'border-rose-500/25 bg-rose-500/10 text-rose-300',
  info: 'border-sky-500/25 bg-sky-500/10 text-sky-300',
  mute: 'border-white/10 bg-white/[0.03] text-slate-400',
}

export function Chip({ tone = 'mute', children, className = '' }) {
  return <span className={`chip ${TONES[tone] ?? TONES.mute} ${className}`}>{children}</span>
}

export function Dot({ tone = 'mute', pulse = false }) {
  const colors = {
    ok: 'bg-emerald-400',
    warn: 'bg-amber-400',
    bad: 'bg-rose-400',
    info: 'bg-sky-400',
    mute: 'bg-slate-600',
  }
  return (
    <span
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${colors[tone] ?? colors.mute} ${
        pulse ? 'animate-pulse-ring' : ''
      }`}
    />
  )
}

export function StatTile({ icon: Icon, label, value, hint, tone = 'mute' }) {
  const accents = {
    ok: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-rose-300',
    info: 'text-sky-300',
    mute: 'text-slate-300',
  }
  return (
    <div className="card card-pad animate-fade-up">
      <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wider text-slate-500">
        {Icon && <Icon size={14} className="shrink-0" />}
        <span className="truncate">{label}</span>
      </div>
      <div className={`mt-2.5 text-2xl font-semibold tabular-nums ${accents[tone] ?? accents.mute}`}>
        {value}
      </div>
      {hint && <div className="mt-1 truncate text-xs text-slate-500">{hint}</div>}
    </div>
  )
}

export function Toggle({ checked, onChange, disabled, label, description }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={!!checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className="group flex w-full items-center justify-between gap-4 text-left disabled:opacity-50"
    >
      <span className="min-w-0">
        {label && <span className="block text-sm font-medium text-slate-200">{label}</span>}
        {description && <span className="mt-0.5 block text-xs text-slate-500">{description}</span>}
      </span>
      <span
        className={`relative inline-flex h-6 w-11 shrink-0 rounded-full border transition ${
          checked
            ? 'border-accent/50 bg-accent/30'
            : 'border-white/10 bg-ink-800 group-hover:border-white/20'
        }`}
      >
        <span
          className={`absolute top-0.5 h-4.5 w-4.5 rounded-full transition-all ${
            checked ? 'left-[1.4rem] bg-accent shadow-glow' : 'left-0.5 bg-slate-500'
          }`}
          style={{ height: '1.125rem', width: '1.125rem' }}
        />
      </span>
    </button>
  )
}

export function Field({ label, hint, error, children, className = '' }) {
  return (
    <label className={`block ${className}`}>
      <span className="label">{label}</span>
      {children}
      {hint && !error && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
      {error && <span className="mt-1 block text-xs text-rose-400">{error}</span>}
    </label>
  )
}

export function Input(props) {
  return <input {...props} className={`input ${props.className ?? ''}`} />
}

export function Select({ children, ...props }) {
  return (
    <select {...props} className={`input ${props.className ?? ''}`}>
      {children}
    </select>
  )
}

export function TagList({ values, onChange, placeholder }) {
  const [draft, setDraft] = useState('')
  const items = values ?? []

  const add = () => {
    const parts = draft
      .split(/[\s,]+/)
      .map((v) => v.trim())
      .filter(Boolean)
    if (!parts.length) return
    onChange([...items, ...parts.filter((p) => !items.includes(p))])
    setDraft('')
  }

  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-1.5">
        {items.length === 0 && <span className="text-xs text-slate-600">список пуст</span>}
        {items.map((item) => (
          <span
            key={item}
            className="chip border-white/10 bg-white/[0.04] text-slate-300"
          >
            <span className="mono">{item}</span>
            <button
              type="button"
              onClick={() => onChange(items.filter((v) => v !== item))}
              className="text-slate-500 transition hover:text-rose-300"
              aria-label={`удалить ${item}`}
            >
              <X size={12} />
            </button>
          </span>
        ))}
      </div>
      <div className="flex gap-2">
        <input
          className="input"
          value={draft}
          placeholder={placeholder}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
        />
        <button type="button" className="btn-ghost shrink-0" onClick={add}>
          Добавить
        </button>
      </div>
    </div>
  )
}

export function Spinner({ size = 16, className = '' }) {
  return <Loader2 size={size} className={`animate-spin ${className}`} />
}

export function Empty({ icon: Icon, title, hint }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
      {Icon && <Icon size={28} className="text-slate-700" />}
      <p className="text-sm text-slate-400">{title}</p>
      {hint && <p className="max-w-sm text-xs text-slate-600">{hint}</p>}
    </div>
  )
}

export function Modal({ open, title, onClose, children, footer, wide = false }) {
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => e.key === 'Escape' && onClose()
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 p-3 backdrop-blur-sm sm:p-8">
      <div
        className={`card my-auto w-full animate-fade-up ${wide ? 'max-w-3xl' : 'max-w-lg'}`}
        role="dialog"
        aria-modal="true"
      >
        <header className="flex items-center justify-between border-b border-white/[0.06] px-5 py-4">
          <h3 className="text-[15px] font-semibold text-slate-100">{title}</h3>
          <button onClick={onClose} className="text-slate-500 transition hover:text-white">
            <X size={18} />
          </button>
        </header>
        <div className="card-pad">{children}</div>
        {footer && (
          <footer className="flex justify-end gap-2 border-t border-white/[0.06] px-5 py-4">
            {footer}
          </footer>
        )}
      </div>
    </div>
  )
}

// ── тосты ────────────────────────────────────────────────────────────────────

const ToastContext = createContext(() => {})
export const useToast = () => useContext(ToastContext)

const TOAST_ICONS = { ok: CheckCircle2, bad: AlertTriangle, info: Info }
const TOAST_TONES = {
  ok: 'border-emerald-500/30 bg-emerald-500/[0.12] text-emerald-200',
  bad: 'border-rose-500/30 bg-rose-500/[0.12] text-rose-200',
  info: 'border-sky-500/30 bg-sky-500/[0.12] text-sky-200',
}

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([])

  const push = useCallback((message, tone = 'ok', ttl = 5000) => {
    const id = Math.random().toString(36).slice(2)
    setToasts((list) => [...list, { id, message, tone }])
    setTimeout(() => setToasts((list) => list.filter((t) => t.id !== id)), ttl)
  }, [])

  const value = useMemo(() => push, [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2">
        {toasts.map((toast) => {
          const Icon = TOAST_ICONS[toast.tone] ?? Info
          return (
            <div
              key={toast.id}
              className={`pointer-events-auto flex animate-fade-up items-start gap-2.5 rounded-xl border px-4 py-3 text-sm shadow-card backdrop-blur ${
                TOAST_TONES[toast.tone] ?? TOAST_TONES.info
              }`}
            >
              <Icon size={16} className="mt-0.5 shrink-0" />
              <span className="min-w-0 break-words">{toast.message}</span>
              <button
                onClick={() => setToasts((list) => list.filter((t) => t.id !== toast.id))}
                className="ml-auto shrink-0 opacity-60 transition hover:opacity-100"
              >
                <X size={14} />
              </button>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}
