"""FC2 专用机器人添加任务的共享归属记录；只追加 hash、编号和标题。"""

from __future__ import annotations

import os

from lib.magnet import parse_magnet
from models.DoneList import (
    WRITE_ADDED,
    WRITE_DUPLICATE,
    WRITE_FAILED,
    _append_jsonl,
    _config_value,
    _file_lock,
    _read_raw_jsonl,
    _shared_lock,
    config_path as done_config_path,
)
from models.Fc2Blacklist import config_path as blacklist_config_path
from models.Fc2Blacklist import normalize_number

DEFAULT_NAME = "fc2_claims.jsonl"


def config_path(config) -> str:
    """从既有共享名单目录派生路径，不引入新的配置项。"""
    for key, resolve in (
        ("fc2.done_file", done_config_path),
        ("fc2.blacklist_file", blacklist_config_path),
    ):
        text = str(_config_value(config, key) or "").strip()
        if text and not text.startswith("<"):
            path = resolve(config)
            if path:
                return os.path.join(os.path.dirname(os.path.abspath(path)), DEFAULT_NAME)
    return os.path.join(
        os.path.dirname(os.path.abspath(done_config_path(config))), DEFAULT_NAME
    )


class Fc2Claims:
    """按 info_hash 精确认领，复用成功名单的 flock + O_APPEND。"""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self._lock = _shared_lock(self.path)

    def _records(self) -> dict[str, dict]:
        records: dict[str, dict] = {}
        for raw in _read_raw_jsonl(self.path, "FC2 任务归属"):
            info_hash = str(raw.get("info_hash") or "").strip().lower()
            if info_hash:
                records[info_hash] = {
                    "info_hash": info_hash,
                    "number": normalize_number(raw.get("number")),
                    "title": str(raw.get("title") or ""),
                }
        return records

    def find(self, info_hash: str | None) -> dict | None:
        key = str(info_hash or "").strip().lower()
        if not key:
            return None
        with self._lock:
            return self._records().get(key)

    def append(self, info_hash: str | None, number=None, title: str = "") -> str:
        key = str(info_hash or "").strip().lower()
        if not key:
            return WRITE_FAILED
        title = str(title or "").strip()
        # 单条预览可能把整条磁力当作标题；只保存 dn，不保存 tracker 参数。
        if title.lower().startswith(("magnet:", "http://", "https://")):
            link = parse_magnet(title)
            title = str(link.name or "") if link is not None else ""
        with self._lock, _file_lock(self.path) as locked:
            if not locked:
                return WRITE_FAILED
            previous = self._records().get(key) or {}
            record = {
                "info_hash": key,
                "number": normalize_number(number) or previous.get("number"),
                "title": title or previous.get("title") or "",
            }
            if record == previous:
                return WRITE_DUPLICATE
            return WRITE_ADDED if _append_jsonl(self.path, record) else WRITE_FAILED
