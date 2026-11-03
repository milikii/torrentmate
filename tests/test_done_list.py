import importlib.util
import json
import logging
from pathlib import Path

from models.DoneList import (
    SOURCE_BATCH,
    SOURCE_MANUAL,
    WRITE_ADDED,
    WRITE_DUPLICATE,
    DoneDeletionQueue,
    DoneList,
    config_path,
    pending_config_path,
)
from models.DownloadQueue import QueueEntry

ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path, **fc2):
    path = tmp_path / "torrentino.yaml"
    values = {f"fc2.{key}": value for key, value in fc2.items()}

    class FakeConfig:
        config_file = str(path)

        def get(self, key, default=None):
            return values.get(key, default)

    return FakeConfig()


# ---------------------------------------------------------------- 路径解析


def test_done_path_defaults_next_to_config(tmp_path):
    config = make_config(tmp_path)

    assert config_path(config) == str(tmp_path / "fc2_done.jsonl")
    assert pending_config_path(config) == str(tmp_path / "fc2_done.jsonl.pending")


def test_done_path_uses_shared_file_and_derived_pending(tmp_path):
    config = make_config(tmp_path, done_file="/shared/fc2_done.jsonl")

    assert config_path(config) == "/shared/fc2_done.jsonl"
    assert pending_config_path(config) == "/shared/fc2_done.jsonl.pending"


def test_pending_path_can_be_configured_separately(tmp_path):
    config = make_config(
        tmp_path,
        done_file="/shared/fc2_done.jsonl",
        done_pending_file="/shared/pending.jsonl",
    )

    assert pending_config_path(config) == "/shared/pending.jsonl"


def test_placeholder_paths_fall_back_to_default(tmp_path):
    config = make_config(tmp_path, done_file="<done_file>")

    assert config_path(config) == str(tmp_path / "fc2_done.jsonl")


# ---------------------------------------------------------------- 名单追加 / 去重


def test_append_and_dedupe_by_number_and_done_time(tmp_path):
    path = str(tmp_path / "done.jsonl")
    done = DoneList(path)

    assert (
        done.append(
            number="05000545",
            title="A",
            done_at="2026-11-03 10:00:00",
            source=SOURCE_MANUAL,
        )
        == WRITE_ADDED
    )
    # 前导零 / ISO 写法不同，归一后仍是同一条：不重复写
    assert (
        done.append(
            number="5000545",
            title="A",
            done_at="2026-11-03T10:00:00",
            source=SOURCE_BATCH,
        )
        == WRITE_DUPLICATE
    )
    # 同一部片以后再下一次，是新的完成记录
    assert (
        done.append(number="5000545", title="A", done_at="2026-11-04 10:00:00")
        == WRITE_ADDED
    )

    assert done.count() == 2
    records = done.all()
    assert [record["number"] for record in records] == ["5000545", "5000545"]
    assert [record["done_at"] for record in records] == [
        "2026-11-03 10:00:00",
        "2026-11-04 10:00:00",
    ]


def test_append_dedupes_non_fc2_by_hash_then_title(tmp_path):
    done = DoneList(str(tmp_path / "done.jsonl"))

    assert (
        done.append(
            title="SSIS-123",
            info_hash="ABCDEF0123456789ABCDEF0123456789ABCDEF01",
            done_at="2026-11-03 10:00:00",
        )
        == WRITE_ADDED
    )
    # 大小写不同的 hash 仍是同一条
    assert (
        done.append(
            title="SSIS-123 renamed",
            info_hash="abcdef0123456789abcdef0123456789abcdef01",
            done_at="2026-11-03 10:00:00",
        )
        == WRITE_DUPLICATE
    )
    # 没编号没 hash：按标题 + 完成时间去重
    assert done.append(title="无编号作品", done_at="2026-11-05 09:00:00") == WRITE_ADDED
    assert done.append(title="无编号作品", done_at="2026-11-05 09:00:00") == WRITE_DUPLICATE
    assert done.append(title="无编号作品", done_at="2026-11-06 09:00:00") == WRITE_ADDED

    assert done.count() == 3


def test_corrupt_line_is_skipped_and_never_rewritten(tmp_path, caplog):
    path = tmp_path / "done.jsonl"
    valid = json.dumps(
        {
            "number": "5000545",
            "title": "A",
            "info_hash": None,
            "done_at": "2026-11-03 10:00:00",
            "size_when_done": 10,
            "source": SOURCE_MANUAL,
        }
    )
    original = "{ this is not json\n" + valid + "\n"
    path.write_text(original, encoding="utf-8")
    done = DoneList(str(path))

    with caplog.at_level(logging.WARNING):
        records = done.all()

    assert len(records) == 1
    assert records[0]["number"] == "5000545"
    assert any("损坏" in record.getMessage() for record in caplog.records)

    # 追加后坏行原样还在：只追加，不改写不截断
    assert done.append(number="5000546", title="B", done_at="2026-11-03 11:00:00") == WRITE_ADDED
    text = path.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert done.count() == 2


def test_find_by_normalized_number_and_hash_only(tmp_path):
    done = DoneList(str(tmp_path / "done.jsonl"))
    done.append(number="05000545", title="编号片", done_at="2026-11-03 10:00:00")
    done.append(
        title="hash 片",
        info_hash="ABCDEF0123456789ABCDEF0123456789ABCDEF01",
        done_at="2026-11-03 11:00:00",
    )

    by_number = done.find(number="05000545")
    assert len(by_number) == 1
    assert by_number[0]["title"] == "编号片"

    by_hash = done.find(info_hash="abcdef0123456789abcdef0123456789abcdef01")
    assert len(by_hash) == 1
    assert by_hash[0]["title"] == "hash 片"

    # 没有 hash 就不查，不做标题模糊匹配
    assert done.find() == []
    assert done.find(number="9999999") == []


def test_recent_returns_latest_ten_in_time_order(tmp_path):
    done = DoneList(str(tmp_path / "done.jsonl"))
    for index in range(12):
        done.append(
            number=str(5000000 + index),
            title=f"t{index}",
            done_at=f"2026-11-{index + 1:02d} 10:00:00",
        )

    recent = done.recent(10)

    assert len(recent) == 10
    assert recent[0]["number"] == "5000011"
    assert recent[-1]["number"] == "5000002"


# ---------------------------------------------------------------- 待删队列


def test_pending_requests_ack_and_requeue(tmp_path):
    path = str(tmp_path / "pending.jsonl")
    queue = DoneDeletionQueue(path)

    assert queue.pending() == []
    assert queue.request("05000545", title="x", done_at="2026-11-03 10:00:00") is True
    # 还在待删时重复完成，不重复排队
    assert queue.request("5000545") is False
    pending = queue.pending()
    assert [item["number"] for item in pending] == ["5000545"]
    assert pending[0]["done_at"] == "2026-11-03 10:00:00"

    assert queue.ack("5000545", "deleted") is True
    assert queue.pending() == []
    # 已 ack 后又完成一遍（比如重下）：重新排队，删的是新卡
    assert queue.request("5000545") is True
    assert [item["number"] for item in queue.pending()] == ["5000545"]


def test_pending_queue_tolerates_corrupt_lines(tmp_path, caplog):
    path = tmp_path / "pending.jsonl"
    path.write_text(
        "not json\n"
        + json.dumps({"op": "pending", "number": "5000545", "done_at": ""})
        + "\n"
        + json.dumps({"op": "ack", "number": "5000546", "status": "deleted"})
        + "\n",
        encoding="utf-8",
    )
    queue = DoneDeletionQueue(str(path))

    with caplog.at_level(logging.WARNING):
        pending = queue.pending()

    assert [item["number"] for item in pending] == ["5000545"]
    assert any("损坏" in record.getMessage() for record in caplog.records)


def test_pending_queue_ack_without_request_is_noop(tmp_path):
    queue = DoneDeletionQueue(str(tmp_path / "pending.jsonl"))

    assert queue.ack("5000545") is False
    assert queue.pending() == []


# ---------------------------------------------------------------- 来源字段


def test_queue_entry_source_is_backward_compatible():
    entry = QueueEntry(torrent_id=1, chat_id=2)
    assert entry.source == "unknown"

    payload = entry.to_json()
    assert payload["source"] == "unknown"

    restored = QueueEntry.from_json(payload)
    assert restored.source == "unknown"

    # 旧版队列文件没有 source 字段，照样能读
    legacy = {"torrent_id": 3, "chat_id": 4, "title": "old"}
    assert QueueEntry.from_json(legacy).source == "unknown"


# ---------------------------------------------------------------- 迁移脚本


def load_migration_module():
    path = ROOT / "tools" / "migrate_done_history.py"
    spec = importlib.util.spec_from_file_location("migrate_done_history", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_log(path, lines):
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def test_migration_dedupes_across_logs_and_existing_records(tmp_path):
    done_path = tmp_path / "fc2_done.jsonl"
    log1 = tmp_path / "torrentino.log"
    log2 = tmp_path / "fc2bot.log"
    fc2_line = json.dumps(
        {
            "date_done": "2026-11-01 10:00:00",
            "name": "FC2-PPV-05000545 老片",
            "download_dir": "/downloads/FC2",
            "size_when_done": 100,
        }
    )
    ssis_line = json.dumps(
        {
            "date_done": "2026-11-02 10:00:00",
            "name": "SSIS-123",
            "download_dir": "/downloads/有码",
            "size_when_done": 200,
        }
    )
    write_log(log1, [fc2_line, ssis_line])
    # 第二个容器同一批记录 + 老式逗号行 + 一条新的
    write_log(
        log2,
        [
            fc2_line,
            ssis_line,
            "2026-11-03 10:00:00,Legacy Title,/downloads/未分类,300",
            json.dumps(
                {
                    "date_done": "2026-11-04 10:00:00",
                    "name": "NEW-1",
                    "download_dir": "/downloads/未分类",
                    "size_when_done": 400,
                }
            ),
        ],
    )
    # 名单里已经有一条同一部片（编号写法不同、时间相同）：迁移不能再写一遍
    DoneList(str(done_path)).append(
        number="5000545", title="已有记录", done_at="2026-11-01 10:00:00"
    )

    migration = load_migration_module()
    stats = migration.migrate([str(log1), str(log2)], str(done_path))

    assert stats["read"] == 6
    assert stats["added"] == 3  # SSIS-123 + Legacy Title + NEW-1
    assert stats["duplicate"] == 3  # 已存在的编号片 + 两条重复日志
    assert stats["failed"] == 0
    records = DoneList(str(done_path)).all()
    assert len(records) == 4
    assert {record["source"] for record in records[1:]} == {"migration"}

    # 再跑一遍不重复写
    again = migration.migrate([str(log1), str(log2)], str(done_path))
    assert again["added"] == 0
    assert again["duplicate"] == 6
    assert DoneList(str(done_path)).count() == 4


def test_migration_dry_run_writes_nothing(tmp_path):
    done_path = tmp_path / "fc2_done.jsonl"
    log = tmp_path / "bot.log"
    write_log(
        log,
        [
            json.dumps(
                {
                    "date_done": "2026-11-01 10:00:00",
                    "name": "FC2-PPV-5000545",
                    "download_dir": "/d",
                    "size_when_done": 1,
                }
            )
        ],
    )

    migration = load_migration_module()
    stats = migration.migrate([str(log)], str(done_path), dry_run=True)

    assert stats["added"] == 1
    assert not done_path.exists() or done_path.read_text(encoding="utf-8") == ""
