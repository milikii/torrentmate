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
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.constants import ParseMode

from lib.constants import CONFIG_FILE
from lib.func import escape_html, get_logger
from models.BotConfigurator import BotConfigurator
from models.DoneList import DoneDeletionQueue
from models.DoneList import pending_config_path as done_pending_config_path
from models.Fc2Blacklist import (
    Fc2Blacklist,
    config_path as blacklist_config_path,
    normalize_number,
)

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
RETRY_LIMIT = 6
RETRY_MAX_DELAY = 6 * 60 * 60

# 只认真正的 FC2 编号形态：fc2 前面不能紧贴字母数字（Ubuntu.afc25000545b.iso
# 里的 "fc2" 不算），编号后面也不能紧贴字母数字（Ubuntu.fc25000545b.iso
# 与更长的数字都不算）。裸的 7 位数字只有用户显式输入命令时才算（normalize_number）。
NUMBER_RE = re.compile(
    r"(?i)(?<![0-9a-z])fc2(?:[\s._-]*ppv)?[\s._\-\[\]()\u3010\u3011\uff08\uff09]*(\d{5,8})(?![0-9a-z])"
)
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_IMAGE_RE = re.compile(
    r"<meta[^>]+property=[\"']og:image[\"'][^>]*content=[\"']([^\"']+)[\"']", re.I
)
THUMB_RE = re.compile(
    r"[\"']//contents-thumbnail2\.fc2\.com/w480/([^\"']+)[\"']", re.I
)
IMG_SRC_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)
IMAGE_EXT_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:$|\?)", re.I)
TAG_RE = re.compile(r'data-tag="([^"]+)"', re.I)
DESC_RE = re.compile(
    r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\']([^"\']+)["\']', re.I
)


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


@dataclass
class OfficialArticle:
    """一次官网请求同时提供图、标签、详情与可安全记录的失败原因。"""

    images: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    failure_reason: str = ""
    not_found: bool = False


def _fetch_official(
    number: str, proxy: str | None, verify_tls: bool
) -> OfficialArticle:
    """官网 HTTP 200 且标题含编号才算存在；失败原因交给推送器记录。"""
    try:
        response = _http_get(ARTICLE_URL.format(number), proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        return OfficialArticle(
            failure_reason=f"stage=official_fetch exception={type(err).__name__}"
        )
    if response.status_code != 200:
        return OfficialArticle(
            failure_reason=f"stage=official_fetch status={response.status_code}",
            not_found=response.status_code == 404,
        )
    html = response.text or ""
    if not re.search(rf"FC2-PPV-{number}", _page_title(html), re.I):
        return OfficialArticle(
            failure_reason="stage=official_title status=200 reason=unconfirmed_number"
        )
    images = _official_images(html)
    return OfficialArticle(
        images=images,
        tags=_official_tags(html),
        description=_official_description(html),
        failure_reason="" if images else "stage=official_images status=200 reason=no_images",
    )


def _official_tags(html: str) -> list[str]:
    """官网商品标签（data-tag），按页面顺序去重。"""
    tags: list[str] = []
    for tag in TAG_RE.findall(html or ""):
        tag = html_module.unescape(tag).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags


def _official_description(html: str) -> str:
    """官网 og:description，去掉开头的 FC2-PPV 编号，截断到 400 字供翻译。"""
    match = DESC_RE.search(html or "")
    if not match:
        return ""
    text = html_module.unescape(match.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^FC2-PPV-\d+\s*", "", text)
    return text[:400]


def _censor_label(tags: list[str]) -> str:
    """有码/无码/薄码三选一，只看官网商品标签，卖家详情文案一律不算。

    無修正标签 => 无码；薄モザイク/薄消し标签 => 薄码（实测 12 部里
    薄码标签一次都没出现过，保留这条规则只是为了标签真有它时能显示）；
    其余（包括没有任何码标签的）默认有码，这是 FC2 资源本身的常态，
    不是卖家说的。个别卖家把无码版放购买者消息里的，按标签判成有码
    是按资源本身判的，不算误判成无码。
    """
    text = " ".join(tags or [])
    if "無修正" in text:
        return "无码"
    if "薄モザイク" in text or "薄消し" in text:
        return "薄码"
    return "有码"


def _translate_ja_to_zh(text: str, proxy: str | None) -> str:
    """日译中：谷歌翻译优先走配置代理，谷歌被挡时才用 MyMemory 兜底。

    顺序：Google gtx 走配置代理（NAS 上就是 7890）-> Google gtx 走环境代理
    -> MyMemory 走配置代理 -> MyMemory 走环境代理。实测谷歌 gtx 在 NAS
    代理下会报 429（NAS 出口 IP 被谷歌限流，换 Hatch 出口就能通），
    所以谷歌被挡时不硬等、不跳过卡片，自动降到 MyMemory 补一个翻译；
    全部失败才返回空串，调用方回退日文原文。只译前 400 字。
    """
    if not text:
        return ""
    proxy_dict = {"http": proxy, "https": proxy} if proxy else None
    attempts = []
    if proxy:
        attempts.append(("google", proxy_dict))
    attempts.append(("google", None))
    if proxy:
        attempts.append(("mymemory", proxy_dict))
    attempts.append(("mymemory", None))
    for provider, proxies in attempts:
        try:
            if provider == "google":
                response = requests.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params={"client": "gtx", "sl": "ja", "tl": "zh-CN", "dt": "t", "q": text},
                    proxies=proxies,
                    timeout=15,
                    headers={"User-Agent": USER_AGENT},
                )
                if response.status_code != 200:
                    continue
                data = response.json()
                translated = "".join(part[0] for part in (data[0] or []) if part)
            else:
                response = requests.get(
                    "https://api.mymemory.translated.net/get",
                    params={"q": text, "langpair": "ja|zh-CN"},
                    proxies=proxies,
                    timeout=15,
                    headers={"User-Agent": USER_AGENT},
                )
                if response.status_code != 200:
                    continue
                translated = (response.json().get("responseData") or {}).get("translatedText") or ""
            translated = translated.strip()
            if translated and "QUERY_LENGTH_LIMIT" not in translated and translated != text:
                return translated
        except Exception as err:  # noqa: BLE001
            log.debug("FC2 详情翻译失败 %s: %s", provider, err)
            continue
    return ""


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
) -> OfficialArticle:
    """官网图优先用满 7 张，不足再补图，并保留同一页面的详情及失败原因。"""
    article = _fetch_official(number, proxy, verify_tls)
    article.images = article.images[:PREVIEW_LIMIT]
    if article.failure_reason or len(article.images) >= PREVIEW_LIMIT:
        return article
    try:
        extra = _fetch_fc2ppvdb(number, proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        log.warning(
            "FC2 补充预览失败 number=%s stage=fc2ppvdb exception=%s",
            number,
            type(err).__name__,
        )
        return article
    for url in extra:
        if len(article.images) >= PREVIEW_LIMIT:
            break
        _append_unique(article.images, url)
    return article


def _download_images(
    urls: list[str], proxy: str | None = None, verify_tls: bool = True
) -> tuple[list[bytes], list[str]]:
    """图片字节留在内存；返回可用图片和脱敏的逐图失败原因，不带 URL。"""
    photos: list[bytes] = []
    failures: list[str] = []
    for index, url in enumerate(urls, start=1):
        stage = f"stage=image_download image={index}"
        try:
            response = _http_get(url, proxy, verify_tls)
        except Exception as err:  # noqa: BLE001
            failures.append(f"{stage} exception={type(err).__name__}")
            continue
        if response.status_code != 200:
            failures.append(f"{stage} status={response.status_code}")
            continue
        content_type = str((response.headers or {}).get("Content-Type", "")).lower()
        if not content_type.startswith("image/"):
            failures.append(f"{stage} status=200 reason=not_image")
            continue
        if len(response.content) > IMAGE_MAX_BYTES:
            failures.append(f"{stage} status=200 reason=too_large")
            continue
        if not response.content:
            failures.append(f"{stage} status=200 reason=empty_image")
            continue
        photos.append(response.content)
    return photos, failures


NYAA_DETAIL_LIMIT = 3
NYAA_IMAGE_RE = re.compile(r"https?://[^\s\"'<>]+\.(?:jpe?g|png|webp)", re.I)
IMAGETWIST_IMG_RE = re.compile(r"https?://img\d+\.imagetwist\.com/i/[^\"'\s]+", re.I)
NYAA_IMAGE_BLOCK = ("favicon", "static_flyer", "apiplayer", "/static/", "logo")


def _resolve_imagetwist(url: str, proxy: str | None, verify_tls: bool) -> str:
    """imagetwist 详情页本身是 HTML 查看页，不是图片，取页内 img*.imagetwist.com/i/ 真图。

    取不到真图就返回空串，调用方跳过这张，不把查看页 HTML 当图片发出去。
    """
    try:
        response = _http_get(url, proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        log.debug("imagetwist 查看页请求失败 %s: %s", url, err)
        return ""
    if response.status_code != 200:
        return ""
    for candidate in IMAGETWIST_IMG_RE.findall(response.text or ""):
        if "/th/" not in candidate:
            return candidate
    return ""


def _nyaa_detail_images(guid: str | None, proxy: str | None, verify_tls: bool) -> list[str]:
    """nyaa 详情页附图：从种子描述里提图片 URL，最多 3 张。

    描述是 Markdown/纯文本，没有 <img> 标签，所以直接扫图片 URL。
    imagetwist 的链接要先解析成真图 URL；广告飞图、favicon、logo 直接过滤；
    去掉 #gallery 这类 fragment 后去重。nyaa 详情页本身走代理拉取。
    """
    if not guid:
        return []
    try:
        response = _http_get(guid, proxy, verify_tls)
    except Exception as err:  # noqa: BLE001
        log.debug("nyaa 详情页请求失败 %s: %s", guid, err)
        return []
    if response.status_code != 200:
        return []
    images: list[str] = []
    for raw in NYAA_IMAGE_RE.findall(response.text or ""):
        url = raw.split("#")[0].strip().rstrip(".,)")
        if not url or any(block in url for block in NYAA_IMAGE_BLOCK):
            continue
        if "imagetwist.com" in url:
            url = _resolve_imagetwist(url, proxy, verify_tls)
            if not url:
                continue
        _append_unique(images, url)
        if len(images) >= NYAA_DETAIL_LIMIT:
            break
    return images


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


def _caption(entry: dict, number: str, censor_label: str = "", detail: str = "") -> str:
    size = escape_html(entry.get("size") or "-")
    when = datetime.fromtimestamp(float(entry.get("pub_ts") or 0)).strftime(
        "%Y-%m-%d %H:%M"
    )
    title = _clean_title(entry.get("title") or "", number)
    if len(title) > 200:
        title = title[:197] + "…"
    lines = [
        f"<b>FC2-PPV-{number}</b>",
    ]
    if censor_label:
        lines.append(f"🏷️ {escape_html(censor_label)}")
    lines += [
        "",
        title,
        f"💾 {size} · 🕒 {escape_html(when)}",
    ]
    if detail:
        detail_text = re.sub(r"\s+", " ", str(detail)).strip()[:250]
        if detail_text:
            lines += ["", f"📝 {escape_html(detail_text)}"]
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
            ],
            [
                InlineKeyboardButton(
                    "🔗 FC2 官网", url=ARTICLE_URL.format(number)
                ),
            ],
        ]
    )


def _shared_path_lock(path: str) -> threading.RLock:
    """同一个状态文件在进程内共用一把锁。"""
    with Fc2Store._locks_guard:
        lock = Fc2Store._locks.get(path)
        if lock is None:
            lock = threading.RLock()
            Fc2Store._locks[path] = lock
        return lock


class Fc2Store:
    """单用户 FC2 状态文件：last_ts + seen + muted + retry + backfill_done。

    retry 是带 count / next_ts / last_reason 的失败记录，按指数退避重试；
    官网明确 404 或超过 RETRY_LIMIT 次则记入 dead，不再重试。旧 retry 列表
    读入时视为尚未计次、立即可重试。backfill_done 保证首轮回补只做一次。

    推送器和回调（fc2dl / fc2no）真实运行时是两个实例，各自一份内存，
    所以同一路径共用一把锁；并且每次落盘前都先把磁盘上的 muted 并回来，
    绝不能用旧内存整份覆盖（否则附图传完后的落盘会把刚点的「不感兴趣」抹掉）。
    """

    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: str | None = None) -> None:
        self.path = os.path.abspath(
            path or os.path.join(os.path.dirname(CONFIG_FILE), "fc2_state.json")
        )
        self._lock = _shared_path_lock(self.path)
        self.last_ts = 0.0
        self.seen: dict[str, dict] = {}
        self.muted: set[str] = set()
        self.retry: dict[str, dict] = {}
        self.dead: dict[str, dict] = {}
        self.backfill_done = False
        # 本次进程里明确 mute/unmute 过的编号（True=加，False=删）。
        # muted 落盘时以磁盘为基准叠加它，绝不用旧内存整份覆盖。
        self._muted_delta: dict[str, bool] = {}
        self._mtime: int = 0
        self.load()

    def _mtime_now(self) -> int:
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return 0

    def load(self) -> None:
        with self._lock:
            self._mtime = self._mtime_now()
            self.last_ts = 0.0
            self.seen = {}
            self.muted = set()
            self.retry = {}
            self.dead = {}
            self.backfill_done = False
            self._muted_delta = {}
            if not os.path.isfile(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                self.last_ts = float(data.get("last_ts") or 0)
                self.seen = {
                    str(key): value for key, value in (data.get("seen") or {}).items()
                }
                self.muted = {
                    normalize_number(item) or str(item)
                    for item in (data.get("muted") or [])
                }
                retry = data.get("retry") or {}
                if isinstance(retry, list):
                    self.retry = {
                        str(item): {"count": 0, "next_ts": 0.0, "last_reason": "legacy_retry"}
                        for item in retry
                    }
                elif isinstance(retry, dict):
                    self.retry = {
                        str(key): {
                            "count": max(int(value.get("count") or 0), 0),
                            "next_ts": float(value.get("next_ts") or 0),
                            "last_reason": str(value.get("last_reason") or ""),
                        }
                        for key, value in retry.items()
                        if isinstance(value, dict)
                    }
                self.dead = {
                    str(key): value
                    for key, value in (data.get("dead") or {}).items()
                    if isinstance(value, dict)
                }
                self.backfill_done = bool(data.get("backfill_done"))
            except Exception as err:  # noqa: BLE001
                log.warning("FC2 状态文件损坏，按空状态继续 %s: %s", self.path, err)
                self.last_ts, self.seen, self.muted = 0.0, {}, set()
                self.retry, self.dead, self.backfill_done = {}, {}, False

    def _disk_state(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _merge_disk_muted(self) -> None:
        """落盘前以磁盘 muted 为基准重算内存 muted。

        不能简单并集：/fc2unblock 在另一个实例里把编号从磁盘删掉之后，
        常驻推送实例手里的旧内存会把它又并回来（解黑失效）。所以除了
        本次进程明确 mute/unmute 过的编号（_muted_delta），其余一律以
        磁盘为准。
        """
        merged = {
            normalize_number(item) or str(item)
            for item in (self._disk_state().get("muted") or [])
        }
        for key, enabled in self._muted_delta.items():
            if enabled:
                merged.add(key)
            else:
                merged.discard(key)
        self.muted = merged

    def _write(self, merge_muted: bool = True) -> None:
        if merge_muted:
            self._merge_disk_muted()
        data = {
            "last_ts": self.last_ts,
            "seen": self.seen,
            "muted": sorted(self.muted),
            "retry": self.retry,
            "dead": self.dead,
            "backfill_done": self.backfill_done,
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
            return
        self._muted_delta.clear()
        self._mtime = self._mtime_now()

    def save(self) -> None:
        with self._lock:
            self._write()

    def entry(self, number: str) -> dict | None:
        with self._lock:
            return self.seen.get(str(number))

    def _record(self, number: str, entry: dict, messages=None) -> None:
        self.seen[str(number)] = {
            "hash": entry.get("hash") or "",
            "title": entry.get("title") or "",
            "pub_ts": float(entry.get("pub_ts") or 0),
            "messages": list(messages or []),
        }
        if entry.get("chat_id") is not None:
            self.seen[str(number)]["chat_id"] = entry["chat_id"]
        self.retry.pop(str(number), None)

    def mark_retry(
        self, number: str, last_reason: str, *, not_found: bool = False
    ) -> None:
        """保留跨游标重试；404 或第七次失败进入持久死信，只记一次 INFO。"""
        key = str(number)
        with self._lock:
            if key in self.dead:
                return
            count = int((self.retry.get(key) or {}).get("count") or 0) + 1
            if not_found or count > RETRY_LIMIT:
                self.retry.pop(key, None)
                self.dead[key] = {"count": count, "last_reason": last_reason}
                log.info(
                    "FC2 条目进入死信 number=%s count=%s %s",
                    key,
                    count,
                    last_reason,
                )
            else:
                delay = min(DEFAULT_INTERVAL * (2 ** (count - 1)), RETRY_MAX_DELAY)
                self.retry[key] = {
                    "count": count,
                    "next_ts": time.time() + delay,
                    "last_reason": last_reason,
                }
            self._write()

    def clear_retry(self, numbers) -> None:
        """撤掉重试记录（成功、已拉黑，或从 RSS 消失）。"""
        drop = {str(item) for item in numbers}
        with self._lock:
            if drop.intersection(self.retry):
                for key in drop:
                    self.retry.pop(key, None)
                self._write()

    def remember(self, number: str, entry: dict, messages=None) -> None:
        with self._lock:
            self._record(number, entry, messages)

    def finalize(self, number: str, entry: dict, messages, blocked=None) -> bool:
        """附图传完后的收尾：期间已被拉黑就不写正常状态，返回 False。

        blocked 由调用方传（推送器传黑名单判定），不让 state.muted 变成
        跨容器解不开的第二真相源；不传时保持旧的 muted 语义兼容测试调用。
        """
        with self._lock:
            self._merge_disk_muted()
            if blocked is not None and blocked(number):
                return False
            if blocked is None and self.is_muted(number):
                return False
            self._record(number, entry, messages)
            self._write()
            return True

    def forget(self, number: str) -> None:
        """撤掉一条 seen 记录（附图期间被 muted 的条目不能算正常推送）。"""
        with self._lock:
            existed = self.seen.pop(str(number), None) is not None
            if str(number) in self.retry:
                self.retry.pop(str(number), None)
                existed = True
            if existed:
                self._write()

    def is_muted(self, number: str) -> bool:
        """muted 一律按归一化编号比（05000545 与 5000545 同一条）。

        磁盘被别人改过（/fc2unblock 在另一个实例里解黑）就先重读，
        否则常驻推送实例会一直按旧内存判 blocked。
        """
        key = normalize_number(number) or str(number)
        with self._lock:
            mtime = self._mtime_now()
            if mtime != self._mtime:
                self._merge_disk_muted()
                self._mtime = mtime
            return key in self.muted

    def unmute(self, number: str) -> None:
        """解除静音：黑名单里删掉编号后，推送端也要跟着放行。

        按归一化编号一次性清掉所有变体（含前导零写法）；并在 _muted_delta
        里记一条「删」，这样常驻推送实例下次落盘不会把编号从磁盘旧状态
        里并回来。
        """
        key = normalize_number(number) or str(number)
        with self._lock:
            self._merge_disk_muted()
            if key not in self.muted:
                return
            self.muted = {item for item in self.muted if normalize_number(item) != key}
            self._muted_delta[key] = False
            self._write()

    def mute(self, number: str) -> list[int]:
        """记 muted，同时返回当前已知的消息 id。

        读 seen 和写 muted 在同一把锁里：附图上传期间点「不感兴趣」时，
        推流端随后落盘不会把 muted 覆盖掉，也不会漏删卡片。
        """
        key = normalize_number(number) or str(number)
        with self._lock:
            self._merge_disk_muted()
            self.muted.add(key)
            self._muted_delta[key] = True
            self.retry.pop(key, None)
            record = self.seen.get(key) or {}
            if not record:
                # 推送器用的是另一个实例，卡片 id 只可能在磁盘上
                disk_seen = self._disk_state().get("seen") or {}
                record = disk_seen.get(key) or {}
            messages = []
            for value in record.get("messages") or []:
                try:
                    messages.append(int(value))
                except (TypeError, ValueError):
                    continue
            self._write()
            return messages

    def message_ids(self, number: str) -> list[int]:
        """只读：返回这条推送当前已知的卡片 / 附图消息 id。

        完成删卡只能读，绝不能复用 mute()——mute() 会写 muted 和黑名单，
        已下载完成的片子一旦被拉黑就再也推不了了。这里不落盘、不改任何
        状态；内存里没有就去磁盘上的 seen 里找（推送器可能是另一个实例）。
        """
        key = normalize_number(number) or str(number)
        with self._lock:
            record = self.seen.get(key) or {}
            if not record:
                disk_seen = self._disk_state().get("seen") or {}
                record = disk_seen.get(key) or {}
            messages = []
            for value in record.get("messages") or []:
                try:
                    messages.append(int(value))
                except (TypeError, ValueError):
                    continue
            return messages


class Fc2Pusher:
    """轮询 nyaa FC2 RSS，核验后把新条目推进频道。"""

    def __init__(self, bot, config=None, store=None, blacklist=None) -> None:
        self.bot = bot
        self.config = config or BotConfigurator()
        self.store = store or Fc2Store()
        # 黑名单独立于状态文件：state 被重置后编号还在，这里必须一起看。
        self.blacklist = blacklist or Fc2Blacklist(blacklist_config_path(self.config))
        # 下载机器人完成 FC2 任务后把编号写进共享待删文件，本机器人读到
        # 就删自己发的推送卡（Telegram 只允许机器人删自己的消息）。
        self.done_deletions = DoneDeletionQueue(done_pending_config_path(self.config))

    def _blocked(self, number: str | None) -> bool:
        """黑名单命中才跳过推送；state.muted 只是 fc2no 的兼容记录。

        muted 每个容器各一份，/fc2unblock 只能清自己那份；判定统一看
        共享黑名单，另一个容器解黑后这边不用改 state 就能放行。
        """
        if not number:
            return False
        return self.blacklist.contains(number)

    def _setting(self, key: str, default=None):
        return self.config.get(f"fc2.{key}", default)

    def _proxy(self) -> str | None:
        proxy = self._setting("proxy")
        return None if _is_placeholder(proxy) else str(proxy)

    def _target_chat_id(self):
        """推送目标：优先 fc2.chat_id，其次旧 fc2.channel_id，都没配就推给 super_user 私聊。

        私聊是现在默认的用法：频道创建者在管理员视图里看不到内联按钮，
        只有私聊里按钮才能正常显示、正常点。super_user 的私聊 chat_id
        就是 super_user 本人的用户 ID，Telegram 私聊就是这么定的。
        """
        for key in ("chat_id", "channel_id"):
            value = self._setting(key)
            if not _is_placeholder(value):
                return value
        super_user = self.config.get("bot.super_user")
        return None if _is_placeholder(super_user) else super_user

    async def run(self) -> None:
        if self._setting("enabled", True) is False:
            log.info("fc2.enabled=false，FC2 推送任务跳过（专用机器人模式下老机器人就靠这个关掉）")
            return
        channel_id = self._target_chat_id()
        if channel_id is None:
            log.warning("FC2 推送目标未配置（fc2.chat_id / fc2.channel_id / bot.super_user 都没有），FC2 推送任务跳过")
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
        await self._consume_done_safely(channel_id)
        entries = await asyncio.to_thread(_fetch_entries, self._proxy())
        if not entries:
            return
        last_ts = float(self.store.last_ts or 0)
        incremental = self._incremental_entries(entries, last_ts)
        if not incremental:
            self._prune_retry(entries)
            return
        incremental.sort(key=lambda item: float(item["pub_ts"]))
        newest = max(float(entry["pub_ts"]) for entry in entries)
        incremental = self._apply_backfill(incremental)
        cursor = await self._push_incremental(incremental, channel_id, last_ts)
        # 游标可以放心推到最新：失败条目由 retry 记录兑现，不靠游标兑。
        self.store.last_ts = max(cursor, newest)
        self._prune_retry(entries)
        self.store.save()

    async def _consume_done_safely(self, channel_id) -> None:
        """先处理完成删卡：哪怕这轮 RSS 拉取失败、没有新条目，下载机器人
        写下的完成编号也要尽快消费掉。"""
        try:
            await self._consume_done_deletions(channel_id)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            log.warning("处理已完成待删卡片失败: %s", err)

    def _incremental_entries(
        self, entries: list[dict], last_ts: float
    ) -> list[dict]:
        """增量 = 新条目 + 已到退避期限的失败条目；死信永不重试。"""
        incremental: list[dict] = []
        position: dict[str, int] = {}
        now = time.time()
        for entry in entries:
            number = extract_number(entry["title"])
            if number in self.store.dead:
                continue
            retry = self.store.retry.get(number) if number else None
            if retry is not None:
                if float(retry["next_ts"]) > now:
                    continue
            elif float(entry["pub_ts"]) <= last_ts:
                continue
            if number and number in position:
                # 同一个 FC2 编号在 RSS 里出现两次时只留新的那条
                incremental[position[number]] = entry
                continue
            if number:
                position[number] = len(incremental)
            incremental.append(entry)
        return incremental

    def _apply_backfill(self, incremental: list[dict]) -> list[dict]:
        """首次运行时只保留最近 ``backfill_limit`` 条，其余记 seen；返回要推的。"""
        if self.store.backfill_done:
            return incremental
        limit = max(int(self._setting("backfill_limit", DEFAULT_BACKFILL) or 0), 0)
        if limit:
            backlog, incremental = incremental[:-limit], incremental[-limit:]
        else:
            backlog, incremental = incremental, []
        # 回补占位不能抢了要推的号：同一个 FC2 编号在 RSS 里出现两次时
        # （旧的在回补里、新的在要推的最后几条里），旧的不能先记 seen，
        # 否则新的那条在 _push_entry 里会因 seen 已存在被跳过，白白丢一张新卡。
        pushed_numbers = {extract_number(entry["title"]) for entry in incremental}
        for entry in backlog:
            number = extract_number(entry["title"])
            if (
                number
                and number not in self.store.seen
                and number not in pushed_numbers
            ):
                self.store.remember(number, entry)
        # 回补只做一次，不能拿 last_ts == 0 当条件：首轮条目全发失败时
        # 游标会停在 0，下一轮又把老条目划进 backlog 记成 seen，从没发出
        # 去的卡片就此永久丢失（sol 复现）。
        self.store.backfill_done = True
        self.store.save()
        return incremental

    async def _push_incremental(
        self, incremental: list[dict], channel_id, cursor: float
    ) -> float:
        """按时间顺序推送增量条目，返回推进后的游标。"""
        for entry in incremental:
            pub_ts = float(entry["pub_ts"])
            number = extract_number(entry["title"])
            if number and (self._blocked(number) or number in self.store.seen):
                # 已推过 / 已静音的条目直接跳过，不白等 PUSH_DELAY。
                if number in self.store.retry:
                    self.store.clear_retry([number])
                cursor = max(cursor, pub_ts)
                continue
            await self._push_entry(entry, channel_id)
            cursor = max(cursor, pub_ts)
            await asyncio.sleep(PUSH_DELAY)
        return cursor

    def _prune_retry(self, entries: list[dict]) -> None:
        """已经从 RSS 里消失、或已静音/已推过的失败条目不再挂着无限重试。"""
        live = {extract_number(entry["title"]) for entry in entries}
        self.store.clear_retry(
            [
                number
                for number in self.store.retry
                if number not in live
                or self._blocked(number)
                or number in self.store.seen
            ]
        )

    async def _consume_done_deletions(self, chat_id=None) -> int:
        """删掉下载机器人标记为「已完成」的推送卡，返回消费的记录条数。

        共享待删文件里只有编号：卡片消息 id 从本机器人自己的 Fc2Store
        读（只读接口 message_ids，绝不走 mute()，不会把已下载的片子拉黑）。
        黑名单相关的卡不动；删失败只记日志，照样写 ack，避免同一个失败
        消息每轮都重试刷日志。

        handled 是「消费掉的记录条数」而不是「删掉的消息条数」：每条记录
        只要 ack 成功（deleted / missing / blacklisted / failed）就计一条。
        """
        records = self.done_deletions.pending()
        if not records:
            return 0
        target_chat = chat_id if chat_id is not None else self._target_chat_id()
        if target_chat is None:
            log.warning("完成删卡跳过：推送目标未配置")
            return 0
        handled = 0
        for record in records:
            if await self._consume_one_done_deletion(record, target_chat):
                handled += 1
        return handled

    async def _consume_one_done_deletion(self, record: dict, target_chat) -> bool:
        """处理并 ack 一条待删记录；返回是否由本轮消费掉。"""
        number = normalize_number(record.get("number"))
        if not number:
            return self.done_deletions.ack(record.get("number"), "invalid")
        if self._blocked(number):
            # 「不感兴趣」已经删过卡、也拉黑了，完成流程无权再动它。
            return self.done_deletions.ack(number, "blacklisted")
        message_ids = self.store.message_ids(number)
        if not message_ids:
            # 例如用户手动发的磁力，没有对应的推送卡。
            return self.done_deletions.ack(number, "missing")
        failed, removed = await self._delete_done_messages(
            number, message_ids, target_chat
        )
        acked = self.done_deletions.ack(number, "failed" if failed else "deleted")
        if acked and not failed:
            log.info("已完成 FC2 卡片已删除 %s（%s 条消息）", number, removed)
        return acked

    async def _delete_done_messages(
        self, number: str, message_ids, target_chat
    ) -> tuple[bool, int]:
        """逐条删卡；返回（是否有非「消息已不存在」的失败, 删除成功条数）。"""
        failed = False
        removed = 0
        for message_id in message_ids:
            try:
                await self.bot.delete_message(
                    chat_id=target_chat, message_id=message_id
                )
                removed += 1
            except Exception as err:  # noqa: BLE001
                text = str(err).lower()
                if "not found" in text:
                    # 已经删掉了（用户手删 / 上一轮删到一半），不算失败。
                    log.debug("FC2 完成卡已不存在 %s/%s", number, message_id)
                else:
                    failed = True
                    log.warning(
                        "删除已完成 FC2 卡片失败 %s/%s: %s",
                        number,
                        message_id,
                        err,
                    )
        return failed, removed

    async def _push_entry(self, entry: dict, channel_id) -> bool:
        """推一条卡片，返回这条是否已经处理完（False 表示失败、下轮要重试）。"""
        number = extract_number(entry["title"])
        if not number:
            log.debug("标题里没有 FC2 编号，跳过: %s", entry["title"])
            return True
        if self._blocked(number) or number in self.store.seen or number in self.store.dead:
            return True

        proxy = self._proxy()
        verify_tls = bool(self._setting("verify_tls", True))
        article = await asyncio.to_thread(_preview_images, number, proxy, verify_tls)
        if not article.images:
            reason = article.failure_reason or "stage=official_images reason=no_images"
            log.warning("FC2 条目推送失败 number=%s %s", number, reason)
            self.store.mark_retry(number, reason, not_found=article.not_found)
            return False
        images = list(article.images)
        # nyaa 附图放官网图后面，最多补到媒体组上限 10 张
        detail_images = await asyncio.to_thread(
            _nyaa_detail_images, entry.get("guid"), proxy, verify_tls
        )
        for url in detail_images:
            if len(images) >= 10:
                break
            _append_unique(images, url)
        photos, failures = await asyncio.to_thread(
            _download_images, images, proxy, verify_tls
        )
        for reason in failures:
            log.warning("FC2 图片失败 number=%s %s", number, reason)
        if not photos:
            reason = failures[-1] if failures else "stage=image_download reason=no_usable_images"
            if not failures:
                log.warning("FC2 条目推送失败 number=%s %s", number, reason)
            self.store.mark_retry(number, reason)
            return False

        censor = _censor_label(article.tags)
        description_ja = article.description
        detail_zh = (
            await asyncio.to_thread(_translate_ja_to_zh, description_ja, proxy)
            if description_ja
            else ""
        )
        caption = _caption(entry, number, censor_label=censor, detail=detail_zh or description_ja)
        keyboard = _keyboard(number)
        # 卡片本体必须是「单张图片 + caption + 键盘」这种普通媒体消息。
        # 媒体组（album）客户端一律不渲染内联键盘：Bot API 负责人在
        # tdlib/telegram-bot-api#86 里明确写了 "Clients are unable to show
        # inline keyboards for albums"；Telegram Android 源码里也是同一个
        # 条件（ChatMessageCell：只有 currentPosition == null 即非相册，或
        # 相册为文件相册且位于最后一条时才画 bot 键盘），所以键盘挂组首、
        # 组尾、全组都没用。改成封面单独发一条（按钮就长在这张图片卡片
        # 上），其余预览图另发一个不带 caption / 键盘的媒体组。
        # 媒体组走 NAS 7890 代理上传 6-10 张图时，PTB 默认 20 秒写超时很容易
        # 误报 Timed out（服务端其实已收到，像 4973177 那次），放宽到 120 秒。
        try:
            card = await self.bot.send_photo(
                chat_id=channel_id,
                photo=photos[0],
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
        except Exception as err:  # noqa: BLE001
            reason = f"stage=send_photo exception={type(err).__name__}"
            log.warning("FC2 卡片发送失败 number=%s %s", number, reason)
            self.store.mark_retry(number, reason)
            return False

        # 封面一发成功、按钮一出现，状态里就必须有这条：下载和不感兴趣两个
        # 按钮要在附图上传期间就能正常工作，不能等附图传完才记。
        sent_ids = [card.message_id]
        entry = {**entry, "chat_id": channel_id}
        self.store.remember(number, entry, messages=sent_ids)
        self.store.save()

        extras = photos[1:]
        if extras:
            try:
                if len(extras) == 1:
                    # 媒体组最少 2 张，只剩 1 张附图时按普通图片发。
                    extra = await self.bot.send_photo(
                        chat_id=channel_id,
                        photo=extras[0],
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=30,
                    )
                    sent_ids.append(extra.message_id)
                else:
                    sent_ids.extend(
                        message.message_id
                        for message in await self.bot.send_media_group(
                            chat_id=channel_id,
                            media=[InputMediaPhoto(photo) for photo in extras],
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=30,
                        )
                    )
            except Exception as err:  # noqa: BLE001
                # 附图只是预览，发失败不影响卡片和按钮；状态照记，避免下一轮重推整张卡。
                log.warning(
                    "FC2 预览图发送失败 number=%s stage=send_previews exception=%s",
                    number,
                    type(err).__name__,
                )

        if not self.store.finalize(number, entry, sent_ids, self._blocked):
            # 附图上传期间用户点了「不感兴趣」：删掉自己发的图，保持 muted，
            # 不记成正常推送，也不许把 muted 覆盖回去。
            for message_id in sent_ids:
                try:
                    await self.bot.delete_message(
                        chat_id=channel_id, message_id=int(message_id)
                    )
                except Exception as err:  # noqa: BLE001
                    log.warning(
                        "FC2 撤卡失败 number=%s msg=%s chat=已脱敏 stage=retract_muted exception=%s",
                        number,
                        message_id,
                        type(err).__name__,
                    )
            self.store.forget(number)
        else:
            log.info(
                "FC2 卡片已推送 number=%s msg=%s chat=已脱敏 messages=%s",
                number,
                card.message_id,
                len(sent_ids),
            )
        return True
