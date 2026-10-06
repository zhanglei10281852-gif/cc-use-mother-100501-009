"""SQLite 只追加台账存储。

台账行一经写入即不可变：没有 update/delete 接口。业务幂等键与内容指纹双约束：
同一原始记录重复导入返回既有记录而不是再次扣减额度；同标识不同内容则冲突报错。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .events import Event, identity_key, make_event

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL,
    fingerprint  TEXT NOT NULL UNIQUE,
    idem_key     TEXT NOT NULL UNIQUE,
    recorded_at  TEXT NOT NULL
);
"""


class LedgerError(Exception):
    """台账写入冲突（幂等重复或同标识内容冲突）。"""


class DuplicateEvent(LedgerError):
    """同一业务单据/原始记录已经入账。"""

    def __init__(self, seq: int, event: Event):
        super().__init__(f"重复入账，已存在 seq={seq} key={identity_key(event.event_type, event.payload)}")
        self.seq = seq
        self.event = event


class ContentConflict(LedgerError):
    """业务标识相同但内容指纹不同：拒绝悄悄覆盖。"""


class Ledger:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # HTTP 服务为多线程，同一连接加锁串行化写入；台账只追加，读不阻塞业务。
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def append(self, event_type: str, payload: dict) -> tuple[Event, bool]:
        """写入事件，返回 (事件, 是否为幂等重复)。"""
        event = make_event(event_type, payload)
        key = identity_key(event_type, payload)
        recorded_at = datetime.now(timezone.utc).isoformat()
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO ledger (event_type, payload, fingerprint, idem_key, recorded_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (event_type, json.dumps(payload, ensure_ascii=False, sort_keys=True),
                     event.fingerprint, key, recorded_at),
                )
                self.conn.commit()
                stored = Event(seq=cur.lastrowid, event_type=event_type,
                               payload=dict(payload), fingerprint=event.fingerprint)
                return stored, False
            except sqlite3.IntegrityError as exc:
                row = self.conn.execute(
                    "SELECT seq, event_type, payload, fingerprint FROM ledger WHERE idem_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    raise LedgerError(f"写入约束失败: {exc}") from exc
                if row["fingerprint"] != event.fingerprint:
                    raise ContentConflict(
                        f"业务标识 {key} 已存在不同内容（既有 seq={row['seq']}）；"
                        "请以更正/修订事件表达差异，不得覆盖原始记录"
                    ) from exc
                existing = Event(
                    seq=row["seq"], event_type=row["event_type"],
                    payload=json.loads(row["payload"]), fingerprint=row["fingerprint"],
                )
                return existing, True

    def all_events(self) -> list[Event]:
        return self.read_since(0)

    def read_since(self, seq: int) -> list[Event]:
        rows = self.conn.execute(
            "SELECT seq, event_type, payload, fingerprint FROM ledger"
            " WHERE seq > ? ORDER BY seq", (seq,),
        ).fetchall()
        return [
            Event(seq=r["seq"], event_type=r["event_type"],
                  payload=json.loads(r["payload"]), fingerprint=r["fingerprint"])
            for r in rows
        ]

    def get(self, seq: int) -> Event | None:
        row = self.conn.execute(
            "SELECT seq, event_type, payload, fingerprint FROM ledger WHERE seq = ?",
            (seq,),
        ).fetchone()
        if row is None:
            return None
        return Event(seq=row["seq"], event_type=row["event_type"],
                     payload=json.loads(row["payload"]), fingerprint=row["fingerprint"])

    def latest_seq(self) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM ledger").fetchone()
        return int(row["m"])

    def append_many(self, items: Iterable[tuple[str, dict]]) -> list[tuple[Event, bool]]:
        return [self.append(t, p) for t, p in items]
