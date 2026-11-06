import asyncio
import os
import pathlib
import re
import secrets
import socket
import tempfile
import time
from datetime import datetime

import pydash as _
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
)
from telegram.ext.filters import ALL, Document, Entity, MessageFilter, Regex

from lib.auth import restricted
from lib.classifier import get_classifier
from lib.constants import (
    AUTO_DIR_CALLBACK,
    MAX_BATCH_LINKS,
)
from lib.done_commands import fc2done_command
from lib.func import (
    bytes_to_human,
    escape_html,
    get_logger,
    get_qr_code,
    parse_speed,
    speed_to_human,
    trans,
)
from lib.i18n import button_label_key_text, button_regex
from lib.magnet import MagnetLink, extract_magnets, parse_magnet
from lib.render import render_progress_card
from models.BotConfigurator import BotConfigurator
from models.Browser import Browser
from models.DownloadHistory import DownloadHistory
from models.DownloadQueue import DownloadQueue
from models.DoneList import (
    SOURCE_BATCH,
    SOURCE_FC2_CARD,
    SOURCE_MANUAL,
    WRITE_FAILED,
    DoneList,
    config_path as done_config_path,
)
from models.Fc2Blacklist import (
    SOURCE_DELETE,
    SOURCE_MANUAL as BLACKLIST_SOURCE_MANUAL,
    SOURCE_MUTE,
    Fc2Blacklist,
    config_path as blacklist_config_path,
    default_path as blacklist_default_path,
    normalize_number,
    source_label,
)
from models.Fc2Channel import Fc2Store, extract_number, _magnet
from models.Fc2Claims import Fc2Claims
from models.Fc2Claims import config_path as claims_config_path
from models.HistoryBrowser import HistoryBrowser
from models.PostsBrowser import PostsBrowser
from models.SearchTorrents import SearchTorrents
from models.TorrentInfoBrowser import TorrentInfoBrowser
from models.TorrentsListBrowser import TorrentsListBrowser
from models.TransmissionClient import TransmissionClient, TorrentAlreadyExists

bot_config = BotConfigurator()


def _super_user_id():
    """超级用户 ID：YAML 里可能是字符串，直接与 int 比永远不相等。"""
    value = bot_config.get("bot.super_user")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return value


log = get_logger("main")

_transmission_client = None

# 邀请新用户用的临时 token
WELCOME_HASHES: dict[str, float] = {}
WELCOME_LINK_TTL = 900

# 底部快捷键盘（按键为 i18n key，真正文案按用户语言渲染）
ACTIONS = ["menu.torrents", "menu.search", "menu.status", "menu.help"]

# Telegram 命令菜单
COMMANDS = [
    ("torrents", "torrents"),
    ("status", "status"),
    ("history", "history"),
    ("last_search", "last_search"),
    ("start_all", "start_all"),
    ("stop_all", "stop_all"),
    ("rules", "rules"),
    ("classify", "classify"),
    ("speed", "speed"),
    ("adduser", "adduser"),
    ("help", "help"),
]

# 兼容旧代码：向 Telegram 注册的命令列表（中文描述）
commands = [(name, button_label_key_text(key, "zh")) for name, key in COMMANDS]


# Init search object with trackers
def get_search():
    return SearchTorrents(
        credentials=bot_config.get("trackers", {}),
        sort_by=bot_config.get("bot.sort_by", "date"),
    )


def get_torrent_connection():
    global _transmission_client
    if _transmission_client is not None:
        return _transmission_client
    try:
        _transmission_client = TransmissionClient(
            telegram_token=bot_config.get("bot.token"),
            host=bot_config.get("transmission.host"),
            port=bot_config.get("transmission.port"),
            username=bot_config.get("transmission.user"),
            password=bot_config.get("transmission.password"),
        )
        return _transmission_client
    except Exception as err:
        log.error(
            "Transmission %s:%s is not available due to error: %s",
            bot_config.get("transmission.host"),
            bot_config.get("transmission.port"),
            err,
        )
        raise ConnectionError("Transmission server is unavailable") from err


class MagnetMessageFilter(MessageFilter):
    """匹配任意包含磁力链接的消息（支持一次多条）。"""

    def filter(self, message) -> bool:
        return bool(extract_magnets(message.text or message.caption or ""))


def _truncate(text: str, limit: int = 64) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _resolve_target(dir_choice: str, title: str | None) -> tuple[str | None, bool]:
    """返回 (目标目录, 是否自动分类)。"""
    classifier = get_classifier()
    if dir_choice != AUTO_DIR_CALLBACK:
        return dir_choice, False
    result = classifier.classify(title)
    target = classifier.resolve_path(result.directory) or classifier.resolve_path(
        classifier.fallback
    )
    return target, True


def _dir_keyboard(lang_code):
    return bot_config.get_downloads_keyboard(lang_code)


@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a message when the command /help is issued."""
    lang_code = update.message.from_user.language_code
    HELP = trans("HELP", lang_code)
    if update.message.chat.id == _super_user_id():
        HELP += "\n" + trans("HELP_ADMIN", lang_code)
    log.debug("%s, %s", update.message.chat.id, context.user_data)
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=HELP,
        parse_mode=ParseMode.HTML,
        reply_markup=bot_config.get_actions_keyboard(ACTIONS, lang_code),
    )


@restricted
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    history = HistoryBrowser(
        user_id=update.message.chat.id,
        user_lang=update.message.from_user.language_code,
        posts=DownloadHistory.show(),
    )
    context.user_data["nav_type"] = "history"
    context.user_data["history"] = history
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=history.get_page(),
        parse_mode=ParseMode.HTML,
        reply_markup=history.get_keyboard(),
    )


@restricted
async def getTorrentFile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _id = int(update.message.text.split("_")[1])
    post = context.user_data["posts"].posts[_id]
    tmp_file_path = post["dl"]
    # If tracker has credential, download file and path file path to Transmission
    if _.has(bot_config.get("trackers"), post["tracker"]) and str(
        post["dl"]
    ).startswith("http"):
        tmp_file_path = await asyncio.to_thread(
            get_search().download, post["dl"], post["tracker"]
        )
        try:
            with open(tmp_file_path, "rb") as document:
                await context.bot.send_document(
                    chat_id=update.message.chat.id,
                    document=document,
                    caption=post["title"],
                )
        finally:
            os.unlink(tmp_file_path)
    else:
        await context.bot.send_message(
            chat_id=update.message.chat.id,
            parse_mode=ParseMode.MARKDOWN,
            text=f"**{post['title']}:**\n"
            "-------------------------------\n"
            f"{trans('HOWTO_DOWNLOAD_MAGNET_LINK', update.message.from_user.language_code)}\n"
            "-------------------------------\n"
            f"```\n{post['dl']}\n```",
        )


@restricted
async def chooseDownloadDir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户发送 .torrent / URL / 搜索结果时，询问保存目录。"""
    lang_code = update.message.from_user.language_code
    if update.message.document:
        title = update.message.document.file_name
        context.user_data["torrent"] = {
            "type": "torrent",
            "file_name": title,
            "file_id": update.message.document.file_id,
        }
    elif re.match(r"^/download_[0-9]+$", update.message.text or ""):
        _id = int(update.message.text.split("_")[1])
        post = context.user_data["posts"].posts[_id]
        title = post["title"]
        context.user_data["torrent"] = {
            "type": "url",
            "url": post["dl"],
            "tracker": post["tracker"],
            "title": title,
        }
    else:
        title = update.message.text
        context.user_data["torrent"] = {"type": "url", "url": title, "title": title}

    context.user_data.pop("batch", None)
    try:
        prompt = await update.message.reply_text(
            trans("CHOOSE_DOWNLOAD_DIR", lang_code).format(
                _truncate(title or "", 80)
            ),
            reply_markup=_dir_keyboard(lang_code),
        )
    except TelegramError as err:
        # 预览没发出去，不能留下“有 torrent + 旧 torrent_prompt”这种可被认领的组合。
        context.user_data.pop("torrent", None)
        context.user_data.pop("torrent_prompt", None)
        context.user_data.pop("torrent_prompt_chat", None)
        log.warning("单条预览发送失败: %s", err)
        return
    context.user_data["torrent_prompt"] = prompt.message_id
    context.user_data["torrent_prompt_chat"] = update.message.chat.id


# ---------------------------------------------------------------- 批量磁力链接


@restricted
async def batchMagnets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """识别消息中的全部磁力链接，展示分类预览并等待选择目录。"""
    lang_code = update.message.from_user.language_code
    text = update.message.text or update.message.caption or ""
    links = extract_magnets(text, limit=MAX_BATCH_LINKS)
    if not links:
        return

    context.user_data.pop("torrent", None)

    classifier = get_classifier()
    auto_enabled = bool(bot_config.get("auto_classify.enabled", True))
    lines = [trans("BATCH_FOUND", lang_code).format(len(links)), ""]
    for index, link in enumerate(links[:10], 1):
        if link.name:
            result = classifier.classify(link.name)
            target = (
                result.directory if auto_enabled else trans("BATCH_MANUAL", lang_code)
            )
            lines.append(
                f"{index}. {escape_html(_truncate(link.name))} → <b>{escape_html(target)}</b>"
            )
        else:
            lines.append(
                trans("BATCH_ITEM_UNKNOWN", lang_code).format(
                    index, link.info_hash[:12]
                )
            )
    if len(links) > 10:
        lines.append(trans("BATCH_MORE", lang_code).format(len(links) - 10))
    if any(not link.name for link in links):
        lines.append("\n" + trans("BATCH_NAME_HINT", lang_code))
    lines.append("\n" + trans("BATCH_CHOOSE", lang_code))

    try:
        prompt = await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=_dir_keyboard(lang_code),
            disable_web_page_preview=True,
        )
    except TelegramError as err:
        old_prompt = context.user_data.get("batch_prompt")
        old_prompt_chat = context.user_data.pop("batch_prompt_chat", None)
        # 预览没发出去，不能留下“新批次 + 旧预览 ID”这种能通过认领检查的组合。
        context.user_data.pop("batch", None)
        context.user_data.pop("batch_prompt", None)
        if old_prompt is not None:
            # 旧预览的键盘也得清掉，否则它还能消费后来发送的单条。
            # 旧预览可能发在别的聊天里，优先用它自己记录的 chat。
            try:
                await context.bot.edit_message_reply_markup(
                    chat_id=(
                        old_prompt_chat
                        if old_prompt_chat is not None
                        else update.message.chat.id
                    ),
                    message_id=old_prompt,
                    reply_markup=None,
                )
            except TelegramError as clear_err:
                log.debug("清理过期批量按钮键盘失败: %s", clear_err)
        log.warning("批量预览发送失败: %s", err)
        return
    context.user_data["batch"] = links
    context.user_data["batch_prompt"] = prompt.message_id
    context.user_data["batch_prompt_chat"] = update.message.chat.id


async def _add_batch(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE, dir_choice: str
):
    query = update.callback_query
    lang_code = query.from_user.language_code
    links: list[MagnetLink | str | pathlib.Path] = context.user_data.pop("batch", [])
    if not links:
        return

    prompt_id = context.user_data.pop("batch_prompt", None)
    prompt_chat = context.user_data.pop("batch_prompt_chat", None)
    if prompt_id is not None and (
        prompt_id != query.message.message_id
        or (prompt_chat is not None and prompt_chat != query.message.chat.id)
    ):
        # 这是旧批次留下的预览按钮，不能让它按旧目录消费新批次。
        context.user_data["batch"] = links
        context.user_data["batch_prompt"] = prompt_id
        if prompt_chat is not None:
            context.user_data["batch_prompt_chat"] = prompt_chat
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=None,
            )
        except TelegramError as err:
            log.debug("清理过期批量按钮键盘失败: %s", err)
        log.debug("忽略过期批量按钮 %s", query.data)
        return
    context.user_data["batch_prompt"] = query.message.message_id
    context.user_data["batch_prompt_chat"] = query.message.chat.id

    try:
        client = get_torrent_connection()
        existing = {
            str(info_hash).lower(): torrent
            for info_hash, torrent in (
                await asyncio.to_thread(client.existing_hashes)
            ).items()
        }
    except Exception as err:  # noqa: BLE001
        # 查重失败不能按空库继续添加，RPC 返回已有任务时会覆盖通知队列。
        # 保留批量预览，用户可以重试原目录按钮，不丢待添加任务。
        context.user_data["batch"] = links
        context.user_data["batch_prompt"] = query.message.message_id
        context.user_data["batch_prompt_chat"] = query.message.chat.id
        log.warning("批量查重失败，未添加任何任务: %s", err)
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=(
                trans("SOMETHING_WENT_WRONG", lang_code)
                + ":\n"
                + escape_html(str(err))
            ),
            parse_mode=ParseMode.HTML,
        )
        return
    seen_paths = {}
    show_cards = len(links) <= 8 and bool(bot_config.get("notify.progress", True))

    chat_id = query.message.chat.id
    message_id = query.message.message_id
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=trans("BATCH_ADDING", lang_code).format(0, len(links)),
            parse_mode=ParseMode.HTML,
            reply_markup=_dir_keyboard(lang_code),
        )
    except TelegramError as err:
        log.debug("无法更新批量提示消息: %s", err)
        # 内容未变化是良性无操作，消息还在，后面的结果编辑照常走。
        if "message is not modified" not in str(err).lower():
            message_id = None

    added, duplicates, failed, blocked = [], [], [], []
    # 曾经下载过的提示（只提示、不拦截；命中也照常添加）
    previously: list[str] = []
    for index, item in enumerate(links, 1):
        if isinstance(item, MagnetLink):
            link = item
        else:
            link = parse_magnet(str(item)) or MagnetLink(
                raw=str(item), info_hash="", name=pathlib.Path(item).stem
            )
        info_hash = str(link.info_hash or "").lower()
        handoff = _fc2_handoff_magnet(link.raw)
        try:
            blocked_number = _fc2_blocked_number(link.name)
        except Exception as err:  # noqa: BLE001
            # 黑名单检查失败不能放行，也不能丢掉待添加项，保留原批量供重试。
            context.user_data["batch"] = links
            context.user_data["batch_prompt"] = query.message.message_id
            context.user_data["batch_prompt_chat"] = query.message.chat.id
            log.warning("批量黑名单检查失败，本次批量未完成: %s", err)
            await context.bot.send_message(
                chat_id=chat_id,
                text="黑名单检查失败，本次批量未完成，可重试原按钮",
                parse_mode=ParseMode.HTML,
            )
            return
        if blocked_number:
            blocked.append(blocked_number)
            continue
        try:
            torrent_source = link.raw
            path = None
            if (
                isinstance(torrent_source, pathlib.Path)
                or not str(torrent_source).lower().startswith(
                    ("magnet:", "http://", "https://")
                )
            ):
                path = pathlib.Path(torrent_source).resolve()
                if path in seen_paths:
                    duplicates.append((link, seen_paths[path]))
                    continue
                # 字符串路径会被 transmission_rpc 当成服务器上的文件名，
                # 批量文件统一用 Path，并在 RPC 添加前读取 info_hash 查重。
                torrent_source = path
                info_hash = await asyncio.to_thread(
                    TransmissionClient.torrent_file_hash, path
                )
            if info_hash and info_hash in existing:
                if handoff is not None:
                    await _record_fc2_claim(
                        info_hash,
                        extract_number(link.name or ""),
                        str(link.name or ""),
                    )
                duplicates.append((link, existing[info_hash]))
                if path is not None:
                    seen_paths[path] = existing[info_hash]
                continue
            target, auto = _resolve_target(dir_choice, link.name)
            if handoff is not None:
                log.info(
                    "FC2 专用磁力入口 number=%s stage=add_torrent",
                    extract_number(link.name or ""),
                )
            torrent = await asyncio.to_thread(
                client.add_torrent,
                chat_id=chat_id,
                lang_code=lang_code,
                torrent=torrent_source,
                download_dir=target,
                title=link.name,
                auto_classify=auto,
                source=SOURCE_BATCH,
                notify=handoff is None,
            )
            if info_hash:
                existing[info_hash] = torrent
            actual_hash = str(getattr(torrent, "info_hash", "") or "").lower()
            if handoff is not None:
                claim_title = str(link.name or getattr(torrent, "name", "") or "")
                await _record_fc2_claim(
                    actual_hash or handoff.info_hash,
                    extract_number(claim_title),
                    claim_title,
                )
            if actual_hash:
                existing[actual_hash] = torrent
            if path is not None:
                seen_paths[path] = torrent
            added.append((link, torrent, target, auto))
            hint_number = extract_number(link.name or "")
            hint = _done_hint(
                number=hint_number,
                info_hash=(
                    None
                    if hint_number
                    else (actual_hash or info_hash or None)
                ),
            )
            if hint:
                previously.append(hint)
        except Exception as err:  # noqa: BLE001
            if handoff is not None:
                log.error(
                    "FC2 专用磁力添加失败 number=%s stage=add_torrent exception=%s",
                    extract_number(link.name or ""),
                    type(err).__name__,
                )
                failed.append((link, type(err).__name__))
            else:
                log.error("添加批量任务失败 %s: %s", info_hash or link.raw, err)
                failed.append((link, err))

        if message_id and index % 3 == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=trans("BATCH_ADDING", lang_code).format(index, len(links)),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_dir_keyboard(lang_code),
                )
            except TelegramError:
                pass

    # 为前几个任务各发一张进度卡片
    if show_cards:
        for link, torrent, target, auto in added:
            entry = DownloadQueue.get(torrent.id)
            if not entry:
                continue
            try:
                card = await context.bot.send_message(
                    chat_id=chat_id,
                    text=render_progress_card(torrent, lang_code, target, link.name),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                entry.message_id = card.message_id
                DownloadQueue.update(entry)
            except TelegramError as err:
                log.debug("发送初始进度卡片失败: %s", err)
    else:
        for _link, torrent, _target, _auto in added:
            entry = DownloadQueue.get(torrent.id)
            if entry:
                entry.progress_card = False
                DownloadQueue.update(entry)

    lines = [trans("BATCH_RESULT", lang_code).format(len(added), len(links))]
    if duplicates:
        lines.append(
            trans("BATCH_DUPLICATES", lang_code).format(
                len(duplicates),
                "、".join(
                    escape_html(_truncate(link.display_name, 40))
                    for link, _torrent in duplicates[:5]
                ),
            )
        )
    if blocked:
        lines.append(
            "🚫 黑名单命中 {0} 条，已跳过：{1}".format(
                len(blocked),
                "、".join(f"FC2-PPV-{number}" for number in blocked[:5]),
            )
        )
    if previously:
        # 只提示：这些任务的添加流程已经走完，提示不改变任何结果。
        lines.append(f"📌 曾经下载过 {len(previously)} 条（任务仍已添加）：")
        for hint in previously[:5]:
            lines.append(f"  • {escape_html(hint)}")
        if len(previously) > 5:
            lines.append(f"  …还有 {len(previously) - 5} 条")
    if failed:
        lines.append(trans("BATCH_FAILED", lang_code).format(len(failed)))
        for link, reason in failed[:5]:
            lines.append(
                f"  • {escape_html(_truncate(link.display_name, 40))}: "
                f"{escape_html(_truncate(str(reason), 120))}"
            )
    if added:
        lines.append("\n" + trans("BATCH_TIPS", lang_code))

    text = "\n".join(lines)
    context.user_data.pop("batch_prompt", None)
    context.user_data.pop("batch_prompt_chat", None)
    edited = False
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            edited = True
        except TelegramError:
            pass
    if not edited:
        for attempt in range(2):
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
                )
                edited = True
                break
            except TelegramError as err:
                if attempt == 0:
                    # 网络瞬断先重试一次（不引入延时依赖，只让出事件循环）。
                    await asyncio.sleep(0)
                    continue
                # 两次都没发出去，也不能把下面的兜底清键盘一起带走；
                # 汇总逐条追加到 user_data，下次交互时补发，不再永久丢失。
                # pending 只活在 user_data 内存里，机器人重启会丢；本函数不做落盘持久化。
                pending = context.user_data.setdefault("pending_batch_results", [])
                pending.append((chat_id, text))
                # 5 条只是软上限：合并会拼出超长消息时宁可条目多，也绝不丢未送达汇总。
                while len(pending) > 5:
                    first_chat, first_text = pending[0]
                    second_chat, second_text = pending[1]
                    if (
                        first_chat != second_chat
                        or len(first_text) + len(second_text) + 2 > 3000
                    ):
                        break
                    pending[:2] = [(first_chat, first_text + "\n\n" + second_text)]
                log.warning("批量结果发送失败: %s", err)
    # 兜底：不管上面哪条编辑链走通，原消息上的键盘都必须清掉。
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=query.message.chat.id,
            message_id=query.message.message_id,
            reply_markup=None,
        )
    except TelegramError as err:
        log.debug("清理批量按钮键盘失败: %s", err)


async def _add_single(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE, dir_choice: str
):
    query = update.callback_query
    lang_code = query.from_user.language_code
    prompt_id = context.user_data.pop("torrent_prompt", None)
    prompt_chat = context.user_data.pop("torrent_prompt_chat", None)
    if prompt_id is not None and (
        prompt_id != query.message.message_id
        or (prompt_chat is not None and prompt_chat != query.message.chat.id)
    ):
        # 这是旧单条预览留下的按钮，不能让它按旧目录消费新单条。
        if "torrent" in context.user_data:
            context.user_data["torrent_prompt"] = prompt_id
            if prompt_chat is not None:
                context.user_data["torrent_prompt_chat"] = prompt_chat
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=None,
            )
        except TelegramError as err:
            log.debug("清理过期单条按钮键盘失败: %s", err)
        log.debug("忽略过期单条按钮 %s", query.data)
        return
    data = context.user_data.pop("torrent", None)
    if not data:
        return

    target, auto = _resolve_target(dir_choice, data.get("title"))
    context.user_data.pop("batch", None)
    chat_id = query.message.chat.id

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError:
        pass

    blocked = _fc2_blocked_number(data.get("title") or data.get("file_name"))
    if blocked:
        await context.bot.send_message(
            chat_id=chat_id, text=f"FC2-PPV-{blocked} 在你黑名单里，已跳过这一条"
        )
        return

    tmp_file_path = None
    is_temp_file = False
    torrent_source = data.get("url") or data.get("file_name") or ""
    handoff = _fc2_handoff_magnet(torrent_source)
    try:
        if data["type"] == "torrent":
            suffix = os.path.splitext(data.get("file_name") or "")[1]
            tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
            tmp_file_path = pathlib.Path(tmp.name)
            tmp.close()
            _file = await context.bot.get_file(data["file_id"])
            await _file.download_to_drive(str(tmp_file_path))
            is_temp_file = True
            torrent_source = tmp_file_path
        else:
            torrent_source = data["url"]
            trackers = bot_config.get("trackers") or {}
            if not str(torrent_source).startswith("magnet") and _.has(
                trackers, data.get("tracker")
            ):
                _tmp = await asyncio.to_thread(
                    get_search().download, data["url"], data["tracker"]
                )
                tmp_file_path = pathlib.Path(_tmp)
                is_temp_file = True
                torrent_source = tmp_file_path

        if handoff is not None:
            log.info(
                "FC2 专用磁力入口 number=%s stage=add_torrent",
                extract_number(handoff.name or ""),
            )
        else:
            log.info("Adding file/URL %s to Transmission", torrent_source)
        torrent = await asyncio.to_thread(
            get_torrent_connection().add_torrent,
            chat_id=query.from_user.id,
            lang_code=lang_code,
            torrent=torrent_source,
            download_dir=target,
            title=data.get("title"),
            auto_classify=auto,
            single_dedup=True,
            source=SOURCE_MANUAL,
            notify=handoff is None,
        )
    except TorrentAlreadyExists:
        if handoff is not None:
            await _record_fc2_claim(
                handoff.info_hash,
                extract_number(handoff.name or ""),
                str(handoff.name or ""),
            )
        await context.bot.send_message(
            chat_id=chat_id, text=trans("SINGLE_ADD_DUPLICATE", lang_code)
        )
        if handoff is None:
            log.info("Skipped duplicate single torrent %s", torrent_source)
        else:
            log.info(
                "FC2 专用磁力结果 number=%s stage=add_torrent result=duplicate",
                extract_number(handoff.name or ""),
            )
        return
    except Exception as err:  # noqa: BLE001
        if handoff is not None:
            text = trans("ADDING_FILE_SOMETHING_WENT_WRONG", lang_code)
            log.error(
                "FC2 专用磁力添加失败 number=%s stage=add_torrent exception=%s",
                extract_number(handoff.name or ""),
                type(err).__name__,
            )
        else:
            text = trans("ADDING_FILE_SOMETHING_WENT_WRONG", lang_code) + ":\n" + str(err)
            if any(
                error_text in str(err).lower()
                for error_text in ("invalid or corrupt torrent file", "unrecognized info")
            ):
                text = trans("ADDING_TORRENT_FILE_IS_CORRUPTED", lang_code)
            log.error("File %s was not added due to error %s", torrent_source, err)
        await context.bot.send_message(chat_id=chat_id, text=text)
        return
    finally:
        if is_temp_file and isinstance(tmp_file_path, pathlib.Path):
            if tmp_file_path.is_file():
                os.unlink(tmp_file_path)

    if handoff is not None:
        claim_title = str(handoff.name or getattr(torrent, "name", "") or "")
        await _record_fc2_claim(
            getattr(torrent, "info_hash", None) or handoff.info_hash,
            extract_number(claim_title),
            claim_title,
        )

    # 发送初始进度卡片，后续由 DownloadMonitor 原地更新
    entry = DownloadQueue.get(torrent.id)
    try:
        if handoff is None:
            card = await context.bot.send_message(
                chat_id=chat_id,
                text=render_progress_card(torrent, lang_code, target, data.get("title")),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            if entry:
                entry.message_id = card.message_id
                DownloadQueue.update(entry)
    except TelegramError as err:
        log.debug("发送初始进度卡片失败: %s", err)

    # 之前下载过就提示一句：只提示，任务已经加进去了，绝不回滚。
    hint_number = extract_number(data.get("title") or data.get("file_name") or "")
    hint = _done_hint(
        number=hint_number,
        info_hash=None if hint_number else getattr(torrent, "info_hash", None),
    )
    if hint:
        try:
            await context.bot.send_message(chat_id=chat_id, text=hint)
        except TelegramError as err:
            log.debug("成功名单提示发送失败: %s", err)

    if auto and handoff is None:
        await context.bot.send_message(
            chat_id=chat_id, text=trans("AUTO_CLASSIFY_ON", lang_code)
        )


@restricted
async def confirmDirChoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择目录后的统一入口（单条 / 批量）。"""
    query = update.callback_query
    await query.answer()
    pending = context.user_data.pop("pending_batch_results", None)
    if pending:
        undelivered = []
        for pending_chat_id, pending_text in pending:
            try:
                await context.bot.send_message(
                    chat_id=pending_chat_id,
                    text=pending_text,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError as err:
                undelivered.append((pending_chat_id, pending_text))
                log.debug("补发批量结果失败: %s", err)
        if undelivered:
            context.user_data["pending_batch_results"] = undelivered
    if context.user_data and "batch" in context.user_data:
        await _add_batch(update, context, query.data)
    else:
        await _add_single(update, context, query.data)


# ---------------------------------------------------------------- 导航与任务


@restricted
async def getMenuPage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    page = query.data
    try:
        nav_type = context.user_data["nav_type"]
        posts: Browser = context.user_data[nav_type]
        if str(page) == "x":
            log.warning("You are trying to click the same page")
            return
        await context.bot.edit_message_text(
            chat_id=update.callback_query.message.chat.id,
            message_id=update.callback_query.message.message_id,
            text=posts.get_page(int(page)),
            parse_mode=ParseMode.HTML,
            reply_markup=posts.get_keyboard(page),
            disable_web_page_preview=True,
        )
    except Exception as err:  # noqa: BLE001
        user_lang = update.callback_query.from_user.language_code
        log.error("Wrong menu page %s: %s", page, err)
        await context.bot.edit_message_text(
            chat_id=update.callback_query.message.chat.id,
            message_id=update.callback_query.message.message_id,
            text=trans("BAD_PAGE_ACCESS", user_lang),
            reply_markup=None,
            disable_web_page_preview=True,
        )


@restricted
async def lastSearchResults(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _.has(context.user_data, "posts"):
        context.user_data["nav_type"] = "posts"
        posts: PostsBrowser = context.user_data["posts"]
        await update.message.reply_text(
            text=posts.get_page(),
            reply_markup=posts.get_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        user_lang = update.message.from_user.language_code
        await context.bot.send_message(
            chat_id=update.message.chat.id, text=trans("NO_SEARCH_RESULTS", user_lang)
        )


@restricted
async def searchOnWebTracker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search on web trackers and return results back to user"""
    lang_code = update.message.from_user.language_code

    # 1. Warning for user if wrong content type was passed
    if update.message.effective_attachment:
        await update.message.reply_text(text=trans("UNSUPPORTED_MIME_TYPE", lang_code))
        return
    # 2. Warning for user if too long message was passed
    if len(update.message.text or "") > 256:
        await update.message.reply_text(text=trans("MESSAGE_IS_TOO_LONG", lang_code))
        return
    # 3. Display dumb message while search is running, so user will understand all is good
    msg = await update.message.reply_text(
        text=trans("DOING_SEARCH", lang_code) + " " + str(update.message.text)
    )

    # Create search object
    searcher = SearchTorrents(
        credentials=bot_config.get("trackers", {}),
        sort_by=bot_config.get("bot.sort_by", "date"),
    )
    # Get search results
    posts = PostsBrowser(
        user_id=msg.chat_id,
        user_lang=lang_code,
        posts=await asyncio.to_thread(searcher.search, str(update.message.text)),
    )
    context.user_data["nav_type"] = "posts"
    context.user_data["posts"] = posts

    # Get search results
    if posts.number_of_pages > 0:
        text = posts.get_page()
        reply_markup = posts.get_keyboard()
    # Tell user about empty search results
    else:
        text = trans("NOTHING_FOUND", lang_code)
        reply_markup = None

    # Add warning message if something went wrong while searching on trackers
    if len(searcher.FAILED_SEARCH) > 0:
        text += "\n--------------\n {0} {1}".format(
            trans("FAILED_SEARCH_ON_TRACKERS", lang_code),
            ", ".join(searcher.FAILED_SEARCH),
        )
    if len(searcher.FAILED_TRACKERS) > 0:
        text += "\n--------------\n {0} {1}".format(
            trans("FAILED_INIT_TRACKERS", lang_code),
            ", ".join(searcher.FAILED_TRACKERS),
        )

    await context.bot.edit_message_text(
        chat_id=update.message.chat.id,
        message_id=msg.message_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@restricted
async def torrentStop(update: Update, _: ContextTypes.DEFAULT_TYPE):
    """Stop torrent by torrent_id"""
    await asyncio.to_thread(
        get_torrent_connection().stop_torrent,
        int(update.message.text.split("_")[1]),
    )


@restricted
async def torrentStopAll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop All Torrents"""
    await asyncio.to_thread(get_torrent_connection().stop_all)
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=trans("ALL_STOPPED", update.message.from_user.language_code),
    )


@restricted
async def torrentStart(update: Update, __: ContextTypes.DEFAULT_TYPE):
    """Start torrent by torrent_id"""
    await asyncio.to_thread(
        get_torrent_connection().start_torrent,
        int(update.message.text.split("_")[1]),
    )


@restricted
async def torrentStartAll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start All Torrents"""
    await asyncio.to_thread(get_torrent_connection().start_all)
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=trans("ALL_STARTED", update.message.from_user.language_code),
    )


@restricted
async def torrentList(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List all torrents on Transmission server"""
    torrents = TorrentsListBrowser(
        user_id=update.message.chat.id,
        user_lang=update.message.from_user.language_code,
        posts=await asyncio.to_thread(get_torrent_connection().get_torrents),
    )
    context.user_data["nav_type"] = "torrents"
    context.user_data["torrents"] = torrents
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=torrents.get_page(),
        parse_mode=ParseMode.HTML,
        reply_markup=torrents.get_keyboard(),
    )


@restricted
async def torrentInfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed information about torrent"""
    torrent_id = update.message.text.split("_")[1]
    user_id = update.message.chat.id
    lang_code = update.message.from_user.language_code
    log.info("Loading torrent id %s", torrent_id)
    await _send_torrent_info(context, user_id, int(torrent_id), lang_code)


async def _send_torrent_info(context, chat_id, torrent_id: int, lang_code):
    torrent_info = TorrentInfoBrowser(
        user_id=chat_id,
        user_lang=lang_code,
        posts=await asyncio.to_thread(
            get_torrent_connection().get_torrent, int(torrent_id)
        ),
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=torrent_info.get_page(),
            parse_mode=ParseMode.HTML,
            reply_markup=torrent_info.get_keyboard(),
        )
    except Exception as err:  # noqa: BLE001
        await context.bot.send_message(
            chat_id=chat_id,
            text=trans("SOMETHING_WENT_WRONG", lang_code) + f": {err}",
            parse_mode=ParseMode.HTML,
        )


@restricted
async def torrentDelete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Remove torrent by torrent_id"""
    torrent_id = int(update.message.text.replace("/delete_", ""))
    log.info("Removing torrent id %s", torrent_id)
    lang_code = update.message.from_user.language_code
    try:
        torrent = await asyncio.to_thread(
            get_torrent_connection().get_torrent, torrent_id
        )
        name = torrent.name
    except Exception:  # noqa: BLE001
        name = str(torrent_id)
    try:
        await asyncio.to_thread(
            get_torrent_connection().remove_torrent,
            torrent_id,
            delete_data=bot_config.get("transmission.delete_data"),
        )
    except Exception as err:  # noqa: BLE001
        await context.bot.send_message(
            chat_id=update.message.chat.id,
            text=trans("SOMETHING_WENT_WRONG", lang_code) + f": {err}",
        )
        return
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=trans("TORRENT_REMOVED", lang_code).format(escape_html(name)),
        parse_mode=ParseMode.HTML,
    )
    DownloadQueue.remove(torrent_id)
    # 机器人里手动删的 FC2 任务也记黑名单，不然下一轮推送 / 再发磁力又加回来。
    _remember_fc2_deletion(name)


@restricted
async def callbackTorrentInfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    torrent_id = int(query.data.split(":")[1])
    await _send_torrent_info(
        context, query.message.chat.id, torrent_id, query.from_user.language_code
    )


@restricted
async def callbackDeleteTorrent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """完成通知里的「仅删种子 / 连文件一起删」按钮。"""
    query = update.callback_query
    await query.answer()
    lang_code = query.from_user.language_code
    try:
        _, mode, raw_id = query.data.split(":")
        torrent_id = int(raw_id)
    except (ValueError, TypeError):
        return

    delete_data = mode == "data"
    client = get_torrent_connection()
    try:
        torrent = await asyncio.to_thread(client.get_torrent, torrent_id)
        name = torrent.name
    except Exception:  # noqa: BLE001
        name = str(torrent_id)

    try:
        await asyncio.to_thread(
            client.remove_torrent, torrent_id, delete_data=delete_data
        )
    except Exception as err:  # noqa: BLE001
        await context.bot.send_message(
            chat_id=query.message.chat.id,
            text=trans("SOMETHING_WENT_WRONG", lang_code) + f": {err}",
        )
        return

    DownloadQueue.remove(torrent_id)
    _remember_fc2_deletion(name)
    text = trans(
        "TORRENT_REMOVED_WITH_DATA" if delete_data else "TORRENT_REMOVED_KEEP_DATA",
        lang_code,
    ).format(escape_html(name))
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
    except TelegramError:
        await context.bot.send_message(
            chat_id=query.message.chat.id, text=text, parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------- FC2 频道


def _fc2_store() -> Fc2Store:
    """回调用一个新实例，状态以磁盘为准（不是和推送器共享同一个对象）。"""
    return Fc2Store()


def _fc2_blacklist() -> Fc2Blacklist:
    """黑名单实例：路径取 fc2.blacklist_file，两个容器要指同一个文件才共享。"""
    return Fc2Blacklist(blacklist_config_path(bot_config))


def _fc2_claims() -> Fc2Claims:
    return Fc2Claims(claims_config_path(bot_config))


async def _record_fc2_claim(info_hash, number=None, title: str = "") -> None:
    """添加确认后写共享归属；失败不谎称认领成功，也不撤回已添加的任务。"""
    try:
        result = await asyncio.to_thread(_fc2_claims().append, info_hash, number, title)
        if result == WRITE_FAILED:
            log.error("FC2 任务归属写入失败 number=%s stage=claim_append", number)
    except Exception as err:  # noqa: BLE001
        log.error(
            "FC2 任务归属写入失败 number=%s stage=claim_append exception=%s",
            number,
            type(err).__name__,
        )


def _fc2_handoff_magnet(source) -> MagnetLink | None:
    """只有专用机器人的磁力任务交由下载机器人统一通知。"""
    if not bool(bot_config.get("fc2.enabled", False)):
        return None
    return parse_magnet(str(source or ""))


def _done_list() -> DoneList:
    """成功名单实例：路径取 fc2.done_file，两个容器指同一文件才共享。"""
    return DoneList(done_config_path(bot_config))


def _done_hint(number=None, info_hash=None) -> str | None:
    """查成功名单，命中就返回一条提示（只提示、绝不拦截添加）。

    FC2 任务按归一化编号比；非 FC2 只按 info_hash，没有 hash 就不查
    （不做标题模糊匹配，免得把名字相近的片子误报成下过）。查询失败
    只记 debug，绝不影响添加流程。
    """
    try:
        records = _done_list().find(number=number, info_hash=info_hash)
    except Exception as err:  # noqa: BLE001
        log.debug("查询成功名单失败 %s/%s: %s", number, info_hash, err)
        return None
    if not records:
        return None
    latest = max(records, key=lambda item: item.get("done_at") or "")
    title = _truncate(latest.get("title") or "无标题", 48)
    done_at = latest.get("done_at") or "时间未知"
    return f"📌 之前下载过：{title}（{done_at}）"


def _fc2_number(raw) -> str | None:
    """把用户输入归一成编号：FC2-PPV-5000545 / fc2ppv 5000545 / 5000545 都认。"""
    text = str(raw or "").strip()
    return extract_number(text) or normalize_number(text)


def _fc2_blocked_number(title) -> str | None:
    """标题里能提出编号且该编号在黑名单里，就返回编号，否则 None。"""
    number = extract_number(title)
    if not number:
        return None
    return number if _fc2_blacklist().contains(number) else None


def _remember_fc2_deletion(name) -> None:
    """机器人里删 FC2 任务时顺手记黑名单（名字就取任务名），写失败不能影响删除。"""
    number = extract_number(name)
    if not number:
        return
    try:
        _fc2_blacklist().add(number, str(name or ""), SOURCE_DELETE)
    except Exception as err:  # noqa: BLE001
        log.debug("记 FC2 黑名单失败 %s: %s", number, err)


def _fc2_records() -> list[dict]:
    """从现有下载历史里挑出 FC2 记录，按完成时间倒序。"""
    records = []
    for record in DownloadHistory.show():
        name = str(record.get("name") or "")
        number = extract_number(name)
        if not number:
            continue
        records.append(
            {
                "number": number,
                "name": name,
                "date": str(record.get("date_done") or ""),
                "dir": str(record.get("download_dir") or ""),
            }
        )
    records.sort(key=lambda item: item["date"], reverse=True)
    return records


FC2_DOWNLOADED_LABEL = "✅已添加任务"


def _fc2_downloaded_markup(markup, number: str):
    """复制卡片现有键盘，只把这一条卡片的下载按钮换成「已添加」样式。

    不拿 _keyboard() 重建整副键盘：旧卡片实际发出的键盘可能和新模板
    对不上，复制现成的键盘才能保证其它按钮和布局原样保留。没有键盘、
    或键盘里找不到这一条的下载按钮时返回 None，调用方跳过编辑。
    """
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return None
    target = f"fc2dl:{number}"
    changed = False
    new_rows = []
    for row in rows:
        new_row = []
        for button in row:
            if getattr(button, "callback_data", None) == target:
                if getattr(button, "text", None) == FC2_DOWNLOADED_LABEL:
                    # 已经标过（例如重复点击）：不用再改，返回 None 让调用方跳过。
                    new_row.append(button)
                    continue
                new_row.append(
                    InlineKeyboardButton(
                        FC2_DOWNLOADED_LABEL, callback_data=button.callback_data
                    )
                )
                changed = True
            else:
                new_row.append(button)
        new_rows.append(new_row)
    return InlineKeyboardMarkup(new_rows) if changed else None


async def _mark_fc2_downloaded(bot, query, number: str) -> None:
    """把回调所在卡片的下载按钮改成「已添加」；纯装饰，失败只记日志。

    编辑对象就是回调所在的那条消息本身，私聊和旧频道卡片都一样；
    消息已删、权限不足、网络抖动都不能影响已经建好的下载任务，也不给
    用户多发消息。
    """
    message = getattr(query, "message", None)
    if message is None:
        log.warning("FC2 卡片按钮更新跳过：回调消息不存在 %s", number)
        return
    try:
        markup = _fc2_downloaded_markup(
            getattr(message, "reply_markup", None), number
        )
        if markup is None:
            return
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=markup,
        )
    except Exception as err:  # noqa: BLE001
        log.warning(
            "FC2 卡片下载按钮更新失败 number=%s stage=keyboard exception=%s",
            number,
            type(err).__name__,
        )


def _fc2_callback_authorized(query, number: str, action: str) -> bool:
    authorized = query.from_user.id == _super_user_id()
    message = getattr(query, "message", None)
    log.info(
        "FC2 %s 回调入口 number=%s user=授权%s chat=已脱敏 msg=%s hostname=%s blacklist=%s",
        action,
        number,
        "一致" if authorized else "不一致",
        getattr(message, "message_id", None),
        socket.gethostname(),
        os.path.abspath(blacklist_config_path(bot_config) or blacklist_default_path()),
    )
    if not authorized:
        log.warning(
            "FC2 %s 权限拒绝 number=%s user=授权不一致 chat=已脱敏 stage=authorization",
            action,
            number,
        )
    return authorized


async def fc2_download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """频道卡片「下载」：现场拼 nyaa 磁力，走现有单条添加流程。"""
    query = update.callback_query
    number = query.data.split(":", 1)[1]
    if not _fc2_callback_authorized(query, number, "fc2dl"):
        await query.answer("你没有操作权限")
        return
    if _fc2_blacklist().contains(number):
        log.info("FC2 fc2dl 回调结果 number=%s result=blacklisted", number)
        await query.answer("这个你拉黑过，已跳过")
        return
    entry = _fc2_store().entry(number)
    if not entry:
        log.info("FC2 fc2dl 回调结果 number=%s result=expired", number)
        await query.answer("这条已过期")
        return
    # 进度卡片由 DownloadMonitor 发到私聊；这张订阅卡片只在确认任务已经进
    # 下载器之后，把下载按钮改成「✅已添加任务」（重复点击也如实标上），
    # 其余按钮和卡片文案都不动。黑名单、过期、添加失败时不改按钮。
    # notify=False：本容器的通知队列不记这条，交给下载机器人（torrentino）
    # 发现并推送，避免 fc2bot 自己再发一份。
    try:
        torrent = await asyncio.to_thread(
            get_torrent_connection().add_torrent,
            chat_id=query.from_user.id,
            lang_code="zh",
            torrent=_magnet(entry.get("hash"), entry.get("title")),
            download_dir=None,
            title=entry.get("title"),
            auto_classify=True,
            single_dedup=True,
            notify=False,
            source=SOURCE_FC2_CARD,
        )
    except TorrentAlreadyExists:
        # 任务已经在下载器里：旧卡片晚点再点、或上次改按钮失败后重进，
        # 这里也把按钮补成「已添加」，让卡片和真实状态对齐。重复点击
        # 靠单条去重保证绝不会再建一份任务。
        await _record_fc2_claim(entry.get("hash"), number, str(entry.get("title") or ""))
        await _mark_fc2_downloaded(context.bot, query, number)
        log.info("FC2 fc2dl 回调结果 number=%s result=duplicate", number)
        await query.answer("已经下过了")
        return
    except Exception as err:  # noqa: BLE001
        log.error(
            "FC2 下载添加失败 number=%s stage=add_torrent exception=%s",
            number,
            type(err).__name__,
        )
        log.info("FC2 fc2dl 回调结果 number=%s result=failed", number)
        await query.answer("添加失败")
        return
    await _record_fc2_claim(
        getattr(torrent, "info_hash", None) or entry.get("hash"),
        number,
        str(entry.get("title") or ""),
    )
    await _mark_fc2_downloaded(context.bot, query, number)
    log.info("FC2 fc2dl 回调结果 number=%s result=added", number)
    # 成功名单里查得到就顺带提示，toast 最长 200 字，截短；绝不拦截。
    hint = _done_hint(number=number)
    if hint:
        await query.answer(("已开始下载｜" + hint)[:190])
    else:
        await query.answer("已开始下载")


async def _mark_fc2_muted(bot, message, number: str, recorded: bool) -> tuple[bool, bool]:
    """删不掉封面时移除键盘并如实标记结果，不发送新消息。"""
    cleared = marked = False
    try:
        await bot.edit_message_reply_markup(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=None,
        )
        cleared = True
    except Exception as err:  # noqa: BLE001
        log.warning(
            "FC2 拉黑标记失败 number=%s msg=%s stage=remove_keyboard exception=%s",
            number,
            message.message_id,
            type(err).__name__,
        )
    label = "🚫 已拉黑" if recorded else "⚠️ 黑名单写入失败"
    try:
        caption = getattr(message, "caption", None)
        if caption is not None:
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=message.message_id,
                caption=(label + "\n" + caption)[:1024],
            )
        else:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=(label + "\n" + (getattr(message, "text", None) or ""))[:4096],
            )
        marked = True
    except Exception as err:  # noqa: BLE001
        log.warning(
            "FC2 拉黑标记失败 number=%s msg=%s stage=mark_card exception=%s",
            number,
            message.message_id,
            type(err).__name__,
        )
    return cleared, marked


async def fc2_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """「不感兴趣」：黑名单写入与实际回调卡片删除分别记录、分别反馈。"""
    query = update.callback_query
    number = query.data.split(":", 1)[1]
    if not _fc2_callback_authorized(query, number, "fc2no"):
        await query.answer("你没有操作权限")
        return
    store = _fc2_store()
    record = store.entry(number) or {}
    recorded = False
    try:
        recorded = bool(
            _fc2_blacklist().add(number, record.get("title") or "", SOURCE_MUTE)
        )
    except Exception as err:  # noqa: BLE001
        log.error(
            "FC2 黑名单写入失败 number=%s stage=blacklist_add exception=%s",
            number,
            type(err).__name__,
        )
    if not recorded:
        log.warning("FC2 黑名单写入失败 number=%s stage=blacklist_add", number)
    stored_ids = store.mute(number)
    message = getattr(query, "message", None)
    target_chat = message.chat.id if message is not None else None
    cover_id = getattr(message, "message_id", None)
    targets = []
    if message is not None:
        # 封面只认本次回调。旧 seen 没有 chat 时，消息 ID 不能跨聊天猜配。
        targets.append(cover_id)
        if record.get("chat_id") is not None and str(record["chat_id"]) == str(target_chat):
            targets.extend(mid for mid in stored_ids if mid not in targets)
    removed = 0
    cover_removed = False
    for message_id in targets:
        try:
            await context.bot.delete_message(
                chat_id=target_chat, message_id=int(message_id)
            )
            removed += 1
            if message_id == cover_id:
                cover_removed = True
        except Exception as err:  # noqa: BLE001
            log.warning(
                "FC2 删卡失败 number=%s msg=%s chat=已脱敏 stage=delete_message exception=%s",
                number,
                message_id,
                type(err).__name__,
            )
    cleared = marked = False
    if message is not None and not cover_removed:
        cleared, marked = await _mark_fc2_muted(context.bot, message, number, recorded)
    deleted = bool(targets) and removed == len(targets)
    log.info(
        "FC2 fc2no 回调结果 number=%s user=授权一致 chat=已脱敏 msg=%s hostname=%s blacklist=%s blacklist_written=%s delete=%s/%s keyboard_removed=%s marked=%s",
        number,
        cover_id,
        socket.gethostname(),
        os.path.abspath(blacklist_config_path(bot_config) or blacklist_default_path()),
        recorded,
        removed,
        len(targets),
        cleared,
        marked,
    )
    if recorded and deleted:
        await query.answer("已拉黑并删除，以后不再推送")
    elif recorded:
        await query.answer("已拉黑，但未能删除全部卡片（可能超时或权限不足）")
    elif deleted:
        await query.answer("卡片已删除，但黑名单写入失败，重启后可能还会推送")
    else:
        await query.answer("黑名单写入失败，卡片也未全部删除")


@restricted
async def fc2list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    records = _fc2_records()[:30]
    if not records:
        await context.bot.send_message(
            chat_id=update.message.chat.id, text="还没有 FC2 下载记录"
        )
        return
    lines = ["<b>FC2 下载记录</b>", ""]
    for item in records:
        lines.append(
            f"FC2-PPV-{item['number']}  {escape_html(_truncate(item['name'], 40))}\n"
            f"  {escape_html(item['date'])}  <code>{escape_html(item['dir'])}</code>"
        )
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@restricted
async def fc2info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args or []).strip()
    number = extract_number(raw) or (str(int(raw)) if raw.isdigit() else None)
    chat_id = update.message.chat.id
    if not number:
        await context.bot.send_message(chat_id=chat_id, text="用法：/fc2 <FC2 编号>")
        return
    records = [item for item in _fc2_records() if item["number"] == number]
    if not records:
        await context.bot.send_message(chat_id=chat_id, text="没有这个编号的下载记录")
        return
    lines = [f"<b>FC2-PPV-{number}</b>", ""]
    for item in records:
        lines.append(
            f"{escape_html(item['date'])}  {escape_html(_truncate(item['name'], 40))}\n"
            f"  <code>{escape_html(item['dir'])}</code>"
        )
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------- FC2 黑名单 / 比对


def _fc2_timestamp(value) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError, OverflowError):
        return "-"


def _fc2_title_for(number: str) -> str:
    """给黑名单找名字：先看推送状态里的 seen，再看下载历史。"""
    try:
        record = _fc2_store().entry(number) or {}
    except Exception:  # noqa: BLE001
        record = {}
    title = str(record.get("title") or "")
    if title:
        return title
    try:
        for item in DownloadHistory.show():
            name = str(item.get("name") or "")
            if extract_number(name) == number:
                return name
    except Exception as err:  # noqa: BLE001
        log.debug("读下载历史失败 %s: %s", number, err)
    return ""


def _fc2_compare(torrents, blacklist) -> tuple[list, dict]:
    """比对 tr-bt 任务与黑名单：返回（黑名单命中, 同编号多任务）。

    去重口径：编号统一 extract_number 归一（不带前导零），标题里提不出
    编号的任务不参与比对；同一编号下 info_hash 相同算完全重复，
    info_hash 不同算同编号多版本。
    """
    hits: list[tuple[str, object, dict]] = []
    groups: dict[str, list] = {}
    for torrent in torrents:
        number = extract_number(getattr(torrent, "name", "") or "")
        if not number:
            continue
        groups.setdefault(number, []).append(torrent)
        record = blacklist.get(number)
        if record:
            hits.append((number, torrent, record))
    duplicates = {key: items for key, items in groups.items() if len(items) > 1}
    return hits, duplicates


def _fc2_kept_and_extra(items) -> tuple[object, list, bool]:
    """同编号多任务：建议保留最早添加的那个，其余算多余；并标出同 hash。"""
    ordered = sorted(items, key=lambda item: str(getattr(item, "added_date", "") or ""))
    hashes = {str(getattr(item, "info_hash", "") or "").lower() for item in items}
    return ordered[0], ordered[1:], len(hashes) < len(items)


def _fc2_disk_leftovers(torrents, limit: int = 10) -> tuple[list[str], list[str]]:
    """磁盘有文件、tr-bt 没任务：只扫配置里 directories 的顶层条目。

    机器人容器默认不挂下载目录，这时目录都不存在，报告里如实说明需要
    加只读挂载，不假装能扫磁盘。只报编号在整个 tr-bt 里都找不到的条目，
    避免把多文件任务误报成残留。
    """
    directories = dict(bot_config.get("directories") or {})
    numbers = {extract_number(getattr(item, "name", "") or "") for item in torrents}
    numbers.discard(None)
    found: list[str] = []
    missing: list[str] = []
    for name, path in directories.items():
        if not os.path.isdir(str(path)):
            missing.append(str(path))
            continue
        try:
            with os.scandir(str(path)) as entries:
                for entry in entries:
                    number = extract_number(entry.name)
                    if not number or number in numbers:
                        continue
                    found.append(f"{name}/{entry.name}")
                    if len(found) >= limit:
                        break
        except OSError as err:
            log.debug("扫下载目录失败 %s: %s", path, err)
        if len(found) >= limit:
            break
    return found, missing


@restricted
async def fc2check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """已存影片比对：只出报告，不删任何东西。"""
    chat_id = update.message.chat.id
    if chat_id != _super_user_id():
        await context.bot.send_message(chat_id=chat_id, text="只有超级用户能跑比对")
        return
    try:
        torrents = await asyncio.to_thread(get_torrent_connection().get_torrents)
    except Exception as err:  # noqa: BLE001
        await context.bot.send_message(
            chat_id=chat_id,
            text="读 tr-bt 任务失败：" + escape_html(_truncate(str(err), 120)),
            parse_mode=ParseMode.HTML,
        )
        return
    hits, duplicates = _fc2_compare(torrents, _fc2_blacklist())
    lines = ["<b>FC2 比对报告</b>（只报告，不删任何东西）", ""]
    if hits:
        lines.append(f"🚫 黑名单命中该删：{len(hits)} 个任务")
        for number, torrent, record in hits[:10]:
            lines.append(
                f"  • FC2-PPV-{number}  id={getattr(torrent, 'id', '?')}  "
                f"{escape_html(_truncate(str(getattr(torrent, 'name', '')), 50))}"
            )
            lines.append(
                f"    来源 {escape_html(source_label(record.get('source')))}"
                f" · {escape_html(_fc2_timestamp(record.get('added_ts')))}"
            )
        if len(hits) > 10:
            lines.append(f"  …还有 {len(hits) - 10} 个")
    else:
        lines.append("🚫 黑名单命中：没有")
    lines.append("")
    if duplicates:
        lines.append(f"♻️ 同编号多任务：{len(duplicates)} 组")
        for number, items in list(duplicates.items())[:5]:
            keep, extra, same_hash = _fc2_kept_and_extra(items)
            lines.append(
                f"  • FC2-PPV-{number}：{len(items)} 个任务，建议保留 "
                f"id={getattr(keep, 'id', '?')}，多余 "
                + ",".join(str(getattr(item, "id", "?")) for item in extra)
            )
            lines.append(
                "    同 info_hash："
                + ("有（内容完全重复）" if same_hash else "无（同编号不同版本）")
            )
    else:
        lines.append("♻️ 同编号多任务：没有")
    lines.append("")
    found, missing = _fc2_disk_leftovers(torrents)
    if found:
        disk_result = f"{len(found)} 条（顶层条目）"
    elif missing and len(missing) == len(bot_config.get("directories") or {}):
        disk_result = "未扫描（未挂载）"
    else:
        disk_result = "没扫到"
    lines.append("🗂 磁盘有文件、tr-bt 没任务：" + disk_result)
    lines.extend(f"  • {escape_html(item)}" for item in found)
    if missing:
        lines.append(
            "  未挂载的下载目录："
            + "、".join(escape_html(item) for item in missing[:5])
            + "（容器加只读挂载后才能扫）"
        )
    if hits:
        numbers = " ".join(number for number, _torrent, _record in hits[:10])
        lines.append("")
        lines.append(f"确认要删：<code>/fc2del {escape_html(numbers)}</code>")
        lines.append(
            "（只删 tr-bt 任务，本地文件保留；编号已在黑名单，不会被推送或加回来）"
        )
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@restricted
async def fc2del_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """确认删除：只删黑名单命中的 tr-bt 任务，必须显式给编号。

    /fc2check 只出清单不动手，删是另一步；不带参数什么都不删。
    本地数据默认保留（delete_data=False），要不要连文件一起删由用户另外定。
    """
    chat_id = update.message.chat.id
    if chat_id != _super_user_id():
        await context.bot.send_message(chat_id=chat_id, text="只有超级用户能删任务")
        return
    numbers: list[str] = []
    for raw in context.args or []:
        number = _fc2_number(raw)
        if number and number not in numbers:
            numbers.append(number)
    if not numbers:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "用法：/fc2del <编号...>，编号从 /fc2check 的报告里抄。\n"
                "只删黑名单命中的任务，本地文件保留；不给编号就什么都不删。"
            ),
        )
        return
    blacklist = _fc2_blacklist()
    allowed = [number for number in numbers if blacklist.contains(number)]
    refused = [number for number in numbers if number not in allowed]
    if not allowed:
        await context.bot.send_message(
            chat_id=chat_id,
            text="这些编号不在黑名单里，未删任何任务："
            + "、".join(f"FC2-PPV-{number}" for number in refused)
            + "\n要删先 /fc2block <编号>。",
        )
        return
    client = get_torrent_connection()
    try:
        torrents = await asyncio.to_thread(client.get_torrents)
    except Exception as err:  # noqa: BLE001
        await context.bot.send_message(
            chat_id=chat_id, text="读 tr-bt 任务失败：" + _truncate(str(err), 120)
        )
        return
    wanted = set(allowed)
    targets = [
        torrent
        for torrent in torrents
        if extract_number(getattr(torrent, "name", "") or "") in wanted
    ]
    if not targets:
        await context.bot.send_message(
            chat_id=chat_id,
            text="tr-bt 里没有这些编号的任务："
            + "、".join(f"FC2-PPV-{number}" for number in allowed),
        )
        return
    removed, failed = [], []
    for torrent in targets:
        number = extract_number(getattr(torrent, "name", "") or "")
        try:
            await asyncio.to_thread(
                client.remove_torrent, torrent.id, delete_data=False
            )
        except Exception as err:  # noqa: BLE001
            failed.append((number, getattr(torrent, "id", "?"), str(err)))
            continue
        DownloadQueue.remove(torrent.id)
        removed.append((number, getattr(torrent, "id", "?")))
    lines = [f"已删除 {len(removed)} 个 tr-bt 任务（本地文件保留）："]
    lines.extend(
        f"  • FC2-PPV-{number}  id={torrent_id}" for number, torrent_id in removed
    )
    if failed:
        lines.append(f"删除失败 {len(failed)} 个：")
        lines.extend(
            f"  • FC2-PPV-{number}  id={torrent_id}："
            + escape_html(_truncate(err, 80))
            for number, torrent_id, err in failed
        )
    if refused:
        lines.append(
            "未在黑名单、未动：" + "、".join(f"FC2-PPV-{n}" for n in refused)
        )
    lines.append("编号留在黑名单里，不会再被推送 / 加回来；要恢复用 /fc2unblock <编号>。")
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@restricted
async def fc2block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动拉黑：/fc2block <编号> 记一条；不带参数列出现有黑名单。"""
    chat_id = update.message.chat.id
    if chat_id != _super_user_id():
        await context.bot.send_message(chat_id=chat_id, text="只有超级用户能改黑名单")
        return
    raw = " ".join(context.args or []).strip()
    if not raw:
        entries = _fc2_blacklist().all()
        if not entries:
            await context.bot.send_message(
                chat_id=chat_id, text="黑名单是空的。用法：/fc2block <FC2 编号>"
            )
            return
        lines = [f"<b>FC2 黑名单</b>（{len(entries)} 条）", ""]
        for record in entries[:30]:
            title = escape_html(_truncate(record.get("title") or "（没有名字）", 40))
            lines.append(
                f"FC2-PPV-{record['number']}  {title}\n"
                f"  {escape_html(source_label(record.get('source')))}"
                f" · {escape_html(_fc2_timestamp(record.get('added_ts')))}"
            )
        if len(entries) > 30:
            lines.append(f"…还有 {len(entries) - 30} 条")
        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return
    number = _fc2_number(raw)
    if not number:
        await context.bot.send_message(
            chat_id=chat_id, text="识别不出 FC2 编号，用法：/fc2block <FC2 编号>"
        )
        return
    try:
        title = _fc2_title_for(number)
        blacklist = _fc2_blacklist()
        recorded = bool(blacklist.add(number, title, BLACKLIST_SOURCE_MANUAL))
    except Exception as err:  # noqa: BLE001
        log.warning("记 FC2 黑名单失败 %s: %s", number, err)
        blacklist = None
        recorded = False
    if not recorded:
        record, read_ok = (None, True)
        if blacklist is not None:
            record, read_ok = blacklist.lookup(number)
        if blacklist is not None and not read_ok:
            text = f"读不到黑名单，FC2-PPV-{number} 这次没确认也没改动（读取失败）"
        elif record:
            text = (
                f"FC2-PPV-{number} 条目本来就在黑名单里，拦截仍然生效；"
                "只是标题/来源补写没写成功（写盘失败）"
            )
        else:
            text = (
                f"记录失败，FC2-PPV-{number} 没有进黑名单（写盘失败），"
                "拦截不会生效"
            )
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        return
    suffix = (
        f"（{escape_html(_truncate(title, 40))}）"
        if title
        else "（名字没查到，只记了编号）"
    )
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"已拉黑 FC2-PPV-{number}{suffix}",
        parse_mode=ParseMode.HTML,
    )


@restricted
async def fc2unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """取消拉黑：黑名单删编号，同时把 state 里的 muted 一并解除。"""
    chat_id = update.message.chat.id
    if chat_id != _super_user_id():
        await context.bot.send_message(chat_id=chat_id, text="只有超级用户能改黑名单")
        return
    numbers: list[str] = []
    for raw in context.args or []:
        number = _fc2_number(raw)
        if number and number not in numbers:
            numbers.append(number)
    if not numbers:
        await context.bot.send_message(
            chat_id=chat_id, text="用法：/fc2unblock <编号...>"
        )
        return
    blacklist = _fc2_blacklist()
    store = _fc2_store()
    removed, failed, missing, unreadable = [], [], [], []
    for number in numbers:
        if blacklist.remove(number):
            removed.append(number)
            store.unmute(number)
            continue
        record, read_ok = blacklist.lookup(number)
        if not read_ok:
            unreadable.append(number)
        elif record:
            failed.append(number)
        else:
            missing.append(number)
    lines = []
    if removed:
        lines.append(
            "已取消拉黑：" + "、".join(f"FC2-PPV-{number}" for number in removed)
        )
    if failed:
        lines.append(
            "取消拉黑没生效，"
            + "、".join(f"FC2-PPV-{number}" for number in failed)
            + " 仍在黑名单里（写盘失败）"
        )
    if unreadable:
        lines.append(
            "读不到黑名单（读取失败），这些编号一个都没动："
            + "、".join(f"FC2-PPV-{number}" for number in unreadable)
        )
    if missing:
        lines.append(
            "这些编号不在黑名单里："
            + "、".join(f"FC2-PPV-{number}" for number in missing)
        )
    if lines:
        await context.bot.send_message(chat_id=chat_id, text="\n".join(lines))


# ---------------------------------------------------------------- 状态 / 限速 / 规则

@restricted
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang_code = update.message.from_user.language_code
    client = get_torrent_connection()
    overview = await asyncio.to_thread(client.overview)

    lines = [
        trans("STATUS_TITLE", lang_code),
        trans("STATUS_TASKS", lang_code).format(
            overview["total"],
            overview["downloading"],
            overview["seeding"],
            overview["stopped"],
        ),
        trans("STATUS_SPEED", lang_code).format(
            speed_to_human(overview["speed_down"]),
            speed_to_human(overview["speed_up"]),
        ),
        trans("STATUS_SESSION", lang_code).format(
            bytes_to_human(overview["session_down"]),
            bytes_to_human(overview["session_up"]),
        ),
        trans("STATUS_TOTAL", lang_code).format(
            bytes_to_human(overview["total_down"]),
            bytes_to_human(overview["total_up"]),
        ),
    ]

    try:
        session = await asyncio.to_thread(client.get_session)
        down_limit = (
            session.speed_limit_down if session.speed_limit_down_enabled else None
        )
        up_limit = session.speed_limit_up if session.speed_limit_up_enabled else None
        if down_limit or up_limit:
            lines.append(
                trans("STATUS_LIMIT", lang_code).format(
                    f"{down_limit or 0} KB/s", f"{up_limit or 0} KB/s"
                )
            )
    except Exception as err:  # noqa: BLE001
        log.debug("读取限速配置失败: %s", err)

    directories = dict(bot_config.get("directories") or {})
    if directories:
        lines.append("")
        lines.append(trans("STATUS_DISK", lang_code))
        for name, path in directories.items():
            try:
                free = await asyncio.to_thread(client.free_space, str(path))
                size_text = bytes_to_human(free) if free is not None else "?"
            except Exception:  # noqa: BLE001
                size_text = "?"
            lines.append(
                f"  • {escape_html(str(name))}  <code>{escape_html(str(path))}</code>  "
                f"{trans('STATUS_FREE', lang_code).format(size_text)}"
            )

    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@restricted
async def speed_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang_code = update.message.from_user.language_code
    client = get_torrent_connection()
    args = context.args or []

    if not args:
        session = await asyncio.to_thread(client.get_session)
        down = session.speed_limit_down if session.speed_limit_down_enabled else None
        up = session.speed_limit_up if session.speed_limit_up_enabled else None
        current = trans("SPEED_UNLIMITED", lang_code)
        if down or up:
            current = trans("SPEED_CURRENT", lang_code).format(
                f"{down or 0} KB/s", f"{up or 0} KB/s"
            )
        await context.bot.send_message(
            chat_id=update.message.chat.id,
            text=trans("SPEED_USAGE", lang_code).format(current),
            parse_mode=ParseMode.HTML,
        )
        return

    if args[0].lower() in ("off", "0", "none", "取消"):
        await asyncio.to_thread(client.set_speed_limit, None, None)
        await context.bot.send_message(
            chat_id=update.message.chat.id, text=trans("SPEED_OFF", lang_code)
        )
        return

    down = parse_speed(args[0])
    up = parse_speed(args[1]) if len(args) > 1 else None
    if down is None:
        await context.bot.send_message(
            chat_id=update.message.chat.id,
            text=trans("SPEED_USAGE", lang_code).format(""),
        )
        return
    await asyncio.to_thread(client.set_speed_limit, down, up)
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=trans("SPEED_SET", lang_code).format(
            f"{down} KB/s", f"{up if up is not None else 0} KB/s"
        ),
    )


@restricted
async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang_code = update.message.from_user.language_code
    classifier = get_classifier()
    lines = [trans("RULES_TITLE", lang_code), ""]
    lines.extend(classifier.describe())
    lines.append("")
    lines.append(trans("RULES_HINT", lang_code))
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@restricted
async def classify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lang_code = update.message.from_user.language_code
    name = " ".join(context.args or []).strip()
    if not name:
        await context.bot.send_message(
            chat_id=update.message.chat.id, text=trans("CLASSIFY_USAGE", lang_code)
        )
        return
    classifier = get_classifier()
    result = classifier.classify(name)
    path = classifier.resolve_path(result.directory) or "?"
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=trans("CLASSIFY_RESULT", lang_code).format(
            escape_html(name),
            escape_html(result.directory),
            escape_html(result.rule),
            escape_html(result.matched or "-"),
            escape_html(path),
        ),
        parse_mode=ParseMode.HTML,
    )


@restricted
async def addNewUser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.id == _super_user_id():
        hash = secrets.token_urlsafe(24)
        WELCOME_HASHES[hash] = time.monotonic() + WELCOME_LINK_TTL
        message = f"https://t.me/{context.bot.username}?start=welcome_{hash}"
        img = get_qr_code(message)
        try:
            with open(img, "rb") as qr_image:
                await context.bot.send_photo(
                    update.message.chat.id, qr_image, caption=message
                )
        finally:
            os.unlink(img)
    else:
        await context.bot.send_message(update.message.chat.id, "Nice try!")


async def welcomeNewUser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    assert update.message.text is not None
    hash_code = update.message.text.replace("/start welcome_", "")
    expires_at = WELCOME_HASHES.pop(hash_code, 0)
    if expires_at >= time.monotonic() and update.message.chat.id:
        bot_config.add_user(update.message.chat.id)
        lang_code = update.message.from_user.language_code
        await context.bot.send_message(
            update.message.chat.id,
            trans("WELCOME_NEW_USER", lang_code).format(update.message.chat.first_name),
            reply_markup=bot_config.get_actions_keyboard(ACTIONS, lang_code),
        )
        log.info(
            "New user %s, %s was added.",
            update.message.chat.id,
            update.message.chat.first_name,
        )
        await help_command(update, context)


HANDLERS = [
    # 新用户邀请链接
    # token_urlsafe 会生成 - 和 _，旧正则只收字母数字，带这两种字符的
    # 邀请链接永远匹配不上。
    MessageHandler(Regex(r"^/start\ welcome_[A-Za-z0-9_-]+$"), welcomeNewUser),
    # 一条消息里可能有多条磁力链接，优先处理
    MessageHandler(MagnetMessageFilter(), batchMagnets),
    # 命令
    CommandHandler("help", help_command),
    CommandHandler("start", help_command),
    CommandHandler("adduser", addNewUser),
    CommandHandler("last_search", lastSearchResults),
    CommandHandler("torrents", torrentList),
    CommandHandler("status", status_command),
    CommandHandler("stop_all", torrentStopAll),
    CommandHandler("start_all", torrentStartAll),
    CommandHandler("history", history),
    CommandHandler("rules", rules_command),
    CommandHandler("classify", classify_command),
    CommandHandler("speed", speed_command),
    # FC2 频道：记录查询 / 黑名单 / 已存比对
    CommandHandler("fc2list", fc2list_command),
    CommandHandler("fc2", fc2info_command),
    CommandHandler("fc2check", fc2check_command),
    CommandHandler("fc2del", fc2del_command),
    CommandHandler("fc2block", fc2block_command),
    CommandHandler("fc2unblock", fc2unblock_command),
    # 下载成功名单（两个机器人共用同一份共享文件，实现见 lib/done_commands.py）
    CommandHandler("fc2done", fc2done_command),
    # 底部键盘按钮（多语言文案）
    MessageHandler(Regex(button_regex("menu.torrents")), torrentList),
    MessageHandler(Regex(button_regex("menu.search")), lastSearchResults),
    MessageHandler(Regex(button_regex("menu.status")), status_command),
    MessageHandler(Regex(button_regex("menu.help")), help_command),
    MessageHandler(Regex(button_regex("menu.stop_all")), torrentStopAll),
    MessageHandler(Regex(button_regex("menu.start_all")), torrentStartAll),
    MessageHandler(Regex(button_regex("menu.history")), history),
    MessageHandler(Regex(button_regex("menu.rules")), rules_command),
    MessageHandler(Regex(button_regex("menu.adduser")), addNewUser),
    # 内联按钮：任务操作
    MessageHandler(Regex(r"^/info_[0-9]+$"), torrentInfo),
    MessageHandler(Regex(r"^/stop_[0-9]+$"), torrentStop),
    MessageHandler(Regex(r"^/start_[0-9]+$"), torrentStart),
    MessageHandler(Regex(r"^/delete_[0-9]+$"), torrentDelete),
    MessageHandler(Regex(r"^/torrent_[0-9]+$"), getTorrentFile),
    # 文件名里带磁力链接的 .torrent 描述不处理，交给普通文档流程
    MessageHandler(Regex(r"^/download_[0-9]+$"), chooseDownloadDir),
    # 普通 http(s) 种子地址
    MessageHandler(Entity("url"), chooseDownloadDir),
    MessageHandler(
        Regex(
            r"^(magnet:\?xt=urn:btih:[a-zA-Z0-9]*|[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b([-a-zA-Z0-9()@:%_\+.~#?&\/\/=]*))$"
        ),
        chooseDownloadDir,
    ),
    # Ask download directory for torrent file
    MessageHandler(
        Document.MimeType("application/x-bittorrent")
        | Document.FileExtension("torrent"),
        chooseDownloadDir,
    ),
    # FC2 频道卡片的按钮（必须在下方分页与兜底之前）
    CallbackQueryHandler(fc2_download, pattern=r"^fc2dl:\d+$"),
    CallbackQueryHandler(fc2_mute, pattern=r"^fc2no:\d+$"),
    # 导航按钮（分页）
    CallbackQueryHandler(getMenuPage, pattern=r"^[x0-9]+$"),
    # 完成通知里的按钮
    CallbackQueryHandler(callbackTorrentInfo, pattern=r"^ti:[0-9]+$"),
    CallbackQueryHandler(callbackDeleteTorrent, pattern=r"^rm:(keep|data):[0-9]+$"),
    # 目录选择（自动分类 / 绝对路径）
    CallbackQueryHandler(confirmDirChoice, pattern=r"^(?:__auto__|/.*)$"),
    # 自定义相对目录的兜底（放在最后，不会抢上面的精确匹配）
    CallbackQueryHandler(confirmDirChoice),
    # Default search input text
    MessageHandler(ALL, searchOnWebTracker),
]
