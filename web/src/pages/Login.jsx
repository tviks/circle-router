import { useState } from 'react'
import { Globe2, LogIn } from 'lucide-react'
import { api } from '../lib/api'
import { Field, Input, Spinner } from '../components/ui'

export default function Login({ onSuccess }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const result = await api.login(username, password)
      onSuccess(result.user)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-4">
      <form onSubmit={submit} className="card card-pad w-full max-w-sm animate-fade-up">
        <div className="mb-6 flex items-center gap-3">
          <span className="flex h-10 w-10 items-center justify-center rounded-xl border border-accent/40 bg-accent/10">
            <Globe2 size={18} className="text-accent" />
          </span>
          <div>
            <h1 className="text-base font-semibold text-slate-100">circle-router</h1>
            <p className="text-xs text-slate-500">панель управления</p>
          </div>
        </div>

        <div className="space-y-4">
          <Field label="Логин">
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
            />
          </Field>
          <Field label="Пароль" error={error}>
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
            />
          </Field>
        </div>

        <button type="submit" className="btn-primary mt-6 w-full" disabled={busy}>
          {busy ? <Spinner /> : <LogIn size={16} />}
          Войти
        </button>
      </form>
    </div>
  )
}
