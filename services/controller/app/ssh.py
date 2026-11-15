"""SSH до своих VDS-нод: подключение, команды от root, ключи.

Пароль от сервера нужен один раз — при первом деплое. Роутер сразу кладёт
на сервер свой собственный ed25519-ключ и дальше ходит только по нему, а
пароль нигде не сохраняется. Ключ хоста запоминается при первом подключении
(TOFU): подменённый сервер дальше получит отказ, а не новые секреты Reality.
"""

import base64
import io
import logging
import os
import shlex
import socket

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import settings

log = logging.getLogger("circle.ssh")

NODES_DIR = settings.DATA / "nodes"
CONNECT_TIMEOUT = 15


class SshError(RuntimeError):
    pass


def key_path(node_id):
    return NODES_DIR / node_id / "id_ed25519"


def ensure_keypair(node_id):
    """Ключ роутера для этой ноды. Возвращает строку для authorized_keys."""
    path = key_path(node_id)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
        path.write_bytes(pem)
        os.chmod(path, 0o600)
    key = paramiko.Ed25519Key.from_private_key_file(str(path))
    return f"{key.get_name()} {key.get_base64()} circle-router"


def forget_keypair(node_id):
    path = key_path(node_id)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _load_user_key(text, passphrase=None):
    """Приватный ключ, вставленный в форму: OpenSSH или PEM любого типа,
    в том числе защищённый паролем."""
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text.strip() + "\n"),
                                        password=passphrase or None)
        except paramiko.PasswordRequiredException as exc:
            raise SshError("ключ защищён паролем — укажите его в поле «Пароль ключа»") from exc
        except (paramiko.SSHException, ValueError):
            continue
    if passphrase:
        raise SshError("ключ не расшифровался: неверный пароль ключа или неподдерживаемый формат")
    raise SshError("не удалось прочитать приватный ключ (поддерживаются ed25519, ecdsa, rsa)")


class _RememberPolicy(paramiko.MissingHostKeyPolicy):
    """Первое подключение: запоминаем ключ хоста. Несовпадение с уже
    запомненным paramiko отвергнет сам, ещё до вызова политики."""

    def __init__(self):
        self.learned = None

    def missing_host_key(self, client, hostname, key):
        self.learned = f"{key.get_name()} {key.get_base64()}"


def _host_entry(host, port):
    return host if int(port) == 22 else f"[{host}]:{port}"


def _http_connect(proxy_port, host, port, timeout):
    """TCP до host:port через HTTP-инбаунд xray (то есть внутри туннеля)."""
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=timeout)
    request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
    sock.sendall(request.encode())
    reply = b""
    while b"\r\n\r\n" not in reply:
        chunk = sock.recv(1024)
        if not chunk:
            sock.close()
            raise OSError("прокси закрыл соединение")
        reply += chunk
        if len(reply) > 8192:
            sock.close()
            raise OSError("слишком длинный ответ прокси")
    status = reply.split(b"\r\n", 1)[0]
    if b" 200" not in status:
        sock.close()
        raise OSError(f"прокси ответил: {status.decode(errors='replace')}")
    return sock


class Session:
    """Одно SSH-подключение к ноде. Команды выполняются от root: напрямую или
    через `sudo -n`, если зашли обычным пользователем."""

    def __init__(self, node, password=None, private_key=None, via_proxy_port=None,
                 key_passphrase=None):
        self.node = node
        self.client = paramiko.SSHClient()
        self.policy = _RememberPolicy()
        host, port = node["host"], int(node.get("ssh_port") or 22)
        if node.get("host_key"):
            keytype, blob = node["host_key"].split(" ", 1)
            key = paramiko.PKey.from_type_string(keytype, base64.b64decode(blob))
            self.client.get_host_keys().add(_host_entry(host, port), keytype, key)
        self.client.set_missing_host_key_policy(self.policy)

        kwargs = {
            "hostname": host, "port": port,
            "username": node.get("ssh_user") or "root",
            "timeout": CONNECT_TIMEOUT, "banner_timeout": CONNECT_TIMEOUT,
            "auth_timeout": CONNECT_TIMEOUT,
            "allow_agent": False, "look_for_keys": False,
        }
        if via_proxy_port:
            kwargs["sock"] = _http_connect(via_proxy_port, host, port, CONNECT_TIMEOUT)
        if private_key:
            kwargs["pkey"] = _load_user_key(private_key, key_passphrase)
        elif password:
            kwargs["password"] = password
        elif key_path(node["id"]).exists():
            kwargs["pkey"] = paramiko.Ed25519Key.from_private_key_file(str(key_path(node["id"])))
        else:
            raise SshError("нет ни пароля, ни ключа для подключения")

        try:
            self.client.connect(**kwargs)
        except paramiko.BadHostKeyException as exc:
            raise SshError("ключ хоста изменился с прошлого подключения — возможна подмена "
                           "сервера. Если сервер переустановлен, сбросьте ключ в настройках ноды") from exc
        except paramiko.AuthenticationException as exc:
            raise SshError("сервер не принял логин/пароль или ключ") from exc
        except (paramiko.SSHException, OSError) as exc:
            raise SshError(f"не удалось подключиться к {host}:{port}: {exc}") from exc

        transport = self.client.get_transport()
        transport.set_keepalive(20)
        self.is_root = (kwargs["username"] == "root")

    @property
    def learned_host_key(self):
        return self.policy.learned

    def close(self):
        try:
            self.client.close()
        except Exception:                              # noqa: BLE001
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _wrap(self, command, root):
        if root and not self.is_root:
            return f"sudo -n sh -c {shlex.quote(command)}"
        return f"sh -c {shlex.quote(command)}"

    def run(self, command, *, root=True, timeout=120, input_data=None, check=True):
        """Выполнить команду. Возвращает (rc, stdout+stderr)."""
        try:
            stdin, stdout, stderr = self.client.exec_command(
                self._wrap(command, root), timeout=timeout)
            if input_data is not None:
                stdin.write(input_data)
                stdin.channel.shutdown_write()
            output = stdout.read().decode("utf-8", "replace")
            output += stderr.read().decode("utf-8", "replace")
            rc = stdout.channel.recv_exit_status()
        except (paramiko.SSHException, OSError, socket.timeout) as exc:
            raise SshError(f"команда не выполнилась: {exc}") from exc
        if check and rc != 0:
            if "sudo" in output and "password" in output:
                raise SshError("пользователю нужен sudo без пароля (NOPASSWD) или вход под root")
            raise SshError(f"код {rc}: {output.strip()[-600:]}")
        return rc, output

    def write_file(self, path, content, mode="0644", owner=None):
        """Записать файл атомарно: через временный рядом и mv."""
        tmp = f"{path}.circle-tmp"
        chown = f" && chown {owner} {shlex.quote(tmp)}" if owner else ""
        self.run(f"umask 077 && cat > {shlex.quote(tmp)} && chmod {mode} {shlex.quote(tmp)}"
                 f"{chown} && mv -f {shlex.quote(tmp)} {shlex.quote(path)}",
                 input_data=content)

    def install_pubkey(self, line):
        """Ключ роутера в authorized_keys того пользователя, под которым зашли."""
        script = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && "
            "chmod 600 ~/.ssh/authorized_keys && "
            f"(grep -qxF {shlex.quote(line)} ~/.ssh/authorized_keys || "
            f"echo {shlex.quote(line)} >> ~/.ssh/authorized_keys)"
        )
        self.run(script, root=False)

    def remove_pubkey(self, line):
        body = line.split(" ")[1]
        self.run(f"[ -f ~/.ssh/authorized_keys ] && sed -i '\\|{body}|d' ~/.ssh/authorized_keys || true",
                 root=False, check=False)

    def open_stream(self, command, root=False):
        """Долгоживущий канал: для потока метрик."""
        channel = self.client.get_transport().open_session()
        channel.exec_command(self._wrap(command, root))
        return channel
