import json
import multiprocessing
import os
from types import SimpleNamespace

import pytest

from models.DoneList import WRITE_ADDED, WRITE_DUPLICATE, WRITE_FAILED
from models.Fc2Claims import Fc2Claims, config_path


def make_config(tmp_path, **values):
    config = SimpleNamespace(config_file=str(tmp_path / "torrentino.yaml"))
    config.get = lambda key, default=None: values.get(key, default)
    return config


@pytest.mark.parametrize(
    ("values", "directory"),
    [
        ({"fc2.done_file": "/shared/fc2_done.jsonl"}, "/shared"),
        ({"fc2.blacklist_file": "/shared/fc2_blacklist.json"}, "/shared"),
        (
            {
                "fc2.done_file": "/shared/done/fc2_done.jsonl",
                "fc2.blacklist_file": "/shared/black/fc2_blacklist.json",
            },
            "/shared/done",
        ),
        ({"fc2.done_file": "<done_file>"}, None),
        ({}, None),
    ],
)
def test_claim_path_derives_existing_shared_directory(tmp_path, values, directory):
    config = make_config(tmp_path, **values)

    assert config_path(config) == os.path.join(
        directory or str(tmp_path), "fc2_claims.jsonl"
    )


def test_claim_append_dedupes_and_merges_only_hash_number_title(tmp_path):
    path = tmp_path / "fc2_claims.jsonl"
    claims = Fc2Claims(str(path))
    key = "a" * 40

    assert claims.append(key.upper()) == WRITE_ADDED
    assert claims.append(key, "04972315", "FC2-PPV-4972315 title") == WRITE_ADDED
    assert claims.append(key, None, "") == WRITE_DUPLICATE
    assert claims.find(key.upper()) == {
        "info_hash": key,
        "number": "4972315",
        "title": "FC2-PPV-4972315 title",
    }
    assert claims.find("b" * 40) is None
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    assert all(set(row) == {"info_hash", "number", "title"} for row in rows)
    assert Fc2Claims(str(path)).find(key) == claims.find(key)


def test_claim_title_never_persists_raw_magnet_tracker_secrets(tmp_path):
    path = tmp_path / "fc2_claims.jsonl"
    key = "c" * 40
    raw = f"magnet:?xt=urn:btih:{key}&dn=sample&tr=https://tracker.invalid/private-key"
    claims = Fc2Claims(str(path))

    assert claims.append(key, title=raw) == WRITE_ADDED
    record = claims.find(key)
    assert record is not None
    assert record["title"] == "sample"
    assert "private-key" not in path.read_text(encoding="utf-8")
    assert "tracker.invalid" not in path.read_text(encoding="utf-8")
    assert claims.append("d" * 40, title="https://tracker.invalid/private-key") == WRITE_ADDED
    record = claims.find("d" * 40)
    assert record is not None
    assert record["title"] == ""


def test_claim_missing_hash_never_creates_file(tmp_path):
    path = tmp_path / "fc2_claims.jsonl"
    claims = Fc2Claims(str(path))

    assert claims.append(None, "4972315", "sample") == WRITE_FAILED
    assert claims.find(None) is None
    assert not path.exists()


def _append_process_claims(path, prefix):
    claims = Fc2Claims(path)
    for index in range(12):
        assert claims.append(f"{prefix:02x}{index:038x}", title="sample") == WRITE_ADDED
        assert claims.append("f" * 40, "4972315", "FC2-PPV-4972315") in (
            WRITE_ADDED,
            WRITE_DUPLICATE,
        )


def test_claim_append_flock_and_append_preserve_concurrent_processes(tmp_path):
    path = tmp_path / "fc2_claims.jsonl"
    context = multiprocessing.get_context("fork")
    processes = [
        context.Process(target=_append_process_claims, args=(str(path), index))
        for index in (1, 2)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    lines = path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 25
    assert len({row["info_hash"] for row in rows}) == 25
    assert sum(row["info_hash"] == "f" * 40 for row in rows) == 1
    record = Fc2Claims(str(path)).find("f" * 40)
    assert record is not None
    assert record["number"] == "4972315"
