#!/usr/bin/env python3
"""一次性历史迁移：把两个机器人的 download.log 补进共享成功名单。

只读日志、只往 done_file 追加；不联网、不读 token、不改机器人配置和队列。
去重完全交给 DoneList.append：有编号按「编号 + 完成时间」，无编号按
「标题 + 完成时间」，已经写过的不重复写，所以脚本可以反复跑。

用法（部署时在 NAS 上跑，两个容器各传一个日志路径）::

    python tools/migrate_done_history.py \\
        --done-file /usr/src/app/shared/fc2_done.jsonl \\
        /path/to/torrentino/download.log /path/to/fc2bot/download.log

先看看会写多少条、不落盘::

    python tools/migrate_done_history.py --dry-run --done-file ... log1 log2
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# 直接 `python tools/migrate_done_history.py` 时脚本目录是 sys.path[0]，
# 仓库根不在路径里；补上自己的仓库根，不依赖调用时的 cwd。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.func import get_logger  # noqa: E402
from models.DoneList import (  # noqa: E402
    SOURCE_MIGRATION,
    WRITE_ADDED,
    WRITE_FAILED,
    DoneList,
)
from models.Fc2Channel import extract_number  # noqa: E402

log = get_logger("migrate_done_history")


def read_log(path: str) -> list[dict]:
    """按 DownloadHistory.show 的口径读日志：JSONL 为主，兼容老式逗号分隔。"""
    records: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                fields = [field.strip() for field in line.rsplit(",", 3)]
                if len(fields) == 4:
                    data = {
                        "date_done": fields[0],
                        "name": fields[1],
                        "download_dir": fields[2],
                        "size_when_done": fields[3],
                    }
                else:
                    log.warning("%s 第 %s 行无法解析，已跳过", path, line_number)
                    continue
            if not isinstance(data, dict):
                log.warning("%s 第 %s 行不是对象，已跳过", path, line_number)
                continue
            records.append(data)
    return records


def migrate(log_paths: list[str], done_file: str, dry_run: bool = False) -> dict:
    """把日志里的完成记录补进成功名单，返回统计。"""
    done = DoneList(done_file)
    stats = {
        "read": 0,
        "added": 0,
        "duplicate": 0,
        "failed": 0,
        "skipped": 0,
    }
    for path in log_paths:
        if not os.path.isfile(path):
            log.warning("日志不存在，跳过: %s", path)
            stats["skipped"] += 1
            continue
        records = read_log(path)
        stats["read"] += len(records)
        for record in records:
            name = str(record.get("name") or "").strip()
            done_at = record.get("date_done")
            if not name or done_at in (None, ""):
                stats["skipped"] += 1
                continue
            result = done.append(
                number=extract_number(name),
                title=name,
                info_hash=None,
                done_at=done_at,
                size_when_done=record.get("size_when_done") or 0,
                source=SOURCE_MIGRATION,
                dry_run=dry_run,
            )
            if result == WRITE_ADDED:
                stats["added"] += 1
            elif result == WRITE_FAILED:
                stats["failed"] += 1
            else:
                stats["duplicate"] += 1
    return stats


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="把 download.log 的完成记录一次性迁移进共享成功名单（只追加、不联网）"
    )
    parser.add_argument(
        "logs",
        nargs="+",
        help="download.log 路径（两个容器各一份就传两个）",
    )
    parser.add_argument(
        "--done-file",
        required=True,
        help="共享成功名单路径（fc2.done_file 指向的那个文件）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计，不写入",
    )
    args = parser.parse_args(argv)

    stats = migrate(args.logs, args.done_file, dry_run=args.dry_run)
    prefix = "（dry-run，未写入）" if args.dry_run else ""
    print(
        "读取 {read} 条，新增 {added} 条，已存在 {duplicate} 条，"
        "失败 {failed} 条，跳过 {skipped} 条{prefix}。目标：{target}".format(
            prefix=prefix, target=args.done_file, **stats
        )
    )
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
