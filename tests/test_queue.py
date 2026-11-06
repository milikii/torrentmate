import json
import threading
from concurrent.futures import ThreadPoolExecutor

from models.DownloadQueue import DownloadQueue, QueueEntry


def test_queue_is_persisted_and_restored(tmp_path):
    state = tmp_path / "queue.json"
    try:
        DownloadQueue.set_state_file(str(state))
        DownloadQueue.clear()
        DownloadQueue.add(
            QueueEntry(
                torrent_id=7,
                chat_id=42,
                lang_code="zh",
                title="SSIS-123",
                requested_dir="/downloads/有码",
                milestones_sent={25, 50},
            )
        )

        assert state.is_file()
        stored = json.loads(state.read_text(encoding="utf-8"))
        assert stored[0]["torrent_id"] == 7
        assert stored[0]["milestones_sent"] == [25, 50]

        # 模拟重启：清空内存后重新加载
        DownloadQueue._items.clear()
        DownloadQueue.load()
        entry = DownloadQueue.get(7)
        assert entry is not None
        assert entry.title == "SSIS-123"
        assert entry.milestones_sent == {25, 50}
    finally:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()


def test_queue_remove():
    DownloadQueue.set_state_file(None)
    DownloadQueue.clear()
    DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=1))
    assert DownloadQueue.size() == 1
    assert DownloadQueue.remove(1) is not None
    assert DownloadQueue.size() == 0
    assert DownloadQueue.remove(99) is None


def test_add_if_new_inserts_and_persists(tmp_path):
    state = tmp_path / "queue.json"
    try:
        DownloadQueue.set_state_file(str(state))
        DownloadQueue.clear()
        entry = QueueEntry(
            torrent_id=3,
            chat_id=42,
            info_hash="ABCDEF0123456789ABCDEF0123456789ABCDEF01",
            message_id=55,
            milestones_sent={25, 50},
        )

        assert DownloadQueue.add_if_new(entry) is True
        assert DownloadQueue.get(3) is entry

        DownloadQueue._items.clear()
        DownloadQueue.load()
        restored = DownloadQueue.get(3)
        assert restored.info_hash == entry.info_hash
        assert restored.message_id == 55
        assert restored.milestones_sent == {25, 50}
    finally:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()


def test_add_if_new_keeps_existing_entry_for_same_hash(tmp_path):
    state = tmp_path / "queue.json"
    try:
        DownloadQueue.set_state_file(str(state))
        DownloadQueue.clear()
        original = QueueEntry(
            torrent_id=1,
            chat_id=42,
            info_hash="abcdef0123456789abcdef0123456789abcdef01",
            message_id=77,
            progress_card=False,
            milestones_sent={25},
            last_card_text="old card",
            classified=True,
        )
        DownloadQueue.add(original)
        before_json = original.to_json()
        before_state = state.read_text(encoding="utf-8")

        duplicate = QueueEntry(
            torrent_id=2,
            chat_id=99,
            info_hash=original.info_hash.upper(),
            message_id=88,
        )

        assert DownloadQueue.add_if_new(duplicate) is False
        assert DownloadQueue.size() == 1
        assert DownloadQueue.get(1) is original
        assert DownloadQueue.get(2) is None
        assert original.to_json() == before_json
        assert state.read_text(encoding="utf-8") == before_state
    finally:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()


def test_add_if_new_same_id_without_existing_hash_is_preserved():
    try:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()
        original = QueueEntry(torrent_id=7, chat_id=42, message_id=77)
        DownloadQueue.add(original)

        fresh = QueueEntry(torrent_id=7, chat_id=99, info_hash="a" * 40)

        assert DownloadQueue.add_if_new(fresh) is False
        assert DownloadQueue.get(7) is original
    finally:
        DownloadQueue.clear()


def test_add_if_new_same_id_with_different_hash_inserts():
    try:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()
        original = QueueEntry(torrent_id=7, chat_id=42, info_hash="a" * 40)
        DownloadQueue.add(original)
        fresh = QueueEntry(torrent_id=7, chat_id=99, info_hash="b" * 40)

        assert DownloadQueue.add_if_new(fresh) is True
        assert DownloadQueue.get(7) is fresh
        assert DownloadQueue.size() == 1
    finally:
        DownloadQueue.clear()


def test_add_if_new_is_atomic_for_concurrent_same_task_inserts():
    try:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()
        barrier = threading.Barrier(2)
        entries = [
            QueueEntry(torrent_id=1, chat_id=42, info_hash="c" * 40, message_id=11),
            QueueEntry(torrent_id=2, chat_id=99, info_hash="C" * 40, message_id=22),
        ]

        def attempt(entry):
            barrier.wait()
            return DownloadQueue.add_if_new(entry)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, entries))

        assert sorted(results) == [False, True]
        assert DownloadQueue.size() == 1
        winner = entries[results.index(True)]
        loser = entries[results.index(False)]
        assert DownloadQueue.get(winner.torrent_id) is winner
        assert DownloadQueue.get(winner.torrent_id).message_id == winner.message_id
        assert DownloadQueue.get(loser.torrent_id) is None
    finally:
        DownloadQueue.clear()


def test_rekey_swapped_ids_persists_and_rejects_old_snapshot(tmp_path):
    state = tmp_path / "queue.json"
    try:
        DownloadQueue.set_state_file(str(state))
        DownloadQueue.clear()
        DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, info_hash="a" * 40, message_id=101))
        DownloadQueue.add(QueueEntry(torrent_id=2, chat_id=10, info_hash="b" * 40, message_id=202))
        stale = DownloadQueue.snapshot()[1]
        updates = DownloadQueue.snapshot()
        updates[1].torrent_id = 2
        updates[2].torrent_id = 1

        assert DownloadQueue.rekey(updates) == {1, 2}
        DownloadQueue._items.clear()
        DownloadQueue.load()
        first = DownloadQueue.get(1)
        second = DownloadQueue.get(2)
        assert first is not None and first.info_hash == "b" * 40
        assert second is not None and second.info_hash == "a" * 40
        assert first.message_id == 202
        assert second.message_id == 101
        stored = json.loads(state.read_text(encoding="utf-8"))
        assert {item["torrent_id"]: item["info_hash"] for item in stored} == {1: "b" * 40, 2: "a" * 40}

        stale.message_id = 999
        DownloadQueue.update(stale)
        assert DownloadQueue.get(1) is first
        assert first.message_id == 202
        assert DownloadQueue.remove(1, "a" * 40) is None
        assert DownloadQueue.get(1) is first
        assert DownloadQueue.remove(2, "a" * 40) is second
        assert DownloadQueue.get(1) is first
    finally:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()


def test_rekey_preserves_unresolved_destination():
    try:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()
        first = QueueEntry(torrent_id=1, chat_id=9, info_hash="a" * 40)
        second = QueueEntry(torrent_id=2, chat_id=10, info_hash="b" * 40)
        DownloadQueue.add(first)
        DownloadQueue.add(second)
        update = DownloadQueue.snapshot()[1]
        update.torrent_id = 2

        assert DownloadQueue.rekey({1: update}) == set()
        assert DownloadQueue.get(1) is first
        assert DownloadQueue.get(2) is second
    finally:
        DownloadQueue.clear()


def test_rekey_does_not_revive_removed_entry():
    try:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()
        DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, info_hash="a" * 40))
        update = DownloadQueue.snapshot()[1]
        update.torrent_id = 2
        DownloadQueue.remove(1)

        assert DownloadQueue.rekey({1: update}) == set()
        assert DownloadQueue.size() == 0
    finally:
        DownloadQueue.clear()


def test_rekey_duplicate_destinations_preserves_entries():
    try:
        DownloadQueue.set_state_file(None)
        DownloadQueue.clear()
        DownloadQueue.add(QueueEntry(torrent_id=1, chat_id=9, info_hash="a" * 40))
        DownloadQueue.add(QueueEntry(torrent_id=2, chat_id=10, info_hash="b" * 40))
        updates = DownloadQueue.snapshot()
        for entry in updates.values():
            entry.torrent_id = 3

        assert DownloadQueue.rekey(updates) == set()
        assert DownloadQueue.get(1) is not None
        assert DownloadQueue.get(2) is not None
        assert DownloadQueue.get(3) is None
        assert DownloadQueue.size() == 2
    finally:
        DownloadQueue.clear()
