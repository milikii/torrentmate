import datetime as dt
import time
from types import SimpleNamespace

import pytest
import yaml

from lib.classifier import DEFAULT_RULES
from models.BotConfigurator import BotConfigurator
from models.DownloadHistory import DownloadHistory
from models.DownloadMonitor import DownloadMonitor
from models.DownloadQueue import DownloadQueue, QueueEntry


class FakeTorrent:
    def __init__(self, **kwargs):
        values = {
            "id": 1,
            "name": "SSIS-123",
            "download_dir": "/downloads/未分类",
            "metadata_percent_complete": 100,
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
    def __init__(self, torrent):
        self.torrent = torrent
        self.moved = []
        self.data_moved = []
        self.stopped = []

    def get_torrent(self, torrent_id):
        return self.torrent

    def change_torrent(self, torrent_id, location=None, **kwargs):
        self.moved.append(location)

    def move_torrent_data(self, torrent_id, location, move=True):
        self.data_moved.append(location)

    def stop_torrent(self, torrent_id):
        self.stopped.append(torrent_id)


def write_config(path, **notify_overrides):
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
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "torrentino.yaml"
    write_config(path)
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


class FailingBot(FakeBot):
    async def send_message(self, **kwargs):
        raise Exception("telegram down")

    async def edit_message_text(self, **kwargs):
        raise Exception("telegram down")


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
