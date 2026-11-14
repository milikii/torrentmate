import asyncio
import json
import logging
import subprocess
import sys
from collections import deque
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest
from telegram import Update
from telegram.error import Conflict, NetworkError, TimedOut
from telegram.ext import Application, ExtBot
from telegram.request import HTTPXRequest

from lib import polling


FAKE_BOT = "42:offline-test"


class TelegramServer:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        self.tasks = set()
        self.writers = set()
        self.full = asyncio.Event()
        self.release = asyncio.Event()
        self.long_poll = asyncio.Event()
        self.holds_started = 0
        self.target_holds = 0
        self.base_url = ""

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            while True:
                head = (await reader.readuntil(b"\r\n\r\n")).decode()
                lines = head.split("\r\n")
                endpoint = lines[0].split()[1].rsplit("/", 1)[-1]
                headers = dict(
                    line.lower().split(":", 1) for line in lines[1:] if ":" in line
                )
                body = await reader.readexactly(int(headers.get("content-length", 0)))
                params = {key: values[0] for key, values in parse_qs(body.decode()).items()}
                self.calls.append((endpoint, params))
                code = 200
                if endpoint == "hold":
                    self.holds_started += 1
                    if self.holds_started >= self.target_holds:
                        self.full.set()
                    await self.release.wait()
                    result = True
                elif endpoint == "getMe":
                    result = {
                        "id": 42,
                        "is_bot": True,
                        "first_name": "Test",
                        "username": "test_bot",
                    }
                elif endpoint == "getUpdates":
                    if float(params.get("timeout", 0)) == 0:
                        result = []
                    elif self.responses:
                        result = self.responses.popleft()
                    else:
                        await self.long_poll.wait()
                        result = []
                    if result == "conflict":
                        code = 409
                        result = "Conflict: terminated by other getUpdates request"
                    elif result == "network":
                        code = 502
                        result = "temporary network failure"
                elif endpoint == "sendMessage":
                    result = {
                        "message_id": 1,
                        "date": 0,
                        "chat": {"id": 7, "type": "private"},
                        "text": params["text"],
                    }
                else:
                    result = True
                response = (
                    {"ok": True, "result": result}
                    if code == 200
                    else {"ok": False, "error_code": code, "description": result}
                )
                payload = json.dumps(response).encode()
                writer.write(
                    f"HTTP/1.1 {code} Test\r\nContent-Type: application/json\r\n"
                    f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload
                )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)

    def polls(self):
        return [
            params
            for endpoint, params in self.calls
            if endpoint == "getUpdates" and float(params.get("timeout", 0)) > 0
        ]


@pytest.fixture
async def telegram_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    server = TelegramServer()
    listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    server.base_url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/bot"
    try:
        yield server
    finally:
        # Python 3.12 Server.wait_closed() also waits for open connections, so the
        # handlers must be cancelled (which closes their writers) before it runs.
        tasks = list(server.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        listener.close()
        await listener.wait_closed()


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0)
    monkeypatch.setattr(polling, "monotonic", lambda: state.now)
    monkeypatch.setattr(polling, "time", lambda: state.now)
    return state


@pytest.fixture
def process_exit(monkeypatch):
    mocked = Mock(return_value=None)
    monkeypatch.setattr(polling.os, "_exit", mocked)
    return mocked


@pytest.fixture
async def real_application(telegram_server, clock, process_exit, monkeypatch, tmp_path):
    monkeypatch.setattr(polling, "PROCESS_EXIT_MARKER", tmp_path / "last-exit")
    applications = []

    async def make(responses=(), pool_timeout=0.05):
        telegram_server.responses.extend(responses)
        request = HTTPXRequest(connection_pool_size=8, pool_timeout=pool_timeout)
        bot = polling.PollingBot(
            token=FAKE_BOT,
            get_updates_request=request,
            base_url=telegram_server.base_url,
        )
        updater = polling.PollingUpdater(
            bot,
            asyncio.Queue(),
            get_updates_request=request,
            super_user=7,
            log=logging.getLogger("test.polling.pool"),
        )
        app = Application.builder().updater(updater).build()
        updater.application = app
        app.add_error_handler(updater.error_handler)
        applications.append(app)
        await app.initialize()
        return app, updater, request

    yield make
    for app in applications:
        updater = app.updater
        assert updater is not None
        await updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()


async def occupy_pool(server, request):
    server.full.clear()
    server.target_holds = server.holds_started + 8
    tasks = [
        asyncio.create_task(request.post(f"{server.base_url}{FAKE_BOT}/hold"))
        for _ in range(8)
    ]
    await asyncio.wait_for(server.full.wait(), timeout=3)
    return tasks


async def eventually(predicate):
    for _ in range(500):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition not reached")


@pytest.fixture
def fast_recovery(monkeypatch, clock):
    real_sleep = asyncio.sleep

    async def backoff(delay):
        clock.now += delay
        await real_sleep(0)

    async def watchdog(updater):
        await asyncio.Event().wait()

    monkeypatch.setattr(polling, "sleep", backoff)
    monkeypatch.setattr(polling, "uniform", lambda lower, upper: lower)
    monkeypatch.setattr(polling.PollingUpdater, "_watchdog_loop", watchdog)


async def test_get_updates_settings_preserve_general_request_defaults(real_application):
    _app, updater, request = await real_application(pool_timeout=10.0)
    general = updater.bot.request
    assert isinstance(general, HTTPXRequest)
    polling_transport = request._client._transport
    general_transport = general._client._transport
    assert isinstance(polling_transport, httpx.AsyncHTTPTransport)
    assert isinstance(general_transport, httpx.AsyncHTTPTransport)
    assert polling_transport._pool._max_connections == 8
    assert general_transport._pool._max_connections == 256
    assert request._client.timeout == httpx.Timeout(5.0, pool=10.0)
    assert general._client.timeout == httpx.Timeout(5.0, pool=1.0)
    assert request._media_write_timeout == general._media_write_timeout == 20.0
    assert request._client_kwargs["http1"] and not request._client_kwargs["http2"]


async def test_real_pool_timeout_rebuilds_and_retries_once_without_conflict_quota(
    real_application, telegram_server, monkeypatch, caplog
):
    _app, updater, request = await real_application([[]])
    updater._restart_times.extend([0.0] * polling.MAX_RESTARTS)
    updater._conflict_restarts = polling.MAX_RESTARTS
    old_client = request._client
    old_pool = old_client._transport._pool
    old_settings = dict(request._client_kwargs)
    held = await occupy_pool(telegram_server, request)
    calls = []
    errors = []
    original = ExtBot.get_updates

    async def counted(bot, **kwargs):
        calls.append(kwargs)
        try:
            return await original(bot, **kwargs)
        except TimedOut as error:
            errors.append(error)
            raise

    monkeypatch.setattr(ExtBot, "get_updates", counted)
    assert await updater.bot.get_updates(
        offset=123,
        limit=2,
        timeout=12,
        allowed_updates=["callback_query"],
        read_timeout=6.0,
        write_timeout=7.0,
        connect_timeout=4.0,
    ) == ()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(errors) == 1 and isinstance(errors[0].__cause__, httpx.PoolTimeout)
    assert old_client.is_closed and request._client is not old_client
    assert request._client._transport._pool is not old_pool
    assert request._client_kwargs == old_settings
    assert updater._client_rebuilds == 1 and updater._rebuild_failures == 0
    assert len(updater._restart_times) == polling.MAX_RESTARTS
    assert not updater._suspended
    assert "connections_total=8 active=8 idle=0" in caplog.text
    params = telegram_server.polls()
    assert len(params) == 1
    assert params[0]["offset"] == "123" and params[0]["limit"] == "2"
    assert float(params[0]["timeout"]) == 12
    assert json.loads(params[0]["allowed_updates"]) == ["callback_query"]
    results = await asyncio.gather(*held, return_exceptions=True)
    assert all(isinstance(result, NetworkError) for result in results)


async def test_rebuilt_retry_failure_does_not_use_conflict_budget(
    real_application, telegram_server
):
    _app, updater, request = await real_application(["network"])
    updater._restart_times.extend([0.0] * polling.MAX_RESTARTS)
    old_client = request._client
    held = await occupy_pool(telegram_server, request)
    with pytest.raises(NetworkError, match="temporary network failure"):
        await updater.bot.get_updates(timeout=10)
    assert old_client.is_closed and request._client is not old_client
    assert updater._client_rebuilds == updater._rebuild_failures == 1
    assert len(updater._restart_times) == polling.MAX_RESTARTS
    assert len(telegram_server.polls()) == 1
    assert updater._recovery_task is None and not updater._suspended
    await asyncio.gather(*held, return_exceptions=True)


async def test_hourly_probe_rebuilds_occupied_pool_and_clears_suspension(
    real_application, telegram_server, clock, fast_recovery, process_exit
):
    app, updater, request = await real_application(["conflict"] * 6)
    await app.start()
    await updater.start_polling(timeout=10, allowed_updates=Update.ALL_TYPES)
    await eventually(lambda: updater._suspended and updater._recovery_task.done())
    assert len(updater._restart_times) == polling.MAX_RESTARTS
    old_client = request._client
    general_client = updater.bot.request._client
    held = await occupy_pool(telegram_server, request)
    probe_at = updater._next_probe_at
    clock.now = probe_at - 1
    await updater.check_watchdog()
    assert request._client is old_client and updater._client_rebuilds == 0
    telegram_server.responses.append([])
    clock.now = probe_at
    await updater.check_watchdog()
    await eventually(lambda: not updater._suspended and updater._recovery_task.done())
    assert old_client.is_closed and request._client is not old_client
    assert updater.bot.request._client is general_client
    assert updater._client_rebuilds == 1 and updater._rebuild_failures == 0
    assert updater._last_heartbeat == clock.now and updater._last_error is None
    assert updater._suspended_at is None and updater._next_probe_at is None
    assert not updater._conflict_active and updater._can_poll.is_set()
    assert updater.running and app.running
    process_exit.assert_not_called()
    await asyncio.gather(*held, return_exceptions=True)


async def test_failed_hourly_probes_preserve_24_hour_deadline(
    real_application, telegram_server, clock, process_exit
):
    app, updater, request = await real_application()
    await app.start()
    updater._last_error = Conflict("duplicate polling instance")
    updater._conflict_active = True
    await updater._suspend("test suspension")
    suspended_at = updater._suspended_at
    for hour in (1, 2):
        telegram_server.responses.append("conflict")
        old_client = request._client
        clock.now = hour * polling.PROBE_INTERVAL
        await updater.check_watchdog()
        await eventually(lambda: updater._recovery_task.done())
        assert old_client.is_closed and request._client is not old_client
        assert updater._suspended and updater._suspended_at == suspended_at
        assert updater._next_probe_at == (hour + 1) * polling.PROBE_INTERVAL
    clock.now = polling.SUSPEND_TIMEOUT
    await updater.check_watchdog()
    assert updater._probe_exhausted and updater._client_rebuilds == 2
    clock.now += polling.PROBE_INTERVAL
    await updater.check_watchdog()
    assert updater._client_rebuilds == 2 and updater._suspended
    process_exit.assert_not_called()


async def test_three_rebuilt_client_failures_trigger_hourly_limited_exit(
    real_application, telegram_server, clock, process_exit
):
    _app, updater, request = await real_application(["network"] * 3)
    held = await occupy_pool(telegram_server, request)
    for _ in range(3):
        updater._can_poll.set()
        with pytest.raises(NetworkError, match="temporary network failure"):
            await updater.bot.get_updates(timeout=10)
    await asyncio.gather(*held, return_exceptions=True)
    assert updater._client_rebuilds == 1 and updater._rebuild_failures == 3
    assert not updater._restart_times
    await updater._recover()
    process_exit.assert_called_once_with(1)
    assert float(polling.PROCESS_EXIT_MARKER.read_text()) == clock.now
    _another_app, another, _another_request = await real_application()
    clock.now = polling.PROCESS_EXIT_INTERVAL - 1
    assert not another._exit_process("same container restart cooldown")
    process_exit.assert_called_once_with(1)
    clock.now = polling.PROCESS_EXIT_INTERVAL
    assert another._exit_process("hourly cooldown expired")
    assert process_exit.call_count == 2


async def test_stale_heartbeat_exits_after_one_hour_with_rate_limit(
    real_application, clock, process_exit
):
    _app, updater, _request = await real_application()
    clock.now = polling.PROCESS_HEARTBEAT_TIMEOUT
    await updater.check_watchdog()
    process_exit.assert_not_called()
    clock.now += 1
    await updater.check_watchdog()
    process_exit.assert_called_once_with(1)
    clock.now += polling.PROCESS_EXIT_INTERVAL - 1
    await updater.check_watchdog()
    process_exit.assert_called_once_with(1)
    clock.now += 1
    await updater.check_watchdog()
    assert process_exit.call_count == 2


@pytest.mark.parametrize("remember_conflict", [False, True])
async def test_conflict_never_requests_process_exit(
    real_application, clock, process_exit, remember_conflict
):
    _app, updater, _request = await real_application()
    updater._conflict_active = remember_conflict
    updater._last_error = (
        NetworkError("connection failed after Conflict")
        if remember_conflict
        else Conflict("duplicate polling instance")
    )
    assert not updater._exit_process("must not restart a Conflict instance")
    clock.now = polling.PROCESS_HEARTBEAT_TIMEOUT + 1
    await updater.check_watchdog()
    process_exit.assert_not_called()
    assert not polling.PROCESS_EXIT_MARKER.exists()


def test_real_process_exit_is_limited_across_process_restarts(tmp_path):
    marker = tmp_path / "subprocess-exit"
    code = """
import logging
import sys
from pathlib import Path
from lib import polling

polling.PROCESS_EXIT_MARKER = Path(sys.argv[1])
polling.time = lambda: float(sys.argv[2])
updater = object.__new__(polling.PollingUpdater)
updater._conflict_active = False
updater._last_error = None
updater.log = logging.getLogger("subprocess-exit")
updater._exit_process("subprocess proof")
print("survived")
"""

    def run(now):
        return subprocess.run(
            [sys.executable, "-c", code, str(marker), str(now)],
            cwd=Path(polling.__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=10,
        )

    first = run(0)
    assert first.returncode == 1 and "survived" not in first.stdout
    assert "Polling process exit for Docker restart" in first.stderr
    limited = run(polling.PROCESS_EXIT_INTERVAL - 1)
    assert limited.returncode == 0 and limited.stdout.strip() == "survived"
    next_hour = run(polling.PROCESS_EXIT_INTERVAL)
    assert next_hour.returncode == 1 and "survived" not in next_hour.stdout
    assert float(marker.read_text()) == polling.PROCESS_EXIT_INTERVAL
