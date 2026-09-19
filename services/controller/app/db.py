"""Хранилище истории трафика (SQLite). Пишем поминутные дельты, не счётчики."""

import sqlite3
import threading
import time

from . import settings

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic (
    ts          INTEGER PRIMARY KEY,
    wan_rx      INTEGER NOT NULL DEFAULT 0,
    wan_tx      INTEGER NOT NULL DEFAULT 0,
    lan_rx      INTEGER NOT NULL DEFAULT 0,
    lan_tx      INTEGER NOT NULL DEFAULT 0,
    proxy_up    INTEGER NOT NULL DEFAULT 0,
    proxy_down  INTEGER NOT NULL DEFAULT 0,
    direct_up   INTEGER NOT NULL DEFAULT 0,
    direct_down INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS client_traffic (
    ts   INTEGER NOT NULL,
    ip   TEXT    NOT NULL,
    up   INTEGER NOT NULL DEFAULT 0,
    down INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (ts, ip)
);

CREATE INDEX IF NOT EXISTS idx_client_traffic_ip ON client_traffic (ip, ts);
"""


def connection():
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(settings.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init():
    global _initialised
    with _init_lock:
        if _initialised:
            return
        connection().executescript(SCHEMA)
        connection().commit()
        _initialised = True


def record_traffic(ts, values):
    conn = connection()
    conn.execute(
        """INSERT INTO traffic
               (ts, wan_rx, wan_tx, lan_rx, lan_tx,
                proxy_up, proxy_down, direct_up, direct_down)
           VALUES (:ts, :wan_rx, :wan_tx, :lan_rx, :lan_tx,
                   :proxy_up, :proxy_down, :direct_up, :direct_down)
           ON CONFLICT(ts) DO UPDATE SET
               wan_rx = wan_rx + excluded.wan_rx,
               wan_tx = wan_tx + excluded.wan_tx,
               lan_rx = lan_rx + excluded.lan_rx,
               lan_tx = lan_tx + excluded.lan_tx,
               proxy_up = proxy_up + excluded.proxy_up,
               proxy_down = proxy_down + excluded.proxy_down,
               direct_up = direct_up + excluded.direct_up,
               direct_down = direct_down + excluded.direct_down""",
        {"ts": ts, **values},
    )
    conn.commit()


def record_clients(ts, per_ip):
    if not per_ip:
        return
    conn = connection()
    conn.executemany(
        """INSERT INTO client_traffic (ts, ip, up, down)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(ts, ip) DO UPDATE SET
               up = up + excluded.up, down = down + excluded.down""",
        [(ts, ip, v.get("up", 0), v.get("down", 0)) for ip, v in per_ip.items()],
    )
    conn.commit()


def history(seconds, bucket):
    """Ряд [{ts, ...}] за последние `seconds`, схлопнутый в корзины по `bucket`."""
    since = int(time.time()) - seconds
    rows = connection().execute(
        f"""SELECT (ts / {int(bucket)}) * {int(bucket)} AS bucket_ts,
                   SUM(wan_rx) AS wan_rx, SUM(wan_tx) AS wan_tx,
                   SUM(lan_rx) AS lan_rx, SUM(lan_tx) AS lan_tx,
                   SUM(proxy_up) AS proxy_up, SUM(proxy_down) AS proxy_down,
                   SUM(direct_up) AS direct_up, SUM(direct_down) AS direct_down
              FROM traffic
             WHERE ts >= ?
          GROUP BY bucket_ts
          ORDER BY bucket_ts""",
        (since,),
    ).fetchall()
    return [dict(row) | {"ts": row["bucket_ts"]} for row in rows]


def totals(seconds):
    since = int(time.time()) - seconds
    row = connection().execute(
        """SELECT COALESCE(SUM(wan_rx), 0) AS wan_rx,
                  COALESCE(SUM(wan_tx), 0) AS wan_tx,
                  COALESCE(SUM(proxy_up), 0) AS proxy_up,
                  COALESCE(SUM(proxy_down), 0) AS proxy_down,
                  COALESCE(SUM(direct_up), 0) AS direct_up,
                  COALESCE(SUM(direct_down), 0) AS direct_down
             FROM traffic WHERE ts >= ?""",
        (since,),
    ).fetchone()
    return dict(row) if row else {}


def client_totals(seconds):
    since = int(time.time()) - seconds
    rows = connection().execute(
        """SELECT ip, COALESCE(SUM(up), 0) AS up, COALESCE(SUM(down), 0) AS down
             FROM client_traffic WHERE ts >= ?
         GROUP BY ip""",
        (since,),
    ).fetchall()
    return {row["ip"]: {"up": row["up"], "down": row["down"]} for row in rows}


def prune(days=30):
    cutoff = int(time.time()) - days * 86400
    conn = connection()
    conn.execute("DELETE FROM traffic WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM client_traffic WHERE ts < ?", (cutoff,))
    conn.commit()
