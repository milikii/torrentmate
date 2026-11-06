import json
import logging
import os
from types import SimpleNamespace

import pytest

from models import Fc2Blacklist as bl
from models import Fc2Channel as fc2


def write_state(path, muted, seen=None):
    path.write_text(
        json.dumps({"muted": muted, "seen": seen or {}}, ensure_ascii=False),
        encoding="utf-8",
    )


def make_blacklist(tmp_path, state=None):
    return bl.Fc2Blacklist(
        str(tmp_path / "fc2_blacklist.json"),
        str(state or tmp_path / "fc2_state.json"),
    )


def test_add_normalizes_and_persists(tmp_path):
    record = make_blacklist(tmp_path).add(
        "05000545", "FC2-PPV-5000545 name", bl.SOURCE_MUTE
    )

    assert record["number"] == "5000545"
    assert record["source"] == bl.SOURCE_MUTE
    assert record["added_ts"] > 0

    reloaded = make_blacklist(tmp_path)
    assert reloaded.contains("5000545")
    assert reloaded.get("5000545")["title"] == "FC2-PPV-5000545 name"
    assert reloaded.contains("FC2-PPV-5000545") is False  # 只认编号，不认整串标题


def test_legacy_muted_migrated_with_title_and_source(tmp_path):
    state = tmp_path / "fc2_state.json"
    write_state(state, ["5000545", "04999999"], {"5000545": {"title": "旧的标题"}})

    store = make_blacklist(tmp_path, state)

    assert store.get("5000545")["title"] == "旧的标题"
    assert store.get("5000545")["source"] == bl.SOURCE_LEGACY
    # 名字找不到就留空，但编号一条都不能丢
    assert store.get("4999999")["title"] == ""
    assert store.get("4999999")["source"] == bl.SOURCE_LEGACY


def test_migration_does_not_resurrect_removed_number(tmp_path):
    state = tmp_path / "fc2_state.json"
    write_state(state, ["5000545"])
    store = make_blacklist(tmp_path, state)

    assert store.remove("5000545")
    again = make_blacklist(tmp_path, state)

    assert not again.contains("5000545")


def test_legacy_marker_without_unblocked_does_not_resurrect(tmp_path, monkeypatch):
    # sol 复现：旧共享表没有 unblocked、只有老式布尔标记，换 hostname 不能重迁。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545"])
    shared.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {},
                "legacy_migrated": [f"old-id:{state}"],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bl, "_host", lambda: "new-id")
    store = bl.Fc2Blacklist(str(shared), str(state))

    assert not store.contains("5000545")
    assert store.migrate_legacy() == 0
    data = json.loads(shared.read_text(encoding="utf-8"))
    assert data["legacy_migrated"] == {str(state): ["5000545"]}
    assert data["entries"] == {}


def test_snapshot_survives_muted_number_removed_from_state(tmp_path, monkeypatch):
    # 编号进了快照后，从 state.muted 删掉它、再从共享表删掉条目（无墓碑），
    # 下次初始化也不能把它当新的迁回来。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    monkeypatch.setattr(bl, "_host", lambda: "container-a")
    write_state(state, ["5000545", "05000546"])
    first = bl.Fc2Blacklist(str(shared), str(state))
    assert set(first.entries) == {"5000545", "5000546"}

    write_state(state, ["5000546"])
    data = json.loads(shared.read_text(encoding="utf-8"))
    data["entries"].pop("5000545")
    data["unblocked"] = []
    shared.write_text(json.dumps(data), encoding="utf-8")

    again = bl.Fc2Blacklist(str(shared), str(state))
    assert again.migrate_legacy() == 0
    assert "5000545" not in again.entries
    data = json.loads(shared.read_text(encoding="utf-8"))
    assert set(data["legacy_migrated"][str(state)]) == {"5000545", "5000546"}


def test_legacy_migrated_structure_upgraded_and_idempotent(tmp_path, monkeypatch):
    # 老格式 [hostname:路径] → {路径: [编号快照]}；第二次初始化不再写盘。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545", "5000546"])
    shared.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {
                    "5000545": {
                        "number": "5000545",
                        "title": "old",
                        "source": bl.SOURCE_LEGACY,
                    }
                },
                "legacy_migrated": [f"old-host:{state}"],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bl, "_host", lambda: "new-host")
    first = bl.Fc2Blacklist(str(shared), str(state))
    data = json.loads(shared.read_text(encoding="utf-8"))
    assert data["legacy_migrated"] == {str(state): ["5000545", "5000546"]}
    assert "5000546" not in first.entries  # 老标记已完整迁过，不补迁
    stamp = shared.stat().st_mtime_ns

    again = bl.Fc2Blacklist(str(shared), str(state))
    assert again.migrate_legacy() == 0
    assert json.loads(shared.read_text(encoding="utf-8")) == data
    assert shared.stat().st_mtime_ns == stamp


def test_legacy_key_with_snapshot_is_adopted_by_path(tmp_path, monkeypatch):
    # 老键 hostname:路径 带编号快照：按路径认领，快照里的编号不重迁，缺的才补。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545", "5000546"])
    shared.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {},
                "legacy_migrated": {f"old-host:{state}": ["5000545"]},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(bl, "_host", lambda: "new-host")
    store = bl.Fc2Blacklist(str(shared), str(state))

    assert "5000545" not in store.entries
    assert "5000546" in store.entries
    data = json.loads(shared.read_text(encoding="utf-8"))
    assert data["legacy_migrated"] == {str(state): ["5000545", "5000546"]}


def test_blacklist_survives_state_reset(tmp_path):
    state = tmp_path / "fc2_state.json"
    write_state(state, ["5000545"])
    store = make_blacklist(tmp_path, state)

    state.unlink()  # fc2_state.json 被删 / 重置

    again = make_blacklist(tmp_path, state)
    assert again.contains("5000545")


def test_two_instances_do_not_overwrite_each_other(tmp_path):
    first = make_blacklist(tmp_path)
    second = make_blacklist(tmp_path)

    first.add("5000545", "a", bl.SOURCE_MUTE)
    second.add("4999999", "b", bl.SOURCE_DELETE)

    fresh = make_blacklist(tmp_path)
    assert set(fresh.entries) == {"5000545", "4999999"}
    assert first.contains("4999999")  # mtime 变了要能重读到另一边写的


def test_real_source_upgrades_legacy_source(tmp_path):
    state = tmp_path / "fc2_state.json"
    write_state(state, ["5000545"])
    store = make_blacklist(tmp_path, state)

    store.add("5000545", "", bl.SOURCE_MUTE)

    assert store.get("5000545")["source"] == bl.SOURCE_MUTE


def test_corrupted_file_does_not_raise(tmp_path):
    path = tmp_path / "fc2_blacklist.json"
    path.write_text("{not json", encoding="utf-8")

    store = bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))

    assert store.entries == {}
    assert store.add("5000545", "x", bl.SOURCE_MANUAL) is None
    assert bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json")).contains(
        "5000545"
    ) is False
    assert path.read_bytes() == b"{not json"


def test_migration_marker_is_per_container_state_file(tmp_path, monkeypatch):
    # 两个容器各有自己的 state（真实容器里路径相同，但 bind mount 不同；
    # 测试用两个目录模拟各自的 config 卷）。一份 state 只迁一次，互不挡。
    shared = tmp_path / "fc2_blacklist.json"
    torrent_state = tmp_path / "torrentino" / "fc2_state.json"
    fc2_state = tmp_path / "fc2bot" / "fc2_state.json"
    torrent_state.parent.mkdir()
    fc2_state.parent.mkdir()

    write_state(torrent_state, [])
    monkeypatch.setattr(bl, "_host", lambda: "torrentino")
    bl.Fc2Blacklist(str(shared), str(torrent_state))

    write_state(fc2_state, ["5000545"])
    monkeypatch.setattr(bl, "_host", lambda: "fc2bot")
    fc2bot = bl.Fc2Blacklist(str(shared), str(fc2_state))

    assert fc2bot.contains("5000545")


def test_migration_marker_survives_container_rebuild(tmp_path, monkeypatch):
    # 容器重建后 hostname（容器 id）变了，标记文件在 state 同目录，仍然有效。
    state = tmp_path / "config" / "fc2_state.json"
    state.parent.mkdir()
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545"])
    monkeypatch.setattr(bl, "_host", lambda: "old-container-id")

    store = bl.Fc2Blacklist(str(shared), str(state))
    assert store.contains("5000545")
    assert store.remove("5000545")
    assert (tmp_path / "config" / "fc2_state.json.fc2_blacklist_migrated").exists()

    monkeypatch.setattr(bl, "_host", lambda: "new-container-id")
    again = bl.Fc2Blacklist(str(shared), str(state))

    assert not again.contains("5000545")


def block_writes(tmp_path, name="fc2_blacklist.json"):
    """让写盘必然失败：占住 tmp 文件名（建目录），open(tmp, "w") 会 OSError。"""
    blocker = tmp_path / f"{name}.tmp.{os.getpid()}"
    blocker.mkdir()
    return blocker


def test_migration_marker_only_after_successful_save(tmp_path):
    # 共享黑名单写不进去时不能写迁移标记：否则权限恢复后编号永久不迁。
    state = tmp_path / "fc2_state.json"
    write_state(state, ["5000545"])
    blocker = block_writes(tmp_path)

    store = make_blacklist(tmp_path, state)

    assert store.entries == {}
    assert not (tmp_path / "fc2_state.json.fc2_blacklist_migrated").exists()

    blocker.rmdir()  # 恢复写权限后重来
    again = make_blacklist(tmp_path, state)

    assert again.contains("5000545")
    assert (tmp_path / "fc2_state.json.fc2_blacklist_migrated").exists()


def test_lock_permission_failure_allows_reads_but_rejects_writes(
    tmp_path, monkeypatch, caplog
):
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    write_state(state, [])
    record = make_blacklist(tmp_path, state).add("5000545", "x", bl.SOURCE_MANUAL)
    before = path.read_text(encoding="utf-8")
    original_open = open

    def denied(file, *args, **kwargs):
        if str(file).endswith(".lock"):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    # 只禁用锁文件，才能验证共享 JSON 仍可读、state 和迁移标记不受影响。
    monkeypatch.setattr("builtins.open", denied)

    store = bl.Fc2Blacklist(str(path), str(state))

    assert store.contains("5000545") is True
    assert store.add("5000546", "new", bl.SOURCE_MANUAL) is None
    assert store.contains("5000546") is False
    assert store.add("5000545", "x", bl.SOURCE_MANUAL) == record
    assert store.remove("5000545") is False
    assert store.contains("5000545") is True
    assert "5000545" not in store.unblocked
    assert path.read_text(encoding="utf-8") == before
    assert sum("锁不可用" in message for message in caplog.messages) == 1


def test_lock_permission_failure_defers_legacy_migration(tmp_path, monkeypatch):
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545"])
    original_open = open

    def denied(file, *args, **kwargs):
        if str(file).endswith(".lock"):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)

    store = bl.Fc2Blacklist(str(path), str(state))

    assert store.migrate_legacy() == 0
    assert store.contains("5000545") is False
    assert store.legacy_migrated == {}
    assert not path.exists()
    assert not (tmp_path / "fc2_state.json.fc2_blacklist_migrated").exists()

    # 权限恢复后必须还能补迁，不能留下虚假的快照或迁移标记。
    monkeypatch.setattr("builtins.open", original_open)
    assert store.migrate_legacy() == 1
    assert store.contains("5000545") is True


def test_read_failure_is_not_cached_as_empty(tmp_path, monkeypatch):
    # 共享表被另一个进程新增记录后，读权限临时没了触发一次失败；
    # 恢复权限且 mtime 不再变，驻留对象也必须重读到旧编号。
    store = make_blacklist(tmp_path)
    store.add("5000545", "x", bl.SOURCE_MUTE)
    writer = make_blacklist(tmp_path)
    writer.add("4999999", "y", bl.SOURCE_MUTE)  # mtime 变了，下一次访问会重读
    original = store._read_raw

    def denied():
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(store, "_read_raw", denied)

    # 失败时按「本次放行」处理，不抛、也不全拦
    assert store.contains("5000545") is False

    monkeypatch.setattr(store, "_read_raw", original)
    # 这次失败不能连 mtime 一起缓存成「确定为空」
    assert store.contains("5000545") is True
    assert store.contains("4999999") is True


def test_old_hostname_marker_does_not_resurrect_after_rebuild(tmp_path, monkeypatch):
    # 只有旧 hostname:路径 标记 + 已解黑 + state 还留着 muted：换 hostname 重建不能重迁。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545"])
    monkeypatch.setattr(bl, "_host", lambda: "old-container-id")
    store = bl.Fc2Blacklist(str(shared), str(state))
    assert store.remove("5000545")
    (tmp_path / "fc2_state.json.fc2_blacklist_migrated").unlink()

    monkeypatch.setattr(bl, "_host", lambda: "new-container-id")
    again = bl.Fc2Blacklist(str(shared), str(state))

    assert not again.contains("5000545")


def test_old_hostname_marker_honored_when_marker_file_unwritable(
    tmp_path, monkeypatch
):
    # config 只读、新标记文件建不出来时，旧标记照样算数，
    # 不能靠「每轮启动重试」把已解黑编号迁回来。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545"])
    monkeypatch.setattr(bl, "_host", lambda: "old-container-id")
    store = bl.Fc2Blacklist(str(shared), str(state))
    assert store.remove("5000545")
    (tmp_path / "fc2_state.json.fc2_blacklist_migrated").unlink()
    block_writes(tmp_path, "fc2_state.json.fc2_blacklist_migrated")

    monkeypatch.setattr(bl, "_host", lambda: "new-container-id")
    again = bl.Fc2Blacklist(str(shared), str(state))

    assert not again.contains("5000545")


def test_old_hostname_marker_is_adopted_without_remigrating(tmp_path, monkeypatch):
    # 升级时老版本已经按 hostname:路径 迁过：只补标记文件，不能把已解黑编号重迁回来。
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545"])
    monkeypatch.setattr(bl, "_host", lambda: "same-host")
    shared.write_text(
        json.dumps(
            {
                "version": 1,
                "entries": {},
                "legacy_migrated": [bl.state_key(str(state))],
            }
        ),
        encoding="utf-8",
    )

    store = bl.Fc2Blacklist(str(shared), str(state))

    assert not store.contains("5000545")
    assert (tmp_path / "fc2_state.json.fc2_blacklist_migrated").exists()


def test_corrupted_file_is_backed_up_and_write_is_refused(tmp_path):
    path = tmp_path / "fc2_blacklist.json"
    good = {
        "version": 1,
        "entries": {"5000545": {"number": "5000545", "title": "old"}},
    }
    path.write_text(json.dumps(good) + "\n{oops", encoding="utf-8")
    before = path.read_bytes()

    store = bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))
    assert store.add("5000550", "new", bl.SOURCE_MANUAL) is None

    fresh = bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))
    assert fresh.entries == {}
    assert fresh.read_ok() is False
    assert path.read_bytes() == before
    backups = list(tmp_path.glob("fc2_blacklist.json.corrupt-*"))
    assert len(backups) == 2
    assert all(backup.read_bytes() == before for backup in backups)


def test_corrupt_backup_uses_bytes_read_not_second_read(tmp_path):
    # 双进程：A 读到坏内容后 B 写回新表，A 再备份时不能把新表当成坏文件备份。
    path = tmp_path / "fc2_blacklist.json"
    bad = (
        b'{"version":1,"entries":{"5000545":{"number":"5000545",'
        b'"title":"old"}}}\n{oops'
    )
    good = json.dumps(
        {"version": 1, "entries": {"5000550": {"number": "5000550"}}}
    ).encode("utf-8")
    path.write_bytes(bad)
    original = bl.Fc2Blacklist._read_raw

    class Racing(bl.Fc2Blacklist):
        def _read_raw(self):
            result = original(self)
            path.write_bytes(good)  # 另一个进程在 A 备份前写回了新表
            return result

    Racing(str(path), str(tmp_path / "fc2_state.json"))

    backups = list(tmp_path.glob("fc2_blacklist.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == bad


def test_read_snapshot_keeps_mtime_matching_old_content(tmp_path, monkeypatch):
    # A 读旧内容后 B 新增编号：A 不能把新 mtime 记到旧内容上，否则之后
    # contains() 一直 False，磁盘上明明有。
    path = tmp_path / "fc2_blacklist.json"
    writer = bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))
    writer.add("4999999", "old", bl.SOURCE_MANUAL)
    reader = bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))

    original = reader._read_raw
    raced = []

    def racing_read():
        before, raw, after = original()
        if not raced:
            raced.append(True)
            writer.add("5000545", "new", bl.SOURCE_MANUAL)
        return before, raw, after

    monkeypatch.setattr(reader, "_read_raw", racing_read)

    reader.load()
    assert reader.contains("5000545")


def test_two_containers_same_state_path_each_migrate_own_muted(tmp_path, monkeypatch):
    # 真实部署里两个容器的 state 绝对路径都是 /usr/src/app/config/fc2_state.json，
    # 只是挂在各自的 config 卷上。测试用同一路径 + 逐次换卷内容模拟：
    # 每次「换容器」都清掉上一份卷里的标记文件。
    config = tmp_path / "config"
    config.mkdir()
    state = config / "fc2_state.json"
    marker = config / "fc2_state.json.fc2_blacklist_migrated"
    shared = tmp_path / "shared" / "fc2_blacklist.json"

    # A 容器：先启动，只有自己独有的 muted（sol 复现第 2 步之前的状态）
    write_state(state, ["5000400"])
    monkeypatch.setattr(bl, "_host", lambda: "container-a")
    a = bl.Fc2Blacklist(str(shared), str(state))
    assert a.contains("5000400")
    marker.unlink()  # A 的标记文件在 A 自己的 config 卷里，B 看不到
    # 老版本按 hostname:路径 在共享表里留下的 A 标记（sol 复现第 2 步）：
    # 新快照还在，老键只是残留，不能拿它把 B 的编号一起认领掉。
    seeded = json.loads(shared.read_text(encoding="utf-8"))
    seeded["legacy_migrated"][f"container-a:{state}"] = "claimed"
    shared.write_text(json.dumps(seeded), encoding="utf-8")

    # B 容器：同一个容器内路径，另一份 config 卷，自己的独有编号
    write_state(state, ["5000545"])
    monkeypatch.setattr(bl, "_host", lambda: "container-b")
    b = bl.Fc2Blacklist(str(shared), str(state))

    # 不能按路径认领 A 的标记而跳过迁移：B 的编号必须补进共享黑名单。
    assert b.contains("5000545")
    assert b.contains("5000400")
    fresh = bl.Fc2Blacklist(str(shared), str(tmp_path / "no_state.json"))
    assert set(fresh.entries) == {"5000400", "5000545"}
    pusher = fc2.Fc2Pusher(object(), config=object(), store=object(), blacklist=fresh)
    assert pusher._blocked("5000545") is True
    assert pusher._blocked("5000400") is True


def test_unblocked_number_never_resurrects_on_rebuild_or_readonly(tmp_path, monkeypatch):
    # /fc2unblock 之后三条路径都不能复活该编号：换 hostname 重建、
    # container 重建（标记文件丢了）、config 只读（标记写不出来）。
    for case in ("hostname-rebuild", "container-rebuild", "readonly-config"):
        root = tmp_path / case
        config = root / "config"
        config.mkdir(parents=True)
        state = config / "fc2_state.json"
        marker = config / "fc2_state.json.fc2_blacklist_migrated"
        shared = root / "fc2_blacklist.json"
        write_state(state, ["5000545"])

        monkeypatch.setattr(bl, "_host", lambda: "old-container-id")
        store = bl.Fc2Blacklist(str(shared), str(state))
        assert store.contains("5000545")
        assert store.remove("5000545")

        if case != "hostname-rebuild":
            marker.unlink(missing_ok=True)  # 标记文件随旧容器/旧卷一起没了
        if case == "readonly-config":
            block_writes(config, "fc2_state.json.fc2_blacklist_migrated")

        monkeypatch.setattr(bl, "_host", lambda: "new-container-id")
        again = bl.Fc2Blacklist(str(shared), str(state))

        assert again.contains("5000545") is False, case
        assert again.migrate_legacy() == 0, case


def test_migration_is_idempotent_on_repeated_init(tmp_path, monkeypatch):
    state = tmp_path / "fc2_state.json"
    shared = tmp_path / "fc2_blacklist.json"
    write_state(state, ["5000545", "05000546"], {"5000545": {"title": "t"}})
    monkeypatch.setattr(bl, "_host", lambda: "container-a")

    first = bl.Fc2Blacklist(str(shared), str(state))
    assert set(first.entries) == {"5000545", "5000546"}
    stamp = json.loads(shared.read_text(encoding="utf-8"))["entries"]["5000545"][
        "added_ts"
    ]

    for _ in range(3):
        again = bl.Fc2Blacklist(str(shared), str(state))
        assert again.migrate_legacy() == 0
        assert set(again.entries) == {"5000545", "5000546"}

    data = json.loads(shared.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 2
    assert data["entries"]["5000545"]["added_ts"] == stamp  # 不重复迁移、不重写时间
    assert data["entries"]["5000545"]["source"] == bl.SOURCE_LEGACY


def test_reblock_after_unblock_clears_tombstone(tmp_path):
    # 墓碑只挡「迁移」：用户明确重新拉黑要能生效，之后迁移也不会把他当已解黑跳过。
    state = tmp_path / "fc2_state.json"
    write_state(state, ["5000545"])
    store = make_blacklist(tmp_path, state)
    assert store.remove("5000545")
    assert store.unblocked == {"5000545"}

    store.add("5000545", "重新拉黑", bl.SOURCE_MUTE)

    fresh = make_blacklist(tmp_path, state)
    assert fresh.unblocked == set()
    assert fresh.contains("5000545")
    assert fresh.get("5000545")["source"] == bl.SOURCE_MUTE


def test_number_and_source_helpers():
    assert bl.normalize_number("05000545") == "5000545"
    assert bl.normalize_number("abc") is None
    assert bl.normalize_number("0") is None
    assert bl.source_label(bl.SOURCE_MUTE) == "不感兴趣"
    assert bl.source_label(bl.SOURCE_LEGACY) == "旧 muted 迁移"
    assert bl.source_label("custom") == "custom"


def test_read_failure_is_distinguishable_from_empty(tmp_path, monkeypatch):
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    write_state(state, [])
    make_blacklist(tmp_path).add("5000545", "x", bl.SOURCE_MANUAL)
    before = path.read_text(encoding="utf-8")
    original_open = open

    def denied(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    # 只拒共享黑名单 JSON，state / 迁移标记 / .lock 仍可读写。
    monkeypatch.setattr("builtins.open", denied)

    store = bl.Fc2Blacklist(str(path), str(state))

    assert store.read_ok() is False
    assert store.contains("5000545") is False

    monkeypatch.setattr("builtins.open", original_open)
    reloaded = bl.Fc2Blacklist(str(path), str(state))

    assert reloaded.read_ok() is True
    assert reloaded.contains("5000545") is True
    assert path.read_text(encoding="utf-8") == before


def test_add_read_failure_reflected_in_state(tmp_path, monkeypatch):
    # sol 复现：命令内实例构造时读盘成功，add() 期间读盘失败；
    # 不能留下旧的 _read_ok=True / 旧 mtime，否则调用方误判「条目本来就在」。
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    original_open = open
    deny = {"on": False}

    def denied(file, *args, **kwargs):
        if deny["on"] and str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    store = make_blacklist(tmp_path, state)
    store.add("5000545", "", bl.SOURCE_MANUAL)

    monkeypatch.setattr("builtins.open", denied)
    deny["on"] = True  # 只拒共享黑名单 JSON，state / .lock / 迁移标记都可读写

    assert store.add("5000545", "新标题", bl.SOURCE_MANUAL) is None
    assert store.read_ok() is False
    record, ok = store.lookup("5000545")
    assert record is None
    assert ok is False

    monkeypatch.setattr("builtins.open", original_open)
    assert make_blacklist(tmp_path, state).get("5000545")["title"] == ""


def test_lookup_entry_and_read_state_consistent(tmp_path, monkeypatch):
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    store = make_blacklist(tmp_path, state)
    record = store.add("5000545", "x", bl.SOURCE_MANUAL)

    assert store.lookup("5000545") == (record, True)
    assert store.lookup("5000546") == (None, True)

    original_open = open

    def denied(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", denied)
    broken = bl.Fc2Blacklist(str(path), str(state))

    # 同一次刷新内：条目不在了，同时必须报「这次读失败」。
    assert broken.lookup("5000545") == (None, False)

    # 同一个驻留实例：mtime 变了且这次读失败，也不能拿上一次的 _read_ok=True
    # 报「确实不在名单」——lookup 必须自己触发 refresh。
    stamp = path.stat().st_mtime_ns
    os.utime(path, ns=(stamp, stamp + 1_000_000))

    assert store.lookup("5000545") == (None, False)


def test_add_read_failure_does_not_leave_phantom_entry(tmp_path, monkeypatch):
    # BLOCK 1：add() 期间读盘失败时，被 add 改过的条目磁盘上并不存在，
    # 内存里不能留下幽灵条目（不先 lookup() 也要如实为空）。
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    original_open = open
    deny = {"on": False}

    def denied(file, *args, **kwargs):
        if deny["on"] and str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    store = make_blacklist(tmp_path, state)
    store.add("5000545", "", bl.SOURCE_MANUAL)

    monkeypatch.setattr("builtins.open", denied)
    deny["on"] = True  # 只拒共享黑名单 JSON，state / .lock / 迁移标记都可读写

    assert store.add("5000546", "新标题", bl.SOURCE_MANUAL) is None
    assert store.read_ok() is False
    # 还没触发 refresh 就直接看内存：磁盘上没有的条目不能留在 entries 里。
    assert "5000546" not in store.entries
    assert store.contains("5000546") is False

    monkeypatch.setattr("builtins.open", original_open)
    assert make_blacklist(tmp_path, state).contains("5000546") is False


def test_failed_read_forces_reread_even_with_zero_mtime(tmp_path, monkeypatch):
    # BLOCK 2：读失败的 _mtime 是 0，文件真实 mtime 也是 0 时，
    # 不能靠 mtime 判断「没变」，必须靠显式 stale 标记强制重读。
    state = tmp_path / "fc2_state.json"
    path = tmp_path / "fc2_blacklist.json"
    store = make_blacklist(tmp_path, state)
    store.add("5000545", "", bl.SOURCE_MANUAL)
    os.utime(path, ns=(0, 0))
    store.load()
    assert store._mtime == 0

    original_open = open
    original_read_raw = store._read_raw
    calls = {"n": 0}
    deny = {"on": False}

    def counting_read_raw():
        calls["n"] += 1
        return original_read_raw()

    def denied(file, *args, **kwargs):
        if deny["on"] and str(file) == str(path):
            raise PermissionError(13, "Permission denied", str(file))
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(store, "_read_raw", counting_read_raw)
    monkeypatch.setattr("builtins.open", denied)
    deny["on"] = True

    assert store.add("5000545", "新标题", bl.SOURCE_MANUAL) is None
    assert calls["n"] >= 1

    monkeypatch.setattr("builtins.open", original_open)
    before = calls["n"]
    record, ok = store.lookup("5000545")

    assert calls["n"] > before  # 确实重读过，没被 mtime=0 卡住
    assert ok is True
    assert record is not None
    assert record["title"] == ""  # 磁盘上的真实内容，不是 add 前被改过的内存
    assert store.contains("5000545") is True


def test_blacklist_startup_logs_absolute_shared_path_without_secrets(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(bl, "BOT_FOLDER", str(tmp_path))
    values = {
        "fc2.blacklist_file": "shared/fc2_blacklist.json",
        "fc2.enabled": True,
        "bot.token": "SECRET",
        "bot.super_user": 987654321,
    }
    config = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    with caplog.at_level(logging.INFO):
        bl.log_startup_path(config)

    assert f"FC2 黑名单实际路径 blacklist={tmp_path / 'shared' / 'fc2_blacklist.json'}" in caplog.text
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert "SECRET" not in caplog.text
    assert "987654321" not in caplog.text
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("values", "warning_expected"),
    [
        pytest.param({"fc2.enabled": True, "notify.adopt_fc2": False}, True, id="push"),
        pytest.param({"fc2.enabled": False, "notify.adopt_fc2": True}, True, id="adopt"),
        pytest.param({"fc2.enabled": False}, True, id="default-adoption"),
        pytest.param({"fc2.enabled": False, "notify.adopt_fc2": False}, False, id="disabled"),
    ],
)
def test_blacklist_startup_warns_only_for_enabled_default_private_path(
    tmp_path, monkeypatch, caplog, values, warning_expected
):
    default = str(tmp_path / "config" / "fc2_blacklist.json")
    monkeypatch.setattr(bl, "default_path", lambda: default)
    config = SimpleNamespace(get=lambda key, default=None: values.get(key, default))
    with caplog.at_level(logging.INFO):
        bl.log_startup_path(config)

    assert f"blacklist={default}" in caplog.text
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert bool(warnings) is warning_expected
    if warning_expected:
        assert "缺省私有路径" in warnings[0]
        assert "stage=startup_path" in warnings[0]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("payload", "exception"),
    [
        pytest.param(b'{not-json SECRET 987654321', "JSONDecodeError", id="bad-json"),
        pytest.param(b'["SECRET", 987654321]', "ValueError", id="not-object"),
        pytest.param(b'\xffSECRET 987654321', "UnicodeDecodeError", id="bad-utf8"),
    ],
)
def test_corrupt_json_refuses_add_preserves_bytes_and_logs_error(
    tmp_path, caplog, payload, exception
):
    path = tmp_path / "fc2_blacklist.json"
    path.write_bytes(payload)
    with caplog.at_level(logging.ERROR):
        store = bl.Fc2Blacklist(str(path), str(tmp_path / "fc2_state.json"))
        assert store._read()["_ok"] is False
        assert store.contains("4972315") is False
        assert store.lookup("4972315") == (None, False)
        assert store.add("4972315", "FC2-PPV-4972315", bl.SOURCE_MUTE) is None
        assert store.read_ok() is False
        assert store.entries == {}

    assert path.read_bytes() == payload
    backups = list(tmp_path.glob("fc2_blacklist.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == payload
    assert "只读放行、拒绝写入" in caplog.text
    assert f"stage=blacklist_parse exception={exception}" in caplog.text
    assert "SECRET" not in caplog.text
    assert "987654321" not in caplog.text


def test_corrupt_json_refuses_legacy_migration_and_recovers_after_manual_repair(tmp_path):
    path = tmp_path / "fc2_blacklist.json"
    state = tmp_path / "fc2_state.json"
    payload = b"{not-json"
    path.write_bytes(payload)
    write_state(state, ["4972315"])

    store = bl.Fc2Blacklist(str(path), str(state))

    assert store.migrate_legacy() == 0
    assert store.add("1698878", "sample") is None
    assert path.read_bytes() == payload
    assert not (tmp_path / "fc2_state.json.fc2_blacklist_migrated").exists()
    path.write_text(json.dumps({"version": 1, "entries": {}}), encoding="utf-8")
    assert store.contains("4972315") is False
    assert store.read_ok() is True
    assert store.add("1698878", "sample") is not None
    assert store.contains("1698878") is True
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]["1698878"]["title"] == "sample"
