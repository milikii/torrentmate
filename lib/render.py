"""下载进度卡片渲染，处理器与监控协程共用同一套模板。"""

from __future__ import annotations

from transmission_rpc.torrent import Torrent

from lib.func import (
    bytes_to_human,
    escape_html,
    human_duration,
    progress_bar,
    speed_to_human,
    trans,
)


def _eta_seconds(torrent: Torrent) -> float | None:
    try:
        eta = torrent.eta
        if eta is None:
            return None
        seconds = eta.total_seconds()
        return seconds if seconds > 0 else None
    except Exception:  # noqa: BLE001
        return None


def status_text(torrent: Torrent, lang_code: str | None) -> str:
    return trans(f"status.{torrent.status}", lang_code)


def render_progress_card(
    torrent: Torrent,
    lang_code: str | None,
    directory: str | None = None,
    title: str | None = None,
) -> str:
    name = escape_html(torrent.name or title or str(torrent.info_hash)[:12])
    lines = [trans("CARD_TITLE", lang_code).format(name)]

    head = trans("CARD_STATUS", lang_code).format(status_text(torrent, lang_code))
    if directory:
        head += " ｜ " + trans("CARD_DIR_INLINE", lang_code).format(
            escape_html(directory)
        )
    lines.append(head)

    # 与 DownloadMonitor 一致：范围是 0~1，不是 0~100。
    if float(torrent.metadata_percent_complete or 0) < 1:
        lines.append(trans("CARD_METADATA", lang_code))
        return "\n".join(lines)

    percent = float(torrent.percent_done) * 100
    lines.append(
        trans("CARD_PROGRESS", lang_code).format(
            progress_bar(percent), f"{percent:.1f}%"
        )
    )

    total = int(torrent.total_size or 0)
    left = int(torrent.left_until_done or 0)
    done = max(total - left, 0)
    lines.append(
        trans("CARD_SIZE", lang_code).format(
            bytes_to_human(done), bytes_to_human(total)
        )
    )

    if torrent.rate_download or torrent.rate_upload:
        lines.append(
            trans("CARD_SPEED", lang_code).format(
                speed_to_human(torrent.rate_download),
                speed_to_human(torrent.rate_upload),
            )
        )

    lines.append(
        trans("CARD_PEERS", lang_code).format(
            torrent.peers_connected, f"{float(torrent.ratio or 0):.2f}"
        )
    )

    eta = _eta_seconds(torrent)
    if eta and percent < 100:
        lines.append(trans("CARD_ETA", lang_code).format(human_duration(eta)))

    return "\n".join(lines)


def render_done_card(
    torrent: Torrent,
    lang_code: str | None,
    directory: str | None = None,
    elapsed: float | None = None,
) -> str:
    name = escape_html(torrent.name)
    total = int(torrent.size_when_done or torrent.total_size or 0)
    lines = [
        trans("DONE_TITLE", lang_code),
        trans("CARD_TITLE", lang_code).format(name),
    ]
    if directory or torrent.download_dir:
        lines.append(
            trans("DONE_DIR", lang_code).format(
                escape_html(directory or torrent.download_dir)
            )
        )
    lines.append(trans("DONE_SIZE", lang_code).format(bytes_to_human(total)))
    if elapsed and elapsed > 0:
        lines.append(
            trans("DONE_SPEED", lang_code).format(
                human_duration(elapsed), speed_to_human(total / elapsed)
            )
        )
    lines.append(
        trans("DONE_RATIO", lang_code).format(f"{float(torrent.ratio or 0):.2f}")
    )
    return "\n".join(lines)
