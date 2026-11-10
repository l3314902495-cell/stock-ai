"""基于 SQLite 的 TTL 缓存，减少免费数据源被限流的概率。"""
from __future__ import annotations

import functools
import hashlib
import pickle
import sqlite3
import threading
import time
from typing import Any, Callable

from .config import expand, get_config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = expand(get_config()["cache"]["path"])
        _conn = sqlite3.connect(path, check_same_thread=False)
        _conn.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, exp REAL, v BLOB)")
        _conn.commit()
    return _conn


def get(key: str) -> Any | None:
    with _lock:
        row = _db().execute("SELECT exp, v FROM cache WHERE k=?", (key,)).fetchone()
    if not row or row[0] < time.time():
        return None
    try:
        return pickle.loads(row[1])
    except Exception:
        return None


def put(key: str, value: Any, ttl: float) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO cache (k, exp, v) VALUES (?, ?, ?)",
            (key, time.time() + ttl, pickle.dumps(value)),
        )
        _db().commit()


def clear() -> None:
    with _lock:
        _db().execute("DELETE FROM cache")
        _db().commit()


def cached(ttl: float) -> Callable:
    """装饰器：按函数名 + 参数缓存返回值（ttl 秒）。返回 None 不缓存。"""

    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            raw = f"{fn.__module__}.{fn.__qualname__}|{args!r}|{sorted(kwargs.items())!r}"
            key = hashlib.sha1(raw.encode()).hexdigest()
            hit = get(key)
            if hit is not None:
                return hit
            val = fn(*args, **kwargs)
            # 返回 None 或带 _nocache 标记的结果（如临时取不到数据）不缓存，下次重新请求
            if isinstance(val, dict) and val.pop("_nocache", False):
                return val
            if val is not None:
                put(key, val, ttl)
            return val

        return wrapper

    return deco
