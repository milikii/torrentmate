import asyncio
import os
import pathlib
import re
import secrets
import tempfile
import time

import pydash as _
from telegram import Update
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
from lib.magnet import MagnetLink, extract_magnets
from lib.render import render_progress_card
from models.BotConfigurator import BotConfigurator
from models.Browser import Browser
from models.DownloadHistory import DownloadHistory
from models.DownloadQueue import DownloadQueue
from models.HistoryBrowser import HistoryBrowser
from models.PostsBrowser import PostsBrowser
from models.SearchTorrents import SearchTorrents
from models.TorrentInfoBrowser import TorrentInfoBrowser
from models.TorrentsListBrowser import TorrentsListBrowser
from models.TransmissionClient import TransmissionClient

bot_config = BotConfigurator()

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
    if update.message.chat.id == bot_config.get("bot.super_user"):
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
    await update.message.reply_text(
        trans("CHOOSE_DOWNLOAD_DIR", lang_code).format(_truncate(title or "", 80)),
        reply_markup=_dir_keyboard(lang_code),
    )


# ---------------------------------------------------------------- 批量磁力链接


@restricted
async def batchMagnets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """识别消息中的全部磁力链接，展示分类预览并等待选择目录。"""
    lang_code = update.message.from_user.language_code
    text = update.message.text or update.message.caption or ""
    links = extract_magnets(text, limit=MAX_BATCH_LINKS)
    if not links:
        return

    context.user_data["batch"] = links
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

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=_dir_keyboard(lang_code),
        disable_web_page_preview=True,
    )


async def _add_batch(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE, dir_choice: str
):
    query = update.callback_query
    lang_code = query.from_user.language_code
    links: list[MagnetLink] = context.user_data.pop("batch", [])
    if not links:
        return

    client = get_torrent_connection()
    existing = await asyncio.to_thread(client.existing_hashes)
    show_cards = len(links) <= 8 and bool(bot_config.get("notify.progress", True))

    chat_id = query.message.chat.id
    message_id = query.message.message_id
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=trans("BATCH_ADDING", lang_code).format(0, len(links)),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as err:
        log.debug("无法更新批量提示消息: %s", err)
        message_id = None

    added, duplicates, failed = [], [], []
    for index, link in enumerate(links, 1):
        target, auto = _resolve_target(dir_choice, link.name)
        if link.info_hash in existing:
            duplicates.append((link, existing[link.info_hash]))
            continue
        try:
            torrent = await asyncio.to_thread(
                client.add_torrent,
                chat_id=chat_id,
                lang_code=lang_code,
                torrent=link.raw,
                download_dir=target,
                title=link.name,
                auto_classify=auto,
            )
            existing[link.info_hash] = torrent
            added.append((link, torrent, target, auto))
        except Exception as err:  # noqa: BLE001
            log.error("添加磁力链接失败 %s: %s", link.info_hash, err)
            failed.append((link, err))

        if message_id and index % 3 == 0:
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=trans("BATCH_ADDING", lang_code).format(index, len(links)),
                    parse_mode=ParseMode.HTML,
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
    if message_id:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            return
        except TelegramError:
            pass
    await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML
    )


async def _add_single(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE, dir_choice: str
):
    query = update.callback_query
    lang_code = query.from_user.language_code
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

    tmp_file_path = None
    is_temp_file = False
    torrent_source = data.get("url") or data.get("file_name") or ""
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

        log.info("Adding file/URL %s to Transmission", torrent_source)
        torrent = await asyncio.to_thread(
            get_torrent_connection().add_torrent,
            chat_id=query.from_user.id,
            lang_code=lang_code,
            torrent=torrent_source,
            download_dir=target,
            title=data.get("title"),
            auto_classify=auto,
        )
    except Exception as err:  # noqa: BLE001
        text = trans("ADDING_FILE_SOMETHING_WENT_WRONG", lang_code) + ":\n" + str(err)
        if any(
            error_text in str(err).lower()
            for error_text in ("invalid or corrupt torrent file", "unrecognized info")
        ):
            text = trans("ADDING_TORRENT_FILE_IS_CORRUPTED", lang_code)
        await context.bot.send_message(chat_id=chat_id, text=text)
        log.error("File %s was not added due to error %s", torrent_source, err)
        return
    finally:
        if is_temp_file and isinstance(tmp_file_path, pathlib.Path):
            if tmp_file_path.is_file():
                os.unlink(tmp_file_path)

    # 发送初始进度卡片，后续由 DownloadMonitor 原地更新
    entry = DownloadQueue.get(torrent.id)
    try:
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

    if auto:
        await context.bot.send_message(
            chat_id=chat_id, text=trans("AUTO_CLASSIFY_ON", lang_code)
        )


@restricted
async def confirmDirChoice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """用户选择目录后的统一入口（单条 / 批量）。"""
    query = update.callback_query
    await query.answer()
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
    await context.bot.send_message(
        chat_id=update.message.chat.id,
        text=trans("TORRENT_REMOVED", lang_code).format(escape_html(name)),
        parse_mode=ParseMode.HTML,
    )
    await asyncio.to_thread(
        get_torrent_connection().remove_torrent,
        torrent_id,
        delete_data=bot_config.get("transmission.delete_data"),
    )
    DownloadQueue.remove(torrent_id)


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
    if update.message.chat.id == bot_config.get("bot.super_user"):
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
    MessageHandler(Regex(r"^/start\ welcome_[A-Za-z0-9]+$"), welcomeNewUser),
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
