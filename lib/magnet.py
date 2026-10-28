"""磁力链接识别与解析工具。

一个 Telegram 消息里可能包含多条磁力链接（每行一条、空格分隔、夹杂文字），
本模块负责把它们全部提取出来、按 info_hash 去重，并解析名称/大小/tracker。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

# 匹配整条磁力链接，遇到空白/引号/尖括号即结束
MAGNET_PATTERN = re.compile(r"magnet:\?[^\s<>\"'`]+", re.IGNORECASE)
# 用于判断一段文本里是否含有磁力链接
MAGNET_HINT = re.compile(r"magnet:\?xt=urn:btih:", re.IGNORECASE)
_BTIH = re.compile(r"^urn:btih:([0-9A-Za-z]{32,40})$", re.IGNORECASE)
# 链接尾部常被中文标点粘连，需要裁掉
_TRAILING_CHARS = ".,;:!?)]}>}）】」』，。；：、！？…"


@dataclass(frozen=True)
class MagnetLink:
    """一条解析后的磁力链接。"""

    raw: str
    info_hash: str  # 统一为 40 位小写 hex，无法识别时为空字符串
    name: str | None = None
    size: int | None = None
    trackers: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        return self.name or self.info_hash[:12] or "未命名任务"

    @property
    def is_valid(self) -> bool:
        return bool(self.info_hash)


def normalize_infohash(value: str) -> str:
    """把 btih 转成统一的 40 位小写 hex；支持 32 位 base32。"""
    value = (value or "").strip()
    if len(value) == 40 and re.fullmatch(r"[0-9A-Fa-f]{40}", value):
        return value.lower()
    if len(value) == 32:
        try:
            raw = base64.b32decode(value.upper())
        except (binascii.Error, ValueError):
            return ""
        return raw.hex()
    return ""


def parse_magnet(raw: str) -> MagnetLink | None:  # noqa: C901
    """解析单条磁力链接，返回 None 表示不是有效的 BT 磁力链接。"""
    raw = raw.strip().rstrip(_TRAILING_CHARS)
    if not MAGNET_HINT.search(raw):
        return None
    try:
        query = parse_qs(urlparse(raw).query, keep_blank_values=False)
    except ValueError:
        return None

    info_hash = ""
    for xt in query.get("xt", []):
        match = _BTIH.match(xt.strip())
        if match:
            info_hash = normalize_infohash(match.group(1))
            if info_hash:
                break

    name = None
    for dn in query.get("dn", []):
        candidate = " ".join(dn.split()).strip()
        if candidate:
            name = candidate
            break

    size = None
    for xl in query.get("xl", []):
        try:
            size = int(xl)
            break
        except (TypeError, ValueError):
            continue

    trackers = tuple(tr for tr in query.get("tr", []) if tr)

    if not info_hash:
        return None
    return MagnetLink(
        raw=raw, info_hash=info_hash, name=name, size=size, trackers=trackers
    )


def extract_magnets(text: str | None, limit: int = 100) -> list[MagnetLink]:
    """从任意文本中提取全部磁力链接，按 info_hash 去重并保持原有顺序。"""
    if not text:
        return []
    links: list[MagnetLink] = []
    seen: set[str] = set()
    for match in MAGNET_PATTERN.finditer(text):
        link = parse_magnet(match.group(0))
        if not link or not link.is_valid:
            continue
        key = link.info_hash
        if key in seen:
            continue
        seen.add(key)
        links.append(link)
        if len(links) >= limit:
            break
    return links


def contains_magnet(text: str | None) -> bool:
    return bool(text and MAGNET_HINT.search(text))


def build_magnet(info_hash: str, name: str | None = None) -> str:
    """根据 info_hash 反向拼一条磁力链接（用于展示/重试）。"""
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    if name:
        from urllib.parse import quote

        magnet += f"&dn={quote(name)}"
    return magnet
