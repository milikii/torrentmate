"""/fc2done：查下载成功名单（两个机器人共用同一份共享文件）。

命令单独放这里而不是塞进 bot_handlers：两个容器注册的是同一套
HANDLERS、读的是同一个 fc2.done_file，查出来的结果天然一致。
"""

from __future__ import annotations

import os

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from lib.auth import restricted
from lib.func import escape_html, get_logger
from models.BotConfigurator import BotConfigurator
from models.DoneList import DoneList, config_path, source_label
from models.Fc2Blacklist import normalize_number
from models.Fc2Channel import extract_number

log = get_logger("DoneCommands")

bot_config = BotConfigurator()


def _super_user_id():
    """超级用户 ID：YAML 里可能是字符串，必须转 int 再比较。"""
    try:
        value = bot_config.get("bot.super_user")
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _done_list() -> DoneList:
    return DoneList(config_path(bot_config))


def _truncate(text: str, limit: int = 48) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _label(record: dict) -> str:
    if record.get("number"):
        head = f"FC2-PPV-{record['number']}"
    else:
        head = escape_html(_truncate(record.get("title") or "无标题"))
    return head


def _describe(record: dict) -> str:
    """一条记录的展示文本：标题 / 完成时间 / 来源 / 大小 / hash。"""
    title = escape_html(_truncate(record.get("title") or "无标题"))
    done_at = escape_html(record.get("done_at") or "-")
    lines = [
        f"{_label(record)}  {title}",
        f"  🕒 {done_at} · 来源 {escape_html(source_label(record.get('source')))}",
    ]
    size = int(record.get("size_when_done") or 0)
    if size > 0:
        lines[1] += f" · 💾 {size} 字节"
    if record.get("info_hash"):
        lines.append(f"  <code>{escape_html(record['info_hash'])}</code>")
    return "\n".join(lines)


@restricted
async def fc2done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/fc2done [编号|export]``：只服务超级用户。"""
    chat_id = update.message.chat.id
    if update.effective_user.id != _super_user_id():
        await context.bot.send_message(chat_id=chat_id, text="只有超级用户能查成功名单")
        return
    done = _done_list()
    raw = " ".join(context.args or []).strip()

    if raw.lower() == "export":
        await _export(update, context, done)
        return
    if raw:
        await _show_number(update, context, done, raw)
        return
    await _show_summary(update, context, done)


async def _export(update, context, done: DoneList) -> None:
    """把共享名单文件原样发给用户（JSONL，一行一条）。"""
    chat_id = update.message.chat.id
    if not os.path.isfile(done.path):
        await context.bot.send_message(chat_id=chat_id, text="还没有成功名单文件")
        return
    total = len(done.all())
    try:
        with open(done.path, "rb") as handle:
            await context.bot.send_document(
                chat_id=chat_id,
                document=handle,
                caption=f"下载成功名单：{total} 条",
            )
    except Exception as err:  # noqa: BLE001
        log.warning("发送成功名单文件失败 %s: %s", done.path, err)
        await context.bot.send_message(
            chat_id=chat_id, text="发送成功名单文件失败：" + escape_html(str(err)),
            parse_mode=ParseMode.HTML,
        )


async def _show_number(update, context, done: DoneList, raw: str) -> None:
    chat_id = update.message.chat.id
    number = extract_number(raw) or normalize_number(raw)
    if not number:
        await context.bot.send_message(
            chat_id=chat_id, text="用法：/fc2done <FC2 编号>、/fc2done export 或无参数看最近记录"
        )
        return
    records = done.find(number=number)
    if not records:
        await context.bot.send_message(
            chat_id=chat_id, text=f"成功名单里没有 FC2-PPV-{number}"
        )
        return
    lines = [f"<b>FC2-PPV-{number}</b> 共 {len(records)} 条成功记录", ""]
    for record in records:
        lines.append(_describe(record))
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def _show_summary(update, context, done: DoneList) -> None:
    chat_id = update.message.chat.id
    total = done.count()
    if not total:
        await context.bot.send_message(chat_id=chat_id, text="还没有成功下载记录")
        return
    lines = [f"<b>下载成功名单</b>：共 {total} 条", "", "最近 10 条："]
    for record in done.recent(10):
        lines.append(_describe(record))
    lines.append("")
    lines.append("查单条：<code>/fc2done 编号</code> · 导出：<code>/fc2done export</code>")
    await context.bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
