"""只追加事件账本：哈希链 + 幂等键 + JSONL 持久化。

- 每条事件携带前一条的哈希，任何对历史事件的改写都会在加载校验时暴露；
- 幂等键保证"同一原始记录重复导入"返回首次结果，不会产生第二条事件、不会多扣额度；
- 存储层不做业务判断，业务规则位于 services / engine。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .events import canonical_hash, deserialize_event, serialize_event


@dataclass(frozen=True, slots=True)
class StoredEvent:
    seq: int
    event_id: str
    event: Any
    hash: str
    idempotency_key: str | None


class IdempotencyConflict(RuntimeError):
    """同一幂等键第二次提交了不同内容。"""


class EventChainBroken(RuntimeError):
    """历史事件被改动或丢失。"""


class EventStore:
    """线程安全的只追加事件存储。

    每次 append 成功后通知订阅者；订阅者异常不会破坏存储，但会向上传播，
    以便调用方知道读模型未更新。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._events: list[StoredEvent] = []
        self._idempotency: dict[str, str] = {}
        self._subscribers: list[Callable[[StoredEvent], None]] = []
        self._lock = threading.RLock()
        if self._path and self._path.exists():
            self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def version(self) -> int:
        with self._lock:
            return len(self._events)

    def subscribe(self, callback: Callable[[StoredEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def append(self, event: Any, idempotency_key: str | None = None) -> StoredEvent:
        """追加事件；同幂等键且同内容则直接返回首次事件（不重复追加）。"""
        with self._lock:
            if idempotency_key is not None and idempotency_key in self._idempotency:
                existing_id = self._idempotency[idempotency_key]
                existing = next(item for item in self._events if item.event_id == existing_id)
                if canonical_hash(_event_body(existing.event)) == canonical_hash(_event_body(event)):
                    return existing
                raise IdempotencyConflict(
                    f"幂等键 {idempotency_key!r} 已存在但内容不同，拒绝覆盖"
                )

            seq = len(self._events) + 1
            event_id = uuid.uuid4().hex
            prev_hash = self._events[-1].hash if self._events else ""
            row = serialize_event(event, seq, event_id)
            own_hash = canonical_hash({"prev": prev_hash, "row": row})
            stored = StoredEvent(
                seq=seq,
                event_id=event_id,
                event=event,
                hash=own_hash,
                idempotency_key=idempotency_key,
            )
            self._events.append(stored)
            if idempotency_key is not None:
                self._idempotency[idempotency_key] = event_id
            if self._path is not None:
                self._persist(row, own_hash, idempotency_key)
            for callback in tuple(self._subscribers):
                callback(stored)
            return stored

    def find_by_key(self, idempotency_key: str) -> StoredEvent | None:
        with self._lock:
            event_id = self._idempotency.get(idempotency_key)
            if event_id is None:
                return None
            return next(item for item in self._events if item.event_id == event_id)

    def events(self) -> list[StoredEvent]:
        with self._lock:
            return list(self._events)

    def replay_into(self, sink: Callable[[StoredEvent], None]) -> None:
        for stored in self.events():
            sink(stored)

    # ---- 持久化 ----

    def _persist(self, row: dict[str, Any], own_hash: str, key: str | None) -> None:
        assert self._path is not None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"hash": own_hash, "idempotency_key": key, **row},
            ensure_ascii=False,
        )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load(self) -> None:
        assert self._path is not None
        prev_hash = ""
        for line_no, raw in enumerate(self._path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            envelope = json.loads(raw)
            row = {k: envelope[k] for k in ("event_id", "seq", "type", "body")}
            own_hash = canonical_hash({"prev": prev_hash, "row": row})
            if own_hash != envelope["hash"]:
                raise EventChainBroken(f"第 {line_no} 行哈希校验失败，历史事件可能被改写")
            if row["seq"] != line_no:
                raise EventChainBroken(f"第 {line_no} 行序号不连续")
            event = deserialize_event(row)
            stored = StoredEvent(
                seq=row["seq"],
                event_id=row["event_id"],
                event=event,
                hash=own_hash,
                idempotency_key=envelope.get("idempotency_key"),
            )
            self._events.append(stored)
            if stored.idempotency_key:
                self._idempotency[stored.idempotency_key] = stored.event_id
            prev_hash = own_hash


def _event_body(event: Any) -> dict[str, Any]:
    return {name: getattr(event, name) for name in getattr(event, "__slots__", ())}


def event_key(prefix: str, identity: str) -> str:
    """统一构造幂等键，例如 event_key('meter', record_id)。"""
    return f"{prefix}:{identity}"
