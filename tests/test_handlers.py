import asyncio
import datetime as dt
from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from transmission_rpc.client import Client

import lib.bot_handlers as handlers
from lib import auth
from lib.classifier import DEFAULT_RULES
from lib.func import trans
from lib.magnet import extract_magnets, parse_magnet
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

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def send_message(self, **kwargs):
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

    async def get_file(self, file_id):
        return FakeFile(self.payload, self.downloads)


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
        edit_message_reply_markup=_async_noop,
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


def make_fc2_callback(data, user_id=9):
    answers = []

    async def answer(text=None, *args, **kwargs):
        answers.append(text)

    query = SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, language_code="zh-hans"),
        message=SimpleNamespace(chat=SimpleNamespace(id=user_id), message_id=77),
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
