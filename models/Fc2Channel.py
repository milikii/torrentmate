"""FC2 频道推送。

定时读 sukebei.nyaa.si 的 FC2-PPV RSS，用 FC2 官网核验条目并取官方图，
按发布时间升序推进一个私有频道；卡片只有「下载 / 不感兴趣」两个按钮。
只服务 bot.super_user 一个人，状态落盘到 config/fc2_state.json。
"""

from __future__ import annotations

import asyncio
import html as html_module
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode

from lib.constants import CONFIG_FILE
from lib.func import escape_html, get_logger
from models.BotConfigurator import BotConfigurator

log = get_logger("Fc2Channel")

FEED_URL = "https://sukebei.nyaa.si/?page=rss&q=FC2-PPV"
ARTICLE_URL = "https://adult.contents.fc2.com/article/{}/"
FC2PPVDB_URL = "https://fc2ppvdb.com/articles/{}"
NYAA_NS = "{https://sukebei.nyaa.si/xmlns/nyaa}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://exodus.desync.com:6969/announce",
)
DEFAULT_INTERVAL = 600
MIN_INTERVAL = 120
DEFAULT_BACKFILL = 10
PREVIEW_LIMIT = 7
IMAGE_MAX_BYTES = 5 * 1024 * 1024
PUSH_DELAY = 2
REQUEST_TIMEOUT = 20

NUMBER_RE = re.compile(r"(?i)fc2[\s._-]*(?:ppv)?[\s._-]*(\d{5,8})")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_IMAGE_RE = re.compile(
    r"<meta[^>]+property=[\"']og:image[\"'][^>]*content=[\"']([^\"']+)[\"']", re.I
)
THUMB_RE = re.compile(
    r"[\"']//contents-thumbnail2\.fc2\.com/w480/([^\"']+)[\"']", re.I
)
IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:$|\?)", re.I)


def extract_number(title: str) -> str | None:
    """从标题里提 FC2-PPV 编号，统一成不带前导零的十进制串。"""
    match = NUMBER_RE.search(str(title or ""))
    if not match:
        return None
    return str(int(match.group(1)))


def parse_feed(text: str | bytes) -> list[dict]:
    """解析 nyaa RSS，丢弃缺字段的条目，返回按 pubDate 升序的列表。"""
    root = ET.fromstring(text)
    items = []
    for node in root.iterfind("channel/item"):
        title = (node.findtext("title") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        pub_date = node.findtext("pubDate")
        info_hash = (node.findtext(f"{NYAA_NS}infoHash") or "").strip()
        if not title or not guid or not pub_date or not info_hash:
            continue
        try:
            pub_ts = parsedate_to_datetime(pub_date).timestamp()
        except (TypeError, ValueError, OverflowError):
            continue
        items.append(
            {
                "title": title,
                "guid": guid,
                "pub_ts": pub_ts,
                "hash": info_hash,
                "size": (node.findtext(f"{NYAA_NS}size") or "").strip(),
            }
        )
    items.sort(key=lambda item: item["pub_ts"])
    return items


def _magnet(info_hash: str, title: str) -> str:
    """用 nyaa 的 infoHash 现场拼磁力链接。"""
    name = quote(str(title or ""), safe="")
    trackers = "".join(f"&tr={quote(tracker, safe='')}" for tracker in TRACKERS)
    return f"magnet:?xt=urn:btih:{str(info_hash or '').strip()}&dn={name}{trackers}"


def _is_placeholder(value) -> bool:
    """配置里的 <channel_id> 之类占位值等同于未配置。"""
    text = str(value or "").strip()
    return not text or text.startswith("<")


def _http_get(url: str, proxy: str | None, verify_tls: bool):
    """统一出口：FC2、fc2ppvdb、nyaa 都走配置代理（NAS 直连已死，直连会超时）。"""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    return requests.get(
        url,
        headers={"User-Agent": USER_AGENT},
        proxies=proxies,
        verify=bool(verify_tls),
        timeout=REQUEST_TIMEOUT,
    )


def _absolute(url: str) -> str:
    url = html_module.unescape(str(url or "")).strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return ""


def _append_unique(urls: list[str], url: str) -> None:
    if url and url not in urls:
        urls.append(url)


def _page_title(html: str) -> str:
    match = TITLE_RE.search(html or "")
    return match.group(1).strip() if match else ""


def _official_images(html: str) -> list[str]:
    """封面优先，再把 w480 缩略图还原成原图，按页面顺序去重。"""
    images: list[str] = []
    for match in OG_IMAGE_RE.finditer(html or ""):
        _append_unique(images, _absolute(match.group(1)))
    for match in THUMB_RE.finditer(html or ""):
        _append_unique(images, f"https://{match.group(1).strip()}")
    return images


def _fc2ppvdb_images(html: str) -> list[str]:
    images: list[str] = []
    for src in IMG_SRC_RE.findall(html or ""):
        url = _absolute(src)
        if url and IMAGE_EXT_RE.search(url):
            _append_unique(images, url)
    return images


def _fetch_official(number: str, proxy: str | None, verify_tls: bool) -> list[str]:
    """官网 HTTP 200 且标题含编号才算存在，取不到就返回空。"""
    try:
        response = _http_get(ARTICLE_URL.format(number), proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        log.debug("FC2 官网请求失败 %s: %s", number, err)
        return []
    if response.status_code != 200:
        return []
    html = response.text or ""
    if not re.search(rf"FC2-PPV-{number}", _page_title(html), re.I):
        return []
    return _official_images(html)


def _fetch_fc2ppvdb(number: str, proxy: str | None, verify_tls: bool) -> list[str]:
    try:
        response = _http_get(FC2PPVDB_URL.format(number), proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        log.debug("fc2ppvdb 请求失败 %s: %s", number, err)
        return []
    if response.status_code != 200:
        return []
    return _fc2ppvdb_images(response.text or "")


def _preview_images(
    number: str, proxy: str | None = None, verify_tls: bool = True
) -> list[str]:
    """官网图优先用满 7 张，不足再用 fc2ppvdb 补，官网不过就返回空。"""
    images = _fetch_official(number, proxy, verify_tls)[:PREVIEW_LIMIT]
    if not images or len(images) >= PREVIEW_LIMIT:
        return images
    try:
        extra = _fetch_fc2ppvdb(number, proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        log.debug("fc2ppvdb 补充预览图失败 %s: %s", number, err)
        return images
    for url in extra:
        if len(images) >= PREVIEW_LIMIT:
            break
        _append_unique(images, url)
    return images


def _download_images(
    urls: list[str], proxy: str | None = None, verify_tls: bool = True
) -> list[bytes]:
    """图片字节下载到内存，非图片和超过 5MB 的丢弃。"""
    photos = []
    for url in urls:
        try:
            response = _http_get(url, proxy, verify_tls)
        except Exception as err:  # noqa: BLE001
            log.debug("FC2 图片下载失败 %s: %s", url, err)
            continue
        if response.status_code != 200:
            continue
        content_type = str((response.headers or {}).get("Content-Type", "")).lower()
        if not content_type.startswith("image/"):
            continue
        if len(response.content) > IMAGE_MAX_BYTES:
            continue
        photos.append(response.content)
    return photos


def _fetch_entries(proxy: str | None = None) -> list[dict]:
    try:
        response = _http_get(FEED_URL, proxy, True)
    except Exception as err:  # noqa: BLE001
        log.warning("FC2 RSS 拉取失败: %s", err)
        return []
    if response.status_code != 200:
        log.warning("FC2 RSS 返回 %s", response.status_code)
        return []
    try:
        return parse_feed(response.text or "")
    except ET.ParseError as err:
        log.warning("FC2 RSS 解析失败: %s", err)
        return []


def _clean_title(title: str, number: str) -> str:
    text = re.sub(r"^\s*\+{2,}\s*", "", str(title or ""))
    text = re.sub(rf"(?i)fc2[\s._-]*(?:ppv)?[\s._-]*0*{number}", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return escape_html(text[:900]) or f"FC2-PPV-{number}"


def _caption(entry: dict, number: str) -> str:
    size = escape_html(entry.get("size") or "-")
    when = datetime.fromtimestamp(float(entry.get("pub_ts") or 0)).strftime(
        "%Y-%m-%d %H:%M"
    )
    lines = [
        f"<b>FC2-PPV-{number}</b>",
        "",
        _clean_title(entry.get("title") or "", number),
        f"💾 {size} · 🕒 {escape_html(when)}",
    ]
    guid = entry.get("guid")
    if guid:
        lines.append(f'<a href="{escape_html(guid)}">nyaa</a>')
    return "\n".join(lines)


def _keyboard(number: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⬇️ 下载", callback_data=f"fc2dl:{number}"),
                InlineKeyboardButton("🚫 不感兴趣", callback_data=f"fc2no:{number}"),
            ]
        ]
    )


class Fc2Store:
    """单用户 FC2 状态文件：last_ts + seen + muted。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.path.join(
            os.path.dirname(CONFIG_FILE), "fc2_state.json"
        )
        self._lock = threading.RLock()
        self.last_ts = 0.0
        self.seen: dict[str, dict] = {}
        self.muted: set[str] = set()
        self.load()

    def load(self) -> None:
        with self._lock:
            self.last_ts = 0.0
            self.seen = {}
            self.muted = set()
            if not os.path.isfile(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                self.last_ts = float(data.get("last_ts") or 0)
                self.seen = {
                    str(key): value for key, value in (data.get("seen") or {}).items()
                }
                self.muted = {str(item) for item in (data.get("muted") or [])}
            except Exception as err:  # noqa: BLE001
                log.warning("FC2 状态文件损坏，按空状态继续 %s: %s", self.path, err)
                self.last_ts, self.seen, self.muted = 0.0, {}, set()

    def _write(self) -> None:
        data = {
            "last_ts": self.last_ts,
            "seen": self.seen,
            "muted": sorted(self.muted),
        }
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except Exception as err:  # noqa: BLE001
            log.warning("FC2 状态写盘失败 %s: %s", self.path, err)

    def save(self) -> None:
        with self._lock:
            self._write()

    def entry(self, number: str) -> dict | None:
        with self._lock:
            return self.seen.get(str(number))

    def remember(self, number: str, entry: dict, messages=None) -> None:
        with self._lock:
            self.seen[str(number)] = {
                "hash": entry.get("hash") or "",
                "title": entry.get("title") or "",
                "pub_ts": float(entry.get("pub_ts") or 0),
                "messages": list(messages or []),
            }

    def mute(self, number: str) -> None:
        with self._lock:
            self.muted.add(str(number))
            self._write()


class Fc2Pusher:
    """轮询 nyaa FC2 RSS，核验后把新条目推进频道。"""

    def __init__(self, bot, config=None, store=None) -> None:
        self.bot = bot
        self.config = config or BotConfigurator()
        self.store = store or Fc2Store()

    def _setting(self, key: str, default=None):
        return self.config.get(f"fc2.{key}", default)

    def _proxy(self) -> str | None:
        proxy = self._setting("proxy")
        return None if _is_placeholder(proxy) else str(proxy)

    async def run(self) -> None:
        channel_id = self._setting("channel_id")
        if _is_placeholder(channel_id):
            log.warning("fc2.channel_id 未配置，FC2 频道推送任务跳过")
            return
        interval = max(int(self._setting("interval", DEFAULT_INTERVAL) or 0), MIN_INTERVAL)
        while True:
            try:
                await self._poll_once(channel_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                log.error("FC2 轮询失败: %s", err)
            await asyncio.sleep(interval)

    async def _poll_once(self, channel_id) -> None:
        entries = await asyncio.to_thread(_fetch_entries, self._proxy())
        if not entries:
            return
        incremental = [
            entry
            for entry in entries
            if float(entry["pub_ts"]) > float(self.store.last_ts or 0)
        ]
        if not incremental:
            return
        newest = max(float(entry["pub_ts"]) for entry in incremental)

        if not self.store.last_ts:
            limit = max(int(self._setting("backfill_limit", DEFAULT_BACKFILL) or 0), 0)
            backlog, incremental = (
                (incremental[:-limit], incremental[-limit:])
                if limit
                else (incremental, [])
            )
            changed = False
            for entry in backlog:
                number = extract_number(entry["title"])
                if number and number not in self.store.seen:
                    self.store.remember(number, entry)
                    changed = True
            if changed:
                self.store.save()

        for index, entry in enumerate(incremental):
            await self._push_entry(entry, channel_id)
            if index < len(incremental) - 1:
                await asyncio.sleep(PUSH_DELAY)

        self.store.last_ts = newest
        self.store.save()

    async def _push_entry(self, entry: dict, channel_id) -> None:
        number = extract_number(entry["title"])
        if not number:
            log.debug("标题里没有 FC2 编号，跳过: %s", entry["title"])
            return
        if number in self.store.muted or number in self.store.seen:
            return

        proxy = self._proxy()
        verify_tls = bool(self._setting("verify_tls", True))
        images = await asyncio.to_thread(_preview_images, number, proxy, verify_tls)
        if not images:
            log.debug("FC2 官网未确认编号 %s，跳过", number)
            return
        photos = await asyncio.to_thread(_download_images, images, proxy, verify_tls)
        if not photos:
            log.debug("FC2 编号 %s 没有可用图片，跳过", number)
            return

        caption = _caption(entry, number)
        keyboard = _keyboard(number)
        try:
            if len(photos) == 1:
                sent = [
                    await self.bot.send_photo(
                        chat_id=channel_id,
                        photo=photos[0],
                        caption=caption,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                ]
            else:
                media = [
                    InputMediaPhoto(
                        photo,
                        caption=caption if index == 0 else None,
                        parse_mode=ParseMode.HTML if index == 0 else None,
                    )
                    for index, photo in enumerate(photos)
                ]
                sent = list(
                    await self.bot.send_media_group(chat_id=channel_id, media=media)
                )
                if sent:
                    # 媒体组发不了 reply_markup，只能单独挂到第一条上
                    try:
                        await self.bot.edit_message_reply_markup(
                            chat_id=channel_id,
                            message_id=sent[0].message_id,
                            reply_markup=keyboard,
                        )
                    except Exception as err:  # noqa: BLE001
                        log.debug("FC2 卡片按钮挂载失败 %s: %s", number, err)
        except Exception as err:  # noqa: BLE001
            log.error("FC2 频道推送失败 %s: %s", number, err)
            return

        self.store.remember(
            number, entry, messages=[message.message_id for message in sent]
        )
        self.store.save()
