"""Авторизация в панели: bcrypt-хеш пароля + подписанная сессионная cookie.

Хеш хранится в data/config/auth.json, а не только в переменной окружения:
смена пароля из браузера иначе требовала бы правки .env и перезапуска
контейнера. Значение из .env используется как начальное — при первом старте.
"""

import hmac
import json
import logging
import os
import threading
import time

import bcrypt
from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from . import settings

log = logging.getLogger("circle.auth")

_signer = TimestampSigner(settings.SECRET_KEY or "circle-router-insecure-dev-key")
_lock = threading.RLock()

MIN_PASSWORD_LENGTH = 8


def _load_store():
    with _lock:
        if settings.AUTH_STORE.exists():
            try:
                store = json.loads(settings.AUTH_STORE.read_text(encoding="utf-8"))
                if store.get("password_hash"):
                    store.setdefault("epoch", 1)
                    store.setdefault("username", settings.ADMIN_USER)
                    return store
            except (json.JSONDecodeError, OSError):
                log.warning("auth.json не читается, беру пароль из окружения")
        store = {
            "username": settings.ADMIN_USER,
            "password_hash": settings.ADMIN_PASSWORD_HASH or "",
            "epoch": 1,
            "updated_at": 0,
        }
        if store["password_hash"]:
            _save_store(store)
        return store


def _save_store(store):
    tmp = settings.AUTH_STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, settings.AUTH_STORE)
    try:
        os.chmod(settings.AUTH_STORE, 0o600)
    except OSError:
        pass


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    try:
        return bcrypt.checkpw((password or "").encode(), stored_hash.encode())
    except ValueError:
        log.error("сохранённый хеш пароля не похож на bcrypt")
        return False


def verify_credentials(username, password):
    store = _load_store()
    if not hmac.compare_digest(username or "", store.get("username", "")):
        return False
    if not store.get("password_hash"):
        log.error("пароль не задан: заполните CR_ADMIN_PASSWORD_HASH в .env")
        return False
    return check_password(password, store["password_hash"])


def change_password(current_password, new_password):
    """Возвращает новый epoch. Бросает ValueError с текстом для пользователя."""
    if len(new_password or "") < MIN_PASSWORD_LENGTH:
        raise ValueError(f"пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов")
    with _lock:
        store = _load_store()
        if not check_password(current_password, store.get("password_hash", "")):
            raise ValueError("текущий пароль неверен")
        if check_password(new_password, store["password_hash"]):
            raise ValueError("новый пароль совпадает с текущим")
        store["password_hash"] = hash_password(new_password)
        # Смена epoch обнуляет все выданные ранее cookie — сессии на других
        # устройствах перестают работать сразу.
        store["epoch"] = int(store.get("epoch", 1)) + 1
        store["updated_at"] = int(time.time())
        _save_store(store)
        return store["epoch"]


def issue_session(response: Response, username: str):
    epoch = _load_store().get("epoch", 1)
    token = _signer.sign(f"{username}|{epoch}".encode()).decode()
    response.set_cookie(
        settings.SESSION_COOKIE,
        token,
        max_age=settings.SESSION_TTL,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_session(response: Response):
    response.delete_cookie(settings.SESSION_COOKIE, path="/")


def current_user(request: Request):
    token = request.cookies.get(settings.SESSION_COOKIE)
    if not token:
        return None
    try:
        payload = _signer.unsign(token, max_age=settings.SESSION_TTL).decode()
    except (BadSignature, SignatureExpired):
        return None
    username, _, epoch = payload.partition("|")
    store = _load_store()
    if epoch != str(store.get("epoch", 1)):
        return None
    if username != store.get("username"):
        return None
    return username


def require_user(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="нужна авторизация")
    return user


def password_is_set():
    return bool(_load_store().get("password_hash"))


def info():
    store = _load_store()
    return {"username": store.get("username"),
            "password_updated_at": store.get("updated_at", 0)}
