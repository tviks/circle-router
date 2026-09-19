#!/usr/bin/env bash
# Создаёт .env: bcrypt-хеш пароля админа и ключ подписи сессий.
#   bash scripts/gen-env.sh                 # пароль спросит интерактивно
#   bash scripts/gen-env.sh 'мой-пароль'    # или передать аргументом
set -euo pipefail

cd "$(dirname "$0")/.."

PASSWORD="${1:-}"
if [ -z "$PASSWORD" ]; then
    read -rsp "Пароль для входа в панель: " PASSWORD
    echo
    read -rsp "Повторите: " CONFIRM
    echo
    [ "$PASSWORD" = "$CONFIRM" ] || { echo "Пароли не совпали." >&2; exit 1; }
fi
[ ${#PASSWORD} -ge 8 ] || { echo "Пароль короче 8 символов." >&2; exit 1; }

# bcrypt считаем в одноразовом контейнере — на хосте питоновских зависимостей нет.
HASH=$(printf '%s' "$PASSWORD" | docker run --rm -i python:3.12-slim sh -c \
    'pip install --quiet --disable-pip-version-check bcrypt >/dev/null 2>&1;
     python -c "import bcrypt,sys; print(bcrypt.hashpw(sys.stdin.buffer.read(), bcrypt.gensalt()).decode())"')

SECRET=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')

if [ -f .env ]; then
    cp .env ".env.bak.$(date +%Y%m%d%H%M%S)"
    echo "Прежний .env сохранён рядом с суффиксом .bak"
fi

cp .env.example .env
# Хеш содержит слеши и $, поэтому подставляем через python, а не sed.
CR_HASH="$HASH" CR_SECRET="$SECRET" python3 - <<'PY'
import os, pathlib
path = pathlib.Path(".env")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("CR_ADMIN_PASSWORD_HASH="):
        # Одинарные кавычки: bcrypt-хеш полон $, а compose интерполирует .env.
        line = "CR_ADMIN_PASSWORD_HASH='" + os.environ["CR_HASH"] + "'"
    elif line.startswith("CR_SECRET_KEY="):
        line = "CR_SECRET_KEY=" + os.environ["CR_SECRET"]
    lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

chmod 600 .env
echo "Готово: .env создан."
