from typing import Union, Any, BinaryIO
from telegram import Bot
from typing_extensions import Literal
from transmission_rpc.client import Client
from transmission_rpc.torrent import Torrent

from lib.func import get_logger

from models.DownloadQueue import DownloadQueue, QueueEntry

log = get_logger("TransmissionClient")


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
        **kwargs: Any,
    ) -> Torrent:
        """添加任务并把通知信息写入下载队列。"""
        if download_dir:
            kwargs["download_dir"] = download_dir
        _torrent = super().add_torrent(torrent, **kwargs)

        DownloadQueue.add(
            QueueEntry(
                torrent_id=_torrent.id,
                chat_id=int(chat_id),
                lang_code=lang_code,
                title=title or _torrent.name or "",
                requested_dir=str(download_dir or ""),
                info_hash=str(getattr(_torrent, "info_hash", "") or "").lower()
                or None,
                auto_classify=auto_classify,
                message_id=message_id,
                final_dir=str(download_dir) if download_dir else None,
            )
        )
        return _torrent

    def existing_hashes(self) -> dict[str, Torrent]:
        """返回 info_hash → 任务 的映射，用于批量查重。"""
        existing: dict[str, Torrent] = {}
        try:
            for torrent in self.get_torrents():
                existing[str(torrent.info_hash).lower()] = torrent
        except Exception as err:  # noqa: BLE001
            log.warning("读取任务列表失败，跳过查重: %s", err)
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
