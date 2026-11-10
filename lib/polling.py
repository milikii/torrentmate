from __future__ import annotations

import asyncio
from asyncio import sleep
from collections import deque
from datetime import timedelta
from random import uniform
from time import monotonic

from telegram.error import RetryAfter, TimedOut
from telegram.ext import Application, ExtBot, Updater

WATCHDOG_INTERVAL = 600
HEARTBEAT_TIMEOUT = 1800
ALERT_INTERVAL = 3600
RESTART_WINDOW = 600
MAX_RESTARTS = 5
BACKOFF_BASE = 20
BACKOFF_CAP = 300
FIRST_BATCH_TIMEOUT = 60


class PollingBot(ExtBot):
    __slots__ = ("_polling_updater", "_get_updates_lock")

    def __init__(self, token, **kwargs):
        super().__init__(token=token, **kwargs)
        self._polling_updater: PollingUpdater | None = None
        self._get_updates_lock = asyncio.Lock()

    async def get_updates(
        self, offset=None, limit=None, timeout=None, allowed_updates=None, **kwargs
    ):
        polling = self._polling_updater
        # Updater.stop() makes a timeout=0 cleanup request, not a polling heartbeat.
        is_polling = timeout not in (0, timedelta(0))
        if polling is not None and is_polling:
            await polling._can_poll.wait()
        async with self._get_updates_lock:
            if polling is not None and is_polling:
                await polling._can_poll.wait()
            try:
                updates = await super().get_updates(
                    offset=offset,
                    limit=limit,
                    timeout=timeout,
                    allowed_updates=allowed_updates,
                    **kwargs,
                )
            except Exception as error:
                if polling is not None and is_polling:
                    polling._polling_error(error)
                raise
            if polling is not None and is_polling:
                polling._heartbeat()
            return updates


class PollingUpdater(Updater):
    def __init__(self, bot, update_queue, *, super_user, log):
        super().__init__(bot=bot, update_queue=update_queue)
        bot._polling_updater = self
        self.application: Application | None = None
        self.log = log
        self.super_user = super_user
        self._started_at = monotonic()
        self._last_heartbeat: float | None = None
        self._last_error: Exception | None = None
        self._last_error_at = self._started_at
        self._last_logged_error: Exception | None = None
        self._last_alert: float | None = None
        self._restart_times = deque()
        self._consecutive_restarts = 0
        self._suspended = False
        self._closing = False
        self._can_poll = asyncio.Event()
        self._can_poll.set()
        self._outcome = asyncio.Event()
        self._recovery_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._polling_options = {}
        self._error_callback = None

    async def start_polling(
        self,
        poll_interval=0.0,
        timeout=timedelta(seconds=10),
        bootstrap_retries=0,
        allowed_updates=None,
        drop_pending_updates=None,
        error_callback=None,
    ):
        self._polling_options = {
            "poll_interval": poll_interval,
            "timeout": timeout,
            "bootstrap_retries": bootstrap_retries,
            "allowed_updates": allowed_updates,
            "drop_pending_updates": drop_pending_updates,
            "error_callback": self._polling_error,
        }
        self._error_callback = error_callback
        self._started_at = monotonic()
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="telegram-polling-watchdog"
        )
        try:
            await super().start_polling(**self._polling_options)
        except Exception as error:
            self._polling_error(error)
        else:
            self.log.info("Polling started; getUpdates heartbeat monitoring enabled")
        return self.update_queue

    def _heartbeat(self):
        first_batch = self._last_heartbeat is None
        recovered = self._last_error is not None
        self._last_heartbeat = monotonic()
        self._last_error = None
        self._consecutive_restarts = 0
        self._outcome.set()
        if recovered:
            self.log.info(
                "Polling recovered; successful getUpdates batch (empty is valid)"
            )
        elif first_batch:
            self.log.info(
                "Polling heartbeat established; successful getUpdates batch (empty is valid)"
            )

    def _polling_error(self, error):
        if self._closing or self._suspended:
            return
        if error is self._last_error and not self._can_poll.is_set():
            return
        self._last_error = error
        self._last_error_at = monotonic()
        self._last_logged_error = None
        self._can_poll.clear()
        self._outcome.set()
        if self._error_callback is not None:
            self._error_callback(error)
        elif self.application is not None:
            self.application.create_task(
                self.application.process_error(update=None, error=error)
            )

    async def error_handler(self, update, context):
        error = context.error
        if error is not self._last_error:
            self.log.error("Application error: type=%s", type(error).__name__)
            return
        if error is not self._last_logged_error:
            self.log.error("Polling error: type=%s", type(error).__name__)
            self._last_logged_error = error
        if (
            not self._closing
            and not self._suspended
            and (self._recovery_task is None or self._recovery_task.done())
        ):
            self._recovery_task = asyncio.create_task(
                self._recover(), name="telegram-polling-recovery"
            )

    async def _recover(self):
        while not self._closing and not self._suspended:
            try:
                if self.running:
                    await super().stop()
            except Exception as error:
                self._last_error = error
                await self._suspend("cannot safely stop the previous polling task")
                return

            now = monotonic()
            while (
                self._restart_times and now - self._restart_times[0] >= RESTART_WINDOW
            ):
                self._restart_times.popleft()
            if (
                len(self._restart_times) >= MAX_RESTARTS
                or self._consecutive_restarts >= MAX_RESTARTS
            ):
                await self._suspend("automatic restart limit reached")
                return

            ceiling = min(BACKOFF_CAP, BACKOFF_BASE * 2**self._consecutive_restarts)
            delay = max(BACKOFF_BASE, uniform(ceiling / 2, ceiling))
            if isinstance(self._last_error, RetryAfter):
                retry_after = self._last_error.retry_after
                if isinstance(retry_after, timedelta):
                    retry_after = retry_after.total_seconds()
                delay = max(delay, retry_after + 1)
            cooldown_until = self._last_error_at + delay
            delay = max(0, cooldown_until - monotonic())
            self.log.error(
                "Polling recovery: type=%s attempt=%d backoff=%.1fs",
                type(self._last_error).__name__,
                self._consecutive_restarts + 1,
                delay,
            )
            await sleep(delay)
            if (
                self._closing
                or self.application is None
                or not self.application.running
            ):
                return

            self._consecutive_restarts += 1
            self._restart_times.append(monotonic())
            self._outcome.clear()
            self._can_poll.set()
            try:
                await super().start_polling(**self._polling_options)
            except Exception as error:
                self._polling_error(error)
                self.log.error("Polling restart failed: type=%s", type(error).__name__)
                continue
            self.log.info("Polling restarted; awaiting successful getUpdates batch")
            try:
                await asyncio.wait_for(
                    self._outcome.wait(), timeout=FIRST_BATCH_TIMEOUT
                )
            except TimeoutError:
                self._polling_error(TimedOut("No polling batch after restart"))
            if self._last_error is None:
                return

    async def _suspend(self, reason):
        self._suspended = True
        self._can_poll.clear()
        self.log.error(
            "Polling suspended: %s; type=%s; application and push tasks remain running",
            reason,
            type(self._last_error).__name__,
        )
        await self._alert(
            "Telegram 轮询已暂停自动重启："
            f"{reason}（{type(self._last_error).__name__}）。"
            "进程、推送和下载监控继续运行；请排查代理/重复实例后人工重启机器人。"
        )

    async def check_watchdog(self):
        if self._closing:
            return
        heartbeat = (
            self._last_heartbeat
            if self._last_heartbeat is not None
            else self._started_at
        )
        age = monotonic() - heartbeat
        if age >= HEARTBEAT_TIMEOUT:
            error_details = (
                "未记录异常，轮询心跳停滞。"
                if self._last_error is None
                else f"最近轮询错误：{type(self._last_error).__name__}。"
            )
            if self._suspended:
                recovery_status = "自动重启已暂停，需人工处理"
            elif self._recovery_task is not None and not self._recovery_task.done():
                recovery_status = "自动重启任务正在运行"
            else:
                recovery_status = "当前无自动重启任务，需人工排查"
            await self._alert(
                f"Telegram 轮询心跳已过期 {age:.0f} 秒，"
                f"{error_details}{recovery_status}；"
                "推送线路不受轮询停止影响。"
            )

    async def _alert(self, text):
        now = monotonic()
        if self._last_alert is not None and now - self._last_alert < ALERT_INTERVAL:
            return
        self._last_alert = now
        self.log.error("Polling alert: %s", text)
        if not self.super_user:
            self.log.error(
                "Polling alert delivery failed: bot.super_user is not configured"
            )
            return
        try:
            await self.bot.send_message(chat_id=self.super_user, text=text)
        except Exception as error:
            self.log.error(
                "Polling alert delivery failed: type=%s", type(error).__name__
            )

    async def _watchdog_loop(self):
        while not self._closing:
            await sleep(WATCHDOG_INTERVAL)
            await self.check_watchdog()

    async def close_supervision(self):
        self._closing = True
        self._can_poll.clear()
        tasks = [
            task
            for task in (self._recovery_task, self._watchdog_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self):
        await self.close_supervision()
        if self.running:
            await super().stop()
