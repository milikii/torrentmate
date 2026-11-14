"""下载成功名单 + 完成卡待删队列（两个机器人共享）。

做两件事：

1. ``DoneList``：任务下载完成后追加一条记录（编号 / 标题 / info_hash /
   完成时间 / 完成时大小 / 来源）。文件只追加、不改写、不截断，任务删了、
   队列清了、容器重建了都不影响名单；想单独看就直接读这个 JSONL 文件，
   或让机器人用 ``/fc2done export`` 发出来。
2. ``DoneDeletionQueue``：下载机器人没法删 Fc2专用 发的卡片（Telegram
   只允许机器人删自己的消息），所以完成时把编号追加到待删文件，
   Fc2专用 轮询时读到、删掉自己的卡片并追加一条 ack，防止重复删。

共享方式和黑名单一致：两个容器的配置把 ``fc2.done_file`` 指到共享卷上的
同一个文件即可；待删文件默认是同目录的 ``<done_file>.pending``，也可以用
``fc2.done_pending_file`` 单独指定。

追加用 O_APPEND + fcntl.flock：两个容器同时写也只会在文件末尾各加一行，
不会互相覆盖。读取时解析失败的行跳过并记 warning——监控循环绝不能被
一行坏数据弄崩。
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

try:  # pragma: no cover - 本项目只在 Linux 容器里跑
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from lib.constants import BOT_FOLDER, CONFIG_FILE
from lib.func import get_logger
from models.Fc2Blacklist import normalize_number

log = get_logger("DoneList")

DEFAULT_NAME = "fc2_done.jsonl"
DEFAULT_PENDING_SUFFIX = ".pending"

# 来源：卡片点下载 / 手动发磁力或文件 / 批量添加 / 接管或无法判定 / 历史迁移
SOURCE_FC2_CARD = "fc2-card"
SOURCE_MANUAL = "manual"
SOURCE_BATCH = "batch"
SOURCE_UNKNOWN = "unknown"
SOURCE_MIGRATION = "migration"

SOURCE_LABELS = {
    SOURCE_FC2_CARD: "卡片下载",
    SOURCE_MANUAL: "手动添加",
    SOURCE_BATCH: "批量添加",
    SOURCE_UNKNOWN: "未知",
    SOURCE_MIGRATION: "历史迁移",
}


def source_label(source) -> str:
    return SOURCE_LABELS.get(str(source or ""), str(source or "未知"))


# append() 的结果：真的写了 / 已有同一条 / 写盘失败（读失败也归到这里，
# 调用方不能把 failed 当成写成功）。
WRITE_ADDED = "added"
WRITE_DUPLICATE = "duplicate"
WRITE_FAILED = "failed"

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _shared_lock(path: str) -> threading.RLock:
    """同一路径在进程内共用一把锁（多个 handler 各自 new 一个实例）。"""
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[path] = lock
        return lock


@contextmanager
def _file_lock(path: str):
    """跨进程追加锁：锁文件与数据文件分开，避免锁文件内容混进 JSONL。

    锁建不起来（权限 / 磁盘满）时 yield False，调用方拒绝无锁追加，
    宁可这次写不进去，也不能两个进程互相覆盖。
    """
    handle = None
    locked = False
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handle = open(f"{path}.lock", "a+", encoding="utf-8")
        if fcntl is not None:  # pragma: no cover - 非 POSIX 退化为进程内锁
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
    except OSError as err:
        log.warning("共享文件锁不可用 %s: %s", path, err)
    try:
        yield locked
    finally:
        if handle is not None:
            try:
                if locked and fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _config_file(config) -> str:
    """当前生效的配置文件路径。

    单测会把 ``BotConfigurator.config_file`` 指到 tmp 目录；默认名单跟着
    配置文件走，测试不会往仓库的 config/ 里写脏数据。
    """
    value = getattr(config, "config_file", None) or CONFIG_FILE
    return str(value)


def _config_value(config, key: str):
    """防御式读配置：没有 ``.get`` 的对象一律按「未配置」处理。

    旧测试会用 bare ``object()`` 当 config 构造 ``Fc2Pusher``，只为验证
    黑名单逻辑；新加的成功名单 / 待删路径读取不能因此把构造流程炸掉。
    """
    getter = getattr(config, "get", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except (AttributeError, TypeError):
        return None


def _resolve(config, key: str, default_name: str) -> str:
    """配置里的路径 → 绝对路径；没配或占位值就用默认位置。"""
    value = _config_value(config, key)
    text = str(value or "").strip()
    if not text or text.startswith("<"):
        return os.path.join(os.path.dirname(_config_file(config)), default_name)
    return text if os.path.isabs(text) else os.path.join(BOT_FOLDER, text)


def config_path(config) -> str:
    """fc2.done_file → 成功名单路径（两个容器要指同一个文件才共享）。"""
    return _resolve(config, "fc2.done_file", DEFAULT_NAME)


def pending_config_path(config) -> str:
    """待删文件路径。

    默认紧挨着成功名单：共享目录里两份配置只要指同一个 done_file，
    待删文件天然也在共享目录、也天然是同一个。要拆开时用
    ``fc2.done_pending_file`` 单独指定。
    """
    value = _config_value(config, "fc2.done_pending_file")
    text = str(value or "").strip()
    if text and not text.startswith("<"):
        return text if os.path.isabs(text) else os.path.join(BOT_FOLDER, text)
    return config_path(config) + DEFAULT_PENDING_SUFFIX


def _time_from_epoch(value) -> str | None:
    """数字时间戳 → 本地时间字符串；转换不了返回 None。"""
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def _parse_datetime_text(text: str) -> datetime | None:
    """ISO 文本 → datetime；认不出来返回 None。"""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def normalize_time(value) -> str:
    """把完成时间归一成 ``YYYY-MM-DD HH:MM:SS``，认不出来就原样保留。

    下载日志里的 ``date_done`` 是 ``str(done_date)``，完成时写名单也传
    datetime；两边都走这里归一，迁移脚本按「编号 + 完成时间」去重时才
    不会因为微秒 / 时区写法不同而重复写入。
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        formatted = _time_from_epoch(value)
        return formatted if formatted is not None else str(value)
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return ""
        parsed = _parse_datetime_text(text)
        if parsed is None:
            return text
    if parsed.tzinfo is not None:
        # 带时区的统一换算成 UTC 再去掉时区，和日志里的 ISO 串对齐。
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def normalize_record(raw) -> dict | None:
    """把一行原始记录规整成标准字段；不是对象或完全空白的行返回 None。"""
    if not isinstance(raw, dict):
        return None
    number = normalize_number(raw.get("number"))
    title = str(raw.get("title") or "").strip()
    info_hash = str(raw.get("info_hash") or "").strip().lower() or None
    done_at = normalize_time(raw.get("done_at"))
    if not number and not title and not info_hash:
        return None
    size = raw.get("size_when_done")
    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0
    return {
        "number": number,
        "title": title,
        "info_hash": info_hash,
        "done_at": done_at,
        "size_when_done": size,
        "source": str(raw.get("source") or SOURCE_UNKNOWN),
    }


def record_key(record: dict) -> tuple:
    """去重身份：有编号按编号，没编号有 hash 按 hash，否则按标题。

    时间一起进 key：同一部片以后又下一次（任务删了重下）是一条新的成功
    记录；同一次完成被重复消费（崩溃 / 重试）则还是同一条。
    """
    number = normalize_number(record.get("number"))
    done_at = normalize_time(record.get("done_at"))
    if number:
        return ("number", number, done_at)
    info_hash = str(record.get("info_hash") or "").strip().lower()
    if info_hash:
        return ("hash", info_hash, done_at)
    return ("title", str(record.get("title") or "").strip(), done_at)


def _read_raw_jsonl(path: str, label: str) -> list[dict]:
    """逐行读 JSONL，只做 JSON 解析；坏行跳过并记 warning，绝不上抛。"""
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError) as err:
                    log.warning(
                        "%s 第 %s 行损坏，已跳过 %s: %s",
                        label,
                        line_number,
                        path,
                        err,
                    )
                    continue
                if not isinstance(raw, dict):
                    log.warning(
                        "%s 第 %s 行不是对象，已跳过 %s", label, line_number, path
                    )
                    continue
                rows.append(raw)
    except FileNotFoundError:
        return []
    except OSError as err:
        log.warning("%s 读取失败 %s: %s", label, path, err)
        return []
    return rows


def _read_jsonl(path: str, label: str) -> list[dict]:
    """读成功名单：逐行 JSON 解析后规整字段，无效行跳过。"""
    records: list[dict] = []
    for raw in _read_raw_jsonl(path, label):
        record = normalize_record(raw)
        if record is None:
            log.warning("%s 有字段无效的记录，已跳过 %s", label, path)
            continue
        records.append(record)
    return records


def _append_jsonl(path: str, payload: dict) -> bool:
    """往文件末尾追加一行；失败只记日志，返回 False。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return True
    except Exception as err:  # noqa: BLE001
        log.warning("追加写失败 %s: %s", path, err)
        return False


class DoneList:
    """append-only 的下载成功名单。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.abspath(path or DEFAULT_NAME)
        self._lock = _shared_lock(self.path)

    # ------------------------------------------------------------ 读

    def all(self) -> list[dict]:
        """按文件顺序返回所有有效记录（坏行跳过）。"""
        return _read_jsonl(self.path, "成功名单")

    def count(self) -> int:
        return len(self.all())

    def find(self, number=None, info_hash=None) -> list[dict]:
        """按编号或 info_hash 精确查。

        - 编号：归一化后精确相等（5000545 / 05000545 同一条）；
        - info_hash：小写精确相等；
        - 两者都没给、或非 FC2 任务没有 hash 时返回空列表——不做标题
          模糊匹配，避免把名字相近的片子误报成「下过」。
        """
        records = self.all()
        key = normalize_number(number)
        if key:
            return [record for record in records if record["number"] == key]
        target = str(info_hash or "").strip().lower()
        if target:
            return [record for record in records if record["info_hash"] == target]
        return []

    def recent(self, limit: int = 10) -> list[dict]:
        """按完成时间倒序取最近几条（时间戳相同保持文件顺序）。"""
        # stable sort：同样的 done_at 不会因为排序把文件先后顺序打乱。
        ordered = sorted(
            self.all(), key=lambda record: record.get("done_at") or "", reverse=True
        )
        return ordered[: max(int(limit), 0)]

    # ------------------------------------------------------------ 写

    def append(
        self,
        number=None,
        title: str = "",
        info_hash: str | None = None,
        done_at=None,
        size_when_done=0,
        source: str = SOURCE_UNKNOWN,
        dry_run: bool = False,
    ) -> str:
        """追加一条成功记录；已存在同一条时返回 duplicate，不重复写。

        「只追加」是硬约束：这里从不打开写模式以外的模式，也不做整文件
        重写，所以并发读、任务删除、容器重建都不会改动已有记录。
        """
        record = normalize_record(
            {
                "number": number,
                "title": title,
                "info_hash": info_hash,
                "done_at": done_at,
                "size_when_done": size_when_done,
                "source": source,
            }
        )
        if record is None:
            return WRITE_FAILED
        with self._lock, _file_lock(self.path) as locked:
            if not locked:
                log.warning("成功名单文件锁不可用，拒绝无锁追加 %s", self.path)
                return WRITE_FAILED
            existing = {
                record_key(item) for item in _read_jsonl(self.path, "成功名单")
            }
            if record_key(record) in existing:
                return WRITE_DUPLICATE
            if dry_run:
                return WRITE_ADDED
            payload = dict(record)
            payload["recorded_ts"] = time.time()
            if not _append_jsonl(self.path, payload):
                return WRITE_FAILED
            return WRITE_ADDED


class DoneDeletionQueue:
    """完成卡待删队列：下载机器人写，Fc2专用读、删自己的卡片、写 ack。

    JSONL 里两种记录::

        {"op": "pending", "number": "5000545", "title": ..., "done_at": ...}
        {"op": "ack", "number": "5000545", "status": "deleted", "ts": ...}

    pending 与 ack 都会追加，文件本身永远只增长；读的时候按顺序回放，
    ack 掉的不再出现在 pending() 里。同一个编号还在待删时重复完成只记
    一次；已经 ack 过之后如果这部片又下一遍、又完成一次，会重新排进
    待删（删的是新卡片）。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.abspath(path or (DEFAULT_NAME + DEFAULT_PENDING_SUFFIX))
        self._lock = _shared_lock(self.path)

    def _replay(self) -> dict[str, dict]:
        """回放文件，返回仍未 ack 的编号 → 最新 pending 记录。"""
        pending: dict[str, dict] = {}
        for raw in _read_raw_jsonl(self.path, "待删卡片队列"):
            op = str(raw.get("op") or "").strip().lower()
            number = normalize_number(raw.get("number"))
            if not number:
                continue
            if op == "ack":
                pending.pop(number, None)
            elif op == "pending":
                pending[number] = {
                    "number": number,
                    "title": str(raw.get("title") or ""),
                    "done_at": normalize_time(raw.get("done_at")),
                }
        return pending

    def pending(self) -> list[dict]:
        with self._lock:
            return list(self._replay().values())

    def request(self, number, title: str = "", done_at=None) -> bool:
        """追加一条待删请求；已在待删队列里就跳过，返回是否新写入。"""
        key = normalize_number(number)
        if not key:
            return False
        with self._lock, _file_lock(self.path) as locked:
            if not locked:
                log.warning("待删卡片队列锁不可用，拒绝无锁追加 %s", self.path)
                return False
            if key in self._replay():
                return False
            payload = {
                "op": "pending",
                "number": key,
                "title": str(title or ""),
                "done_at": normalize_time(done_at),
                "ts": time.time(),
            }
            return _append_jsonl(self.path, payload)

    def ack(self, number, status: str = "deleted") -> bool:
        """标记已处理（删成功 / 已不存在 / 黑名单不动 / 处理失败）。"""
        key = normalize_number(number)
        if not key:
            return False
        with self._lock, _file_lock(self.path) as locked:
            if not locked:
                log.warning("待删卡片队列锁不可用，拒绝无锁追加 %s", self.path)
                return False
            if key not in self._replay():
                return False
            payload = {
                "op": "ack",
                "number": key,
                "status": str(status or "deleted"),
                "ts": time.time(),
            }
            return _append_jsonl(self.path, payload)

    def history(self) -> list[dict[str, Any]]:
        """完整回放结果，方便排查（命令 / 测试用）。"""
        with self._lock:
            return list(self._replay().values())
