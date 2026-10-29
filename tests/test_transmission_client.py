from hashlib import sha1
from pathlib import Path
from types import SimpleNamespace

import pytest
from transmission_rpc.client import Client

from lib.magnet import build_magnet
from models.DownloadQueue import DownloadQueue, QueueEntry
from models.TransmissionClient import TorrentAlreadyExists, TransmissionClient


@pytest.fixture(autouse=True)
def clean_queue():
    DownloadQueue.set_state_file(None)
    DownloadQueue.clear()
    yield
    DownloadQueue.set_state_file(None)
    DownloadQueue.clear()


def write_torrent(path: Path, name: bytes = b"video.bin", tracker: bytes = b"https://t.invalid"):
    def string(value: bytes) -> bytes:
        return str(len(value)).encode() + b":" + value

    info = (
        b"d6:lengthi4e4:name"
        + string(name)
        + b"12:piece lengthi16384e6:pieces20:"
        + b"x" * 20
        + b"e"
    )
    path.write_bytes(
        b"d8:announce"
        + string(tracker)
        + b"4:info"
        + info
        + b"e"
    )
    return sha1(info).hexdigest()


def remote(torrent_id: int, info_hash: str | None):
    return SimpleNamespace(id=torrent_id, name="remote", info_hash=info_hash)


def make_client(monkeypatch, torrents, add_result=None, add_error=None):
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
    return client, calls


def test_single_dedup_skips_queue_hash_before_rpc(tmp_path, monkeypatch):
    path = tmp_path / "existing.torrent"
    info_hash = write_torrent(path)
    original = QueueEntry(
        torrent_id=1,
        chat_id=9,
        info_hash=info_hash.upper(),
        message_id=777,
    )
    DownloadQueue.add(original)
    client, calls = make_client(monkeypatch, [])

    with pytest.raises(TorrentAlreadyExists):
        client.add_torrent(9, "zh", path, single_dedup=True)

    assert calls == []
    assert DownloadQueue.get(1) is original


def test_single_dedup_skips_transmission_hash_from_magnet(tmp_path, monkeypatch):
    info_hash = "0123456789ABCDEF0123456789ABCDEF01234567"
    client, calls = make_client(monkeypatch, [remote(2, info_hash.lower())])

    with pytest.raises(TorrentAlreadyExists):
        client.add_torrent(
            9,
            "zh",
            build_magnet(info_hash),
            single_dedup=True,
        )

    assert calls == []
    assert DownloadQueue.size() == 0


def test_single_dedup_url_returning_existing_torrent_is_duplicate(monkeypatch):
    info_hash = "89abcdef0123456789abcdef0123456789abcdef"
    result = SimpleNamespace(id=2, name="existing", info_hash=info_hash.upper())
    client, calls = make_client(
        monkeypatch, [remote(2, info_hash)], add_result=result
    )

    with pytest.raises(TorrentAlreadyExists):
        client.add_torrent(
            9,
            "zh",
            "https://tracker.invalid/dl/2",
            single_dedup=True,
        )

    assert len(calls) == 1
    assert DownloadQueue.size() == 0


def test_single_dedup_lookup_failure_aborts_before_add(monkeypatch):
    original = QueueEntry(torrent_id=1, chat_id=9, message_id=777)
    DownloadQueue.add(original)

    def fail_lookup():
        raise RuntimeError("RPC list unavailable")

    client, calls = make_client(monkeypatch, fail_lookup)

    with pytest.raises(RuntimeError):
        client.add_torrent(
            9,
            "zh",
            "https://tracker.invalid/dl/2",
            single_dedup=True,
        )

    assert calls == []
    assert DownloadQueue.get(1) is original


def test_single_dedup_adds_new_torrent(monkeypatch):
    info_hash = "abcdef0123456789abcdef0123456789abcdef01"
    result = SimpleNamespace(id=5, name="new", info_hash=info_hash.upper())
    client, calls = make_client(monkeypatch, [], add_result=result)

    returned = client.add_torrent(
        9,
        "zh",
        "https://tracker.invalid/dl/5",
        download_dir="/downloads/未分类",
        title="new",
        single_dedup=True,
    )

    assert returned is result
    assert len(calls) == 1
    entry = DownloadQueue.get(5)
    assert entry is not None
    assert entry.info_hash == info_hash
    assert entry.requested_dir == "/downloads/未分类"
    assert entry.title == "new"


def test_single_dedup_uses_add_if_new_after_rpc(monkeypatch):
    info_hash = "1111111111111111111111111111111111111111"
    original = QueueEntry(
        torrent_id=1,
        chat_id=9,
        info_hash=info_hash,
        message_id=777,
        milestones_sent={25},
    )
    DownloadQueue.add(original)
    before = original.to_json()
    result = SimpleNamespace(id=2, name="again", info_hash=info_hash)
    client, calls = make_client(monkeypatch, [], add_result=result)

    with pytest.raises(TorrentAlreadyExists):
        client.add_torrent(
            9,
            "zh",
            "https://tracker.invalid/dl/2",
            single_dedup=True,
        )

    assert len(calls) == 1
    assert DownloadQueue.size() == 1
    assert DownloadQueue.get(1) is original
    assert original.to_json() == before


def test_default_mode_skips_dedup_and_keeps_original_add_semantics(monkeypatch):
    DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, message_id=777))
    result = SimpleNamespace(id=1, name="replacement", info_hash="a" * 40)

    def unexpected_lookup():
        raise AssertionError("default mode must not query torrents")

    client, calls = make_client(monkeypatch, unexpected_lookup, add_result=result)

    returned = client.add_torrent(
        9,
        "zh",
        "https://tracker.invalid/dl/1",
        download_dir="/downloads/未分类",
    )

    assert returned is result
    assert len(calls) == 1
    entry = DownloadQueue.get(1)
    assert entry is not None
    assert entry.message_id is None
    assert entry.info_hash == "a" * 40
