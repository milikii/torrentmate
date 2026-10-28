import datetime as dt
from types import SimpleNamespace

import pytest
import yaml

import lib.bot_handlers as handlers
from lib import auth
from lib.classifier import DEFAULT_RULES
from lib.magnet import extract_magnets
from models.BotConfigurator import BotConfigurator
from models.DownloadQueue import DownloadQueue

TEXT = (
    "一起下\n"
    "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=FC2-PPV-1234567\n"
    "magnet:?xt=urn:btih:89abcdef0123456789abcdef0123456789abcdef&dn=SSIS-123\n"
    "magnet:?xt=urn:btih:abcdef0123456789abcdef0123456789abcdef01&dn=麻豆MD-0123\n"
)


class FakeTorrent:
    def __init__(self, torrent_id: int, name: str, download_dir: str):
        self.id = torrent_id
        self.name = name
        self.download_dir = download_dir
        self.info_hash = f"{torrent_id:040x}"
        self.metadata_percent_complete = 0
        self.percent_done = 0.0
        self.total_size = 0
        self.left_until_done = 0
        self.rate_download = 0
        self.rate_upload = 0
        self.peers_connected = 0
        self.ratio = 0
        self.status = "downloading"
        self.size_when_done = 0
        self.done_date = dt.datetime(2026, 10, 28)
        self.added_date = dt.datetime(2026, 10, 28)

    @property
    def eta(self):
        return None


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
    def __init__(self):
        self.added = []
        self.existing = {}

    def existing_hashes(self):
        return dict(self.existing)

    def add_torrent(self, **kwargs):
        torrent = FakeTorrent(
            len(self.added) + 1,
            kwargs.get("title") or "unnamed",
            kwargs.get("download_dir") or "/downloads/未分类",
        )
        self.added.append(kwargs)
        return torrent


def make_update(callback_data="__auto__"):
    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=9, language_code="zh-hans"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=9), message_id=77, text="preview"
        ),
        answer=_async_noop,
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        message=None,
    )


async def _async_noop(*args, **kwargs):
    return None


@pytest.fixture
def env(tmp_path, monkeypatch):
    path = tmp_path / "torrentino.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "bot": {"token": "token", "super_user": 9, "allowed_users": []},
                "transmission": {},
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
                "notify": {"progress": True},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    original_file = BotConfigurator.config_file
    BotConfigurator.config_file = str(path)
    handlers.bot_config._config = None
    auth.bot_config._config = None
    DownloadQueue.set_state_file(None)
    DownloadQueue.clear()
    client = FakeClient()
    monkeypatch.setattr(handlers, "_transmission_client", client)
    monkeypatch.setattr(handlers, "get_torrent_connection", lambda: client)
    yield SimpleNamespace(client=client, bot=FakeBot())
    BotConfigurator.config_file = original_file
    handlers.bot_config._config = None
    auth.bot_config._config = None
    DownloadQueue.clear()


async def test_batch_add_uses_classified_directories(env):
    update = make_update("__auto__")
    context = SimpleNamespace(user_data={"batch": extract_magnets(TEXT)}, bot=env.bot)

    await handlers.confirmDirChoice(update, context)

    dirs = [call["download_dir"] for call in env.client.added]
    assert dirs == ["/downloads/FC2", "/downloads/有码", "/downloads/国产"]
    assert all(call["auto_classify"] is True for call in env.client.added)
    assert context.user_data.get("batch") is None

    summary = env.bot.edits[-1]["text"]
    assert "已添加 3 / 3" in summary


async def test_batch_add_skips_duplicates(env):
    duplicate_hash = "0123456789abcdef0123456789abcdef01234567"
    env.client.existing = {duplicate_hash: FakeTorrent(1, "old", "/downloads/FC2")}
    update = make_update("/downloads/未分类")
    context = SimpleNamespace(user_data={"batch": extract_magnets(TEXT)}, bot=env.bot)

    await handlers.confirmDirChoice(update, context)

    assert len(env.client.added) == 2
    assert all(call["download_dir"] == "/downloads/未分类" for call in env.client.added)
    assert "跳过 1" in env.bot.edits[-1]["text"]


async def test_batch_add_reports_failure(env, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("transmission is down")

    monkeypatch.setattr(env.client, "add_torrent", boom)
    update = make_update("__auto__")
    context = SimpleNamespace(user_data={"batch": extract_magnets(TEXT)}, bot=env.bot)

    await handlers.confirmDirChoice(update, context)

    assert "失败 3 个" in env.bot.edits[-1]["text"]
