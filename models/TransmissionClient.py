import hashlib
from pathlib import Path
from typing import Union, Any, BinaryIO
from telegram import Bot
from typing_extensions import Literal
from transmission_rpc.client import Client
from transmission_rpc.torrent import Torrent

from lib.func import get_logger
from lib.magnet import parse_magnet

from models.DownloadQueue import DownloadQueue, QueueEntry

log = get_logger("TransmissionClient")


class TorrentAlreadyExists(Exception):
    pass


class TransmissionClient(Client):
    """
    Features on top of transmission_rpc Client:
    - stop/start all torrents at once
    - get detailed information about torrent
    - notify user on torrent download done / progress
    - detect duplicated torrents by info hash
    """

    def __init__(
        self,
        *,
        protocol: Literal["http", "https"] = "http",
        username: str | None = None,
        password: str | None = None,
        host: str = "127.0.0.1",
        port: int = 9091,
        path: str = "/transmission/",
        telegram_token: str | None = None,
    ):
        self.telegram_token = telegram_token
        super().__init__(
            protocol=protocol,
            username=username,
            password=password,
            host=host,
            port=port,
            path=path,
        )

    async def monitor_downloads(self, bot: Bot) -> None:
        """兼容旧入口：交给 DownloadMonitor 处理。"""
        from models.DownloadMonitor import DownloadMonitor

        await DownloadMonitor(self, bot).run()

    def add_torrent(  # type: ignore[override]
        self,
        chat_id,
        lang_code,
        torrent: Union[BinaryIO, str],
        download_dir: str | None = None,
        title: str | None = None,
        auto_classify: bool = False,
        message_id: int | None = None,
        single_dedup: bool = False,
        notify: bool = True,
        **kwargs: Any,
    ) -> Torrent:
        """添加任务并把通知信息写入下载队列。

        notify=False 时只往 tr-bt 加任务、不写通知队列（由别的机器人
        负责发现并推送）；single_dedup 的前后两次查重语义保持不变。
        """
        if download_dir:
            kwargs["download_dir"] = download_dir
        existing: dict[str, Torrent] = {}
        if single_dedup:
            existing = {
                str(info_hash).lower(): item
                for info_hash, item in self.existing_hashes().items()
            }
            known_hashes = set(existing)
            known_hashes.update(
                str(item.info_hash).lower()
                for item in DownloadQueue.snapshot().values()
                if item.info_hash
            )
            source_hash = ""
            if isinstance(torrent, Path):
                source_hash = self.torrent_file_hash(torrent).lower()
            elif isinstance(torrent, str):
                magnet = parse_magnet(torrent)
                source_hash = str(getattr(magnet, "info_hash", "") or "").lower()
            if source_hash and source_hash in known_hashes:
                raise TorrentAlreadyExists(source_hash)
        _torrent = super().add_torrent(torrent, **kwargs)
        actual_hash = str(getattr(_torrent, "info_hash", "") or "").lower()

        # 查重要在决定是否写队列之前完成：notify=False 也必须按 tr-bt
        # 实际 hash 判重，重复照样抛 TorrentAlreadyExists。
        if single_dedup and actual_hash and actual_hash in existing:
            raise TorrentAlreadyExists(actual_hash)
        if not notify:
            return _torrent

        entry = QueueEntry(
            torrent_id=_torrent.id,
            chat_id=int(chat_id),
            lang_code=lang_code,
            title=title or _torrent.name or "",
            requested_dir=str(download_dir or ""),
            info_hash=actual_hash or None,
            auto_classify=auto_classify,
            message_id=message_id,
            final_dir=str(download_dir) if download_dir else None,
        )
        if single_dedup:
            if not DownloadQueue.add_if_new(entry):
                raise TorrentAlreadyExists(actual_hash)
        else:
            DownloadQueue.add(entry)
        return _torrent

    @staticmethod
    def torrent_file_hash(path: Path) -> str:
        """批量查重专用：对种子文件原始 info 字典求 SHA-1，不重编码。"""
        data = path.read_bytes()

        def read_string(start: int) -> tuple[bytes, int]:
            colon = data.index(b":", start)
            length = data[start:colon]
            if not length.isdigit():
                raise ValueError("Invalid torrent metainfo string")
            end = colon + 1 + int(length)
            if end > len(data):
                raise ValueError("Truncated torrent metainfo string")
            return data[colon + 1 : end], end

        def skip_value(start: int) -> int:
            token = data[start : start + 1]
            if token in (b"d", b"l"):
                end = start + 1
                while data[end : end + 1] != b"e":
                    end = skip_value(end)
                return end + 1
            if token == b"i":
                end = data.index(b"e", start + 1)
                int(data[start + 1 : end])
                return end + 1
            if token and token.isdigit():
                return read_string(start)[1]
            raise ValueError("Invalid torrent metainfo value")

        if data[:1] != b"d":
            raise ValueError("Torrent metainfo must be a dictionary")
        cursor = 1
        while data[cursor : cursor + 1] != b"e":
            key, start = read_string(cursor)
            end = skip_value(start)
            if key == b"info":
                if data[start : start + 1] != b"d":
                    raise ValueError("Torrent info must be a dictionary")
                return hashlib.sha1(data[start:end]).hexdigest()
            cursor = end
        raise ValueError("Torrent metainfo has no info dictionary")

    def existing_hashes(self) -> dict[str, Torrent]:
        """返回 info_hash → 任务 的映射；读取失败上抛，不能当作空库。"""
        existing: dict[str, Torrent] = {}
        for torrent in self.get_torrents():
            existing[str(torrent.info_hash).lower()] = torrent
        return existing

    def stop_all(self) -> None:
        """Stop all torrent on server"""
        for torrent in self.get_torrents():
            self.stop_torrent(torrent.id)
            log.info("Stopped torrent %s (id: %s)", torrent.id, torrent.name)

    def start_all(self) -> None:
        """Start all torrent on server"""
        for torrent in self.get_torrents():
            self.start_torrent(torrent.id)
            log.info("Started torrent %s (id: %s)", torrent.id, torrent.name)

    def status(self, torrent_id):
        return self.get_torrent(int(torrent_id)).status

    def overview(self) -> dict[str, Any]:
        """汇总服务器状态，供 /status 命令使用。"""
        torrents = self.get_torrents()
        downloading = [t for t in torrents if t.status == "downloading"]
        seeding = [t for t in torrents if t.status == "seeding"]
        stopped = [t for t in torrents if t.status == "stopped"]
        stats = self.session_stats()
        return {
            "total": len(torrents),
            "downloading": len(downloading),
            "seeding": len(seeding),
            "stopped": len(stopped),
            "speed_down": sum(int(t.rate_download or 0) for t in torrents),
            "speed_up": sum(int(t.rate_upload or 0) for t in torrents),
            "session_down": int(stats.current_stats.downloaded_bytes),
            "session_up": int(stats.current_stats.uploaded_bytes),
            "total_down": int(stats.cumulative_stats.downloaded_bytes),
            "total_up": int(stats.cumulative_stats.uploaded_bytes),
            "torrents": torrents,
        }

    def set_speed_limit(self, down_kbps: int | None, up_kbps: int | None) -> None:
        """设置全局限速，None 表示取消限速。"""
        if down_kbps is None and up_kbps is None:
            self.set_session(
                speed_limit_down_enabled=False, speed_limit_up_enabled=False
            )
            return
        kwargs: dict[str, Any] = {}
        if down_kbps is not None:
            kwargs["speed_limit_down"] = int(down_kbps)
            kwargs["speed_limit_down_enabled"] = int(down_kbps) > 0
        if up_kbps is not None:
            kwargs["speed_limit_up"] = int(up_kbps)
            kwargs["speed_limit_up_enabled"] = int(up_kbps) > 0
        self.set_session(**kwargs)
