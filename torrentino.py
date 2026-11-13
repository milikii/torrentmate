#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Usage:
Press Ctrl-C on the command line or send a signal to the process to stop the
bot.
"""

import asyncio
import os
import sys
from contextlib import suppress
from telegram.ext import Application
from telegram import Update
from telegram.error import InvalidToken
from telegram.request import HTTPXRequest
from models.BotConfigurator import BotConfigurator
from models.DownloadHistory import DownloadHistory
from models.DownloadQueue import DownloadQueue
from models.Fc2Channel import Fc2Pusher
from models.Fc2Blacklist import log_startup_path

from lib.constants import BOT_FOLDER, CONFIG_FILE
from lib.func import get_logger
from lib.bot_handlers import HANDLERS, commands, get_torrent_connection
from lib.polling import PollingBot, PollingUpdater


def bot():

    bot_config = BotConfigurator()

    DownloadHistory.set_log_file(bot_config.get("bot.download_log_file"))
    # 下载通知队列落盘，容器重启后仍能继续推送
    queue_file = bot_config.get("bot.queue_file") or os.path.join(
        os.path.dirname(CONFIG_FILE), "queue.json"
    )
    if not os.path.isabs(queue_file):
        queue_file = os.path.join(BOT_FOLDER, queue_file)
    DownloadQueue.set_state_file(queue_file)

    log = get_logger(
        "main", bot_config.get("bot.log_level"), bot_config.get("bot.log_file")
    )

    if not bot_config.validate():
        raise SystemExit(2)

    log_startup_path(bot_config)
    bot_config.set_bot_commands(commands)

    """Start the bot."""
    try:
        monitor_task = None
        fc2_task = None
        get_updates_request = HTTPXRequest(connection_pool_size=8, pool_timeout=10.0)
        polling_updater = PollingUpdater(
            PollingBot(
                token=bot_config.get("bot.token"),
                get_updates_request=get_updates_request,
            ),
            asyncio.Queue(),
            get_updates_request=get_updates_request,
            super_user=bot_config.get("bot.super_user"),
            log=log,
        )

        async def post_init(application):
            nonlocal monitor_task, fc2_task
            polling_updater.application = application
            await application.bot.set_my_commands(commands)
            client = get_torrent_connection()
            monitor_task = application.create_task(
                client.monitor_downloads(application.bot),
                name="transmission-download-monitor",
            )
            fc2_task = application.create_task(
                Fc2Pusher(application.bot).run(),
                name="fc2-channel-poller",
            )

        async def post_stop(_application):
            await polling_updater.close_supervision()

        async def post_shutdown(_application):
            for task in (monitor_task, fc2_task):
                if task is not None:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

        app = (
            Application.builder()
            .updater(polling_updater)
            .post_init(post_init)
            .post_stop(post_stop)
            .post_shutdown(post_shutdown)
            .build()
        )
        """Add bot handlers"""
        app.add_handlers(HANDLERS)
        app.add_error_handler(polling_updater.error_handler)
        """Run the bot until the user presses Ctrl-C"""
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except InvalidToken:
        log.info("Invalid token provided")
        sys.exit(1)
    except Exception as err:
        log.exception("Unhandled bot error: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    BotConfigurator.argparser()
    bot()
