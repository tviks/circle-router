"""Тонкая обёртка над subprocess: везде одинаковый таймаут и логирование."""

import logging
import os
import subprocess

log = logging.getLogger("circle.shell")


class CommandError(RuntimeError):
    def __init__(self, argv, returncode, output):
        self.argv = argv
        self.returncode = returncode
        self.output = output
        super().__init__(f"{' '.join(argv)} -> {returncode}: {output.strip()[:500]}")


def run(argv, *, check=True, timeout=20, input_text=None, env=None):
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env={**os.environ, **env} if env else None,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Часть утилит (networkctl) в slim-образе отсутствует — для
        # необязательных шагов это не повод падать.
        if check:
            raise CommandError(argv, -1, str(exc)) from exc
        log.debug("cmd unavailable: %s (%s)", argv, exc)
        return -1, str(exc)
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        log.debug("cmd failed: %s -> %s: %s", argv, proc.returncode, output.strip()[:300])
        if check:
            raise CommandError(argv, proc.returncode, output)
    return proc.returncode, output


def out(argv, *, timeout=20, default=""):
    """Вернуть stdout команды, а при любой ошибке — default."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return default
    if proc.returncode != 0:
        return default
    return proc.stdout
