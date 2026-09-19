const BYTE_UNITS = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ', 'ПБ']
const RATE_UNITS = ['бит/с', 'Кбит/с', 'Мбит/с', 'Гбит/с']

export function bytes(value) {
  let n = Number(value) || 0
  let unit = 0
  while (n >= 1024 && unit < BYTE_UNITS.length - 1) {
    n /= 1024
    unit += 1
  }
  const digits = n >= 100 || unit === 0 ? 0 : n >= 10 ? 1 : 2
  return `${n.toFixed(digits)} ${BYTE_UNITS[unit]}`
}

/** Скорость показываем в битах — так её печатают провайдеры и спидтесты. */
export function rate(bytesPerSecond) {
  let n = (Number(bytesPerSecond) || 0) * 8
  let unit = 0
  while (n >= 1000 && unit < RATE_UNITS.length - 1) {
    n /= 1000
    unit += 1
  }
  const digits = n >= 100 || unit === 0 ? 0 : n >= 10 ? 1 : 2
  return `${n.toFixed(digits)} ${RATE_UNITS[unit]}`
}

export function duration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds) || 0))
  const days = Math.floor(s / 86400)
  const hours = Math.floor((s % 86400) / 3600)
  const minutes = Math.floor((s % 3600) / 60)
  if (days) return `${days} д ${hours} ч`
  if (hours) return `${hours} ч ${minutes} мин`
  if (minutes) return `${minutes} мин`
  return `${s} с`
}

export function clockLabel(ts) {
  const date = new Date((Number(ts) || 0) * 1000)
  return date.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
}

export function dateLabel(ts) {
  const date = new Date((Number(ts) || 0) * 1000)
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function plural(n, one, few, many) {
  const abs = Math.abs(n) % 100
  const last = abs % 10
  if (abs > 10 && abs < 20) return many
  if (last > 1 && last < 5) return few
  if (last === 1) return one
  return many
}
