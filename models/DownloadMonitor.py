"""下载监控：进度推送、自动归类、完成通知。"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from transmission_rpc.torrent import Torrent

from lib.classifier import get_classifier
from lib.constants import PROGRESS_EDIT_INTERVAL, QUEUE_CHECK_INTERVAL
from lib.func import escape_html, get_logger, speed_to_human, trans
from lib.render import render_done_card, render_progress_card
from models.BotConfigurator import BotConfigurator
from models.DownloadHistory import DownloadHistory
from models.DownloadQueue import DownloadQueue, QueueEntry

log = get_logger("DownloadMonitor")


class DownloadMonitor:
    """定期轮询 Transmission，把状态变化推送给发起下载的用户。"""

    def __init__(self, client, bot: Bot) -> None:
        self.client = client
        self.bot = bot
        self.config = BotConfigurator()
        self.interval = max(
            int(self.config.get("notify.check_interval", QUEUE_CHECK_INTERVAL) or 30),
            10,
        )
        self.edit_interval = max(
            int(
                self.config.get("notify.progress_interval", PROGRESS_EDIT_INTERVAL)
                or 45
            ),
            15,
        )
        self.progress_enabled = bool(self.config.get("notify.progress", True))
        self.milestones = [
            int(x)
            for x in (self.config.get("notify.milestones", []) or [])
            if str(x).strip().lstrip("-").isdigit()
        ]
        self.stalled_minutes = int(self.config.get("notify.stalled_minutes", 0) or 0)
        self.pause_on_complete = bool(
            self.config.get("transmission.pause_on_complete", False)
        )

    async def run(self) -> None:
        log.info("下载监控已启动，轮询间隔 %s 秒", self.interval)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                log.exception("下载监控轮询异常: %s", err)
            await asyncio.sleep(self.interval)

    async def tick(self) -> None:
        entries = DownloadQueue.snapshot()
        if not entries:
            return
        for torrent_id, entry in entries.items():
            try:
                torrent = await asyncio.to_thread(self.client.get_torrent, torrent_id)
            except KeyError:
                log.warning("任务 %s 已不存在，移出通知队列", torrent_id)
                DownloadQueue.remove(torrent_id)
                continue
            except Exception as err:  # noqa: BLE001
                log.warning("读取任务 %s 状态失败: %s", torrent_id, err)
                continue
            try:
                await self.handle(torrent, entry)
            except Exception as err:  # noqa: BLE001
                log.exception("处理任务 %s 通知失败: %s", torrent_id, err)

    async def handle(self, torrent: Torrent, entry: QueueEntry) -> None:
        # 1. 任务出错，只提醒一次
        if torrent.error_string and not entry.notified_error:
            entry.notified_error = True
            DownloadQueue.update(entry)
            await self._send(
                entry,
                trans("TORRENT_ERROR", entry.lang_code).format(
                    escape_html(torrent.name),
                    escape_html(torrent.error_string),
                ),
            )
            return

        # 2. 磁力链接还在获取元数据
        if int(torrent.metadata_percent_complete or 0) < 100:
            await self._update_card(torrent, entry, metadata=True)
            return

        # 3. 自动归类（只执行一次）
        await self._maybe_classify(torrent, entry)

        # 4. 下载完成
        if float(torrent.percent_done or 0) >= 1 and not entry.finished:
            await self._complete(torrent, entry)
            return

        # 5. 进度推送
        await self._update_card(torrent, entry)

        # 6. 长时间无进度提醒
        await self._check_stalled(torrent, entry)

    async def _maybe_classify(self, torrent: Torrent, entry: QueueEntry) -> None:
        if not entry.auto_classify or entry.classified:
            return

        classifier = get_classifier()
        result = classifier.classify(torrent.name)
        target = classifier.resolve_path(result.directory) or entry.requested_dir

        entry.classified = True
        entry.final_dir = target or torrent.download_dir
        DownloadQueue.update(entry)

        if not target or os.path.normpath(str(target)) == os.path.normpath(
            str(torrent.download_dir)
        ):
            return

        try:
            await asyncio.to_thread(
                self.client.change_torrent, torrent.id, location=target
            )
            if float(torrent.percent_done or 0) > 0:
                await asyncio.to_thread(
                    self.client.move_torrent_data, torrent.id, target, move=True
                )
            log.info("任务 %s 自动归类到 %s", torrent.name, target)
            await self._send(
                entry,
                trans("CLASSIFIED", entry.lang_code).format(
                    escape_html(torrent.name), escape_html(target), result.rule
                ),
            )
        except Exception as err:  # noqa: BLE001
            log.error("任务 %s 自动归类失败: %s", torrent.name, err)
            await self._send(
                entry,
                trans("CLASSIFY_FAILED", entry.lang_code).format(
                    escape_html(torrent.name), escape_html(str(err))
                ),
            )

    async def _complete(self, torrent: Torrent, entry: QueueEntry) -> None:
        entry.finished = True
        DownloadHistory.add(
            torrent.done_date,
            torrent.name,
            torrent.download_dir,
            torrent.size_when_done,
        )

        elapsed = None
        try:
            if torrent.done_date and torrent.added_date:
                elapsed = (torrent.done_date - torrent.added_date).total_seconds()
        except Exception:  # noqa: BLE001
            elapsed = None

        text = render_done_card(torrent, entry.lang_code, None, elapsed)
        keyboard = self._done_keyboard(torrent, entry.lang_code)
        log.info("下载完成: %s -> %s", torrent.name, torrent.download_dir)

        if entry.message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=entry.chat_id,
                    message_id=entry.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                    disable_web_page_preview=True,
                )
            except TelegramError as err:
                log.debug("进度卡片更新失败，改为发送新消息: %s", err)
                await self._send(entry, text, keyboard)
        else:
            await self._send(entry, text, keyboard)

        DownloadQueue.remove(torrent.id)

        if self.pause_on_complete and torrent.status == "seeding":
            try:
                await asyncio.to_thread(self.client.stop_torrent, torrent.id)
                await self._send(entry, trans("PAUSED_ON_COMPLETE", entry.lang_code))
            except Exception as err:  # noqa: BLE001
                log.warning("完成后暂停失败: %s", err)

    def _done_keyboard(
        self, torrent: Torrent, lang_code: str | None
    ) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        trans("BTN_DETAILS", lang_code),
                        callback_data=f"ti:{torrent.id}",
                    ),
                    InlineKeyboardButton(
                        trans("BTN_DEL_KEEP", lang_code),
                        callback_data=f"rm:keep:{torrent.id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        trans("BTN_DEL_DATA", lang_code),
                        callback_data=f"rm:data:{torrent.id}",
                    )
                ],
            ]
        )

    async def _update_card(  # noqa: C901
        self, torrent: Torrent, entry: QueueEntry, metadata: bool = False
    ) -> None:
        if not self.progress_enabled:
            return
        if not entry.progress_card and not entry.message_id:
            return

        percent = float(torrent.percent_done or 0) * 100
        now = time.time()
        if metadata:
            # 元数据阶段只在第一次或间隔足够久时更新
            if entry.last_edit_ts and now - entry.last_edit_ts < self.edit_interval:
                return
        else:
            changed = percent - float(entry.last_percent or 0) >= 5
            if (
                not changed
                and now - float(entry.last_edit_ts or 0) < self.edit_interval
            ):
                return

        text = render_progress_card(
            torrent, entry.lang_code, entry.final_dir, entry.title
        )

        if entry.message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=entry.chat_id,
                    message_id=entry.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError as err:
                log.debug("进度卡片更新失败: %s", err)
                message = await self._send(entry, text)
                if message:
                    entry.message_id = message.message_id
        else:
            message = await self._send(entry, text)
            if message:
                entry.message_id = message.message_id

        entry.last_edit_ts = now
        entry.last_percent = percent
        DownloadQueue.update(entry)

        await self._maybe_milestone(torrent, entry, percent)

    async def _maybe_milestone(
        self, torrent: Torrent, entry: QueueEntry, percent: float
    ) -> None:
        if not self.milestones:
            return
        for milestone in self.milestones:
            if percent >= milestone and milestone not in entry.milestones_sent:
                entry.milestones_sent.add(milestone)
                DownloadQueue.update(entry)
                await self._send(
                    entry,
                    trans("MILESTONE", entry.lang_code).format(
                        escape_html(torrent.name), milestone
                    ),
                )

    async def _check_stalled(self, torrent: Torrent, entry: QueueEntry) -> None:
        if not self.stalled_minutes:
            return
        percent = float(torrent.percent_done or 0) * 100
        if percent > float(entry.last_progress_value or 0) + 0.01:
            entry.last_progress_value = percent
            entry.last_progress_ts = time.time()
            entry.notified_stalled = False
            DownloadQueue.update(entry)
            return
        if entry.notified_stalled:
            return
        idle = time.time() - float(entry.last_progress_ts or time.time())
        if idle >= self.stalled_minutes * 60:
            entry.notified_stalled = True
            DownloadQueue.update(entry)
            speed = speed_to_human(torrent.rate_download)
            await self._send(
                entry,
                trans("STALLED", entry.lang_code).format(
                    escape_html(torrent.name),
                    int(idle // 60),
                    f"{percent:.1f}%",
                    speed,
                ),
            )

    async def _send(self, entry: QueueEntry, text: str, reply_markup: Any = None):
        try:
            return await self.bot.send_message(
                chat_id=entry.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except TelegramError as err:
            log.warning("向 %s 推送消息失败: %s", entry.chat_id, err)
            return None
