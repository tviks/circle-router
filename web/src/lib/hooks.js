import { useCallback, useEffect, useRef, useState } from 'react'

/**
 * Периодически дёргает переданный загрузчик. Пауза, пока вкладка скрыта, —
 * панель обычно висит открытой сутками, незачем греть роутер вхолостую.
 */
export function usePoll(loader, intervalMs, deps = []) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  const refresh = useCallback(async () => {
    try {
      const result = await loaderRef.current()
      setData(result)
      setError(null)
      return result
    } catch (err) {
      setError(err)
      throw err
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    let alive = true
    let timer = null

    // Первую загрузку делаем всегда, даже если вкладка открылась в фоне:
    // иначе страница остаётся пустой до тех пор, пока на неё не переключатся,
    // и выглядит это как «панель не работает».
    let first = true

    const tick = async () => {
      if (!alive) return
      if (first || !document.hidden) {
        first = false
        try {
          await refresh()
        } catch {
          /* ошибка уже в state */
        }
      }
      if (alive) timer = setTimeout(tick, intervalMs)
    }

    tick()
    const onVisible = () => !document.hidden && refresh().catch(() => {})
    document.addEventListener('visibilitychange', onVisible)

    return () => {
      alive = false
      if (timer) clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps])

  return { data, error, loading, refresh, setData }
}

/**
 * Форма, наполняемая с сервера, но не затираемая его фоновыми ответами.
 *
 * Опрос отдаёт каждый раз НОВЫЙ объект, даже когда данные не изменились. Если
 * сажать его в черновик по идентичности, всё набранное в полях исчезает на
 * очередном тике — что и происходило раз в тридцать секунд. Поэтому пока есть
 * несохранённые правки, серверная версия в форму не попадает.
 */
export function useConfigDraft(loader, intervalMs = 30000) {
  const { data, refresh } = usePoll(loader, intervalMs)
  const [draft, setDraft] = useState(null)
  const [dirty, setDirty] = useState(false)
  // Ref, а не state: эффект ниже должен видеть актуальное значение сразу, не
  // дожидаясь следующего рендера.
  const dirtyRef = useRef(false)

  useEffect(() => {
    if (data && !dirtyRef.current) setDraft(structuredClone(data))
  }, [data])

  const edit = useCallback((updater) => {
    dirtyRef.current = true
    setDirty(true)
    setDraft((current) => (typeof updater === 'function' ? updater(current) : updater))
  }, [])

  /** Правки сохранены или отменены — снова принимаем серверную версию. */
  const settle = useCallback(async () => {
    dirtyRef.current = false
    setDirty(false)
    try {
      const fresh = await refresh()
      if (fresh) setDraft(structuredClone(fresh))
    } catch {
      /* следующий опрос подтянет */
    }
  }, [refresh])

  return { draft, edit, dirty, settle, refresh }
}
