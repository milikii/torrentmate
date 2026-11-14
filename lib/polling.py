from __future__ import annotations

import asyncio
import os
from asyncio import sleep
from collections import deque
from datetime import timedelta
from pathlib import Path
from random import uniform
from time import monotonic, time

from telegram.error import Conflict, RetryAfter, TimedOut
from telegram.ext import Application, ExtBot, Updater

WATCHDOG_INTERVAL = 600
HEARTBEAT_TIMEOUT = 1800
ALERT_INTERVAL = 3600
RESTART_WINDOW = 600
MAX_RESTARTS = 5
BACKOFF_BASE = 20
BACKOFF_CAP = 300
FIRST_BATCH_TIMEOUT = 60
PROBE_INTERVAL = 3600
SUSPEND_TIMEOUT = 86400
PROCESS_HEARTBEAT_TIMEOUT = 3600
MAX_REBUILD_FAILURES = 3
PROCESS_EXIT_INTERVAL = 3600
PROCESS_EXIT_MARKER = Path.home() / ".torrentino-polling-last-exit"


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
                try:
                    updates = await super().get_updates(
                        offset=offset,
                        limit=limit,
                        timeout=timeout,
                        allowed_updates=allowed_updates,
                        **kwargs,
                    )
                except TimedOut as error:
                    if polling is None or "Pool timeout" not in str(error):
                        raise
                    polling._log_pool_state(error)
                    await polling._rebuild_client()
                    try:
                        updates = await super().get_updates(
                            offset=offset,
                            limit=limit,
                            timeout=timeout,
                            allowed_updates=allowed_updates,
                            **kwargs,
                        )
                    except TimedOut as retry_error:
                        if "Pool timeout" in str(retry_error):
                            polling._log_pool_state(retry_error)
                        raise
            except Exception as error:
                if polling is not None and is_polling:
                    polling._polling_error(error)
                raise
            if polling is not None and is_polling:
                polling._heartbeat()
            return updates


class PollingUpdater(Updater):
    def __init__(self, bot, update_queue, *, get_updates_request, super_user, log):
        super().__init__(bot=bot, update_queue=update_queue)
        bot._polling_updater = self
        self._get_updates_request = get_updates_request
        self._get_updates_lock = bot._get_updates_lock
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
        self._conflict_restarts = 0
        self._conflict_active = False
        self._client_rebuilds = 0
        self._rebuild_failures = 0
        self._awaiting_rebuilt_heartbeat = False
        self._suspended = False
        self._suspended_at: float | None = None
        self._next_probe_at: float | None = None
        self._probing = False
        self._probe_exhausted = False
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
        recovered = self._last_error is not None or self._suspended
        self._last_heartbeat = monotonic()
        self._last_error = None
        self._consecutive_restarts = 0
        self._conflict_restarts = 0
        self._conflict_active = False
        self._rebuild_failures = 0
        self._awaiting_rebuilt_heartbeat = False
        self._suspended = False
        self._suspended_at = None
        self._next_probe_at = None
        self._probe_exhausted = False
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
        if self._closing or (self._suspended and not self._probing):
            return
        if error is self._last_error and not self._can_poll.is_set():
            return
        if isinstance(error, Conflict):
            self._conflict_active = True
            self._rebuild_failures = 0
        elif self._awaiting_rebuilt_heartbeat and not self._conflict_active:
            self._rebuild_failures += 1
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
            self.log.error(
                "Application error: type=%s error=%s",
                type(error).__name__,
                error,
                exc_info=error,
            )
            return
        if error is not self._last_logged_error:
            self.log.error(
                "Polling error: type=%s error=%s", type(error).__name__, error
            )
            self._last_logged_error = error
        if (
            not self._closing
            and not self._suspended
            and (self._recovery_task is None or self._recovery_task.done())
        ):
            self._recovery_task = asyncio.create_task(
                self._recover(), name="telegram-polling-recovery"
            )

    def _log_pool_state(self, error):
        client = getattr(self._get_updates_request, "_client", None)
        transports = [getattr(client, "_transport", None)]
        transports.extend(getattr(client, "_mounts", {}).values())
        seen = set()
        connections = []
        for transport in transports:
            pool = getattr(transport, "_pool", None)
            if pool is None or id(pool) in seen:
                continue
            seen.add(id(pool))
            connections.extend(getattr(pool, "connections", ()))
        if seen:
            connections = [c for c in connections if not c.is_closed()]
            total = len(connections)
            idle = sum(c.is_idle() for c in connections)
            active = total - idle
        else:
            total = active = idle = "unavailable"
        self.log.error(
            "Polling pool timeout: error=%s connections_total=%s active=%s idle=%s rebuilds=%d",
            error,
            total,
            active,
            idle,
            self._client_rebuilds,
        )

    async def _rebuild_client(self):
        self._client_rebuilds += 1
        self._awaiting_rebuilt_heartbeat = True
        await self._get_updates_request.shutdown()
        await self._get_updates_request.initialize()
        self.log.info(
            "getUpdates HTTP client rebuilt; rebuilds=%d", self._client_rebuilds
        )

    def _exit_process(self, reason):
        if self._conflict_active or isinstance(self._last_error, Conflict):
            return False
        now = time()
        try:
            last_exit = float(PROCESS_EXIT_MARKER.read_text())
        except FileNotFoundError:
            last_exit = None
        except (OSError, ValueError) as error:
            self.log.error("Polling process exit suppressed: cannot read exit limiter: %s", error)
            return False
        if last_exit is not None and now - last_exit < PROCESS_EXIT_INTERVAL:
            self.log.error("Polling process exit rate limited: %s", reason)
            return False
        try:
            PROCESS_EXIT_MARKER.write_text(str(now))
        except OSError as error:
            self.log.error("Polling process exit suppressed: cannot save exit limiter: %s", error)
            return False
        self.log.critical("Polling process exit for Docker restart: %s", reason)
        os._exit(1)
        return True

    async def _recover(self):
        while not self._closing and not self._suspended:
            if self._rebuild_failures >= MAX_REBUILD_FAILURES:
                self._exit_process("rebuilt getUpdates client failed three consecutive polls")
                await self._suspend("rebuilt getUpdates client failure limit reached")
                return
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
            self._conflict_active = self._conflict_active or isinstance(
                self._last_error, Conflict
            )
            if self._conflict_active and (
                len(self._restart_times) >= MAX_RESTARTS
                or self._conflict_restarts >= MAX_RESTARTS
            ):
                await self._suspend("automatic restart limit reached")
                return

            ceiling = min(
                BACKOFF_CAP, BACKOFF_BASE * 2**min(self._consecutive_restarts, MAX_RESTARTS)
            )
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
            if self._conflict_active:
                self._conflict_restarts += 1
                self._restart_times.append(monotonic())
            self._outcome.clear()
            self._can_poll.set()
            try:
                await super().start_polling(**self._polling_options)
            except Exception as error:
                self._polling_error(error)
                self.log.error(
                    "Polling restart failed: type=%s error=%s", type(error).__name__, error
                )
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
        now = monotonic()
        if self._suspended_at is None:
            self._suspended_at = now
        self._next_probe_at = now + PROBE_INTERVAL
        self._suspended = True
        self._can_poll.clear()
        self.log.error(
            "Polling suspended: %s; type=%s; error=%s; application and push tasks remain running",
            reason,
            type(self._last_error).__name__,
            self._last_error,
        )
        await self._alert(
            "Telegram 轮询已暂停自动重启："
            f"{reason}（{type(self._last_error).__name__}）。"
            "进程、推送和下载监控继续运行；每小时重建客户端试恢复，连续 24 小时失败后停止试探。"
        )

    async def _resume(self):
        self._probing = True
        try:
            if self.running:
                await super().stop()
            if self._closing or self.application is None or not self.application.running:
                return
            async with self._get_updates_lock:
                await self._rebuild_client()
            self._outcome.clear()
            self._can_poll.set()
            try:
                await super().start_polling(**self._polling_options)
                await asyncio.wait_for(self._outcome.wait(), timeout=FIRST_BATCH_TIMEOUT)
            except TimeoutError:
                self._polling_error(TimedOut("No polling batch after hourly rebuild"))
            except Exception as error:
                self._polling_error(error)
            if self._last_error is None:
                return
            if self._rebuild_failures >= MAX_REBUILD_FAILURES:
                self._exit_process("rebuilt getUpdates client failed three consecutive polls")
            if self.running:
                await super().stop()
            await self._suspend("hourly rebuilt-client probe failed")
        except Exception as error:
            self._last_error = error
            await self._suspend("hourly rebuilt-client recovery failed")
        finally:
            self._probing = False

    async def check_watchdog(self):
        if self._closing:
            return
        now = monotonic()
        heartbeat = (
            self._last_heartbeat
            if self._last_heartbeat is not None
            else self._started_at
        )
        age = now - heartbeat
        if age > PROCESS_HEARTBEAT_TIMEOUT:
            self._exit_process(f"getUpdates heartbeat expired for {age:.0f} seconds")
        if self._suspended and not self._probe_exhausted:
            if self._suspended_at is not None and now - self._suspended_at >= SUSPEND_TIMEOUT:
                self._probe_exhausted = True
                await self._alert("Telegram 轮询连续 24 小时恢复失败，已停止小时级试探，请人工排查。")
            elif (
                self._next_probe_at is not None
                and now >= self._next_probe_at
                and (self._recovery_task is None or self._recovery_task.done())
            ):
                self._recovery_task = asyncio.create_task(
                    self._resume(), name="telegram-polling-hourly-recovery"
                )
        if age >= HEARTBEAT_TIMEOUT:
            error_details = (
                "未记录异常，轮询心跳停滞。"
                if self._last_error is None
                else f"最近轮询错误：{type(self._last_error).__name__}。"
            )
            if self._suspended:
                recovery_status = (
                    "已停止小时级试探，需人工处理"
                    if self._probe_exhausted
                    else "自动重启已暂停，每小时重建客户端试恢复"
                )
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
                "Polling alert delivery failed: type=%s error=%s",
                type(error).__name__,
                error,
                exc_info=error,
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
