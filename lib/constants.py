"""File is used to store constants user withing code"""

import os
from pathlib import Path

# Find bot root folder
BOT_FOLDER = str(Path(__file__).parent.parent)

# Configuration file
CONFIG_FILE = os.path.join(BOT_FOLDER, "config", "torrentino.yaml")

# Translation file
LANGUAGE_FILE = os.path.join(BOT_FOLDER, "lib", "torrentino.lang")

# Use this language if client language doesn't mach with any of
# supported languages
DEFAULT_LANGUAGE = "zh"

# How often check status of torrents on transmissions,
# Variable is used to send notification to user
# about completed downloads
QUEUE_CHECK_INTERVAL = 30

# Minimal interval between two edits of the same progress card (seconds)
PROGRESS_EDIT_INTERVAL = 45

# Maximum number of magnet links accepted from a single Telegram message
MAX_BATCH_LINKS = 50

# Callback data used for "自动分类" button in download directory keyboard
AUTO_DIR_CALLBACK = "__auto__"

# Number of posts per page to display user while
# browsing torrent, search results, download history, etc
POSTS_PER_PAGE = 5

# How long keep search results cached?
CACHE_TIMEOUT = 3600
