import asyncio
import datetime as dt
import json
import os
from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from transmission_rpc.client import Client

from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut

import lib.bot_handlers as handlers
from lib import auth
from lib.classifier import DEFAULT_RULES
from lib.func import trans
from lib.magnet import extract_magnets, parse_magnet
from models import Fc2Blacklist as fc2bl
from models import Fc2Channel as fc2
from models.BotConfigurator import BotConfigurator
from models.DownloadHistory import DownloadHistory
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


class FakeFile:
    def __init__(self, payload: bytes, downloads: list[Path]):
        self.payload = payload
        self.downloads = downloads

    async def download_to_drive(self, destination):
        path = Path(destination)
        self.downloads.append(path)
        path.write_bytes(self.payload)


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.deleted = []
        self.photos = []
        self.groups = []
        self.payload = b""
        self.downloads: list[Path] = []
        self.keyboard_clears = []
        self.send_attempts = []
        self.send_message_failures: list[Exception] = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def send_message(self, **kwargs):
        self.send_attempts.append(kwargs)
        if self.send_message_failures:
            raise self.send_message_failures.pop(0)
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000 + len(self.sent))

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return SimpleNamespace(message_id=2000 + len(self.photos))

    async def send_media_group(self, **kwargs):
        self.groups.append(kwargs)
        start = 3000 + 10 * len(self.groups)
        return [
            SimpleNamespace(message_id=start + index)
            for index in range(len(kwargs["media"]))
        ]

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)
        return True

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.keyboard_clears.append((chat_id, message_id, reply_markup))
        return True

    async def get_file(self, file_id):
        return FakeFile(self.payload, self.downloads)


class FakeClient:
    def __init__(self):
        self.added = []
        self.existing = {}
        self.torrents = []
        self.removed = []

    def existing_hashes(self):
        return dict(self.existing)

    def get_torrents(self):
        return list(self.torrents)

    def get_torrent(self, torrent_id):
        for torrent in self.torrents:
            if torrent.id == torrent_id:
                return torrent
        raise ValueError(torrent_id)

    def remove_torrent(self, torrent_id, delete_data=False):
        self.removed.append((torrent_id, delete_data))

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


def make_update(callback_data="__auto__", message_id=77, chat_id=9):
    query = SimpleNamespace(
        data=callback_data,
        from_user=SimpleNamespace(id=9, language_code="zh-hans"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=message_id, text="preview"
        ),
        answer=_async_noop,
        edit_message_reply_markup=_async_noop,
    )
    return SimpleNamespace(
        callback_query=query,
        effective_user=query.from_user,
        message=None,
    )


async def _async_noop(*args, **kwargs):
    return None


def make_batch_preview_update(text, message_id, chat_id=9):
    async def reply_text(text, **kwargs):
        return SimpleNamespace(message_id=message_id)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=9, language_code="zh-hans"),
        text=text,
        caption=None,
        reply_text=reply_text,
    )
    return SimpleNamespace(
        message=message, effective_user=message.from_user, callback_query=None
    )


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
                "fc2": {"channel_id": -1001234},
                "trackers": {"rutracker": {"base_url": "https://rutracker.invalid"}},
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    original_file = BotConfigurator.config_file
    original_history_file = DownloadHistory.download_log_file
    BotConfigurator.config_file = str(path)
    DownloadHistory.set_log_file(str(tmp_path / "download.log"))
    # 黑名单默认落在 config 里，测试必须指到 tmp，否则会跑脏仓库目录
    monkeypatch.setattr(
        handlers,
        "blacklist_config_path",
        lambda config: str(tmp_path / "fc2_blacklist.json"),
    )
    monkeypatch.setattr(
        fc2,
        "blacklist_config_path",
        lambda config: str(tmp_path / "fc2_blacklist.json"),
    )
    handlers.bot_config._config = None
    auth.bot_config._config = None
    DownloadQueue.set_state_file(None)
    DownloadQueue.clear()
    client = FakeClient()
    monkeypatch.setattr(handlers, "_transmission_client", client)
    monkeypatch.setattr(handlers, "get_torrent_connection", lambda: client)
    yield SimpleNamespace(client=client, bot=FakeBot())
    BotConfigurator.config_file = original_file
    DownloadHistory.set_log_file(original_history_file)
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


def existing_torrent(torrent_id: int, info_hash: str):
    return SimpleNamespace(id=torrent_id, name="old", info_hash=info_hash)


def install_client(monkeypatch, torrents, add_result=None, add_error=None):
    client = object.__new__(TransmissionClient)
    calls = []

    def get_torrents():
        return torrents() if callable(torrents) else list(torrents)

    def fake_add(self, torrent, **kwargs):
        calls.append({"torrent": torrent, **kwargs})
        if add_error is not None:
            raise add_error
        return add_result

    monkeypatch.setattr(client, "get_torrents", get_torrents)
    monkeypatch.setattr(Client, "add_torrent", fake_add)
    monkeypatch.setattr(handlers, "get_torrent_connection", lambda: client)
    return calls


def torrent_upload_context(bot, file_name="sample.torrent"):
    return SimpleNamespace(
        user_data={
            "torrent": {
                "type": "torrent",
                "file_name": file_name,
                "file_id": "file-id",
            }
        },
        bot=bot,
    )


def torrent_url_context(bot, url, tracker=None, title="sample"):
    data = {"type": "url", "url": url, "title": title}
    if tracker:
        data["tracker"] = tracker
    return SimpleNamespace(user_data={"torrent": data}, bot=bot)


async def test_single_duplicate_keeps_original_entry_and_sends_only_notice(
    env, tmp_path, monkeypatch
):
    source = tmp_path / "source.torrent"
    info_hash = write_torrent(source)
    env.bot.payload = source.read_bytes()
    state = tmp_path / "queue.json"
    DownloadQueue.set_state_file(str(state))
    original = QueueEntry(
        torrent_id=55,
        chat_id=8,
        lang_code="en",
        title="old title",
        requested_dir="/downloads/FC2",
        info_hash=info_hash.upper(),
        auto_classify=True,
        message_id=777,
        progress_card=False,
        milestones_sent={25, 50},
        last_card_text="old card",
        classified=True,
        final_dir="/downloads/FC2",
    )
    DownloadQueue.add(original)
    before_json = original.to_json()
    before_state = state.read_text(encoding="utf-8")
    calls = install_client(
        monkeypatch, [existing_torrent(55, info_hash.upper())]
    )

    await handlers.confirmDirChoice(make_update("__auto__"), torrent_upload_context(env.bot))

    assert calls == []
    assert DownloadQueue.get(55) is original
    assert original.to_json() == before_json
    assert state.read_text(encoding="utf-8") == before_state
    assert [message["text"] for message in env.bot.sent] == [
        trans("SINGLE_ADD_DUPLICATE", "zh-hans")
    ]
    assert not env.bot.downloads[-1].exists()

    DownloadQueue._items.clear()
    DownloadQueue.load()
    restored = DownloadQueue.get(55)
    assert restored.to_json() == before_json


async def test_single_skips_same_hash_from_upload_search_and_magnet(
    env, tmp_path, monkeypatch
):
    source = tmp_path / "source.torrent"
    info_hash = write_torrent(source)
    env.bot.payload = source.read_bytes()
    DownloadQueue.add(
        QueueEntry(torrent_id=1, chat_id=8, info_hash=info_hash.upper())
    )
    calls = install_client(monkeypatch, [])

    copy = tmp_path / "renamed.torrent"
    assert write_torrent(copy, tracker=b"https://another.invalid") == info_hash
    env.bot.payload = copy.read_bytes()
    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_upload_context(env.bot, "renamed.torrent"),
    )

    downloaded = tmp_path / "search.torrent"
    write_torrent(downloaded, tracker=b"https://third.invalid")
    monkeypatch.setattr(
        handlers,
        "get_search",
        lambda: SimpleNamespace(download=lambda url, tracker: str(downloaded)),
    )
    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_url_context(env.bot, "https://tracker.invalid/dl/1", "rutracker"),
    )
    assert not downloaded.exists()

    magnet = f"magnet:?xt=urn:btih:{info_hash.upper()}&dn=renamed"
    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_url_context(env.bot, magnet),
    )

    assert calls == []
    assert DownloadQueue.size() == 1
    assert len(env.bot.sent) == 3
    assert all(
        message["text"] == trans("SINGLE_ADD_DUPLICATE", "zh-hans")
        for message in env.bot.sent
    )


async def test_single_same_name_different_hash_is_added(env, tmp_path, monkeypatch):
    source = tmp_path / "source.torrent"
    info_hash = write_torrent(source)
    env.bot.payload = source.read_bytes()
    old_hash = "f" * 40
    original = QueueEntry(
        torrent_id=1,
        chat_id=8,
        info_hash=old_hash,
        message_id=777,
        title="video.bin",
    )
    DownloadQueue.add(original)
    before_json = original.to_json()
    snapshot = [SimpleNamespace(id=1, name="video.bin", info_hash=old_hash)]
    added = FakeTorrent(3, "video.bin", "/downloads/未分类")
    added.info_hash = info_hash.upper()
    calls = install_client(monkeypatch, snapshot, add_result=added)

    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_upload_context(env.bot),
    )

    assert len(calls) == 1
    assert calls[0]["download_dir"] == "/downloads/未分类"
    assert DownloadQueue.get(1) is original
    assert original.to_json() == before_json
    assert original.message_id == 777
    entry = DownloadQueue.get(3)
    assert entry is not None
    assert entry.info_hash == info_hash
    assert entry.message_id == 1001
    card_text = env.bot.sent[0]["text"]
    assert "video.bin" in card_text
    assert all(
        message["text"] != trans("AUTO_CLASSIFY_ON", "zh-hans")
        for message in env.bot.sent
    )
    assert not env.bot.downloads[-1].exists()


async def test_single_transmission_existing_without_queue_entry_is_skipped(
    env, tmp_path, monkeypatch
):
    source = tmp_path / "source.torrent"
    info_hash = write_torrent(source)
    env.bot.payload = source.read_bytes()
    url_hash = "abcabcabcabcabcabcabcabcabcabcabcabcabca"
    snapshot = [existing_torrent(1, info_hash), existing_torrent(2, url_hash)]
    url_torrent = SimpleNamespace(id=2, name="url", info_hash=url_hash)
    calls = install_client(monkeypatch, snapshot, add_result=url_torrent)

    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"), torrent_upload_context(env.bot)
    )
    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_url_context(env.bot, "https://tracker.invalid/dl/2"),
    )

    assert len(calls) == 1
    assert DownloadQueue.size() == 0
    assert [message["text"] for message in env.bot.sent] == [
        trans("SINGLE_ADD_DUPLICATE", "zh-hans"),
        trans("SINGLE_ADD_DUPLICATE", "zh-hans"),
    ]


async def test_single_rpc_result_matching_queue_entry_is_preserved(
    env, monkeypatch
):
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    original = QueueEntry(
        torrent_id=9,
        chat_id=8,
        lang_code="en",
        title="queued",
        info_hash=info_hash,
        message_id=777,
        progress_card=False,
        milestones_sent={25},
        last_card_text="old card",
        auto_classify=True,
        classified=True,
    )
    DownloadQueue.add(original)
    before_json = original.to_json()
    result = SimpleNamespace(id=9, name="again", info_hash=info_hash.upper())
    calls = install_client(monkeypatch, [], add_result=result)

    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_url_context(env.bot, "https://tracker.invalid/dl/9"),
    )

    assert len(calls) == 1
    assert DownloadQueue.get(9) is original
    assert original.to_json() == before_json
    assert [message["text"] for message in env.bot.sent] == [
        trans("SINGLE_ADD_DUPLICATE", "zh-hans")
    ]


async def test_single_lookup_failure_aborts_without_adding(env, tmp_path, monkeypatch):
    state = tmp_path / "queue.json"
    DownloadQueue.set_state_file(str(state))
    original = QueueEntry(torrent_id=9, chat_id=8, message_id=777)
    DownloadQueue.add(original)
    before_state = state.read_text(encoding="utf-8")

    def fail_lookup():
        raise RuntimeError("RPC list unavailable")

    calls = install_client(monkeypatch, fail_lookup)

    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"),
        torrent_url_context(env.bot, "https://tracker.invalid/dl/9"),
    )

    assert calls == []
    assert DownloadQueue.get(9) is original
    assert state.read_text(encoding="utf-8") == before_state
    assert len(env.bot.sent) == 1
    assert "RPC list unavailable" in env.bot.sent[0]["text"]
    assert env.bot.sent[0]["text"] != trans("SINGLE_ADD_DUPLICATE", "zh-hans")


async def test_single_new_add_cleans_temp_and_announces_auto_classify(
    env, tmp_path, monkeypatch
):
    source = tmp_path / "source.torrent"
    info_hash = write_torrent(source)
    env.bot.payload = source.read_bytes()
    added = FakeTorrent(4, "video.bin", "/downloads/未分类")
    added.info_hash = info_hash
    calls = install_client(monkeypatch, [], add_result=added)

    await handlers.confirmDirChoice(make_update("__auto__"), torrent_upload_context(env.bot))

    assert len(calls) == 1
    assert DownloadQueue.get(4) is not None
    assert [message["text"] for message in env.bot.sent][-1] == trans(
        "AUTO_CLASSIFY_ON", "zh-hans"
    )
    assert not env.bot.downloads[-1].exists()


async def test_single_add_error_cleans_temp_without_queue_entry(
    env, tmp_path, monkeypatch
):
    source = tmp_path / "source.torrent"
    write_torrent(source)
    env.bot.payload = source.read_bytes()
    install_client(
        monkeypatch, [], add_error=RuntimeError("transmission is down")
    )

    await handlers.confirmDirChoice(
        make_update("/downloads/未分类"), torrent_upload_context(env.bot)
    )

    assert DownloadQueue.size() == 0
    assert not env.bot.downloads[-1].exists()
    assert "transmission is down" in env.bot.sent[0]["text"]


# ---------------------------------------------------------------- FC2 频道


FC2_HASH = "d00c0ffee" * 4


def install_fc2_store(monkeypatch, tmp_path):
    store = handlers.Fc2Store(str(tmp_path / "fc2_state.json"))
    monkeypatch.setattr(handlers, "Fc2Store", lambda: store)
    return store


def make_fc2_callback(data, user_id=9, reply_markup=None):
    answers = []

    async def answer(text=None, *args, **kwargs):
        answers.append(text)

    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, language_code="zh-hans"),
        message=SimpleNamespace(
            chat=SimpleNamespace(id=user_id),
            message_id=77,
            reply_markup=reply_markup,
        ),
        answer=answer,
        edit_message_reply_markup=_async_noop,
    )
    return (
        SimpleNamespace(
            callback_query=query, effective_user=query.from_user, message=None
        ),
        answers,
    )


def make_command_update(args):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=9),
        from_user=SimpleNamespace(id=9, language_code="zh-hans"),
    )
    return SimpleNamespace(
        message=message, effective_user=message.from_user, callback_query=None
    ), SimpleNamespace(args=args, bot=None, user_data={})


async def test_fc2_download_adds_magnet_to_private_chat(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    update, answers = make_fc2_callback("fc2dl:5000545")
    context = SimpleNamespace(user_data={}, bot=env.bot)

    await handlers.fc2_download(update, context)

    assert len(env.client.added) == 1
    call = env.client.added[0]
    assert call["single_dedup"] is True
    assert call["chat_id"] == 9
    assert call["torrent"].startswith(f"magnet:?xt=urn:btih:{FC2_HASH}")
    assert call["title"] == "FC2-PPV-5000545 sample"
    assert env.bot.edits == []
    assert env.bot.sent == []
    assert answers == ["已开始下载"]


async def test_fc2_download_does_not_write_fc2bot_queue(env, tmp_path, monkeypatch):
    """fc2bot 只负责加任务：不写自己的通知队列，也不发任何下载通知。"""
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    update, answers = make_fc2_callback("fc2dl:5000545")

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["已开始下载"]
    assert len(env.client.added) == 1
    assert env.client.added[0]["notify"] is False
    assert DownloadQueue.size() == 0
    assert env.bot.sent == []
    assert env.bot.edits == []


async def test_fc2_download_reports_duplicate_without_card(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )

    def duplicate(**kwargs):
        raise handlers.TorrentAlreadyExists(FC2_HASH)

    monkeypatch.setattr(env.client, "add_torrent", duplicate)
    update, answers = make_fc2_callback("fc2dl:5000545")

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["已经下过了"]
    assert env.bot.sent == []
    assert env.bot.edits == []


def fc2_card_markup(number="5000545"):
    """和推送器实际发出去的一样：下载 / 不感兴趣 / 官网 三颗按钮。"""
    return fc2._keyboard(number)


def assert_single_download_button_marked(env, original):
    assert len(env.bot.keyboard_clears) == 1
    chat_id, message_id, markup = env.bot.keyboard_clears[0]
    assert (chat_id, message_id) == (9, 77)
    rows = markup.inline_keyboard
    original_rows = original.inline_keyboard
    assert len(rows) == len(original_rows) == 2
    assert [len(row) for row in rows] == [len(row) for row in original_rows]
    download, mute = rows[0]
    assert download.text == "✅已添加任务"
    assert download.callback_data == "fc2dl:5000545"
    # 不感兴趣、官网按钮原样保留，布局不变
    assert mute.text == original_rows[0][1].text
    assert mute.callback_data == original_rows[0][1].callback_data
    assert rows[1][0].text == original_rows[1][0].text
    assert rows[1][0].url == original_rows[1][0].url


async def test_fc2_download_marks_card_button_after_add_success(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    keyboard = fc2_card_markup()
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=keyboard)
    context = SimpleNamespace(user_data={}, bot=env.bot)

    await handlers.fc2_download(update, context)

    assert len(env.client.added) == 1
    assert answers == ["已开始下载"]
    assert env.bot.sent == []
    assert_single_download_button_marked(env, keyboard)


async def test_fc2_download_marks_card_button_on_duplicate(env, tmp_path, monkeypatch):
    """重复点击不重复建任务，也把按钮补成「已添加」对齐真实状态。"""
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    calls = []

    def duplicate(**kwargs):
        calls.append(kwargs)
        raise handlers.TorrentAlreadyExists(FC2_HASH)

    monkeypatch.setattr(env.client, "add_torrent", duplicate)
    keyboard = fc2_card_markup()
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=keyboard)

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert len(calls) == 1
    assert env.client.added == []
    assert answers == ["已经下过了"]
    assert env.bot.sent == []
    assert_single_download_button_marked(env, keyboard)


async def test_fc2_download_already_marked_button_is_left_alone(
    env, tmp_path, monkeypatch
):
    """按钮已经是 ✅ 时重复点击不再发编辑请求（避免 not modified 噪声）。"""
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )

    def duplicate(**kwargs):
        raise handlers.TorrentAlreadyExists(FC2_HASH)

    monkeypatch.setattr(env.client, "add_torrent", duplicate)
    keyboard = handlers._fc2_downloaded_markup(fc2_card_markup(), "5000545")
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=keyboard)

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["已经下过了"]
    assert env.bot.keyboard_clears == []
    assert env.client.added == []


async def test_fc2_download_blacklisted_leaves_card_button(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    blacklist.add("5000545", "FC2-PPV-5000545 sample", fc2bl.SOURCE_MUTE)
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=fc2_card_markup())

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert env.client.added == []
    assert answers == ["这个你拉黑过，已跳过"]
    assert env.bot.keyboard_clears == []


async def test_fc2_download_expired_leaves_card_button(env, tmp_path, monkeypatch):
    install_fc2_store(monkeypatch, tmp_path)
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=fc2_card_markup())

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert env.client.added == []
    assert answers == ["这条已过期"]
    assert env.bot.keyboard_clears == []


async def test_fc2_download_add_failure_leaves_card_button(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )

    def boom(**kwargs):
        raise RuntimeError("transmission is down")

    monkeypatch.setattr(env.client, "add_torrent", boom)
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=fc2_card_markup())

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["添加失败"]
    assert env.bot.keyboard_clears == []
    assert env.bot.sent == []


async def test_fc2_download_keyboard_edit_failure_keeps_success(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )

    async def broken(chat_id, message_id, reply_markup=None):
        raise BadRequest("Message to edit not found")

    monkeypatch.setattr(env.bot, "edit_message_reply_markup", broken)
    update, answers = make_fc2_callback("fc2dl:5000545", reply_markup=fc2_card_markup())

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert len(env.client.added) == 1
    assert answers == ["已开始下载"]
    assert env.bot.sent == []


async def test_fc2_download_missing_callback_message_keeps_success(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    update, answers = make_fc2_callback("fc2dl:5000545")
    update.callback_query.message = None

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert len(env.client.added) == 1
    assert answers == ["已开始下载"]
    assert env.bot.keyboard_clears == []
    assert env.bot.sent == []


async def test_fc2_mute_deletes_all_cards_and_persists(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[123, 124, 125],
    )
    update, answers = make_fc2_callback("fc2no:5000545")

    await handlers.fc2_mute(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert env.bot.deleted == [
        (-1001234, 123),
        (-1001234, 124),
        (-1001234, 125),
    ]
    assert "5000545" in store.muted
    fresh = type(store)(str(store.path))
    assert "5000545" in fresh.muted
    assert answers == ["已删除，以后不再推送"]


@pytest.mark.parametrize("data", ["fc2dl:5000545", "fc2no:5000545"])
async def test_fc2_callbacks_ignore_other_users(
    env, tmp_path, monkeypatch, data
):
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[123],
    )
    update, answers = make_fc2_callback(data, user_id=42)
    handler = handlers.fc2_download if data.startswith("fc2dl") else handlers.fc2_mute

    await handler(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == [None]
    assert env.client.added == []
    assert env.bot.deleted == []
    assert env.bot.sent == []
    assert env.bot.edits == []
    assert store.muted == set()


# ---------------------------------------------------------------- BLOCK 2 窗口期

FC2_ENTRY = {
    "title": "FC2-PPV-5000545 x",
    "guid": "https://sukebei.nyaa.si/view/5000545",
    "hash": FC2_HASH,
    "pub_ts": 11,
    "size": "1 GiB",
}


class Fc2PusherConfig:
    """推送器只需 fc2.proxy / fc2.verify_tls，取不到就走默认值。"""

    def get(self, path, default=None):
        return default


def install_fc2_media(monkeypatch):
    monkeypatch.setattr(
        fc2,
        "_preview_images",
        lambda *a, **k: ["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"],
    )
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: [b"1", b"2", b"3"])
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_fetch_official_detail", lambda *a, **k: ([], ""))
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")


def gated_album(env, monkeypatch):
    """附图上传卡在闸门上，模拟「封面已发、附图还在传」的窗口期。"""
    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_group(**kwargs):
        started.set()
        await gate.wait()
        return [
            SimpleNamespace(message_id=900 + index)
            for index in range(len(kwargs["media"]))
        ]

    monkeypatch.setattr(env.bot, "send_media_group", slow_group)
    return gate, started


async def test_fc2_download_works_while_preview_upload_in_flight(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    install_fc2_media(monkeypatch)
    gate, started = gated_album(env, monkeypatch)
    pusher = fc2.Fc2Pusher(env.bot, Fc2PusherConfig(), store)

    task = asyncio.create_task(pusher._push_entry(dict(FC2_ENTRY), -1001234))
    await asyncio.wait_for(started.wait(), timeout=2)

    # 窗口期：卡片已经落盘，按钮已出现
    assert store.entry("5000545")["messages"] == [2001]

    update, answers = make_fc2_callback("fc2dl:5000545")
    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["已开始下载"]
    assert len(env.client.added) == 1
    assert env.client.added[0]["torrent"].startswith(f"magnet:?xt=urn:btih:{FC2_HASH}")

    gate.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(store.entry("5000545")["messages"]) == 3


async def test_fc2_mute_via_real_callback_store_during_window(
    env, tmp_path, monkeypatch
):
    # sol 复现的真实路径：推送器持有一个 Fc2Store，回调 _fc2_store() 自己新建
    # 另一个，两者不是同一个对象、各有一份内存。这里刻意不 monkeypatch
    # Fc2Store，用真实路径跑「附图上传中 -> 真实回调 mute -> 放行附图」。
    monkeypatch.setattr(fc2, "CONFIG_FILE", str(tmp_path / "torrentino.yaml"))
    install_fc2_media(monkeypatch)
    gate, started = gated_album(env, monkeypatch)
    pusher_store = fc2.Fc2Store()
    pusher = fc2.Fc2Pusher(env.bot, Fc2PusherConfig(), pusher_store)

    task = asyncio.create_task(pusher._push_entry(dict(FC2_ENTRY), -1001234))
    await asyncio.wait_for(started.wait(), timeout=2)

    # 回调真实新建的 store：和推送器手里那个不是同一个实例
    callback_store = handlers._fc2_store()
    assert callback_store is not pusher_store
    assert callback_store.path == pusher_store.path
    assert callback_store.entry("5000545")["messages"] == [2001]

    update, answers = make_fc2_callback("fc2no:5000545")
    await handlers.fc2_mute(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["已删除，以后不再推送"]
    assert env.bot.deleted == [(-1001234, 2001)]

    gate.set()
    await asyncio.wait_for(task, timeout=2)

    # 附图传完后：磁盘 muted 不被旧内存覆盖，不记正常推送，封面与附图都被删
    fresh = fc2.Fc2Store()
    assert "5000545" in fresh.muted
    assert fresh.entry("5000545") is None
    for message_id in (2001, 900, 901):
        assert (-1001234, message_id) in env.bot.deleted


async def test_fc2_history_commands_only_list_fc2(env):
    records = [
        ("2026-10-28 10:00:00", "FC2-PPV-5000545 sample", "/downloads/FC2"),
        ("2026-10-29 10:00:00", "SSIS-123 movie", "/downloads/有码"),
        ("2026-10-27 10:00:00", "fc2ppv 1234567 other", "/downloads/FC2"),
    ]
    for date_done, name, directory in records:
        DownloadHistory.add(date_done, name, directory, 100)

    update, context = make_command_update([])
    context.bot = env.bot
    await handlers.fc2list_command(update, context)

    text = env.bot.sent[-1]["text"]
    assert "FC2-PPV-5000545" in text
    assert "FC2-PPV-1234567" in text
    assert "SSIS-123" not in text

    update, context = make_command_update(["5000545"])
    context.bot = env.bot
    await handlers.fc2info_command(update, context)

    text = env.bot.sent[-1]["text"]
    assert "FC2-PPV-5000545" in text
    assert "FC2-PPV-1234567" not in text

    update, context = make_command_update(["9999999"])
    context.bot = env.bot
    await handlers.fc2info_command(update, context)

    assert env.bot.sent[-1]["text"] == "没有这个编号的下载记录"


# ---------------------------------------------------------------- FC2 黑名单


def install_fc2_blacklist(monkeypatch, tmp_path):
    path = str(tmp_path / "fc2_blacklist.json")
    state = str(tmp_path / "fc2_state.json")

    def factory(blacklist_path=None, state_path=None):
        return fc2bl.Fc2Blacklist(path, state)

    monkeypatch.setattr(handlers, "Fc2Blacklist", factory)
    return fc2bl.Fc2Blacklist(path, state)


def make_delete_update(text="/delete_1", user_id=9):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=user_id),
        from_user=SimpleNamespace(id=user_id, language_code="zh-hans"),
        text=text,
    )
    return SimpleNamespace(
        message=message, effective_user=message.from_user, callback_query=None
    )


def make_rm_callback(data, user_id=9):
    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, language_code="zh-hans"),
        message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=77),
        answer=_async_noop,
        edit_message_text=_async_noop,
    )
    return SimpleNamespace(
        callback_query=query, effective_user=query.from_user, message=None
    )


async def test_fc2_download_skips_blacklisted_number(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[555],
    )
    blacklist.add("5000545", "FC2-PPV-5000545 sample", fc2bl.SOURCE_MUTE)
    update, answers = make_fc2_callback("fc2dl:5000545")

    await handlers.fc2_download(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert env.client.added == []
    assert answers == ["这个你拉黑过，已跳过"]


async def test_single_add_skips_blacklisted_fc2(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "FC2-PPV-5000545 sample", fc2bl.SOURCE_MUTE)
    context = torrent_url_context(
        env.bot,
        f"magnet:?xt=urn:btih:{FC2_HASH}&dn=FC2-PPV-5000545",
        title="FC2-PPV-5000545 sample",
    )

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert env.client.added == []
    assert "黑名单" in env.bot.sent[-1]["text"]


async def test_single_add_allows_fc2_not_in_blacklist(env, tmp_path, monkeypatch):
    install_fc2_blacklist(monkeypatch, tmp_path)
    context = torrent_url_context(
        env.bot,
        f"magnet:?xt=urn:btih:{FC2_HASH}&dn=FC2-PPV-5000546",
        title="FC2-PPV-5000546 sample",
    )

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert len(env.client.added) == 1


async def test_batch_add_skips_only_blacklisted_fc2(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "blocked", fc2bl.SOURCE_MUTE)
    links = extract_magnets(
        "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=FC2-PPV-5000545\n"
        "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=FC2-PPV-5000546\n"
    )
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert [call["title"] for call in env.client.added] == ["FC2-PPV-5000546"]
    assert "黑名单命中 1 条" in env.bot.edits[-1]["text"]


async def test_batch_blacklist_check_failure_preserves_items_for_retry(
    env, monkeypatch
):
    def boom(title):
        raise RuntimeError("blacklist unavailable")

    monkeypatch.setattr(handlers, "_fc2_blocked_number", boom)
    links = extract_magnets(TEXT)[:2]
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert env.client.added == []
    assert context.user_data["batch"] is links
    assert len(context.user_data["batch"]) == 2
    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "黑名单检查失败" in text
    assert "本次批量未完成" in text
    assert "可重试原按钮" in text
    assert "添加中" not in text


async def test_fc2_mute_reports_blacklist_write_failure(
    env, tmp_path, monkeypatch
):
    # 共享黑名单写不进去（比如目录只剩 .lock 可写）：只落了 state.muted，
    # 回调必须如实说记录失败，不能谎报「以后不再推送」。
    store = install_fc2_store(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[123],
    )
    path = tmp_path / "fc2_blacklist.json"
    (tmp_path / f"fc2_blacklist.json.tmp.{os.getpid()}").mkdir()
    blacklist = fc2bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))
    monkeypatch.setattr(handlers, "Fc2Blacklist", lambda *a, **k: blacklist)
    update, answers = make_fc2_callback("fc2no:5000545")

    await handlers.fc2_mute(update, SimpleNamespace(user_data={}, bot=env.bot))

    assert answers == ["已删除，但记进黑名单失败，重启后可能还会推送"]
    assert "5000545" in store.muted
    assert blacklist.contains("5000545") is False


async def test_fc2_mute_records_title_and_source(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
        messages=[123],
    )
    update, _answers = make_fc2_callback("fc2no:5000545")

    await handlers.fc2_mute(update, SimpleNamespace(user_data={}, bot=env.bot))

    record = blacklist.get("5000545")
    assert record["title"] == "FC2-PPV-5000545 sample"
    assert record["source"] == fc2bl.SOURCE_MUTE
    assert record["added_ts"] > 0


async def test_manual_torrent_delete_records_blacklist(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    env.client.torrents = [FakeTorrent(31, "FC2-PPV-5000545 sample", "/downloads/FC2")]
    update = make_delete_update("/delete_31")

    await handlers.torrentDelete(
        update, SimpleNamespace(user_data={}, bot=env.bot)
    )

    assert env.client.removed == [(31, None)]
    record = blacklist.get("5000545")
    assert record["title"] == "FC2-PPV-5000545 sample"
    assert record["source"] == fc2bl.SOURCE_DELETE


async def test_completion_button_delete_records_blacklist(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    env.client.torrents = [FakeTorrent(32, "FC2-PPV-5000550 other", "/downloads/FC2")]

    await handlers.callbackDeleteTorrent(
        make_rm_callback("rm:data:32"), SimpleNamespace(user_data={}, bot=env.bot)
    )

    assert env.client.removed == [(32, True)]
    assert blacklist.get("5000550")["source"] == fc2bl.SOURCE_DELETE


async def test_fc2check_reports_but_never_deletes(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "FC2-PPV-5000545 sample", fc2bl.SOURCE_MUTE)
    env.client.torrents = [
        FakeTorrent(11, "FC2-PPV-5000545 sample", "/downloads/FC2"),
        FakeTorrent(12, "FC2-PPV-4999999 dup", "/downloads/FC2"),
        FakeTorrent(13, "FC2-PPV-4999999 dup", "/downloads/FC2"),
        FakeTorrent(14, "SSIS-123 normal", "/downloads/有码"),
    ]
    update, context = make_command_update([])
    context.bot = env.bot

    await handlers.fc2check_command(update, context)

    text = env.bot.sent[-1]["text"]
    assert "黑名单命中该删：1 个任务" in text
    assert "id=11" in text
    assert "同编号多任务：1 组" in text
    assert "FC2-PPV-4999999" in text
    assert "SSIS-123" not in text
    assert "/fc2del 5000545" in text
    assert "未挂载的下载目录" in text
    assert env.client.removed == []


async def test_fc2check_reports_disk_leftovers_when_mounted(
    env, tmp_path, monkeypatch
):
    install_fc2_blacklist(monkeypatch, tmp_path)
    leftover = tmp_path / "downloads" / "FC2" / "FC2-PPV-5000545 leftover"
    leftover.mkdir(parents=True)
    covered = tmp_path / "downloads" / "FC2" / "FC2-PPV-5000546 has-task"
    covered.mkdir()
    env.client.torrents = [FakeTorrent(15, "FC2-PPV-5000546 has-task", "/downloads/FC2")]
    original_get = handlers.bot_config.get
    monkeypatch.setattr(
        handlers.bot_config,
        "get",
        lambda path, default=None: (
            {"FC2": str(tmp_path / "downloads" / "FC2")}
            if path == "directories"
            else original_get(path, default)
        ),
    )
    update, context = make_command_update([])
    context.bot = env.bot

    await handlers.fc2check_command(update, context)

    text = env.bot.sent[-1]["text"]
    assert "磁盘有文件、tr-bt 没任务：1 条" in text
    assert "FC2-PPV-5000545 leftover" in text
    assert "FC2-PPV-5000546 has-task" not in text


async def test_fc2del_without_numbers_deletes_nothing(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    env.client.torrents = [FakeTorrent(11, "FC2-PPV-5000545 x", "/downloads/FC2")]
    update, context = make_command_update([])
    context.bot = env.bot

    await handlers.fc2del_command(update, context)

    assert env.client.removed == []
    assert "用法" in env.bot.sent[-1]["text"]


async def test_fc2del_refuses_number_not_in_blacklist(env, tmp_path, monkeypatch):
    install_fc2_blacklist(monkeypatch, tmp_path)
    env.client.torrents = [FakeTorrent(11, "FC2-PPV-5000545 x", "/downloads/FC2")]
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2del_command(update, context)

    assert env.client.removed == []
    assert "不在黑名单" in env.bot.sent[-1]["text"]


async def test_fc2del_deletes_blacklisted_task_and_keeps_data(
    env, tmp_path, monkeypatch
):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    env.client.torrents = [
        FakeTorrent(11, "FC2-PPV-5000545 x", "/downloads/FC2"),
        FakeTorrent(12, "FC2-PPV-5000550 other", "/downloads/FC2"),
    ]
    update, context = make_command_update(["FC2-PPV-5000545"])
    context.bot = env.bot

    await handlers.fc2del_command(update, context)

    assert env.client.removed == [(11, False)]
    text = env.bot.sent[-1]["text"]
    assert "已删除 1 个" in text
    assert "本地文件保留" in text
    assert blacklist.contains("5000545")
    assert DownloadQueue.get(11) is None


async def test_fc2del_only_touches_real_fc2_names(env, tmp_path, monkeypatch):
    # 黑名单有 5000545 时，普通文件名里碰巧出现的 "fc2...5000545" 不能被当成该编号删掉。
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "FC2-PPV-5000545 x", fc2bl.SOURCE_MUTE)
    assert handlers._fc2_blocked_number("Ubuntu.afc25000545b.iso") is None
    assert handlers._fc2_blocked_number("Ubuntu.fc25000545b.iso") is None
    assert handlers._fc2_blocked_number("myfc25000545backup.iso") is None
    assert handlers._fc2_blocked_number("FC2-PPV-5000545 x") == "5000545"
    assert handlers._fc2_blocked_number("FC2-PPV（5000545） fullwidth") == "5000545"
    env.client.torrents = [
        FakeTorrent(11, "FC2-PPV-5000545 x", "/downloads/FC2"),
        FakeTorrent(12, "Ubuntu.fc25000545b.iso", "/downloads/有码"),
        FakeTorrent(13, "FC2-PPV（5000545） fullwidth", "/downloads/FC2"),
    ]
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2del_command(update, context)

    assert env.client.removed == [(11, False), (13, False)]


async def test_fc2del_refuses_other_users(env, tmp_path, monkeypatch):
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    env.client.torrents = [FakeTorrent(11, "FC2-PPV-5000545 x", "/downloads/FC2")]
    update, context = make_command_update(["5000545"])
    update.message.chat.id = 42
    update.message.from_user.id = 42
    update.effective_user = update.message.from_user
    context.bot = env.bot

    await handlers.fc2del_command(update, context)

    assert env.client.removed == []
    assert "权限" in env.bot.sent[-1]["text"]


async def test_fc2block_adds_manual_entry_and_lists(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
    )
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2block_command(update, context)

    record = blacklist.get("5000545")
    assert record["title"] == "FC2-PPV-5000545 sample"
    assert record["source"] == fc2bl.SOURCE_MANUAL

    update, context = make_command_update([])
    context.bot = env.bot
    await handlers.fc2block_command(update, context)

    text = env.bot.sent[-1]["text"]
    assert "FC2 黑名单" in text
    assert "FC2-PPV-5000545" in text


async def test_fc2block_reports_blacklist_write_failure(env, tmp_path, monkeypatch):
    install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    # 占住原子写入的临时路径，避免 root 跑测试时 chmod 仍然可写。
    (tmp_path / f"fc2_blacklist.json.tmp.{os.getpid()}").mkdir()
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2block_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "记录失败" in text
    assert "写盘失败" in text
    assert "已拉黑" not in text
    assert blacklist.contains("5000545") is False


async def test_fc2block_reports_blacklist_add_exception(env, tmp_path, monkeypatch):
    install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("blacklist unavailable")

    monkeypatch.setattr(blacklist, "add", boom)
    monkeypatch.setattr(handlers, "_fc2_blacklist", lambda: blacklist)
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2block_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "记录失败" in text
    assert "已拉黑" not in text
    assert blacklist.contains("5000545") is False


async def test_fc2unblock_reports_blacklist_write_failure_and_keeps_muted(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    (tmp_path / f"fc2_blacklist.json.tmp.{os.getpid()}").mkdir()
    store.mute("5000545")
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2unblock_command(update, context)

    assert blacklist.contains("5000545") is True
    assert "5000545" in store.muted
    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "没生效" in text
    assert "写盘失败" in text
    assert "已取消拉黑" not in text


async def test_fc2unblock_reports_mixed_results_in_one_message(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    blacklist.add("5000546", "y", fc2bl.SOURCE_MUTE)
    for number in ("5000545", "5000546", "5000547"):
        store.mute(number)
    original_remove = blacklist.remove

    def remove_with_failure(number):
        if number == "5000546":
            return False
        return original_remove(number)

    # 固定同一实例，避免旧 muted 迁移把「不在名单」这一项重新拉黑。
    monkeypatch.setattr(blacklist, "remove", remove_with_failure)
    monkeypatch.setattr(handlers, "_fc2_blacklist", lambda: blacklist)
    update, context = make_command_update(["5000545", "5000546", "5000547"])
    context.bot = env.bot

    await handlers.fc2unblock_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "已取消拉黑：FC2-PPV-5000545" in text
    assert "取消拉黑没生效，FC2-PPV-5000546 仍在黑名单里（写盘失败）" in text
    assert "这些编号不在黑名单里：FC2-PPV-5000547" in text
    assert blacklist.contains("5000545") is False
    assert blacklist.contains("5000546") is True
    assert blacklist.contains("5000547") is False
    assert store.muted == {"5000546", "5000547"}


async def test_fc2unblock_clears_blacklist_and_muted(env, tmp_path, monkeypatch):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    store.mute("5000545")
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2unblock_command(update, context)

    assert not blacklist.contains("5000545")
    assert store.muted == set()
    assert "已取消拉黑" in env.bot.sent[-1]["text"]


async def test_fc2unblock_in_other_container_releases_resident_pusher(
    env, tmp_path, monkeypatch
):
    # 两份独立 state + 同一共享黑名单：fc2bot 点过 fc2no，torrentino 解黑后
    # fc2bot 常驻推送器不能再拿自己的 state.muted 把编号挡住。
    shared = str(tmp_path / "fc2_blacklist.json")
    fc2_state = str(tmp_path / "fc2bot_state.json")
    tor_state = str(tmp_path / "torrentino_state.json")

    fc2bot_store = fc2.Fc2Store(fc2_state)
    fc2bot_store.mute("5000545")
    fc2bot_blacklist = fc2bl.Fc2Blacklist(shared, fc2_state)
    fc2bot_blacklist.add("5000545", "FC2-PPV-5000545 x", fc2bl.SOURCE_MUTE)

    tor_store = fc2.Fc2Store(tor_state)
    tor_blacklist = fc2bl.Fc2Blacklist(shared, tor_state)
    monkeypatch.setattr(
        handlers, "_fc2_blacklist", lambda: fc2bl.Fc2Blacklist(shared, tor_state)
    )
    monkeypatch.setattr(handlers, "_fc2_store", lambda: tor_store)

    pusher = fc2.Fc2Pusher(
        env.bot,
        Fc2PusherConfig(),
        fc2bot_store,
        fc2bl.Fc2Blacklist(shared, fc2_state),
    )
    assert pusher._blocked("5000545") is True

    update, context = make_command_update(["5000545"])
    context.bot = env.bot
    await handlers.fc2unblock_command(update, context)

    assert not tor_blacklist.contains("5000545")
    assert fc2bot_store.is_muted("5000545")  # 本容器旧 muted 还在
    assert pusher._blocked("5000545") is False

    install_fc2_media(monkeypatch)
    await pusher._push_entry(dict(FC2_ENTRY), -1001234)

    assert len(env.bot.photos) == 1
    assert fc2bot_store.entry("5000545") is not None


async def test_batch_blacklist_failure_keeps_keyboard_for_retry(env, monkeypatch):
    def boom(title):
        raise RuntimeError("blacklist unavailable")

    monkeypatch.setattr(handlers, "_fc2_blocked_number", boom)
    links = extract_magnets(TEXT)[:2]
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert env.client.added == []
    assert context.user_data["batch"] is links
    assert len(context.user_data["batch"]) == 2
    progress = [edit for edit in env.bot.edits if "正在添加" in edit["text"]]
    assert progress
    assert all(edit.get("reply_markup") is not None for edit in progress)
    text = env.bot.sent[-1]["text"]
    assert "黑名单检查失败" in text
    assert "可重试原按钮" in text

    monkeypatch.setattr(handlers, "_fc2_blocked_number", lambda title: None)
    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert len(env.client.added) == 2
    assert context.user_data.get("batch") is None


async def test_batch_progress_not_modified_keeps_message_id_and_clears_keyboard(
    env, monkeypatch
):
    links = extract_magnets(TEXT)[:2]
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)
    original_edit = env.bot.edit_message_text
    attempts = []

    async def edit_message_text(**kwargs):
        attempts.append(kwargs)
        if "正在添加" in kwargs["text"]:
            raise BadRequest("Message is not modified")
        return await original_edit(**kwargs)

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert len(env.client.added) == 2
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None
    result_edits = [
        edit
        for edit in env.bot.edits
        if edit["message_id"] == 77 and "已添加 2 / 2" in edit["text"]
    ]
    assert result_edits
    assert result_edits[-1].get("reply_markup") is None
    assert (9, 77, None) in env.bot.keyboard_clears


async def test_batch_result_fallback_clears_clicked_keyboard(env, monkeypatch):
    links = extract_magnets(TEXT)[:2]
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)

    async def edit_message_text(**kwargs):
        raise TelegramError("Bad Request: message can't be edited")

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert len(env.client.added) == 2
    assert context.user_data.get("batch") is None
    assert any("已添加 2 / 2" in sent["text"] for sent in env.bot.sent)
    assert env.bot.keyboard_clears == [(9, 77, None)]


async def test_stale_batch_button_cannot_consume_new_batch(env, monkeypatch):
    def boom(title):
        raise RuntimeError("blacklist unavailable")

    monkeypatch.setattr(handlers, "_fc2_blocked_number", boom)
    links = extract_magnets(TEXT)[:2]
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert env.client.added == []
    assert context.user_data["batch"] is links
    assert context.user_data["batch_prompt"] == 77

    monkeypatch.setattr(handlers, "_fc2_blocked_number", lambda title: None)
    original_edit = env.bot.edit_message_text

    async def edit_message_text(**kwargs):
        if "正在添加" in kwargs["text"]:
            raise BadRequest("Message is not modified")
        return await original_edit(**kwargs)

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)

    await handlers.confirmDirChoice(make_update("/downloads/FC2"), context)

    assert len(env.client.added) == 2
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None
    assert (9, 77, None) in env.bot.keyboard_clears

    await handlers.batchMagnets(
        make_batch_preview_update(
            "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=麻豆MD-7777", 88
        ),
        context,
    )

    assert context.user_data["batch_prompt"] == 88
    added_before = len(env.client.added)

    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=77), context
    )

    assert len(env.client.added) == added_before
    assert context.user_data["batch"]
    assert context.user_data["batch_prompt"] == 88

    await handlers.confirmDirChoice(make_update("__auto__", message_id=88), context)

    assert len(env.client.added) == added_before + 1
    assert env.client.added[-1]["download_dir"] == "/downloads/国产"
    assert env.client.added[-1]["auto_classify"] is True
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None


async def test_fc2block_reports_existing_entry_backfill_failure(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "", fc2bl.SOURCE_MUTE)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
    )
    (tmp_path / f"fc2_blacklist.json.tmp.{os.getpid()}").mkdir()
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2block_command(update, context)

    assert blacklist.contains("5000545") is True
    text = env.bot.sent[-1]["text"]
    assert "没有进黑名单" not in text
    assert "拦截不会生效" not in text
    assert "本来就在" in text
    assert "仍然生效" in text
    assert "补写" in text
    data = json.loads((tmp_path / "fc2_blacklist.json").read_text(encoding="utf-8"))
    assert data["entries"]["5000545"]["title"] == ""


async def test_fc2unblock_read_failure_does_not_report_absent(
    env, tmp_path, monkeypatch
):
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    store.mute("5000545")
    path = tmp_path / "fc2_blacklist.json"
    original_open = open

    def denied(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2unblock_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "不在黑名单" not in text
    assert "读不到" in text or "读取失败" in text
    assert "5000545" in store.muted
    assert "5000545" in path.read_text(encoding="utf-8")
    probe = fc2bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))
    assert probe.read_ok() is False


async def test_fc2unblock_read_race_does_not_report_absent(env, tmp_path, monkeypatch):
    # BLOCK 2：remove() 写盘失败后补读成功，随后共享文件 mtime 变了且读失败，
    # 不能再回「不在黑名单」——磁盘条目和 muted 都还在。
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "x", fc2bl.SOURCE_MUTE)
    store.mute("5000545")
    path = tmp_path / "fc2_blacklist.json"
    (tmp_path / f"fc2_blacklist.json.tmp.{os.getpid()}").mkdir()
    original_remove = blacklist.remove
    original_open = open
    gate = {"deny": False}

    def denied(file, *args, **kwargs):
        if gate["deny"] and str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    def remove_then_break(number):
        result = original_remove(number)  # 写盘被 tmp 目录挡住 → False
        gate["deny"] = True
        stamp = path.stat().st_mtime_ns
        os.utime(path, ns=(stamp, stamp + 1_000_000))
        return result

    monkeypatch.setattr("builtins.open", denied)
    monkeypatch.setattr(blacklist, "remove", remove_then_break)
    monkeypatch.setattr(handlers, "_fc2_blacklist", lambda: blacklist)
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2unblock_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "读不到" in text or "读取失败" in text
    assert "不在黑名单" not in text
    assert "5000545" in store.muted
    monkeypatch.setattr("builtins.open", original_open)
    assert "5000545" in path.read_text(encoding="utf-8")


async def test_fc2block_read_race_does_not_report_not_blocked(env, tmp_path, monkeypatch):
    # BLOCK 2：add() 写盘失败后、查 contains() 前刷新读失败，
    # 不能报「没有进黑名单 / 拦截不会生效」——磁盘条目仍然在。
    store = install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "", fc2bl.SOURCE_MUTE)
    store.remember(
        "5000545",
        {"hash": FC2_HASH, "title": "FC2-PPV-5000545 sample", "pub_ts": 1},
    )
    path = tmp_path / "fc2_blacklist.json"
    (tmp_path / f"fc2_blacklist.json.tmp.{os.getpid()}").mkdir()
    original_add = blacklist.add
    original_open = open
    gate = {"deny": False}

    def denied(file, *args, **kwargs):
        if gate["deny"] and str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    def add_then_break(number, title="", source="", note=""):
        result = original_add(number, title, source, note)  # 补写被 tmp 目录挡住
        gate["deny"] = True
        stamp = path.stat().st_mtime_ns
        os.utime(path, ns=(stamp, stamp + 1_000_000))
        return result

    monkeypatch.setattr("builtins.open", denied)
    monkeypatch.setattr(blacklist, "add", add_then_break)
    monkeypatch.setattr(handlers, "_fc2_blacklist", lambda: blacklist)
    update, context = make_command_update(["5000545"])
    context.bot = env.bot

    await handlers.fc2block_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "读不到" in text or "读取失败" in text
    assert "没有进黑名单" not in text
    assert "拦截不会生效" not in text
    monkeypatch.setattr("builtins.open", original_open)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["entries"]["5000545"]["title"] == ""


async def test_fc2block_read_failure_does_not_leave_phantom_entry(
    env, tmp_path, monkeypatch
):
    # BLOCK 1：读失败时 add() 不能把磁盘上不存在的编号留在内存里，
    # 回话要如实说读不到，也不能说「没有进黑名单」。
    install_fc2_store(monkeypatch, tmp_path)
    blacklist = install_fc2_blacklist(monkeypatch, tmp_path)
    blacklist.add("5000545", "", fc2bl.SOURCE_MUTE)
    path = tmp_path / "fc2_blacklist.json"
    original_open = open
    deny = {"on": False}

    def denied(file, *args, **kwargs):
        if deny["on"] and str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    deny["on"] = True  # .lock 可写，只拒共享黑名单 JSON
    update, context = make_command_update(["5000546"])
    context.bot = env.bot

    await handlers.fc2block_command(update, context)

    assert len(env.bot.sent) == 1
    text = env.bot.sent[-1]["text"]
    assert "读不到" in text or "读取失败" in text
    assert "没有进黑名单" not in text
    assert "拦截不会生效" not in text
    monkeypatch.setattr("builtins.open", original_open)
    assert blacklist.contains("5000546") is False
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "5000546" not in data["entries"]


async def test_batch_preview_failure_does_not_let_old_button_consume_new_batch(
    env, monkeypatch
):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    await handlers.batchMagnets(
        make_batch_preview_update(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=FC2-PPV-1234567", 77
        ),
        context,
    )
    assert context.user_data["batch_prompt"] == 77
    assert len(context.user_data["batch"]) == 1

    async def reply_text(text, **kwargs):
        raise TimedOut("timed out")

    failed = make_batch_preview_update(
        "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=麻豆MD-7777", 88
    )
    monkeypatch.setattr(failed.message, "reply_text", reply_text)

    await handlers.batchMagnets(failed, context)

    # 预览没发出去，新批次和旧预览 ID 都不能留在 user_data 里。
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None

    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=77), context
    )

    assert env.client.added == []


async def test_batch_result_send_failure_still_clears_keyboard(env, monkeypatch):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    await handlers.batchMagnets(make_batch_preview_update(TEXT, 77), context)
    assert context.user_data["batch_prompt"] == 77

    async def edit_message_text(**kwargs):
        raise NetworkError("network down")

    async def send_message(**kwargs):
        raise TimedOut("timed out")

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)
    monkeypatch.setattr(env.bot, "send_message", send_message)

    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert len(env.client.added) == 3
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None
    assert (9, 77, None) in env.bot.keyboard_clears


def make_dir_prompt_update(text, message_id, chat_id=9):
    async def reply_text(text, **kwargs):
        return SimpleNamespace(message_id=message_id)

    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        from_user=SimpleNamespace(id=9, language_code="zh-hans"),
        text=text,
        caption=None,
        document=None,
        reply_text=reply_text,
    )
    return SimpleNamespace(
        message=message, effective_user=message.from_user, callback_query=None
    )


async def test_old_single_preview_button_cannot_consume_new_single(env):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    await handlers.chooseDownloadDir(
        make_dir_prompt_update(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=SSIS-111", 77
        ),
        context,
    )
    assert context.user_data["torrent_prompt"] == 77

    await handlers.chooseDownloadDir(
        make_dir_prompt_update(
            "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=SSIS-999", 99
        ),
        context,
    )
    assert context.user_data["torrent_prompt"] == 99

    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=77), context
    )

    assert env.client.added == []
    assert not any(call["download_dir"] == "/downloads/FC2" for call in env.client.added)
    assert (9, 77, None) in env.bot.keyboard_clears
    # 单条状态必须原样保留，用户点正确的预览按钮还能添加。
    assert context.user_data["torrent"]["title"].endswith("SSIS-999")
    assert context.user_data["torrent_prompt"] == 99

    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=99), context
    )

    assert len(env.client.added) == 1
    assert env.client.added[0]["download_dir"] == "/downloads/FC2"
    assert context.user_data.get("torrent") is None
    assert context.user_data.get("torrent_prompt") is None


async def test_batch_preview_failure_clears_old_keyboard_and_blocks_old_button(
    env, monkeypatch
):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    await handlers.batchMagnets(
        make_batch_preview_update(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=SSIS-111", 77
        ),
        context,
    )
    assert context.user_data["batch_prompt"] == 77

    async def reply_text(text, **kwargs):
        raise TimedOut("timed out")

    failed = make_batch_preview_update(
        "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=SSIS-222", 88
    )
    monkeypatch.setattr(failed.message, "reply_text", reply_text)

    await handlers.batchMagnets(failed, context)

    assert (9, 77, None) in env.bot.keyboard_clears
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None

    await handlers.chooseDownloadDir(
        make_dir_prompt_update(
            "magnet:?xt=urn:btih:" + "c" * 40 + "&dn=SSIS-999", 99
        ),
        context,
    )
    assert context.user_data["torrent_prompt"] == 99

    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=77), context
    )

    assert env.client.added == []
    assert not any(call["download_dir"] == "/downloads/FC2" for call in env.client.added)
    assert context.user_data["torrent_prompt"] == 99


async def test_batch_result_send_failure_retries_and_delivers_summary(env, monkeypatch):
    links = [
        extract_magnets(
            "magnet:?xt=urn:btih:" + f"{index:040x}" + f"&dn=SSIS-{100 + index}"
        )[0]
        for index in range(1, 10)
    ]
    context = SimpleNamespace(user_data={"batch": links}, bot=env.bot)
    original_edit = env.bot.edit_message_text

    async def edit_message_text(**kwargs):
        if "正在添加" in kwargs["text"]:
            raise NetworkError("network down")
        return await original_edit(**kwargs)

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)
    env.bot.send_message_failures = [TimedOut("timed out")]

    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert len(env.client.added) == 9
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None
    assert context.user_data.get("pending_batch_results") is None
    assert any("已添加 9 / 9" in sent["text"] for sent in env.bot.sent)
    # 第一次失败、第二次成功。
    assert len(
        [call for call in env.bot.send_attempts if "已添加 9 / 9" in call["text"]]
    ) == 2
    assert (9, 77, None) in env.bot.keyboard_clears
    summary = next(
        sent["text"] for sent in env.bot.sent if "已添加 9 / 9" in sent["text"]
    )
    delivered = sum(1 for sent in env.bot.sent if sent["text"] == summary)

    # 两次发送都失败：汇总留在 user_data，下一次回调补发。
    env.bot.send_message_failures = [
        TimedOut("timed out"),
        NetworkError("network down"),
    ]
    context.user_data["batch"] = links

    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert len(env.client.added) == 18
    pending_results = context.user_data.get("pending_batch_results")
    assert pending_results is not None
    assert len(pending_results) == 1
    assert pending_results[0][1] == summary
    assert sum(1 for sent in env.bot.sent if sent["text"] == summary) == delivered

    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert context.user_data.get("pending_batch_results") is None
    assert sum(1 for sent in env.bot.sent if sent["text"] == summary) == delivered + 1


def make_batch_links(count, base=100):
    return [
        extract_magnets(
            "magnet:?xt=urn:btih:" + f"{index:040x}" + f"&dn=SSIS-{base + index}"
        )[0]
        for index in range(1, count + 1)
    ]


async def test_single_preview_ownership_includes_chat(env):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    await handlers.chooseDownloadDir(
        make_dir_prompt_update(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=SSIS-111", 77, chat_id=9
        ),
        context,
    )
    assert context.user_data["torrent_prompt"] == 77
    assert context.user_data["torrent_prompt_chat"] == 9

    # 同一个 user_data：群里出了同号 77 的预览，私聊的旧 77 按钮不能认领它。
    await handlers.chooseDownloadDir(
        make_dir_prompt_update(
            "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=SSIS-999", 77, chat_id=5
        ),
        context,
    )
    assert context.user_data["torrent_prompt"] == 77
    assert context.user_data["torrent_prompt_chat"] == 5

    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=77, chat_id=9), context
    )

    assert env.client.added == []
    assert not any(call["download_dir"] == "/downloads/FC2" for call in env.client.added)
    assert context.user_data["torrent"]["title"].endswith("SSIS-999")
    assert context.user_data["torrent_prompt"] == 77
    assert context.user_data["torrent_prompt_chat"] == 5

    # 群里重发一次同一号的预览，群聊按钮仍能正常添加。
    await handlers.chooseDownloadDir(
        make_dir_prompt_update(
            "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=SSIS-999", 77, chat_id=5
        ),
        context,
    )
    await handlers.confirmDirChoice(
        make_update("/downloads/FC2", message_id=77, chat_id=5), context
    )

    assert len(env.client.added) == 1
    assert env.client.added[0]["download_dir"] == "/downloads/FC2"
    assert context.user_data.get("torrent") is None
    assert context.user_data.get("torrent_prompt") is None
    assert context.user_data.get("torrent_prompt_chat") is None


async def test_batch_preview_failure_clears_old_keyboard_in_old_chat_only(
    env, monkeypatch
):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    await handlers.batchMagnets(
        make_batch_preview_update(
            "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=SSIS-111", 77, chat_id=9
        ),
        context,
    )
    assert context.user_data["batch_prompt"] == 77
    assert context.user_data["batch_prompt_chat"] == 9

    async def reply_text(text, **kwargs):
        raise TimedOut("timed out")

    failed = make_batch_preview_update(
        "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=SSIS-222", 77, chat_id=5
    )
    monkeypatch.setattr(failed.message, "reply_text", reply_text)

    await handlers.batchMagnets(failed, context)

    assert (9, 77, None) in env.bot.keyboard_clears
    assert (5, 77, None) not in env.bot.keyboard_clears
    assert context.user_data.get("batch") is None
    assert context.user_data.get("batch_prompt") is None
    assert context.user_data.get("batch_prompt_chat") is None


async def test_pending_batch_results_accumulate_instead_of_overwrite(env, monkeypatch):
    links_a = make_batch_links(9, 100)
    links_b = make_batch_links(10, 200)
    context = SimpleNamespace(user_data={"batch": links_a}, bot=env.bot)

    async def edit_message_text(**kwargs):
        raise NetworkError("network down")

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)

    # 第一轮：A 批次汇总两次都发不出去，留下 pending。
    env.bot.send_message_failures = [TimedOut("timed out"), TimedOut("timed out")]
    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert len(env.client.added) == 9
    pending = context.user_data["pending_batch_results"]
    assert len(pending) == 1
    first_text = pending[0][1]
    assert "已添加 9 / 9" in first_text

    # 第二轮：补发 A 失败（1 次），B 批次汇总两次也失败 → 追加而不是覆盖。
    context.user_data["batch"] = links_b
    env.bot.send_message_failures = [
        TimedOut("timed out"),
        TimedOut("timed out"),
        TimedOut("timed out"),
    ]
    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert len(env.client.added) == 19
    pending = context.user_data["pending_batch_results"]
    assert len(pending) == 2
    second_text = pending[1][1]
    assert "已添加 10 / 10" in second_text
    assert [entry[1] for entry in pending] == [first_text, second_text]

    # 补发仍全部失败：列表原样（顺序不变）保留。
    env.bot.send_message_failures = [
        TimedOut("timed out"),
        NetworkError("network down"),
    ]
    await handlers.confirmDirChoice(make_update("__auto__"), context)

    pending = context.user_data["pending_batch_results"]
    assert [entry[1] for entry in pending] == [first_text, second_text]

    # 网络恢复：两条汇总都补发出去，列表被清空。
    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert context.user_data.get("pending_batch_results") is None
    delivered = [sent["text"] for sent in env.bot.sent]
    assert delivered.count(first_text) == 1
    assert delivered.count(second_text) == 1


async def test_pending_overflow_six_batches_keeps_first_summary(env, monkeypatch):
    context = SimpleNamespace(user_data={}, bot=env.bot)

    async def edit_message_text(**kwargs):
        raise NetworkError("network down")

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)

    expected_added = 0
    for base, count in zip((100, 200, 300, 400, 500, 600), (9, 10, 11, 12, 13, 14)):
        context.user_data["batch"] = make_batch_links(count, base)
        env.bot.send_message_failures = [TimedOut("timed out")] * 10
        await handlers.confirmDirChoice(make_update("__auto__"), context)
        expected_added += count
        assert len(env.client.added) == expected_added

    pending = context.user_data["pending_batch_results"]
    # 溢出合并生效，但仍保留每一批的结果。
    assert len(pending) <= 5
    combined = "\n\n".join(entry[1] for entry in pending)
    for count in (9, 10, 11, 12, 13, 14):
        assert f"已添加 {count} / {count}" in combined


async def test_pending_overflow_six_batches_all_redelivered_after_recovery(
    env, monkeypatch
):
    context = SimpleNamespace(user_data={}, bot=env.bot)
    original_edit = env.bot.edit_message_text

    async def edit_message_text(**kwargs):
        raise NetworkError("network down")

    monkeypatch.setattr(env.bot, "edit_message_text", edit_message_text)

    for base, count in zip((100, 200, 300, 400, 500, 600), (9, 10, 11, 12, 13, 14)):
        context.user_data["batch"] = make_batch_links(count, base)
        env.bot.send_message_failures = [TimedOut("timed out")] * 10
        await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert len(env.client.added) == 69
    assert context.user_data.get("pending_batch_results") is not None

    # 网络恢复：编辑与发送都恢复默认。
    monkeypatch.setattr(env.bot, "edit_message_text", original_edit)
    env.bot.send_message_failures = []

    await handlers.confirmDirChoice(make_update("__auto__"), context)

    assert context.user_data.get("pending_batch_results") is None
    delivered = "\n\n".join(sent["text"] for sent in env.bot.sent)
    for count in (9, 10, 11, 12, 13, 14):
        assert delivered.count(f"已添加 {count} / {count}") == 1
