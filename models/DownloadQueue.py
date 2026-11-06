"""下载任务通知队列。

机器人重启后仍能继续推送未完成的下载，所以队列会落盘到 config/queue.json。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from lib.func import get_logger

log = get_logger("DownloadQueue")


@dataclass
class QueueEntry:
    torrent_id: int
    chat_id: int
    lang_code: str | None = None
    title: str = ""
    requested_dir: str = ""
    # 数字 ID 可能变化或被复用；监控按 info_hash 查找并批量重键队列。
    info_hash: str | None = None
    # 任务来源：fc2-card / manual / batch / unknown（历史条目没有这个字段，
    # from_json 只认已知字段 + dataclass 默认值，旧队列文件照常能读）。
    source: str = "unknown"
    auto_classify: bool = False
    message_id: int | None = None
    # 批量添加时，任务过多就不为每个任务单独发进度卡片
    progress_card: bool = True
    added_ts: float = field(default_factory=time.time)
    last_percent: float = -1.0
    last_edit_ts: float = 0.0
    milestones_sent: set[int] = field(default_factory=set)
    classified: bool = False
    final_dir: str | None = None
    # 上次实际发出去的卡片文本：相同文本直接跳过，不再编辑也不重发，
    # 否则速度/ peers / ETA 一变就每 45 秒刷一次。
    last_card_text: str = ""
    last_classify_fail_ts: float = 0.0
    notified_error: bool = False
    notified_stalled: bool = False
    # 上次停滞提醒的发送时间；同一任务两次提醒至少间隔 6 小时
    # （见 DownloadMonitor.STALLED_NOTIFY_INTERVAL）。
    last_stalled_notify_ts: float = 0.0
    last_progress_ts: float = field(default_factory=time.time)
    last_progress_value: float = -1.0
    finished: bool = False

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["milestones_sent"] = sorted(self.milestones_sent)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "QueueEntry":
        known = {f for f in cls.__dataclass_fields__}
        payload = {k: v for k, v in data.items() if k in known}
        payload["milestones_sent"] = set(payload.get("milestones_sent") or [])
        payload["torrent_id"] = int(payload["torrent_id"])
        payload["chat_id"] = int(payload["chat_id"])
        return cls(**payload)


class DownloadQueue:
    """线程安全的队列容器，供处理器与监控协程共享。"""

    _items: dict[int, QueueEntry] = {}
    _lock = threading.RLock()
    _state_file: str | None = None

    @classmethod
    def set_state_file(cls, path: str | None) -> None:
        cls._state_file = path
        cls.load()

    @classmethod
    def load(cls) -> None:
        if not cls._state_file or not os.path.isfile(cls._state_file):
            return
        try:
            with open(cls._state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            with cls._lock:
                for item in data:
                    try:
                        entry = QueueEntry.from_json(item)
                        cls._items[entry.torrent_id] = entry
                    except Exception as err:  # noqa: BLE001
                        log.debug("跳过损坏的队列记录: %s", err)
            log.info("已从 %s 恢复 %s 个下载任务", cls._state_file, len(cls._items))
        except Exception as err:  # noqa: BLE001
            log.warning("读取下载队列失败 %s: %s", cls._state_file, err)

    @classmethod
    def _save(cls) -> None:
        if not cls._state_file:
            return
        # 落盘必须在同一把锁内完成快照和写入：之前先放锁再调 _save，
        # 监控协程和工作线程可能同时遍历/写同一个 .tmp 文件互相截断。
        with cls._lock:
            items = [e.to_json() for e in cls._items.values()]
            try:
                os.makedirs(os.path.dirname(cls._state_file) or ".", exist_ok=True)
                tmp = f"{cls._state_file}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(items, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, cls._state_file)
            except Exception as err:  # noqa: BLE001
                log.warning("保存下载队列失败: %s", err)

    @classmethod
    def add(cls, entry: QueueEntry) -> None:
        with cls._lock:
            cls._items[entry.torrent_id] = entry
        cls._save()

    @classmethod
    def add_if_new(cls, entry: QueueEntry) -> bool:
        new_hash = str(entry.info_hash or "").lower()
        with cls._lock:
            for existing in cls._items.values():
                existing_hash = str(existing.info_hash or "").lower()
                if existing_hash and new_hash:
                    duplicate = existing_hash == new_hash
                else:
                    duplicate = existing.torrent_id == entry.torrent_id
                if duplicate:
                    return False
            cls._items[entry.torrent_id] = entry
        cls._save()
        return True

    @classmethod
    def get(cls, torrent_id: int) -> QueueEntry | None:
        with cls._lock:
            return cls._items.get(int(torrent_id))

    @classmethod
    def remove(
        cls, torrent_id: int, info_hash: str | None = None
    ) -> QueueEntry | None:
        with cls._lock:
            entry = cls._items.get(int(torrent_id))
            if entry is None:
                return None
            if info_hash and str(entry.info_hash or "").lower() != info_hash.lower():
                return None
            del cls._items[int(torrent_id)]
        cls._save()
        return entry

    @classmethod
    def snapshot(cls) -> dict[int, QueueEntry]:
        # 返回深一点的副本：调用方改 entry 字段时不再直接改动内存里的
        # 原条目，改完必须显式 update() 才生效；同时避免旧快照在条目
        # 已被删除后又 update() 把它复活。
        import copy

        with cls._lock:
            result = {}
            for k, v in cls._items.items():
                entry = copy.copy(v)
                entry.milestones_sent = set(v.milestones_sent)
                result[k] = entry
            return result

    @classmethod
    def rekey(cls, entries: dict[int, QueueEntry]) -> set[int]:
        """一次替换本轮已定位的键，避免两个 ID 互换时覆盖另一条任务。"""
        with cls._lock:
            updates = {
                old_id: entry
                for old_id, entry in entries.items()
                if old_id in cls._items
                and str(cls._items[old_id].info_hash or "").lower()
                == str(entry.info_hash or "").lower()
            }
            destinations: dict[int, list[int]] = {}
            for old_id, entry in updates.items():
                destinations.setdefault(entry.torrent_id, []).append(old_id)
            for old_ids in destinations.values():
                if len(old_ids) > 1:
                    for old_id in old_ids:
                        updates.pop(old_id)
            # 未能读取的任务仍占着旧键时，本轮保留双方，不能用新键覆盖它。
            while True:
                blocked = {
                    old_id
                    for old_id, entry in updates.items()
                    if entry.torrent_id in cls._items
                    and entry.torrent_id not in updates
                }
                if not blocked:
                    break
                for old_id in blocked:
                    updates.pop(old_id)
            if not updates:
                return set()
            for old_id in updates:
                del cls._items[old_id]
            for entry in updates.values():
                cls._items[entry.torrent_id] = entry
            cls._save()
            return set(updates)

    @classmethod
    def update(cls, entry: QueueEntry) -> None:
        with cls._lock:
            current = cls._items.get(entry.torrent_id)
            if current is None:
                # 条目已在别处被删除（完成/不存在），不要把它复活回来。
                return
            if str(current.info_hash or "").lower() != str(entry.info_hash or "").lower():
                # 旧快照的数字 ID 已属于另一个 hash，不能覆盖它。
                return
            cls._items[entry.torrent_id] = entry
        cls._save()

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._items.clear()
        cls._save()

    @classmethod
    def size(cls) -> int:
        with cls._lock:
            return len(cls._items)
