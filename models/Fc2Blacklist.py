"""FC2 黑名单：编号 + 名字 + 记入时间 + 来源。

独立落盘，不放在 fc2_state.json 里：状态文件被删 / 重置 / forget 之后，
黑名单不能跟着丢。fc2bot 与 torrentino 是两个容器、各挂自己的 config，
要把黑名单共享给两边，就在两个 config 里把 fc2.blacklist_file 指到同一个
共享卷上的文件（部署改动见 README / 交付说明）。

存储用标准库 JSON + 原子替换，跨进程再套一把 fcntl.flock：
文件很小、一次只写一条，JSON 比 sqlite3 更简单、可读、可直接手改；
flock 保证两个容器同时写（一边点不感兴趣、一边删任务）时不会互相覆盖。
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from contextlib import contextmanager

try:  # pragma: no cover - 本项目只在 Linux 容器里跑，非 POSIX 平台退化为单进程锁
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from lib.constants import BOT_FOLDER, CONFIG_FILE
from lib.func import get_logger

log = get_logger("Fc2Blacklist")

DEFAULT_NAME = "fc2_blacklist.json"

# 来源：不感兴趣按钮 / 机器人里删任务 / 手动拉黑命令 / 旧 muted 迁移
SOURCE_MUTE = "fc2no"
SOURCE_DELETE = "torrent_delete"
SOURCE_MANUAL = "manual"
SOURCE_LEGACY = "legacy_muted"

SOURCE_LABELS = {
    SOURCE_MUTE: "不感兴趣",
    SOURCE_DELETE: "手动删除",
    SOURCE_MANUAL: "手动拉黑",
    SOURCE_LEGACY: "旧 muted 迁移",
}


def normalize_number(value) -> str | None:
    """FC2 编号归一成不带前导零的十进制串；不是纯数字就返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        number = int(text)
    except (TypeError, ValueError):
        return None
    return str(number) if number > 0 else None


def source_label(source) -> str:
    return SOURCE_LABELS.get(str(source or ""), str(source or "未知"))


def default_path() -> str:
    """默认和 fc2_state.json 同一个目录（config/）。"""
    return os.path.join(os.path.dirname(CONFIG_FILE), DEFAULT_NAME)


def default_state_path() -> str:
    return os.path.join(os.path.dirname(CONFIG_FILE), "fc2_state.json")


def config_path(config) -> str | None:
    """fc2.blacklist_file → 绝对路径；没配或是占位值就返回 None 用默认路径。"""
    value = config.get("fc2.blacklist_file") if config is not None else None
    text = str(value or "").strip()
    if not text or text.startswith("<"):
        return None
    return text if os.path.isabs(text) else os.path.join(BOT_FOLDER, text)


def _host() -> str:
    """容器标识：Docker 里 hostname 就是容器自己的名字/id。"""
    try:
        return socket.gethostname() or "unknown"
    except OSError:  # pragma: no cover
        return "unknown"


def state_key(path: str) -> str:
    """迁移标记的键 = 主机名 + 状态文件绝对路径。

    只用绝对路径不行：fc2bot 与 torrentino 的 state 都挂在
    /usr/src/app/config/fc2_state.json，先跑的那个会把标记占掉，
    另一个容器里真实的 muted 就永远迁不进共享黑名单。
    """
    return f"{_host()}:{os.path.abspath(path)}"


def _shared_lock(path: str) -> threading.RLock:
    """同一路径在进程内共用一把锁（多个 handler 各自 new 一个实例）。"""
    with Fc2Blacklist._locks_guard:
        lock = Fc2Blacklist._locks.get(path)
        if lock is None:
            lock = threading.RLock()
            Fc2Blacklist._locks[path] = lock
        return lock


class Fc2Blacklist:
    """单文件黑名单。

    结构 {"version":1,"entries":{编号:{...}},"unblocked":[...],
    "legacy_migrated":{state 绝对路径:[已迁编号]}}：entries 是现行黑名单，
    unblocked 是 /fc2unblock 留下的「已解黑墓碑」（迁移时跳过，免得把解黑
    的编号迁回来），legacy_migrated 按 state 绝对路径记已迁过的编号快照。
    """

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: str | None = None, state_path: str | None = None) -> None:
        self.path = os.path.abspath(path or default_path())
        self.state_path = os.path.abspath(state_path or default_state_path())
        self._lock = _shared_lock(self.path)
        self._lock_warned = False
        self.entries: dict[str, dict] = {}
        self.unblocked: set[str] = set()
        self.legacy_migrated: dict[str, set[str]] = {}
        self._legacy_unknown: set[str] = set()
        self._legacy_upgrade = False
        self._mtime: int = 0
        self._read_ok = True
        self._stale = False
        self._corrupt_stamp: int | None = None
        self._corrupt_unbacked = False
        self.load()
        self.migrate_legacy()

    # ------------------------------------------------------------ 读

    def _read_raw(self):
        """读原始字节和前后 stat，供内容/mtime 一致性校验。"""
        before = os.stat(self.path)
        with open(self.path, "rb") as handle:
            raw = handle.read()
        after = os.stat(self.path)
        return before, raw, after

    @staticmethod
    def _same_file(before, after) -> bool:
        return (
            before.st_mtime_ns == after.st_mtime_ns
            and before.st_ino == after.st_ino
            and before.st_size == after.st_size
        )

    def _disk(self) -> tuple[dict, int, bool]:
        """返回（内容, 内容对应的 mtime, 读取是否成功），三者来自同一份快照。

        先 stat、再读、再 stat，前后不一致就重读。否则会出现「读到旧
        内容、却记下别人写后的新 mtime」，之后 refresh() 认为文件没变，
        新编号永远查不到。

        I/O 失败（权限 / EIO）不能当成「确定为空」：mtime 记 0，下一次
        refresh() 一定重读，读不到也不会拿空表去覆盖磁盘上的真实内容。
        这里按「本次放行」（fail-open）处理：共享卷一时抽风不该把所有
        FC2 下载都拦死，但绝不缓存这次失败。
        """
        for _ in range(5):
            try:
                before, raw, after = self._read_raw()
            except FileNotFoundError:
                return {}, 0, True
            except OSError as err:
                log.warning("FC2 黑名单读取失败 %s: %s", self.path, err)
                return {}, 0, False
            mtime = after.st_mtime_ns
            if not self._same_file(before, after):
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as err:  # noqa: BLE001
                self._backup_corrupt(err, raw)
                return {}, mtime, True
            if not isinstance(data, dict):
                self._backup_corrupt(ValueError("顶层不是 JSON 对象"), raw)
                return {}, mtime, True
            return data, mtime, True
        # 文件一直有人在改：这次不记 mtime，下一次访问强制重读，
        # 绝不会把旧内容配上新 mtime。
        log.warning("FC2 黑名单反复变化，本次按空表读 %s", self.path)
        return {}, 0, True

    def _backup_corrupt(self, err, raw: bytes | None = None) -> None:
        """坏文件先另存一份再继续。

        备份内容直接用这次读到的原始字节，不再二次读文件：两个进程同时
        读到坏文件时，另一个可能已经写回新表，二次读会把新表当成备份，
        唯一的坏文件内容就没了。文件名撞车时换后缀，绝不覆盖已有备份。
        连备份都做不了（磁盘满 / 权限）就拒绝写，宁可写不进去也不能丢。
        """
        try:
            stamp = os.stat(self.path).st_mtime_ns
        except OSError:  # pragma: no cover
            stamp = 0
        if stamp and self._corrupt_stamp == stamp:
            return
        self._corrupt_stamp = stamp
        base = f"{self.path}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"
        try:
            payload = raw
            if payload is None:  # pragma: no cover - 兼容直接调用
                with open(self.path, "rb") as handle:
                    payload = handle.read()
            index = 0
            while True:
                backup = base if index == 0 else f"{base}.{index}"
                try:
                    with open(backup, "xb") as handle:
                        handle.write(payload)
                    break
                except FileExistsError:
                    index += 1
                    if index > 50:  # pragma: no cover
                        raise
        except OSError as copy_err:  # pragma: no cover
            self._corrupt_unbacked = True
            log.error(
                "FC2 黑名单文件损坏且备份失败，暂停写入 %s（%s / %s）",
                self.path,
                err,
                copy_err,
            )
            return
        log.warning("FC2 黑名单文件损坏，已另存 %s（原因：%s）", backup, err)

    @staticmethod
    def _normalize_entries(raw) -> dict[str, dict]:
        entries: dict[str, dict] = {}
        if not isinstance(raw, dict):
            return entries
        for key, value in raw.items():
            record = value if isinstance(value, dict) else {}
            number = normalize_number(record.get("number") or key)
            if not number:
                continue
            entries[number] = {
                "number": number,
                "title": str(record.get("title") or ""),
                "added_ts": float(record.get("added_ts") or 0),
                "source": str(record.get("source") or ""),
                "note": str(record.get("note") or ""),
            }
        return entries

    @staticmethod
    def _normalize_legacy(raw) -> tuple[dict[str, set[str]], set[str], bool]:
        """legacy_migrated 兼容读入：新格式 {路径:[编号快照]}，老格式是列表。

        返回（按路径的编号快照, 只有布尔式标记的路径, 是否需要重写结构）。
        老式标记只说明「这份 state 迁过」，没有快照内容：不落空快照，而是
        记在 unknown 里，迁移时按「当前 muted 已全部认领」升级，不能当普通
        空快照再迁一遍——旧共享表可能根本没有 unblocked 字段。
        """
        snapshots: dict[str, set[str]] = {}
        unknown: set[str] = set()
        upgrade = False
        if isinstance(raw, dict):
            items = list(raw.items())
        elif isinstance(raw, (list, tuple)):
            items = [(item, None) for item in raw]
            upgrade = bool(raw)
        else:
            return snapshots, unknown, bool(raw)
        for key, value in items:
            text = str(key or "").strip()
            if not text:
                continue
            path = text
            if ":" in text:
                _, _, rest = text.partition(":")
                if rest.startswith("/"):
                    path = rest  # 老键 hostname:绝对路径，按路径认领
                    upgrade = True
            path = os.path.abspath(path) if os.path.isabs(path) else path
            if isinstance(value, (list, tuple, set)):
                numbers = {
                    number
                    for number in (normalize_number(item) for item in value)
                    if number
                }
                snapshots.setdefault(path, set()).update(numbers)
            else:
                # 老式字符串 / 空值：只记「迁过」，不写空快照，否则会被
                # 当成「这份 state 确实什么都没迁过」而重新迁一遍。
                unknown.add(path)
                upgrade = True
        return snapshots, unknown, upgrade

    def _read(self) -> dict:
        data, mtime, ok = self._disk()
        legacy_migrated, legacy_unknown, legacy_upgrade = self._normalize_legacy(
            data.get("legacy_migrated")
        )
        return {
            "entries": self._normalize_entries(data.get("entries")),
            "unblocked": {
                number
                for number in (
                    normalize_number(item) for item in (data.get("unblocked") or [])
                )
                if number
            },
            "legacy_migrated": legacy_migrated,
            "_legacy_unknown": legacy_unknown,
            "_legacy_upgrade": legacy_upgrade,
            "_mtime": mtime,
            "_ok": ok,
        }

    def _apply(self, state: dict) -> None:
        self.entries = state["entries"]
        self.unblocked = set(state["unblocked"])
        self.legacy_migrated = {
            path: set(numbers) for path, numbers in state["legacy_migrated"].items()
        }
        self._legacy_unknown = set(state.get("_legacy_unknown") or ())
        self._legacy_upgrade = bool(state.get("_legacy_upgrade"))
        self._mtime = int(state.get("_mtime") or 0)
        self._read_ok = bool(state.get("_ok", True))
        # 读失败必须让下次 refresh() 强制重读，不能靠 mtime 数值判断：
        # 读失败记的 _mtime=0 与文件真实 mtime 为 0 时会永远跳过重读。
        self._stale = not self._read_ok

    def _mtime_now(self) -> int:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return 0

    def load(self) -> None:
        with self._lock:
            self._apply(self._read())

    def refresh(self) -> None:
        """文件被另一个进程改过（mtime 变了）或上次读失败就重读。"""
        if self._stale or self._mtime_now() != self._mtime:
            self.load()

    def get(self, number) -> dict | None:
        key = normalize_number(number)
        if not key:
            return None
        self.refresh()
        record = self.entries.get(key)
        return dict(record) if record else None

    def contains(self, number) -> bool:
        return self.get(number) is not None

    def lookup(self, number) -> tuple[dict | None, bool]:
        """一次刷新里同时给出条目和读盘结果。

        「有没有条目」和「这次读是否成功」必须来自同一次 refresh()：
        先 read_ok() 再 contains() 会被中间那次刷新插队，读失败时仍拿
        旧的成功值去判「不在黑名单」。
        """
        record = self.get(number)
        return record, self._read_ok

    def read_ok(self) -> bool:
        """最近一次读盘是否成功；失败时 contains() 为 False 不代表条目不存在。"""
        return self._read_ok

    def all(self) -> list[dict]:
        """按记入时间倒序，给机器人列出来看。"""
        self.refresh()
        return sorted(
            (dict(record) for record in self.entries.values()),
            key=lambda item: float(item.get("added_ts") or 0),
            reverse=True,
        )

    # ------------------------------------------------------------ 写

    @contextmanager
    def _file_lock(self):
        if fcntl is None:  # pragma: no cover
            yield True
            return
        handle = None
        locked = False
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            handle = open(f"{self.path}.lock", "a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError as err:
            # 锁不可写时仍可读共享表，但不能无锁覆盖其他进程的修改。
            if not self._lock_warned:
                log.warning("FC2 黑名单锁不可用，暂停写入 %s: %s", self.path, err)
                self._lock_warned = True
        try:
            yield locked
        finally:
            if handle is not None:
                try:
                    if locked:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    def _save_locked(self, state: dict) -> bool:
        """落盘成功返回 True；失败（含读不出来的情况下拒绝覆盖）返回 False。"""
        if self._corrupt_unbacked:  # pragma: no cover
            log.error("FC2 黑名单文件损坏且未备份，拒绝覆盖 %s", self.path)
            return False
        if not state.get("_ok", True):
            log.warning("FC2 黑名单读取失败，拒绝用空表覆盖 %s", self.path)
            return False
        data = {
            "version": 1,
            "entries": state["entries"],
            "unblocked": sorted(state["unblocked"]),
            "legacy_migrated": {
                path: sorted(numbers)
                for path, numbers in sorted(state["legacy_migrated"].items())
            },
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=1)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            state["_mtime"] = self._mtime_now()
        except Exception as err:  # noqa: BLE001
            log.warning("FC2 黑名单写盘失败 %s: %s", self.path, err)
            return False
        return True

    def add(self, number, title="", source=SOURCE_MANUAL, note="") -> dict | None:
        """记一条；已存在的补名字、并把「旧 muted 迁移」升级成真实来源。"""
        key = normalize_number(number)
        if not key:
            return None
        with self._lock, self._file_lock() as locked:
            state = self._read()
            entries = state["entries"]
            record = entries.get(key)
            changed = False
            if record is None:
                record = {
                    "number": key,
                    "title": str(title or ""),
                    "added_ts": time.time(),
                    "source": str(source or SOURCE_MANUAL),
                    "note": str(note or ""),
                }
                entries[key] = record
                changed = True
            else:
                if title and not record.get("title"):
                    record["title"] = str(title)
                    changed = True
                if (
                    source
                    and source != SOURCE_LEGACY
                    and record.get("source") == SOURCE_LEGACY
                ):
                    record["source"] = str(source)
                    changed = True
                if note and not record.get("note"):
                    record["note"] = str(note)
                    changed = True
            if key in state["unblocked"]:
                # 用户明确重新拉黑，墓碑作废，否则下次迁移还会把他当已解黑跳过。
                state["unblocked"].discard(key)
                changed = True
            if changed:
                # 写不进去就不能假装加上了：调用方（fc2no 回调）要如实告诉用户。
                # 读成功时也不 _apply，否则内存里像是成功、磁盘上没有。
                if not locked or not self._save_locked(state):
                    if not state.get("_ok", True):
                        # 这次读盘本身失败：state 里已被 add 改过的条目磁盘上
                        # 并不存在，直接重读一次，如实反映磁盘（读不到就是空表）。
                        self.load()
                    return None
            self._apply(state)
            return dict(record)

    def remove(self, number) -> bool:
        """删掉一条，并在共享表里留下「已解黑墓碑」。

        墓碑必须先于各容器的迁移生效：两个容器的 state 各有一份 muted，
        只删黑名单条目的话，另一份 state 下次启动还会把它迁回来。
        """
        key = normalize_number(number)
        if not key:
            return False
        with self._lock, self._file_lock() as locked:
            state = self._read()
            existed = key in state["entries"]
            changed = existed or key not in state["unblocked"]
            state["entries"].pop(key, None)
            state["unblocked"].add(key)
            if changed and (not locked or not self._save_locked(state)):
                # 墓碑没落盘就不能留在内存里假装成功，下次访问以磁盘为准。
                self.load()
                return False
            self._apply(state)
            return existed

    # ------------------------------------------------------------ 迁移

    def marker_path(self) -> str:
        """迁移标记文件：放在 state 同目录（config 是 bind mount，容器重建还在）。

        现在只是「这份 state 至少迁过一次」的记录，迁移本身是幂等补齐，
        不再靠它决定要不要迁：老版本在 fc2bot / torrentino 上留下的标记
        如果还当成闸门，被判错的容器就永远迁不进自己的 muted。
        """
        return f"{self.state_path}.fc2_blacklist_migrated"

    def _marker_exists(self) -> bool:
        return os.path.exists(self.marker_path())

    def _write_marker(self) -> bool:
        path = self.marker_path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = f"{path}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump({"state": self.state_path, "ts": time.time()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            return True
        except OSError as err:  # pragma: no cover
            log.warning("FC2 黑名单迁移标记写失败 %s: %s", path, err)
            return False

    def _legacy_claimed(self, state: dict, path: str) -> bool:
        """这份 state 是否只带着老版本（hostname:路径）的布尔式标记。"""
        return path in state.get("_legacy_unknown", set())

    def migrate_legacy(self) -> int:
        """把旧 fc2_state.json 里只有编号的 muted 补齐进黑名单（幂等）。

        每次都拿 state.muted 跟共享黑名单对一遍，把「黑名单里没有、也不是
        已解黑墓碑」的编号补进去：两个容器即便 state 路径完全一样，也各自
        补自己那份，谁都不会吞掉对方；重复初始化只是空转，不会产生重复
        条目。名字从 seen 的 title 补，来源记「旧 muted 迁移」。

        解黑不复活靠共享表里的 unblocked 墓碑（/fc2unblock 时写入）和
        legacy_migrated 里按 state 路径记下的编号快照，不再依赖本地标记文件，
        所以换 hostname 重建、config 只读（标记写不了）都不会把编号迁回来。

        待迁 = 当前 state.muted - 该路径的已迁快照 - 已解黑墓碑；只有落盘
        成功才把快照更新为「已记录 ∪ 待迁」，不是直接等于当前 muted，这样
        从 state 里删掉的编号也不会因为「快照里没有」再被迁回来。
        """
        path = os.path.abspath(self.state_path)
        if not os.path.isfile(path):
            return 0
        with self._lock, self._file_lock() as locked:
            state = self._read()
            if not state.get("_ok", True):
                self._apply(state)
                return 0
            upgrade = bool(state.get("_legacy_upgrade"))
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    old = json.load(handle)
            except Exception as err:  # noqa: BLE001
                log.warning("读旧 FC2 状态失败，暂不迁移 %s: %s", path, err)
                self._apply(state)
                return 0
            if not isinstance(old, dict):
                old = {}
            seen = old.get("seen") if isinstance(old.get("seen"), dict) else {}
            current = {
                number
                for number in (
                    normalize_number(item) for item in (old.get("muted") or [])
                )
                if number
            }
            recorded = state["legacy_migrated"].get(path)
            if recorded is None and self._legacy_claimed(state, path):
                # 老版本已完整迁过这份 state：当前 muted 全部算作已迁快照，
                # 一条都不重迁（旧共享表没有 unblocked 时也不会复活）。
                snapshot = set(current)
                pending: set[str] = set()
                changed = True
            else:
                snapshot = set(recorded or set())
                pending = current - snapshot - set(state["unblocked"])
                changed = bool(pending)
                snapshot |= pending
            if changed or upgrade:
                state["legacy_migrated"][path] = snapshot
                changed = True
            added = 0
            for number in sorted(pending):
                record = state["entries"].get(number)
                title = ""
                old_record = seen.get(number) or seen.get(str(number))
                if isinstance(old_record, dict):
                    title = str(old_record.get("title") or "")
                if record is None:
                    state["entries"][number] = {
                        "number": number,
                        "title": title,
                        "added_ts": time.time(),
                        "source": SOURCE_LEGACY,
                        "note": "",
                    }
                    added += 1
                elif title and not record.get("title"):
                    record["title"] = title
                    changed = True
            # 只有真正落盘成功才能写迁移标记，否则下次启动重试，
            # 不能出现「标记有了、编号没进黑名单」的永久漏迁。
            if changed and (not locked or not self._save_locked(state)):
                log.warning("FC2 黑名单迁移落盘失败，下次启动重试 %s", path)
                # 内存不能装作迁好了：以磁盘为准，下次启动还会重试。
                self.load()
                return 0
            if changed:
                state["_legacy_upgrade"] = False
            if not self._marker_exists():
                self._write_marker()
            self._apply(state)
            if added:
                log.info("旧 muted 迁移进 FC2 黑名单 %s 条（来自 %s）", added, path)
            return added
