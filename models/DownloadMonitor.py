"""下载监控：进度推送、自动归类、完成通知。"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import math
import os
import time
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from transmission_rpc.torrent import Torrent

from lib.classifier import get_classifier
from lib.constants import CONFIG_FILE, PROGRESS_EDIT_INTERVAL, QUEUE_CHECK_INTERVAL
from lib.func import escape_html, get_logger, speed_to_human, trans
from lib.render import render_done_card, render_progress_card
from models.BotConfigurator import BotConfigurator
from models.DoneList import (
    SOURCE_UNKNOWN,
    WRITE_FAILED,
    DoneDeletionQueue,
    DoneList,
    config_path as done_config_path,
    pending_config_path as done_pending_config_path,
)
from models.DownloadHistory import DownloadHistory
from models.DownloadQueue import DownloadQueue, QueueEntry
from models.Fc2Blacklist import Fc2Blacklist
from models.Fc2Blacklist import config_path as blacklist_config_path
from models.Fc2Channel import extract_number

log = get_logger("DownloadMonitor")

# 接管基线状态文件，和主配置文件同目录；只记录“从哪一刻开始接管”，
# 不写进 torrentino.yaml，避免动到用户配置项。
ADOPT_BASELINE_FILE = "fc2_adopt_baseline.json"

# 同一任务两次停滞提醒的最小间隔（秒）。进度动一点又停不算新的一轮，
# 6 小时内不再提醒；超过后再次停滞才会再发。
STALLED_NOTIFY_INTERVAL = 6 * 3600


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
        # FC2 专用机器人（fc2.enabled=true）只负责推送，不再发停滞提醒：
        # 两个机器人各只有自己的 token，无法跨机器人发送；停滞提醒统一由
        # 下载机器人（含它部署后接管的新 FC2 下载）发出。
        self.fc2_enabled = bool(self.config.get("fc2.enabled", False))
        # FC2 任务通知分流：只有下载机器人（fc2.enabled=false）接管 FC2
        # 任务；fc2bot 配置里 fc2.enabled=true，永远不接管，避免两份通知。
        self.adopt_fc2 = bool(self.config.get("notify.adopt_fc2", True)) and not bool(
            self.config.get("fc2.enabled", False)
        )
        # 成功名单与待删卡片队列都落共享目录：两个机器人配同一个
        # fc2.done_file 就能查到同一份记录；待删文件默认紧挨着它。
        self.done_list = DoneList(done_config_path(self.config))
        self.done_deletions = DoneDeletionQueue(done_pending_config_path(self.config))
        self._super_user_warned = False

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
        # 先发现再取快照：刚接管的 FC2 任务同一轮就能发出第一张卡。
        # 发现失败只记日志，不能影响原有队列的处理。
        await self._maybe_adopt_fc2()
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
            # 队列只存数字 ID，Transmission 重启/删种后 ID 可能被别的任务
            # 复用：hash 对不上就停止对这个条目做一切自动操作，避免移错、
            # 删错、通知错任务；旧条目（无 hash）不受影响。
            entry_hash = (entry.info_hash or "").lower() or None
            torrent_hash = str(getattr(torrent, "info_hash", "") or "").lower() or None
            if entry_hash and torrent_hash and entry_hash != torrent_hash:
                log.error(
                    "任务 %s 的 info_hash 与队列记录不符（可能 ID 被复用），"
                    "停止处理并移出队列",
                    torrent_id,
                )
                DownloadQueue.remove(torrent_id)
                continue
            try:
                await self.handle(torrent, entry)
            except Exception as err:  # noqa: BLE001
                log.exception("处理任务 %s 通知失败: %s", torrent_id, err)

    async def _maybe_adopt_fc2(self) -> None:
        """接管 FC2 任务；失败只记日志，不影响原有队列的处理。"""
        if not self.adopt_fc2:
            return
        try:
            await self._adopt_fc2_torrents()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            log.warning("接管 FC2 任务失败: %s", err)

    async def _adopt_fc2_torrents(self) -> None:
        """把基线之后新加的未完成 FC2 任务接管进通知队列。

        基线（ADOPT_BASELINE_FILE）在部署后第一次接管时以当前时间建立，
        之后持久保存：只有 addedDate 晚于基线的任务才算“部署后新加”，
        老任务一律不接管、不通知、不移动。

        只接管标题能提出严格 FC2 编号、且尚未完成/做种的任务：
        - 已完成任务不碰，完成卡发过、队列已清后不会被重新捡回来重发；
        - 已在队列里的（同 torrent_id 或同 info_hash）跳过；
        - 共享黑名单命中的跳过；黑名单读盘失败本轮也跳过，下轮再判。
        """
        cutoff = self._ensure_adopt_baseline()
        if cutoff is None:
            return

        torrents = await asyncio.to_thread(self.client.get_torrents)
        if not torrents:
            return

        queued = DownloadQueue.snapshot()
        known_ids = set(queued)
        known_hashes = {
            str(entry.info_hash).lower() for entry in queued.values() if entry.info_hash
        }
        candidates = self._fc2_adopt_candidates(
            torrents, known_ids, known_hashes, cutoff
        )
        if not candidates:
            return

        chat_id = self._adopt_chat_id()
        if chat_id is None:
            return

        blacklist = Fc2Blacklist(blacklist_config_path(BotConfigurator()))
        for torrent, number, torrent_hash in candidates:
            self._adopt_entry(torrent, number, torrent_hash, chat_id, blacklist)

    def _adopt_baseline_path(self) -> str:
        """接管基线状态文件：与主配置文件同目录（测试里随 config_file 走）。"""
        config_file = getattr(BotConfigurator, "config_file", "") or CONFIG_FILE
        return os.path.join(os.path.dirname(config_file) or ".", ADOPT_BASELINE_FILE)

    def _ensure_adopt_baseline(self) -> float | None:
        """读取接管基线；第一次运行时以当前时间建立并原子落盘。

        基线文件缺失（首次部署）时写入当前时间，本轮不会有任务的
        addedDate 晚于它，因此第一轮不接管任何已有任务。
        文件存在但读不出/内容无效时返回 None（本轮不接管），绝不静默
        重置基线——否则重启后老任务会被当成新任务重新接管。
        """
        path = self._adopt_baseline_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            raw = data["adopted_since"]
            if isinstance(raw, bool):
                raise TypeError("adopted_since 不是数字")
            cutoff = float(raw)
        except FileNotFoundError:
            cutoff = time.time()
            try:
                self._write_adopt_baseline(path, cutoff)
            except OSError as err:
                log.warning("写入 FC2 接管基线失败: %s", err)
                return None
            log.info("FC2 接管基线已建立: %s", cutoff)
            return cutoff
        except (OSError, ValueError, TypeError, KeyError) as err:
            log.error("读取 FC2 接管基线失败（%s），本轮不接管 FC2 任务", err)
            return None
        if not math.isfinite(cutoff) or cutoff < 0:
            log.error("FC2 接管基线无效（%s），本轮不接管 FC2 任务", cutoff)
            return None
        return cutoff

    @staticmethod
    def _write_adopt_baseline(path: str, cutoff: float) -> None:
        """原子写基线文件（临时文件 + os.replace）。"""
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "adopted_since": cutoff,
                    "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
                fh,
            )
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    @staticmethod
    def _torrent_added_timestamp(torrent: Torrent) -> float | None:
        """取任务 addedDate 的 Unix 时间戳；拿不到时返回 None。

        transmission-rpc 7.0.11 的 Torrent 暴露的是 ``added_date``
        （RPC 字段 addedDate，datetime；本项目现有完成耗时计算也在用它）。
        aware datetime 直接取绝对时间；naive datetime 按进程本地时区解释，
        与 ``datetime.fromtimestamp(addedDate)`` 互为逆运算。其它类型一律
        视为不可用，调用方据此跳过，宁可漏接管也不接管老任务。
        """
        try:
            value = getattr(torrent, "added_date", None)
        except Exception:  # noqa: BLE001
            return None
        if isinstance(value, dt.datetime):
            try:
                return value.timestamp()
            except (OverflowError, OSError, ValueError):
                return None
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            number = float(value)
            return number if math.isfinite(number) else None
        return None

    def _fc2_adopt_candidates(
        self,
        torrents: list[Torrent],
        known_ids: set[int],
        known_hashes: set[str],
        cutoff: float,
    ) -> list[tuple[Torrent, str, str]]:
        """挑出基线之后添加、尚未在通知队列里的未完成 FC2 任务。"""
        candidates: list[tuple[Torrent, str, str]] = []
        for torrent in torrents:
            # 防复活：只发现未完成任务，完成/做种中的一律不碰。
            if float(torrent.percent_done or 0) >= 1 or torrent.status == "seeding":
                continue
            number = extract_number(getattr(torrent, "name", "") or "")
            if not number:
                continue
            # 只接管部署基线之后新加的任务；添加时间不可用时也不接管。
            added_at = self._torrent_added_timestamp(torrent)
            if added_at is None or added_at <= cutoff:
                continue
            torrent_hash = str(getattr(torrent, "info_hash", "") or "").lower()
            if torrent.id in known_ids:
                continue
            if torrent_hash and torrent_hash in known_hashes:
                continue
            candidates.append((torrent, number, torrent_hash))
        return candidates

    def _adopt_chat_id(self) -> int | None:
        """取接管通知的接收 chat；未配置 bot.super_user 时每轮只警告一次。"""
        try:
            chat_id = int(self.config.get("bot.super_user"))
        except (TypeError, ValueError):
            chat_id = None
        if chat_id is not None:
            return chat_id
        if not self._super_user_warned:
            log.warning("未配置 bot.super_user，本轮不接管 FC2 任务")
            self._super_user_warned = True
        return None

    def _adopt_entry(
        self,
        torrent: Torrent,
        number: str,
        torrent_hash: str,
        chat_id: int,
        blacklist: Fc2Blacklist,
    ) -> None:
        """黑名单通过后把任务加进通知队列。"""
        record, read_ok = blacklist.lookup(number)
        if record is not None or not read_ok:
            return
        entry = QueueEntry(
            torrent_id=int(torrent.id),
            chat_id=chat_id,
            lang_code="zh",
            title=str(torrent.name or ""),
            requested_dir=str(torrent.download_dir or ""),
            info_hash=torrent_hash or None,
            auto_classify=True,
            # 接管进来的任务不是用户此刻主动添加的，来源无法判定，记 unknown。
            source=SOURCE_UNKNOWN,
        )
        if not DownloadQueue.add_if_new(entry):
            return
        log.info("接管 FC2 任务 %s（编号 %s）", torrent.name, number)

    async def handle(self, torrent: Torrent, entry: QueueEntry) -> None:
        # 1. 任务出错，只提醒一次：先发送，确认送达才标记已通知，
        # 否则发送失败也会被当成已通知，错误提醒就永久丢失了。
        if torrent.error_string and not entry.notified_error:
            sent = await self._send(
                entry,
                trans("TORRENT_ERROR", entry.lang_code).format(
                    escape_html(torrent.name),
                    escape_html(torrent.error_string),
                ),
            )
            if sent is not None:
                entry.notified_error = True
                DownloadQueue.update(entry)
            return

        # 2. 磁力链接还在获取元数据：transmission_rpc 的
        # metadata_percent_complete 范围是 0~1，不是 0~100，
        # 之前按 <100 判断，真实任务永远提前返回，归类、完成、
        # 停滞全不执行，进度卡片还每轮发新消息刷屏。
        if float(torrent.metadata_percent_complete or 0) < 1:
            await self._update_card(torrent, entry, metadata=True)
            return

        # 3. 自动归类（只执行一次）：归类失败返回 False 时不进完成，
        # 否则已完成但未归类的任务清了队列就永远没机会再移了。
        if not await self._maybe_classify(torrent, entry):
            return

        # 4. 下载完成
        if float(torrent.percent_done or 0) >= 1 and not entry.finished:
            await self._complete(torrent, entry)
            return

        # 5. 进度推送
        await self._update_card(torrent, entry)

        # 6. 长时间无进度提醒
        await self._check_stalled(torrent, entry)

    async def _maybe_classify(self, torrent: Torrent, entry: QueueEntry) -> bool:
        if not entry.auto_classify or entry.classified:
            return True

        classifier = get_classifier()
        result = classifier.classify(torrent.name)
        target = classifier.resolve_path(result.directory) or entry.requested_dir

        if not target or os.path.normpath(str(target)) == os.path.normpath(
            str(torrent.download_dir)
        ):
            # 已在目标目录：标记已归类即可，不用再移动。
            entry.classified = True
            entry.final_dir = target or torrent.download_dir
            DownloadQueue.update(entry)
            return True

        try:
            await asyncio.to_thread(
                self.client.change_torrent, torrent.id, location=target
            )
            if float(torrent.percent_done or 0) > 0:
                await asyncio.to_thread(
                    self.client.move_torrent_data, torrent.id, target, move=True
                )
        except Exception as err:  # noqa: BLE001
            # 移动失败时不要标记 classified：下一轮还会重试，而不是
            # 卡片显示目标目录、实际却永远没去成。
            # 失败通知 1 小时内只发一次，否则每 30 秒一轮也是刷屏。
            log.error("任务 %s 自动归类失败: %s", torrent.name, err)
            now = time.time()
            if now - float(entry.last_classify_fail_ts or 0) >= 3600:
                sent = await self._send(
                    entry,
                    trans("CLASSIFY_FAILED", entry.lang_code).format(
                        escape_html(torrent.name), escape_html(str(err))
                    ),
                )
                if sent is not None:
                    entry.last_classify_fail_ts = now
                    DownloadQueue.update(entry)
            return False

        # 只有移动成功后才标记已归类、写最终目录。
        entry.classified = True
        entry.final_dir = target
        DownloadQueue.update(entry)
        log.info("任务 %s 自动归类到 %s", torrent.name, target)
        await self._send(
            entry,
            trans("CLASSIFIED", entry.lang_code).format(
                escape_html(torrent.name), escape_html(target), result.rule
            ),
        )
        return True

    async def _complete(self, torrent: Torrent, entry: QueueEntry) -> None:
        elapsed = self._elapsed_seconds(torrent)
        text = render_done_card(torrent, entry.lang_code, None, elapsed)
        keyboard = self._done_keyboard(torrent, entry.lang_code)
        log.info("下载完成: %s -> %s", torrent.name, torrent.download_dir)

        # 先确认完成卡片真的送达，再写历史、清队列：之前是先标记
        # finished、直接清队列，发送失败时完成通知就永久丢失了。
        delivered, delete_message_id = await self._deliver_done_card(
            entry, text, keyboard
        )
        if not delivered:
            log.warning("完成卡片发送失败，任务 %s 保留在队列中下轮重试", torrent.id)
            # 通知失败也不能跳过暂停：否则任务会一直做种，
            # 原来的 pause_on_complete 开关就失效了。
            await self._pause_after_complete(torrent)
            return

        entry.finished = True
        DownloadHistory.add(
            torrent.done_date,
            torrent.name,
            torrent.download_dir,
            torrent.size_when_done,
        )
        # 成功名单只在这里写：完成卡没送达时上面已经 return，不会写。
        self._record_done(torrent, entry)
        DownloadQueue.remove(torrent.id)

        # 只删“原地编辑成完成卡”的那条旧进度卡；新发的完成消息是用户
        # 唯一的完成提醒，必须留着。删卡失败只记日志，不影响已经写好的
        # 历史和成功名单。
        await self._delete_card(entry.chat_id, delete_message_id)

        if self.pause_on_complete and torrent.status == "seeding":
            await self._pause_and_notify(torrent, entry)

    @staticmethod
    def _elapsed_seconds(torrent: Torrent) -> float | None:
        try:
            if torrent.done_date and torrent.added_date:
                return (torrent.done_date - torrent.added_date).total_seconds()
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _deliver_done_card(self, entry: QueueEntry, text, keyboard):
        """发送（或原地更新）完成卡，返回（是否送达, 完成后可删的消息 id）。

        只有把原进度卡原地编辑成完成卡时，才返回这条旧卡片的 id（之后
        可以删）。以下情况都返回 None，新发的完成消息一律保留：
        - 本来就没有进度卡（批量添加、notify.progress=false、初始卡
          发送失败）：新消息是用户唯一的完成提醒；
        - 编辑旧卡失败、回退新发：旧卡保持不变（宁可留着也不误删用户
          可见的内容），新消息同样保留。
        """
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
                return True, entry.message_id
            except TelegramError as err:
                log.debug("进度卡片更新失败，改为发送新消息: %s", err)
        message = await self._send(entry, text, keyboard)
        if message is None:
            return False, None
        return True, None

    async def _pause_after_complete(self, torrent: Torrent) -> None:
        """完成卡没送达时也要暂停；失败只记日志。"""
        if not (self.pause_on_complete and torrent.status == "seeding"):
            return
        try:
            await asyncio.to_thread(self.client.stop_torrent, torrent.id)
        except Exception as err:  # noqa: BLE001
            log.warning("完成后暂停失败: %s", err)

    async def _pause_and_notify(self, torrent: Torrent, entry: QueueEntry) -> None:
        """完成后暂停并通知；原逻辑两步在同一个 try 里，任一失败都只记日志。"""
        try:
            await asyncio.to_thread(self.client.stop_torrent, torrent.id)
            await self._send(entry, trans("PAUSED_ON_COMPLETE", entry.lang_code))
        except Exception as err:  # noqa: BLE001
            log.warning("完成后暂停失败: %s", err)

    def _record_done(self, torrent: Torrent, entry: QueueEntry) -> None:
        """追加成功名单；FC2 任务同时排进待删卡片队列。

        这一步的任何失败都不许影响完成流程：名单写不进去就记 error，
        任务照样算完成（下次不会重来），用户也照样收到完成卡。
        """
        name = str(getattr(torrent, "name", "") or entry.title or "")
        number = extract_number(name)
        info_hash = str(getattr(torrent, "info_hash", "") or "").lower() or None
        done_at = getattr(torrent, "done_date", None) or time.time()
        try:
            result = self.done_list.append(
                number=number,
                title=name,
                info_hash=info_hash,
                done_at=done_at,
                size_when_done=getattr(torrent, "size_when_done", 0) or 0,
                source=getattr(entry, "source", None) or SOURCE_UNKNOWN,
            )
            if result == WRITE_FAILED:
                log.error("成功名单写入失败: %s", name)
        except Exception as err:  # noqa: BLE001
            log.error("成功名单写入异常 %s: %s", name, err)
        if not number:
            return
        # 下载机器人删不了 Fc2专用 发的卡片：把编号放进共享待删队列，
        # Fc2专用 轮询时自己删。写失败只记日志，不影响完成。
        try:
            self.done_deletions.request(number, title=name, done_at=done_at)
        except Exception as err:  # noqa: BLE001
            log.warning("写完成待删卡片队列失败 %s: %s", number, err)

    async def _delete_card(self, chat_id, message_id) -> None:
        """删掉自己发的任务卡片；失败、消息已删都只记日志。"""
        if not message_id:
            return
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=int(message_id))
        except Exception as err:  # noqa: BLE001
            log.warning("删除完成任务卡片失败 %s/%s: %s", chat_id, message_id, err)

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
        if not metadata:
            # 节点通知独立于卡片节流；失败的节点通知不能被相同文本
            # 的提前返回永久跳过，成功后仍由 milestones_sent 去重。
            await self._maybe_milestone(torrent, entry, percent)
        now = time.time()
        if metadata:
            # 元数据阶段文本基本不变，10 分钟更新一次就够了，
            # 之前是 45 秒一次，一直刷。
            if entry.last_edit_ts and now - entry.last_edit_ts < 600:
                return
        else:
            changed = percent - float(entry.last_percent or 0) >= 5
            if not changed:
                # 进度没动（<5%）时不要每 45 秒编辑一次：速度、
                # peers、ETA 一变文本就变，编辑本身就一直在刷。
                # 静态进度最多 30 分钟更新一次，动了 5% 再立即更新。
                if now - float(entry.last_edit_ts or 0) < 1800:
                    return

        text = render_progress_card(
            torrent, entry.lang_code, entry.final_dir, entry.title
        )

        # 文本和上次发出去的完全一样，直接跳过：不编辑、不重发。
        if text == entry.last_card_text and entry.message_id:
            return

        delivered = False
        if entry.message_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=entry.chat_id,
                    message_id=entry.message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                delivered = True
            except TelegramError as err:
                # 内容没变时 Telegram 会报 message is not modified，
                # 这不是失败：原卡片已经是这个样子，不要再发一条新消息，
                # 否则进度不动的任务每 45 秒就刷一条新通知。
                if "not modified" in str(err).lower():
                    log.debug("进度卡片内容未变，跳过重发")
                    delivered = True
                else:
                    log.debug("进度卡片更新失败: %s", err)
                    message = await self._send(entry, text)
                    if message is not None:
                        entry.message_id = message.message_id
                        delivered = True
        else:
            message = await self._send(entry, text)
            if message is not None:
                entry.message_id = message.message_id
                delivered = True

        if not delivered:
            # 不缓存未送达的文本/进度，否则旧 message_id 会让后续
            # 相同文本直接跳过，卡片在 Telegram 恢复后也无法补发。
            return
        entry.last_edit_ts = now
        entry.last_percent = percent
        entry.last_card_text = text
        DownloadQueue.update(entry)

    async def _maybe_milestone(
        self, torrent: Torrent, entry: QueueEntry, percent: float
    ) -> None:
        if not self.milestones:
            return
        for milestone in self.milestones:
            if percent >= milestone and milestone not in entry.milestones_sent:
                sent = await self._send(
                    entry,
                    trans("MILESTONE", entry.lang_code).format(
                        escape_html(torrent.name), milestone
                    ),
                )
                if sent is not None:
                    entry.milestones_sent.add(milestone)
                    DownloadQueue.update(entry)

    async def _check_stalled(self, torrent: Torrent, entry: QueueEntry) -> None:
        # FC2 专用机器人不发停滞提醒：这类任务要么由下载机器人接管后提醒，
        # 要么（部署前就在 Fc2专用队列里的老任务）不再有停滞提醒。
        if not self.stalled_minutes or self.fc2_enabled:
            return
        now = time.time()
        percent = float(torrent.percent_done or 0) * 100
        if percent > float(entry.last_progress_value or 0) + 0.01:
            entry.last_progress_value = percent
            entry.last_progress_ts = now
            entry.notified_stalled = False
            DownloadQueue.update(entry)
            return
        if entry.notified_stalled:
            return
        # 按任务节流：进度动一点又停不算新的一轮，两次提醒至少间隔 6 小时，
        # 否则“爬一点又停”的任务会反复复位再发。
        # last_stalled_notify_ts 为 0/None 表示“从未提醒过”，必须跳过节流；
        # 否则拿它跟 now 相减，在时间戳较小（如测试假时钟）时会把首条提醒吞掉。
        last_notify_ts = float(entry.last_stalled_notify_ts or 0)
        if last_notify_ts and now - last_notify_ts < STALLED_NOTIFY_INTERVAL:
            return
        idle = now - float(entry.last_progress_ts or now)
        if idle >= self.stalled_minutes * 60:
            speed = speed_to_human(torrent.rate_download)
            sent = await self._send(
                entry,
                trans("STALLED", entry.lang_code).format(
                    escape_html(torrent.name),
                    int(idle // 60),
                    f"{percent:.1f}%",
                    speed,
                ),
            )
            if sent is not None:
                entry.notified_stalled = True
                entry.last_stalled_notify_ts = now
                DownloadQueue.update(entry)

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
