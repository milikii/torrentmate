import asyncio
import json
import logging
from collections import deque
from contextlib import suppress
from datetime import timedelta
from inspect import isawaitable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import Conflict, NetworkError, RetryAfter, TimedOut
from telegram.ext import Application, Updater
from telegram.request import BaseRequest

from lib import polling


class TelegramRequest(BaseRequest):
    def __init__(self, responses=(), bootstrap_errors=()):
        self.responses = deque(responses)
        self.bootstrap_errors = deque(bootstrap_errors)
        self.calls = []
        self.inflight = 0
        self.max_inflight = 0
        self.send_error = None
        self.block = asyncio.Event()
        self.alert_sent = asyncio.Event()

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    @staticmethod
    def response(result):
        return 200, json.dumps({"ok": True, "result": result}).encode()

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            return self.response(
                {"id": 42, "is_bot": True, "first_name": "Test", "username": "test_bot"}
            )
        if endpoint == "deleteWebhook":
            if self.bootstrap_errors:
                raise self.bootstrap_errors.popleft()
            return self.response(True)
        if endpoint == "sendMessage":
            self.alert_sent.set()
            if self.send_error is not None:
                raise self.send_error
            return self.response(
                {
                    "message_id": 1,
                    "date": 0,
                    "chat": {"id": 7, "type": "private"},
                    "text": params["text"],
                }
            )
        if endpoint != "getUpdates":
            return self.response(True)
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if params.get("timeout", 0) == 0:
                return self.response([])
            if not self.responses:
                await self.block.wait()
                return self.response([])
            result = self.responses.popleft()
            if isinstance(result, Exception):
                raise result
            if result == "conflict":
                return (
                    409,
                    json.dumps(
                        {
                            "ok": False,
                            "error_code": 409,
                            "description": "Conflict: terminated by other getUpdates request",
                        }
                    ).encode(),
                )
            if callable(result):
                result = result()
                assert isawaitable(result)
                result = await result
            return self.response(result)
        finally:
            self.inflight -= 1

    def polls(self):
        return [
            params
            for endpoint, params in self.calls
            if endpoint == "getUpdates" and params.get("timeout", 0) > 0
        ]

    def alerts(self):
        return [params for endpoint, params in self.calls if endpoint == "sendMessage"]


@pytest.fixture
async def make_application():
    applications = []

    def make(responses=(), bootstrap_errors=()):
        request = TelegramRequest(responses, bootstrap_errors)
        bot = polling.PollingBot(
            token="42:offline-test", request=request, get_updates_request=request
        )
        updater = polling.PollingUpdater(
            bot, asyncio.Queue(), super_user=7, log=logging.getLogger("test.polling")
        )
        application = Application.builder().updater(updater).build()
        updater.application = application
        application.add_error_handler(updater.error_handler)
        applications.append(application)
        return application, updater, request

    yield make
    for application in applications:
        updater = application.updater
        assert updater is not None
        await updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0)
    monkeypatch.setattr(polling, "monotonic", lambda: state.now)
    return state


@pytest.fixture
def fast_backoff(monkeypatch, clock):
    delays = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda seconds: real_sleep(0))
    monkeypatch.setattr(polling, "uniform", lambda lower, upper: lower)

    async def pause(seconds):
        task = asyncio.current_task()
        if task is not None and task.get_name() == "telegram-polling-watchdog":
            await asyncio.Event().wait()
        delays.append(seconds)
        clock.now += seconds
        await real_sleep(0)

    monkeypatch.setattr(polling, "sleep", pause)
    return delays


async def start(application, updater):
    await application.initialize()
    await application.start()
    await updater.start_polling(allowed_updates=Update.ALL_TYPES)


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=2)


@pytest.mark.parametrize(
    ("error", "error_name"),
    [
        ("conflict", "Conflict"),
        (NetworkError("offline"), "NetworkError"),
        (TimedOut(), "TimedOut"),
    ],
)
async def test_polling_errors_trigger_backoff_and_real_updater_restart(
    make_application, fast_backoff, caplog, error, error_name
):
    caplog.set_level(logging.INFO)
    app, updater, request = make_application([error, []])
    await start(app, updater)
    await eventually(
        lambda: updater._last_heartbeat is not None
        and updater._recovery_task is not None
        and updater._recovery_task.done()
    )

    assert fast_backoff == [polling.BACKOFF_BASE]
    assert updater.running and app.running
    assert updater._last_error is None
    assert request.max_inflight == 1
    assert f"Polling error: type={error_name}" in caplog.text
    assert "Polling restarted; awaiting successful getUpdates batch" in caplog.text
    assert "Polling recovered; successful getUpdates batch" in caplog.text
    assert all(record.created > 0 for record in caplog.records)
    assert request.alerts() == []


@pytest.mark.parametrize("jitter_fraction", [0, 1])
async def test_bounded_exponential_jitter_and_failure_limit_preserve_push(
    make_application, fast_backoff, monkeypatch, jitter_fraction, caplog
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(
        polling,
        "uniform",
        lambda lower, upper: lower + (upper - lower) * jitter_fraction,
    )
    app, updater, request = make_application(["conflict"] * 7)
    await start(app, updater)
    pushing = app.create_task(asyncio.Event().wait(), name="test-push-loop")
    try:
        await eventually(lambda: updater._suspended and updater._recovery_task.done())
        ceilings = [20, 40, 80, 160, 300]
        assert fast_backoff == [
            max(polling.BACKOFF_BASE, ceiling / 2) if jitter_fraction == 0 else ceiling
            for ceiling in ceilings
        ]
        assert all(
            polling.BACKOFF_BASE <= delay <= polling.BACKOFF_CAP
            for delay in fast_backoff
        )
        assert len(request.polls()) == 1 + polling.MAX_RESTARTS
        assert not updater.running
        assert app.running and not pushing.done()
        assert len(request.alerts()) == 1
        assert request.alerts()[0]["chat_id"] == 7
        assert "Polling suspended:" in caplog.text
        assert "application and push tasks remain running" in caplog.text
        await app.bot.send_message(chat_id=7, text="push path is still available")
        assert len(request.alerts()) == 2
    finally:
        pushing.cancel()
        with suppress(asyncio.CancelledError):
            await pushing


async def test_no_successful_batch_is_failure_and_cleanup_waits_for_old_request(
    make_application, fast_backoff, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(polling, "FIRST_BATCH_TIMEOUT", 0.02)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def delayed_cancellation():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    app, updater, request = make_application(["conflict", delayed_cancellation, []])
    await start(app, updater)
    await asyncio.wait_for(entered.wait(), timeout=2)
    await asyncio.wait_for(cancelled.wait(), timeout=2)
    try:
        assert request.inflight == 1
        assert len(request.polls()) == 2
        assert fast_backoff == [20]
        assert updater._last_heartbeat is None
    finally:
        release.set()
    await eventually(
        lambda: updater._last_heartbeat is not None and updater._recovery_task.done()
    )
    assert fast_backoff == [20, 20]
    assert request.max_inflight == 1
    assert "type=TimedOut" in caplog.text


async def test_rolling_restart_limit_also_stops_successful_but_flapping_polls(
    make_application, fast_backoff
):
    responses = ["conflict", []] * 5 + ["conflict"]
    app, updater, request = make_application(responses)
    await start(app, updater)
    await eventually(lambda: updater._suspended and updater._recovery_task.done())

    assert fast_backoff == [polling.BACKOFF_BASE] * polling.MAX_RESTARTS
    assert updater._last_heartbeat is not None
    assert updater._consecutive_restarts == 0
    assert len(updater._restart_times) == polling.MAX_RESTARTS
    assert len(request.alerts()) == 1
    assert app.running


async def test_old_restarts_expire_from_rolling_window(
    make_application, fast_backoff, clock
):
    async def good_batch_after_quiet_period():
        clock.now += polling.RESTART_WINDOW + 1
        return []

    app, updater, request = make_application(
        ["conflict", good_batch_after_quiet_period] * 6
    )
    await start(app, updater)
    await eventually(
        lambda: len(fast_backoff) == 6
        and updater._last_error is None
        and updater._recovery_task.done()
    )
    assert not updater._suspended
    assert updater.running and app.running
    assert request.alerts() == []


async def test_reusing_same_timeout_exception_does_not_bypass_backoff(
    make_application, fast_backoff
):
    error = TimedOut()
    app, updater, request = make_application([error] * 6)
    await start(app, updater)
    await eventually(lambda: updater._suspended and updater._recovery_task.done())
    assert fast_backoff == [20, 20, 40, 80, 150]
    assert len(request.polls()) == 6
    assert len(request.alerts()) == 1


async def test_retry_after_respects_server_cooldown(make_application, fast_backoff):
    app, updater, _request = make_application([RetryAfter(400), []])
    await start(app, updater)
    await eventually(
        lambda: updater._last_heartbeat is not None and updater._recovery_task.done()
    )
    assert fast_backoff == [401]
    assert not updater._suspended


@pytest.mark.parametrize(
    ("retry_after", "stop_elapsed", "remaining"),
    [(400, 400.5, 0.5), (timedelta(seconds=400), 400.5, 0.5), (400, 430.5, 0)],
)
async def test_retry_after_stop_wait_counts_towards_cooldown(
    make_application, clock, monkeypatch, retry_after, stop_elapsed, remaining
):
    from telegram.ext._utils import networkloop

    ptb_entered = asyncio.Event()
    finish_ptb = asyncio.Event()
    backoff_entered = asyncio.Event()
    finish_backoff = asyncio.Event()
    ptb_delays = []
    backoff_delays = []

    async def controlled_sleep(seconds):
        task = asyncio.current_task()
        assert task is not None
        name = task.get_name()
        if name == "telegram-polling-watchdog":
            await asyncio.Event().wait()
        elif name == "Updater:start_polling:polling_task":
            ptb_delays.append(seconds)
            ptb_entered.set()
            await finish_ptb.wait()
            clock.now += stop_elapsed
        else:
            assert name == "telegram-polling-recovery"
            backoff_delays.append(seconds)
            backoff_entered.set()
            await finish_backoff.wait()
            clock.now += seconds
        await asyncio.sleep(0)

    ptb_asyncio = SimpleNamespace(**vars(asyncio))
    ptb_asyncio.sleep = controlled_sleep
    monkeypatch.setattr(networkloop, "asyncio", ptb_asyncio)
    monkeypatch.setattr(polling, "sleep", controlled_sleep)
    monkeypatch.setattr(polling, "uniform", lambda lower, upper: lower)

    async def limited_request():
        clock.now += 37
        raise RetryAfter(retry_after)

    app, updater, request = make_application([limited_request, []])
    clock.now = 1000
    try:
        await start(app, updater)
        await asyncio.wait_for(ptb_entered.wait(), timeout=2)
        await eventually(
            lambda: updater._recovery_task is not None and not updater.running
        )
        error_at = clock.now
        assert error_at == updater._started_at + 37
        assert ptb_delays == [400.5]
        assert backoff_delays == []
        assert not updater._can_poll.is_set()
        assert len(request.polls()) == 1
        assert not updater._recovery_task.done()

        finish_ptb.set()
        await asyncio.wait_for(backoff_entered.wait(), timeout=2)
        assert clock.now == error_at + stop_elapsed
        assert backoff_delays == [remaining]
        assert not updater._can_poll.is_set()
        assert len(request.polls()) == 1

        finish_backoff.set()
        await eventually(
            lambda: updater._last_heartbeat is not None
            and updater._recovery_task.done()
        )
        assert updater._last_heartbeat == error_at + max(401, stop_elapsed)
        assert request.max_inflight == 1
        assert updater.running and app.running and not updater._suspended
    finally:
        finish_ptb.set()
        finish_backoff.set()


async def test_bootstrap_error_recovers_after_application_starts(
    make_application, fast_backoff
):
    app, updater, _request = make_application(
        [[]], bootstrap_errors=[NetworkError("offline bootstrap")]
    )
    await app.initialize()
    await updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await app.start()
    await eventually(
        lambda: updater._last_heartbeat is not None and updater._recovery_task.done()
    )
    assert app.running and updater.running
    assert fast_backoff == [20]


async def test_successful_restart_preserves_offset_allowed_updates_and_queue(
    make_application, fast_backoff
):
    app, updater, request = make_application([[{"update_id": 100}], "conflict", []])
    queue = updater.update_queue
    await start(app, updater)
    await eventually(
        lambda: updater._recovery_task is not None
        and updater._recovery_task.done()
        and updater._last_error is None
    )
    assert updater.update_queue is queue
    assert request.polls()[1]["offset"] == 101
    assert request.polls()[2]["offset"] == 101
    assert all(
        params["allowed_updates"] == Update.ALL_TYPES for params in request.polls()
    )
    assert all(
        params.get("drop_pending_updates") is not True
        for endpoint, params in request.calls
        if endpoint == "deleteWebhook"
    )


@pytest.mark.parametrize("update", [None, Update(update_id=1)])
async def test_error_handler_does_not_restart_for_push_or_command_errors(
    make_application, update, caplog
):
    app, updater, _request = make_application()
    await start(app, updater)
    await app.process_error(update=update, error=NetworkError("not polling"))
    assert updater._last_error is None
    assert updater._recovery_task is None
    assert updater.running and app.running
    assert updater._can_poll.is_set()
    assert "Application error: type=NetworkError" in caplog.text


async def test_empty_batches_are_heartbeats_even_when_users_are_inactive(
    make_application, clock, caplog
):
    caplog.set_level(logging.INFO)
    app, updater, request = make_application([[]])
    await app.initialize()
    clock.now = 8 * 3600
    updates = await app.bot.get_updates(timeout=10, allowed_updates=Update.ALL_TYPES)
    assert updates == ()
    assert updater.update_queue.empty()
    assert updater._last_heartbeat == clock.now
    await updater.check_watchdog()
    assert request.alerts() == []
    assert "Polling heartbeat established; successful getUpdates batch" in caplog.text


@pytest.mark.parametrize(
    ("age", "error", "alert"),
    [
        (1800, Conflict("offline"), True),
        (8 * 3600, None, True),
        (1799, None, False),
        (1799, TimedOut(), False),
        (5, NetworkError("offline"), False),
    ],
)
async def test_watchdog_alerts_on_expired_heartbeat_with_or_without_error(
    make_application, clock, caplog, age, error, alert
):
    app, updater, request = make_application()
    await app.initialize()
    clock.now = 100000
    updater._last_heartbeat = clock.now - age
    updater._last_error = error
    await updater.check_watchdog()
    assert len(request.alerts()) == int(alert)
    if alert:
        assert "Polling alert:" in caplog.text
        text = request.alerts()[0]["text"]
        assert "NoneType" not in text
        assert "当前无自动重启任务" in text
        if error is None:
            assert "未记录异常，轮询心跳停滞" in text
        else:
            assert type(error).__name__ in text
        assert request.alerts()[0]["chat_id"] == 7


async def test_watchdog_alerts_when_polling_hangs_after_success(
    make_application, clock
):
    app, updater, request = make_application([[]])
    clock.now = 1000
    await start(app, updater)
    await eventually(lambda: len(request.polls()) == 2 and request.inflight == 1)
    heartbeat = updater._last_heartbeat
    assert heartbeat == clock.now
    assert updater._last_error is None and updater._recovery_task is None
    clock.now = heartbeat + polling.HEARTBEAT_TIMEOUT - 1
    await updater.check_watchdog()
    assert request.alerts() == []
    clock.now += 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 1
    text = request.alerts()[0]["text"]
    assert "未记录异常，轮询心跳停滞" in text
    assert "当前无自动重启任务" in text
    assert "NoneType" not in text and "仍在尝试" not in text
    clock.now += polling.ALERT_INTERVAL - 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 1
    clock.now += 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 2
    assert updater._last_heartbeat == heartbeat
    assert updater._last_error is None and updater._recovery_task is None
    assert request.inflight == 1 and len(request.polls()) == 2
    assert updater.running and app.running


async def test_watchdog_grace_period_when_first_poll_hangs(make_application, clock):
    app, updater, request = make_application()
    clock.now = 8 * 3600
    await start(app, updater)
    await eventually(lambda: len(request.polls()) == 1 and request.inflight == 1)
    assert updater._started_at == clock.now
    assert updater._last_heartbeat is None and updater._last_error is None
    clock.now = updater._started_at + polling.HEARTBEAT_TIMEOUT - 1
    await updater.check_watchdog()
    assert request.alerts() == []
    clock.now += 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 1
    assert "未记录异常，轮询心跳停滞" in request.alerts()[0]["text"]
    assert updater._last_heartbeat is None and updater._last_error is None
    assert updater._recovery_task is None and updater._can_poll.is_set()
    assert request.inflight == 1 and len(request.polls()) == 1


@pytest.mark.parametrize("finished", [False, True])
async def test_watchdog_reports_actual_recovery_task_state(
    make_application, clock, finished
):
    app, updater, request = make_application()
    await app.initialize()
    updater._recovery_task = asyncio.create_task(
        asyncio.sleep(0) if finished else asyncio.Event().wait()
    )
    if finished:
        await updater._recovery_task
    clock.now = polling.HEARTBEAT_TIMEOUT
    await updater.check_watchdog()
    text = request.alerts()[0]["text"]
    expected = "当前无自动重启任务" if finished else "自动重启任务正在运行"
    assert expected in text


async def test_stop_cleanup_is_not_a_heartbeat_and_cannot_clear_polling_error(
    make_application, clock
):
    app, updater, request = make_application()
    await app.initialize()
    updater._last_heartbeat = 0
    updater._last_error = Conflict("offline")
    clock.now = polling.HEARTBEAT_TIMEOUT
    await app.bot.get_updates(timeout=0)
    assert updater._last_heartbeat == 0
    assert isinstance(updater._last_error, Conflict)
    await updater.check_watchdog()
    assert len(request.alerts()) == 1


async def test_watchdog_grace_period_before_first_successful_batch(
    make_application, clock
):
    app, updater, request = make_application()
    await app.initialize()
    updater._last_error = NetworkError("offline")
    clock.now = polling.HEARTBEAT_TIMEOUT - 1
    await updater.check_watchdog()
    assert request.alerts() == []
    clock.now += 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 1


async def test_failed_alert_send_is_rate_limited_and_does_not_stop_application(
    make_application, clock, caplog
):
    app, updater, request = make_application()
    await app.initialize()
    await app.start()
    request.send_error = TimedOut()
    updater._last_heartbeat = 0
    updater._last_error = Conflict("offline")
    clock.now = polling.HEARTBEAT_TIMEOUT
    await updater.check_watchdog()
    clock.now += polling.ALERT_INTERVAL - 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 1
    assert app.running
    assert "Polling alert delivery failed: type=TimedOut" in caplog.text
    clock.now += 1
    await updater.check_watchdog()
    assert len(request.alerts()) == 2


async def test_suspension_and_watchdog_share_one_hour_alert_limit(
    make_application, clock
):
    app, updater, request = make_application()
    await app.initialize()
    updater._last_heartbeat = 0
    updater._last_error = Conflict("offline")
    await updater._suspend("test failure limit")
    clock.now = polling.HEARTBEAT_TIMEOUT
    await updater.check_watchdog()
    assert len(request.alerts()) == 1
    clock.now = polling.ALERT_INTERVAL
    await updater.check_watchdog()
    assert len(request.alerts()) == 2
    assert "自动重启已暂停，需人工处理" in request.alerts()[1]["text"]


async def test_watchdog_runs_on_a_ten_minute_period(
    make_application, clock, monkeypatch
):
    app, updater, request = make_application()
    await app.initialize()
    intervals = []

    async def tick(seconds):
        intervals.append(seconds)
        if len(intervals) > 1:
            await asyncio.Event().wait()
        await asyncio.sleep(0)

    monkeypatch.setattr(polling, "sleep", tick)
    updater._last_heartbeat = 0
    updater._last_error = Conflict("offline")
    clock.now = polling.HEARTBEAT_TIMEOUT
    updater._watchdog_task = asyncio.create_task(updater._watchdog_loop())
    await asyncio.wait_for(request.alert_sent.wait(), timeout=2)
    assert intervals[0] == polling.WATCHDOG_INTERVAL == 600


async def test_stop_cancels_backoff_and_watchdog_without_stopping_application(
    make_application, fast_backoff, monkeypatch
):
    entered = asyncio.Event()

    async def hold(seconds):
        task = asyncio.current_task()
        if task is not None and task.get_name() == "telegram-polling-recovery":
            entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(polling, "sleep", hold)
    app, updater, request = make_application(["conflict"])
    await start(app, updater)
    await asyncio.wait_for(entered.wait(), timeout=2)
    await updater.stop()
    assert updater._recovery_task.cancelled()
    assert updater._watchdog_task.cancelled()
    assert len(request.polls()) == 1
    assert app.running


async def test_failed_old_poller_stop_does_not_start_a_second_poller(
    make_application, fast_backoff, monkeypatch
):
    app, updater, request = make_application()
    await start(app, updater)
    await eventually(lambda: len(request.polls()) == 1)
    with monkeypatch.context() as patch:
        patch.setattr(
            Updater, "stop", AsyncMock(side_effect=RuntimeError("cannot stop"))
        )
        updater._polling_error(Conflict("offline"))
        await eventually(lambda: updater._suspended and updater._recovery_task.done())
        assert fast_backoff == []
        assert len(request.polls()) == 1
        assert len(request.alerts()) == 1
        assert app.running


async def test_torrentino_registers_guard_and_preserves_business_task_lifecycle(
    monkeypatch,
):
    import torrentino

    request = TelegramRequest()
    bot = polling.PollingBot(
        token="42:offline-test", request=request, get_updates_request=request
    )
    values = {"bot.token": "42:offline-test", "bot.super_user": 7}
    config = SimpleNamespace(
        get=lambda key: values.get(key),
        validate=lambda: True,
        set_bot_commands=lambda commands: None,
    )
    seen = {}
    tasks = {}

    async def wait_forever(*args):
        await asyncio.Event().wait()

    client = SimpleNamespace(monitor_downloads=AsyncMock(side_effect=wait_forever))
    pusher = SimpleNamespace(run=AsyncMock(side_effect=wait_forever))
    monkeypatch.setattr(torrentino, "BotConfigurator", lambda: config)
    monkeypatch.setattr(torrentino, "PollingBot", lambda token: bot)
    monkeypatch.setattr(torrentino.DownloadHistory, "set_log_file", lambda path: None)
    monkeypatch.setattr(torrentino.DownloadQueue, "set_state_file", lambda path: None)
    monkeypatch.setattr(torrentino, "log_startup_path", lambda config: None)
    monkeypatch.setattr(
        torrentino, "get_logger", lambda *args: logging.getLogger("test")
    )
    monkeypatch.setattr(torrentino, "get_torrent_connection", lambda: client)
    monkeypatch.setattr(torrentino, "Fc2Pusher", lambda bot: pusher)

    def run_polling(application, **kwargs):
        seen["application"] = application
        seen["options"] = kwargs

    def create_task(application, coroutine, *, name=None):
        task = asyncio.create_task(coroutine, name=name)
        tasks[name] = task
        return task

    original_add_handlers = Application.add_handlers

    def add_handlers(application, handlers):
        seen["handlers"] = handlers
        original_add_handlers(application, handlers)

    monkeypatch.setattr(Application, "run_polling", run_polling)
    monkeypatch.setattr(Application, "create_task", create_task)
    monkeypatch.setattr(Application, "add_handlers", add_handlers)
    torrentino.bot()
    app = seen["application"]
    updater = app.updater
    assert isinstance(updater, polling.PollingUpdater)
    assert updater.error_handler in app.error_handlers
    assert seen["handlers"] is torrentino.HANDLERS
    assert seen["options"] == {"allowed_updates": Update.ALL_TYPES}
    assert app.post_init is not None and app.post_stop is not None
    assert app.post_shutdown is not None
    try:
        await app.initialize()
        await app.post_init(app)
        await asyncio.sleep(0)
        assert updater.application is app
        assert set(tasks) == {"transmission-download-monitor", "fc2-channel-poller"}
        client.monitor_downloads.assert_awaited_once_with(bot)
        pusher.run.assert_awaited_once_with()
        updater._watchdog_task = asyncio.create_task(wait_forever())
        await app.post_stop(app)
        assert updater._watchdog_task.cancelled()
        assert all(not task.done() for task in tasks.values())
        await app.post_shutdown(app)
        assert all(task.cancelled() for task in tasks.values())
    finally:
        await app.post_shutdown(app)
        await updater.close_supervision()
        await app.shutdown()
