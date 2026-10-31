import datetime as dt
import json
import os
import time
from types import SimpleNamespace

import pytest
import yaml

from lib.classifier import DEFAULT_RULES
from lib.func import trans
from models.BotConfigurator import BotConfigurator
from models.DownloadHistory import DownloadHistory
from models.DownloadMonitor import ADOPT_BASELINE_FILE, DownloadMonitor
from models.DownloadQueue import DownloadQueue, QueueEntry
from models.Fc2Blacklist import Fc2Blacklist

# 测试基线：视作 2026-10-01 部署，之后添加的 FC2 任务才算新任务。
BASELINE_TS = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()
NEW_ADDED = dt.datetime(2026, 10, 28, 11, 50, 0)  # naive，按本地时区解释
OLD_ADDED = dt.datetime(2026, 9, 1, 12, 0, 0)


class FakeTorrent:
    def __init__(self, **kwargs):
        values = {
            "id": 1,
            "name": "SSIS-123",
            "download_dir": "/downloads/未分类",
            "metadata_percent_complete": 1,
            "percent_done": 0.4,
            "total_size": 1000,
            "left_until_done": 600,
            "rate_download": 1024,
            "rate_upload": 0,
            "peers_connected": 3,
            "ratio": 0.12,
            "status": "downloading",
            "size_when_done": 1000,
            "done_date": None,
            "added_date": None,
            "error_string": "",
            "info_hash": "abc",
        }
        values.update(kwargs)
        for key, value in values.items():
            setattr(self, key, value)

    @property
    def eta(self):
        return dt.timedelta(minutes=5)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True


class FakeClient:
    def __init__(self, torrent=None, torrents=None):
        self.torrent = torrent
        self.torrents = (
            list(torrents)
            if torrents is not None
            else ([torrent] if torrent is not None else [])
        )
        self.moved = []
        self.data_moved = []
        self.stopped = []

    def get_torrents(self):
        return list(self.torrents)

    def get_torrent(self, torrent_id):
        for torrent in self.torrents:
            if torrent.id == torrent_id:
                return torrent
        if self.torrent is not None:
            return self.torrent
        raise KeyError(torrent_id)

    def change_torrent(self, torrent_id, location=None, **kwargs):
        self.moved.append(location)

    def move_torrent_data(self, torrent_id, location, move=True):
        self.data_moved.append(location)

    def stop_torrent(self, torrent_id):
        self.stopped.append(torrent_id)


def write_config(path, fc2=None, **notify_overrides):
    data = {
        "bot": {"token": "token", "super_user": 1},
        "transmission": {"pause_on_complete": False},
        "directories": {
            "FC2": "/downloads/FC2",
            "有码": "/downloads/有码",
            "无码": "/downloads/无码",
            "国产": "/downloads/国产",
            "未分类": "/downloads/未分类",
        },
        "auto_classify": {
            "enabled": True,
            "fallback": "未分类",
            "rules": DEFAULT_RULES,
        },
        "notify": {
            "progress": True,
            "check_interval": 10,
            "progress_interval": 15,
            "milestones": [],
            "stalled_minutes": 0,
            **notify_overrides,
        },
    }
    if fc2 is not None:
        data["fc2"] = fc2
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "torrentino.yaml"
    write_config(path)
    # 预置接管基线文件，避免每个旧用例都变成“部署第一轮”（不接管）。
    (tmp_path / ADOPT_BASELINE_FILE).write_text(
        json.dumps({"adopted_since": BASELINE_TS}), encoding="utf-8"
    )
    original_file = BotConfigurator.config_file
    original_history = DownloadHistory.download_log_file
    BotConfigurator.config_file = str(path)
    DownloadHistory.set_log_file(str(tmp_path / "history.jsonl"))
    DownloadQueue.set_state_file(None)
    DownloadQueue.clear()
    yield path
    BotConfigurator.config_file = original_file
    DownloadHistory.set_log_file(original_history)
    DownloadQueue.clear()


async def test_auto_classify_moves_torrent(env):
    torrent = FakeTorrent(name="FC2-PPV-1234567", download_dir="/downloads/未分类")
    client = FakeClient(torrent)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/未分类",
            auto_classify=True,
        )
    )

    await DownloadMonitor(client, bot).tick()

    assert client.moved == ["/downloads/FC2"]
    entry = DownloadQueue.get(1)
    assert entry is not None
    assert entry.classified is True
    assert entry.final_dir == "/downloads/FC2"
    assert any("自动归类" in message["text"] for message in bot.sent)


async def test_completion_sends_card_and_writes_history(env, tmp_path):
    torrent = FakeTorrent(
        name="SSIS-123",
        percent_done=1.0,
        left_until_done=0,
        status="seeding",
        download_dir="/downloads/有码",
        done_date=dt.datetime(2026, 10, 28, 12, 0, 0),
        added_date=dt.datetime(2026, 10, 28, 11, 50, 0),
    )
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/有码",
            message_id=555,
        )
    )

    await DownloadMonitor(FakeClient(torrent), bot).tick()

    assert bot.edits, "完成时应更新原有进度卡片"
    assert "下载完成" in bot.edits[0]["text"]
    assert DownloadQueue.get(1) is None
    assert "SSIS-123" in (tmp_path / "history.jsonl").read_text(encoding="utf-8")


async def test_progress_card_is_not_spammed(env):
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1, chat_id=9, lang_code="zh", requested_dir="/downloads/有码"
        )
    )
    monitor = DownloadMonitor(FakeClient(torrent), bot)

    await monitor.tick()
    first_count = len(bot.sent)
    assert DownloadQueue.get(1).message_id is not None

    await monitor.tick()
    assert len(bot.sent) == first_count, "间隔内进度未变化时不应重复发送"


async def test_error_is_reported_once(env):
    torrent = FakeTorrent(name="SSIS-123", error_string="tracker unreachable")
    bot = FakeBot()
    DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, lang_code="zh"))
    monitor = DownloadMonitor(FakeClient(torrent), bot)

    await monitor.tick()
    await monitor.tick()

    messages = [message["text"] for message in bot.sent if "出错" in message["text"]]
    assert len(messages) == 1


async def test_stalled_notification(env):
    write_config(env, stalled_minutes=1)
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            last_progress_ts=time.time() - 120,
            last_progress_value=30.0,
            progress_card=False,
        )
    )

    await DownloadMonitor(FakeClient(torrent), bot).tick()

    assert any("没有进度" in message["text"] for message in bot.sent)


async def test_fc2_bot_does_not_send_stalled_notice(env):
    """FC2 专用机器人（fc2.enabled=true）不再发任何停滞提醒。"""
    write_config(env, fc2={"enabled": True}, stalled_minutes=1)
    torrent = FakeTorrent(name="FC2-PPV-5000545", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            last_progress_ts=time.time() - 7200,
            last_progress_value=30.0,
            progress_card=False,
        )
    )

    await DownloadMonitor(FakeClient(torrent), bot).tick()

    assert bot.sent == []


async def test_download_bot_still_sends_stalled_notice(env):
    """下载机器人（fc2.enabled=false）的停滞提醒行为不变。"""
    write_config(env, fc2={"enabled": False}, stalled_minutes=1)
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            last_progress_ts=time.time() - 120,
            last_progress_value=30.0,
            progress_card=False,
        )
    )

    await DownloadMonitor(FakeClient(torrent), bot).tick()

    stalled = [message for message in bot.sent if "没有进度" in message["text"]]
    assert len(stalled) == 1
    assert stalled[0]["chat_id"] == 9


async def test_stalled_notice_is_throttled_to_once_per_six_hours(env, monkeypatch):
    """同一任务 6 小时内最多一次；进度动一点又停也不许提前再发。"""
    now = [10000.0]
    monkeypatch.setattr("models.DownloadMonitor.time.time", lambda: now[0])
    write_config(env, stalled_minutes=1)
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            last_progress_ts=now[0] - 120,
            last_progress_value=30.0,
            progress_card=False,
        )
    )
    monitor = DownloadMonitor(FakeClient(torrent), bot)

    def stalled_messages():
        return [message for message in bot.sent if "没有进度" in message["text"]]

    await monitor.tick()
    assert len(stalled_messages()) == 1
    assert DownloadQueue.get(1).last_stalled_notify_ts == now[0]

    # 进度动了 0.02%（>0.01% 复位阈值）后再次停滞：notified_stalled 复位，
    # 但仍在上次提醒后的 6 小时内，不许再发。
    torrent.percent_done = 0.3002
    now[0] += 600
    await monitor.tick()
    assert DownloadQueue.get(1).notified_stalled is False

    now[0] += 120
    await monitor.tick()
    assert len(stalled_messages()) == 1

    # 距上次提醒满 6 小时后再次停滞：可以再发。
    now[0] = DownloadQueue.get(1).last_stalled_notify_ts + 6 * 3600
    await monitor.tick()
    assert len(stalled_messages()) == 2


async def test_stalled_threshold_minutes_still_configurable(env, monkeypatch):
    """notify.stalled_minutes 仍是触发阈值：29 分钟不发，30 分钟才发。"""
    now = [50000.0]
    monkeypatch.setattr("models.DownloadMonitor.time.time", lambda: now[0])
    write_config(env, stalled_minutes=30)
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            last_progress_ts=now[0] - 29 * 60,
            last_progress_value=30.0,
            progress_card=False,
        )
    )
    monitor = DownloadMonitor(FakeClient(torrent), bot)

    await monitor.tick()
    assert bot.sent == []

    now[0] += 60
    await monitor.tick()
    stalled = [message for message in bot.sent if "没有进度" in message["text"]]
    assert len(stalled) == 1


from telegram.error import TelegramError


class FailingBot(FakeBot):
    async def send_message(self, **kwargs):
        raise TelegramError("telegram down")

    async def edit_message_text(self, **kwargs):
        raise TelegramError("telegram down")


async def test_classify_failure_retries_next_tick(env):
    torrent = FakeTorrent(name="FC2-PPV-1234567", download_dir="/downloads/未分类")

    class FailingMoveClient(FakeClient):
        def change_torrent(self, torrent_id, location=None, **kwargs):
            raise RuntimeError("disk full")

    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/未分类",
            auto_classify=True,
        )
    )
    await DownloadMonitor(FailingMoveClient(torrent), FakeBot()).tick()
    entry = DownloadQueue.get(1)
    assert entry is not None and entry.classified is False

    # 移动恢复后，下一轮应能成功归类。
    await DownloadMonitor(FakeClient(torrent), FakeBot()).tick()
    entry = DownloadQueue.get(1)
    assert entry is not None and entry.classified is True
    assert entry.final_dir == "/downloads/FC2"


async def test_completion_failure_keeps_queue_and_history(env, tmp_path):
    torrent = FakeTorrent(
        name="SSIS-123",
        percent_done=1.0,
        left_until_done=0,
        status="seeding",
        download_dir="/downloads/有码",
        done_date=dt.datetime(2026, 10, 28, 12, 0, 0),
        added_date=dt.datetime(2026, 10, 28, 11, 50, 0),
    )
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/有码",
            message_id=555,
        )
    )
    await DownloadMonitor(FakeClient(torrent), FailingBot()).tick()
    assert DownloadQueue.get(1) is not None
    assert not (tmp_path / "history.jsonl").exists()


async def test_hash_mismatch_does_not_move_torrent(env):
    torrent = FakeTorrent(name="FC2-PPV-1234567", download_dir="/downloads/未分类")
    client = FakeClient(torrent)
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/未分类",
            info_hash="deadbeef",
            auto_classify=True,
        )
    )
    await DownloadMonitor(client, FakeBot()).tick()
    assert client.moved == []
    assert DownloadQueue.get(1) is None


async def test_metadata_incomplete_does_not_classify_or_complete(env):
    torrent = FakeTorrent(
        name="FC2-PPV-1234567",
        download_dir="/downloads/未分类",
        metadata_percent_complete=0.5,
        percent_done=1.0,
        status="seeding",
    )
    client = FakeClient(torrent)
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/未分类",
            auto_classify=True,
        )
    )
    await DownloadMonitor(client, FakeBot()).tick()
    assert client.moved == []
    assert DownloadQueue.get(1) is not None


async def test_classify_failure_blocks_completion(env, tmp_path):
    torrent = FakeTorrent(
        name="FC2-PPV-1234567",
        download_dir="/downloads/未分类",
        percent_done=1.0,
        left_until_done=0,
        status="seeding",
        done_date=dt.datetime(2026, 10, 28, 12, 0, 0),
        added_date=dt.datetime(2026, 10, 28, 11, 50, 0),
    )

    class FailingMoveClient(FakeClient):
        def change_torrent(self, torrent_id, location=None, **kwargs):
            raise RuntimeError("disk full")

    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/未分类",
            auto_classify=True,
        )
    )
    await DownloadMonitor(FailingMoveClient(torrent), FakeBot()).tick()
    assert DownloadQueue.get(1) is not None
    assert not (tmp_path / "history.jsonl").exists()


async def test_completion_failure_still_pauses(env):
    torrent = FakeTorrent(
        name="SSIS-123",
        percent_done=1.0,
        left_until_done=0,
        status="seeding",
        download_dir="/downloads/有码",
        done_date=dt.datetime(2026, 10, 28, 12, 0, 0),
        added_date=dt.datetime(2026, 10, 28, 11, 50, 0),
    )
    client = FakeClient(torrent)
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/有码",
            message_id=555,
        )
    )
    monitor = DownloadMonitor(client, FailingBot())
    monitor.pause_on_complete = True
    await monitor.tick()
    assert DownloadQueue.get(1) is not None
    assert client.stopped == [1]


async def test_progress_card_not_modified_does_not_resend(env):
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)

    class NotModifiedBot(FakeBot):
        async def edit_message_text(self, **kwargs):
            raise TelegramError("Message is not modified")

    bot = NotModifiedBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/有码",
            message_id=555,
            last_percent=30.0,
            last_edit_ts=0.0,
        )
    )
    await DownloadMonitor(FakeClient(torrent), bot).tick()
    assert bot.sent == []
    assert DownloadQueue.get(1).message_id == 555


async def test_static_progress_is_not_updated_every_interval(env, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr("models.DownloadMonitor.time.time", lambda: now[0])
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/有码",
            message_id=555,
            last_percent=30.0,
            last_edit_ts=now[0] - 100,
        )
    )
    monitor = DownloadMonitor(FakeClient(torrent), bot)
    await monitor.tick()
    assert bot.sent == []
    assert bot.edits == []

    torrent.rate_download = 2048
    now[0] += 1699
    await monitor.tick()
    assert bot.edits == []
    now[0] += 1
    await monitor.tick()
    assert len(bot.edits) == 1
    assert bot.sent == []


async def test_identical_card_text_is_skipped(env):
    torrent = FakeTorrent(name="SSIS-123", percent_done=0.3)
    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/有码",
        )
    )
    monitor = DownloadMonitor(FakeClient(torrent), bot)
    await monitor.tick()
    first_sent = len(bot.sent)
    first_edits = len(bot.edits)
    assert first_sent == 1

    # 30 分钟后再次轮询，文本没变（速度/peers 都一样），不应再编辑或重发。
    entry = DownloadQueue.get(1)
    entry.last_edit_ts = time.time() - 2000
    entry.last_percent = 30.0
    DownloadQueue.update(entry)
    await monitor.tick()
    assert len(bot.sent) == first_sent
    assert len(bot.edits) == first_edits


async def test_classify_failure_notice_is_throttled(env, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr("models.DownloadMonitor.time.time", lambda: now[0])
    torrent = FakeTorrent(name="FC2-PPV-1234567", download_dir="/downloads/未分类")

    class FailingMoveClient(FakeClient):
        def change_torrent(self, torrent_id, location=None, **kwargs):
            raise RuntimeError("disk full")

    bot = FakeBot()
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            requested_dir="/downloads/未分类",
            auto_classify=True,
        )
    )
    monitor = DownloadMonitor(FailingMoveClient(torrent), bot)
    await monitor.tick()
    await monitor.tick()
    failed = [m for m in bot.sent if "归类失败" in m["text"]]
    assert len(failed) == 1
    now[0] += 3599
    await monitor.tick()
    assert len(bot.sent) == 1
    now[0] += 1
    await monitor.tick()
    assert len(bot.sent) == 2


async def test_failed_progress_delivery_does_not_cache_unsent_text(env):
    torrent = FakeTorrent(percent_done=0.3)
    client = FakeClient(torrent)
    DownloadQueue.add(
        QueueEntry(
            torrent_id=1,
            chat_id=9,
            lang_code="zh",
            message_id=555,
            last_percent=10.0,
            last_card_text="previous delivered card",
        )
    )
    monitor = DownloadMonitor(client, FailingBot())
    await monitor.tick()
    entry = DownloadQueue.get(1)
    assert entry.last_card_text == "previous delivered card"
    assert entry.last_percent == 10.0
    assert entry.last_edit_ts == 0.0

    monitor.bot = FakeBot()
    await monitor.tick()
    assert len(monitor.bot.edits) == 1
    assert monitor.bot.sent == []
    assert DownloadQueue.get(1).last_card_text == monitor.bot.edits[0]["text"]


async def test_failed_classify_notice_is_retried_before_throttle(env):
    class FailingMoveClient(FakeClient):
        def change_torrent(self, torrent_id, location=None, **kwargs):
            raise RuntimeError("disk full")

    torrent = FakeTorrent(name="FC2-PPV-1234567")
    DownloadQueue.add(
        QueueEntry(torrent_id=1, chat_id=9, lang_code="zh", auto_classify=True)
    )
    monitor = DownloadMonitor(FailingMoveClient(torrent), FailingBot())
    await monitor.tick()
    assert DownloadQueue.get(1).last_classify_fail_ts == 0.0

    monitor.bot = FakeBot()
    await monitor.tick()
    await monitor.tick()
    assert len(monitor.bot.sent) == 1
    assert "归类失败" in monitor.bot.sent[0]["text"]


async def test_metadata_card_is_throttled_for_ten_minutes(env, monkeypatch):
    now = [10000.0]
    monkeypatch.setattr("models.DownloadMonitor.time.time", lambda: now[0])
    torrent = FakeTorrent(metadata_percent_complete=0.5, percent_done=0.0)
    bot = FakeBot()
    DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, lang_code="zh"))
    monitor = DownloadMonitor(FakeClient(torrent), bot)
    await monitor.tick()
    assert len(bot.sent) == 1
    assert "元数据" in bot.sent[0]["text"]

    torrent.status = "stopped"
    now[0] += 599
    await monitor.tick()
    assert bot.edits == []
    now[0] += 1
    await monitor.tick()
    assert len(bot.edits) == 1
    assert len(bot.sent) == 1


async def test_failed_milestone_is_retried_when_card_text_is_identical(env):
    write_config(env, milestones=[25])

    milestone_text = trans("MILESTONE", "zh").format("SSIS-123", 25)

    class FailMilestoneOnceBot(FakeBot):
        failed = False

        async def send_message(self, **kwargs):
            if kwargs["text"] == milestone_text and not self.failed:
                self.failed = True
                raise TelegramError("telegram down")
            return await super().send_message(**kwargs)

    bot = FailMilestoneOnceBot()
    DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, lang_code="zh"))
    monitor = DownloadMonitor(FakeClient(FakeTorrent(percent_done=0.3)), bot)
    await monitor.tick()
    assert bot.failed
    assert DownloadQueue.get(1).milestones_sent == set()
    assert len(bot.sent) == 1

    entry = DownloadQueue.get(1)
    entry.last_edit_ts -= 2000
    DownloadQueue.update(entry)
    await monitor.tick()
    await monitor.tick()
    assert DownloadQueue.get(1).milestones_sent == {25}
    assert len(bot.sent) == 2
    assert bot.edits == []


# ---------------------------------------------------------------- FC2 任务接管


async def test_adopts_unfinished_fc2_torrent_and_notifies_super_user(env):
    torrent = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 sample",
        download_dir="/downloads/未分类",
        percent_done=0.4,
        info_hash="D00C0FFEE",
        added_date=NEW_ADDED,
    )
    client = FakeClient(torrent)
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    entry = DownloadQueue.get(7)
    assert entry is not None
    assert entry.chat_id == 1  # bot.super_user
    assert entry.auto_classify is True
    assert entry.info_hash == "d00c0ffee"
    assert client.moved == ["/downloads/FC2"]
    assert any("FC2-PPV-5000545" in message["text"] for message in bot.sent)


async def test_non_fc2_torrent_is_not_adopted(env):
    torrent = FakeTorrent(id=7, name="SSIS-123", percent_done=0.4)
    client = FakeClient(torrent)
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    assert DownloadQueue.size() == 0
    assert bot.sent == []
    assert client.moved == []


async def test_finished_or_seeding_fc2_torrent_is_not_adopted(env):
    torrents = [
        FakeTorrent(
            id=1,
            name="FC2-PPV-5000545",
            percent_done=1.0,
            status="seeding",
            added_date=NEW_ADDED,
        ),
        FakeTorrent(
            id=2,
            name="FC2-PPV-5000546",
            percent_done=1.0,
            status="downloading",
            added_date=NEW_ADDED,
        ),
        FakeTorrent(
            id=3,
            name="FC2-PPV-5000547",
            percent_done=0.99,
            status="seeding",
            added_date=NEW_ADDED,
        ),
    ]
    client = FakeClient(torrents=torrents)
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    assert DownloadQueue.size() == 0
    assert bot.sent == []
    assert client.moved == []


async def test_blacklisted_fc2_torrent_is_not_adopted(env, tmp_path):
    blacklist_path = tmp_path / "fc2_blacklist.json"
    write_config(env, fc2={"blacklist_file": str(blacklist_path)})
    blacklist = Fc2Blacklist(
        str(blacklist_path), state_path=str(tmp_path / "fc2_state.json")
    )
    blacklist.add("5000545", "FC2-PPV-5000545 sample")

    torrent = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 sample",
        percent_done=0.4,
        added_date=NEW_ADDED,
    )
    client = FakeClient(torrent)
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    assert DownloadQueue.size() == 0
    assert bot.sent == []
    assert client.moved == []


async def test_only_default_monitor_adopts_when_fc2_enabled_elsewhere(env):
    torrent = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 sample",
        download_dir="/downloads/未分类",
        percent_done=0.4,
        info_hash="D00C0FFEE",
        added_date=NEW_ADDED,
    )

    # fc2bot 视角：fc2.enabled=true，构造出的监控永远不接管。
    write_config(env, fc2={"enabled": True})
    fc2_bot = FakeBot()
    fc2_monitor = DownloadMonitor(FakeClient(torrent), fc2_bot)

    # 下载机器人视角：默认配置。
    write_config(env)
    default_bot = FakeBot()
    default_client = FakeClient(torrent)
    default_monitor = DownloadMonitor(default_client, default_bot)
    # 两个实例共享同一个配置文件路径，冻结 fc2bot 实例的配置缓存，
    # 免得 tick 时按 mtime 重载成默认配置。
    fc2_monitor.config._config_mtime = os.path.getmtime(str(env))

    await fc2_monitor.tick()
    await default_monitor.tick()

    assert fc2_bot.sent == []
    assert fc2_bot.edits == []
    assert DownloadQueue.size() == 1
    assert DownloadQueue.get(7).chat_id == 1
    assert default_bot.sent
    assert default_client.moved == ["/downloads/FC2"]


async def test_already_queued_torrent_is_not_adopted_again(env):
    torrent = FakeTorrent(
        id=5,
        name="FC2-PPV-5000545 sample",
        percent_done=0.4,
        info_hash="D00C0FFEE",
        added_date=NEW_ADDED,
    )
    DownloadQueue.add(
        QueueEntry(
            torrent_id=5,
            chat_id=9,
            lang_code="zh",
            info_hash="d00c0ffee",
        )
    )
    bot = FakeBot()

    await DownloadMonitor(FakeClient(torrent), bot).tick()

    assert DownloadQueue.size() == 1
    assert DownloadQueue.get(5).chat_id == 9
    assert len(bot.sent) == 1
    assert all(message["chat_id"] == 9 for message in bot.sent)


async def test_fc2_torrent_added_before_baseline_is_not_adopted(env):
    """部署前就存在的老任务：即使未完成、编号合法，也不接管。"""
    torrent = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 old",
        download_dir="/downloads/未分类",
        percent_done=0.4,
        info_hash="D00C0FFEE",
        added_date=OLD_ADDED,
    )
    client = FakeClient(torrent)
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    assert DownloadQueue.size() == 0
    assert bot.sent == []
    assert client.moved == []


@pytest.mark.parametrize("added_date", [None, "2026-10-28 11:50:00"])
async def test_fc2_torrent_without_usable_added_date_is_not_adopted(env, added_date):
    """添加时间缺失/类型不可用时 fail-safe：宁可不接管。"""
    torrent = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 sample",
        download_dir="/downloads/未分类",
        percent_done=0.4,
        info_hash="D00C0FFEE",
        added_date=added_date,
    )
    client = FakeClient(torrent)
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    assert DownloadQueue.size() == 0
    assert bot.sent == []
    assert client.moved == []


async def test_adopt_baseline_is_created_on_first_tick_and_persists(env, tmp_path):
    """首轮建立基线且不接管老任务；重启读同一文件，老任务仍不接管。"""
    baseline_file = tmp_path / ADOPT_BASELINE_FILE
    baseline_file.unlink()  # 模拟全新部署：还没有基线文件
    now = dt.datetime.now(dt.timezone.utc)
    old = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 old",
        download_dir="/downloads/未分类",
        percent_done=0.4,
        info_hash="OLDHASH",
        added_date=now - dt.timedelta(hours=1),
    )
    old_bot = FakeBot()
    old_client = FakeClient(old)

    await DownloadMonitor(old_client, old_bot).tick()

    # 第一轮：基线以当前时间建立，已有任务全部算老任务。
    assert DownloadQueue.size() == 0
    assert old_bot.sent == []
    assert old_client.moved == []
    assert baseline_file.exists()
    cutoff = json.loads(baseline_file.read_text(encoding="utf-8"))["adopted_since"]
    assert cutoff > 0

    # 模拟监控重启：新实例读同一个状态文件。
    DownloadQueue.clear()
    new = FakeTorrent(
        id=8,
        name="FC2-PPV-5000546 new",
        download_dir="/downloads/未分类",
        percent_done=0.3,
        info_hash="NEWHASH",
        added_date=dt.datetime.fromtimestamp(cutoff + 60, dt.timezone.utc),
    )
    bot = FakeBot()
    client = FakeClient(torrents=[old, new])

    await DownloadMonitor(client, bot).tick()

    assert DownloadQueue.get(7) is None
    assert DownloadQueue.get(8) is not None
    assert client.moved == ["/downloads/FC2"]
    assert any("FC2-PPV-5000546" in message["text"] for message in bot.sent)


async def test_readded_torrent_with_same_info_hash_is_adopted(env):
    """同一内容删了重加：addedDate 是新的，不因旧任务出现过同 hash 被拦。"""
    old = FakeTorrent(
        id=7,
        name="FC2-PPV-5000545 sample",
        download_dir="/downloads/未分类",
        percent_done=1.0,
        status="seeding",
        info_hash="D00C0FFEE",
        added_date=OLD_ADDED,
    )
    new = FakeTorrent(
        id=9,
        name="FC2-PPV-5000545 sample",
        download_dir="/downloads/未分类",
        percent_done=0.2,
        info_hash="D00C0FFEE",
        added_date=NEW_ADDED,
    )
    client = FakeClient(torrents=[old, new])
    bot = FakeBot()

    await DownloadMonitor(client, bot).tick()

    entry = DownloadQueue.get(9)
    assert entry is not None
    assert entry.info_hash == "d00c0ffee"
    assert DownloadQueue.get(7) is None
