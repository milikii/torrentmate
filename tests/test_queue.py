import json

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
