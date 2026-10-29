import datetime as dt
from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import lib.bot_handlers as handlers
from lib import auth
from lib.classifier import DEFAULT_RULES
from lib.magnet import extract_magnets, parse_magnet
from models.BotConfigurator import BotConfigurator
from models.DownloadQueue import DownloadQueue, QueueEntry
from models.TransmissionClient import TransmissionClient

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
        source = kwargs["torrent"]
        if isinstance(source, Path):
            torrent.info_hash = TransmissionClient.torrent_file_hash(source)
        else:
            link = parse_magnet(str(source))
            if link:
                torrent.info_hash = link.info_hash
        self.added.append(kwargs)
        return torrent


def write_torrent(
    path: Path, name: bytes = b"video.bin", tracker: bytes = b"https://t.invalid"
):
    def string(value: bytes) -> bytes:
        return str(len(value)).encode() + b":" + value

    info = (
        b"d6:lengthi4e4:name"
        + string(name)
        + b"12:piece lengthi16384e6:pieces20:"
        + b"x" * 20
        + b"e"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"d8:announce"
        + string(tracker)
        + b"7:comment"
        + string(b"4:info is only text here")
        + b"4:info"
        + info
        + b"e"
    )
    return sha1(info).hexdigest()


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


@pytest.mark.parametrize("magnet_first", [False, True])
async def test_batch_add_deduplicates_torrent_paths_and_hashes(env, tmp_path, magnet_first):
    first = tmp_path / "sample.torrent"
    copy = tmp_path / "copy.torrent"
    distinct = tmp_path / "other" / "sample.torrent"
    info_hash = write_torrent(first)
    assert write_torrent(copy, tracker=b"https://another.invalid") == info_hash
    write_torrent(distinct, name=b"different.bin")
    magnet = extract_magnets(f"magnet:?xt=urn:btih:{info_hash.upper()}")[0]
    batch = [first, str(tmp_path / "other" / ".." / first.name), copy, distinct]
    batch.insert(0 if magnet_first else len(batch), magnet)
    context = SimpleNamespace(user_data={"batch": batch}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/未分类"), context)

    assert len(env.client.added) == 2
    sources = [call["torrent"] for call in env.client.added]
    assert sources == ([magnet.raw, distinct] if magnet_first else [first, distinct])
    assert "已添加 2 / 5" in env.bot.edits[-1]["text"]
    assert "跳过 3" in env.bot.edits[-1]["text"]


async def test_batch_add_skips_existing_torrent_file_without_replacing_queue(
    env, tmp_path
):
    path = tmp_path / "existing.torrent"
    info_hash = write_torrent(path)
    env.client.existing = {info_hash.upper(): FakeTorrent(99, "old", "/downloads/FC2")}
    entry = QueueEntry(torrent_id=99, chat_id=8, info_hash=info_hash, message_id=777)
    DownloadQueue.add(entry)
    context = SimpleNamespace(user_data={"batch": [path]}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/未分类"), context)

    assert env.client.added == []
    assert DownloadQueue.get(99) is entry
    assert "跳过 1" in env.bot.edits[-1]["text"]


async def test_batch_lookup_failure_preserves_items_and_existing_queue(
    env, tmp_path, monkeypatch
):
    path = tmp_path / "existing.torrent"
    info_hash = write_torrent(path)
    entry = QueueEntry(torrent_id=99, chat_id=8, info_hash=info_hash, message_id=777)
    DownloadQueue.add(entry)
    client = object.__new__(TransmissionClient)
    attempts = []

    def fail_lookup():
        raise RuntimeError("RPC list unavailable")

    def record_add(**kwargs):
        attempts.append(kwargs)
        raise AssertionError("must not add without a duplicate snapshot")

    monkeypatch.setattr(client, "get_torrents", fail_lookup)
    monkeypatch.setattr(client, "add_torrent", record_add)
    monkeypatch.setattr(handlers, "get_torrent_connection", lambda: client)
    batch = [path]
    context = SimpleNamespace(user_data={"batch": batch}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/未分类"), context)

    assert attempts == []
    assert context.user_data["batch"] is batch
    assert DownloadQueue.get(99) is entry
    assert "RPC list unavailable" in env.bot.sent[-1]["text"]


async def test_batch_bad_torrent_file_does_not_block_other_items(env, tmp_path):
    path = tmp_path / "bad.torrent"
    path.write_bytes(b"d7:comment4:nonee")
    context = SimpleNamespace(
        user_data={"batch": [path, extract_magnets(TEXT)[0]]}, bot=env.bot
    )

    await handlers.confirmDirChoice(make_update("/downloads/未分类"), context)

    assert len(env.client.added) == 1
    assert "已添加 1 / 2" in env.bot.edits[-1]["text"]
    assert "失败 1 个" in env.bot.edits[-1]["text"]


async def test_batch_add_reports_failure(env, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("transmission is down")

    monkeypatch.setattr(env.client, "add_torrent", boom)
    update = make_update("__auto__")
    context = SimpleNamespace(user_data={"batch": extract_magnets(TEXT)}, bot=env.bot)

    await handlers.confirmDirChoice(update, context)

    assert "失败 3 个" in env.bot.edits[-1]["text"]
