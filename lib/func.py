import configparser
import html
import os

import sys
import tempfile
import uuid
import logging
import logging.handlers
import qrcode
import pydash as _
import math

# from functools import wraps
from tempfile import mkstemp
from lib.constants import LANGUAGE_FILE, DEFAULT_LANGUAGE

size_names = ["B", "KB", "MB", "GB", "TB", "PB", "EB", "ZB", "YB"]

lang = configparser.ConfigParser()
lang.read(LANGUAGE_FILE, encoding="utf-8")

# Telegram 客户端返回的中文语言代码是 zh-hans / zh-hant，需要归一到语言文件里的段名
_LANG_ALIASES = {
    "zh": "zh",
    "zh-cn": "zh",
    "zh-sg": "zh",
    "zh-hans": "zh",
    "zh-hans-cn": "zh",
    "zh-tw": "zh-hant",
    "zh-hk": "zh-hant",
    "zh-mo": "zh-hant",
    "zh-hant": "zh-hant",
}


def normalize_lang(lang_code: str | None) -> str:
    """把 Telegram 的 language_code 归一成语言文件里的段名。"""
    if not lang_code:
        return DEFAULT_LANGUAGE
    code = str(lang_code).strip().lower().replace("_", "-")
    if code in _LANG_ALIASES:
        return _LANG_ALIASES[code]
    base = code.split("-")[0]
    return _LANG_ALIASES.get(base, base)


def trans(text, lang_code) -> str:
    """按 语言 → 基础语言 → 默认语言 → key 的顺序取翻译。"""
    logging.debug(f"Translate: {text}, {lang_code}")
    code = normalize_lang(lang_code)
    for candidate in (code, code.split("-")[0], DEFAULT_LANGUAGE, "en"):
        value = _.get(lang, [candidate, text])
        if value:
            return value
    return text


def get_logger(
    class_name: str, log_level: str | None = None, log_file: str | None = None
) -> logging.Logger:
    if not log_level:
        log_level = os.environ.get("log_level", "INFO").upper()
    # Configure telegram bot logging
    log_handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        logging.info("Adding file handler: %s", log_file)
        # First run: create directory
        if not os.path.isdir(os.path.dirname(log_file)):
            os.makedirs(os.path.dirname(log_file))
    logging.basicConfig(
        format="[%(asctime)s] [%(levelname)s] %(name)s %(message)s",
        level=log_level,
        handlers=log_handlers,
    )
    # Silence for httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)

    return logging.getLogger(class_name)


def get_qr_code(input_data):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(input_data)
    qr.make(fit=True)
    img_file = mkstemp()[1]
    log.debug(f"Temporal file created: {img_file}")
    img = qr.make_image(fill="black", back_color="white")
    img.save(img_file)
    return img_file


def bytes_to_human(size_bytes: int) -> str:
    size_bytes = int(size_bytes)
    if int(size_bytes) == 0:
        return "0B"
    try:
        i = int(math.floor(math.log(size_bytes, 1024)))
        p = math.pow(1024, i)
        s = round(size_bytes / p, 2)
        return "%s%s" % (s, size_names[i])
    except TypeError as err:
        log.warning("Wrong value for conversion to bytes %s: %s", size_bytes, err)
        return str(size_bytes)


def speed_to_human(size_bytes: float) -> str:
    """字节/秒 转成可读速度。"""
    return bytes_to_human(max(int(size_bytes or 0), 0)) + "/s"


def human_duration(seconds: float | None) -> str:
    """秒数转 HH:MM:SS，语言无关，避免翻译负担。"""
    try:
        total = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "--:--"
    if total <= 0:
        return "--:--"
    if total >= 86400:
        days, total = divmod(total, 86400)
        hours, rem = divmod(total, 3600)
        return f"{days}天 {hours:02d}:{rem // 60:02d}:{rem % 60:02d}"
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def progress_bar(percent: float, width: int = 10) -> str:
    """生成进度条，例如 ██████░░░░"""
    try:
        value = max(0.0, min(100.0, float(percent)))
    except (TypeError, ValueError):
        value = 0.0
    filled = int(round(value / 100 * width))
    filled = max(0, min(width, filled))
    return "█" * filled + "░" * (width - filled)


def escape_html(text: str | None) -> str:
    """转义 Telegram HTML 模式下的特殊字符。"""
    return html.escape(str(text or ""), quote=False)


def parse_speed(value: str) -> int | None:
    """解析 '5M' / '500K' / '0' 这类限速写法，返回 KB/s。"""
    if value is None:
        return None
    text = str(value).strip().upper().replace("/S", "").replace("B", "")
    if not text:
        return None
    units = {"K": 1, "M": 1024, "G": 1024 * 1024}
    multiplier = 1
    if text[-1] in units:
        multiplier = units[text[-1]]
        text = text[:-1]
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0:
        return None
    return int(number * multiplier)


def save_file(path: str, content: bytes):
    with open(path, "wb") as f:
        f.write(content)
    return path


def save_torrent_to_tempfile(content: bytes) -> str:
    """Save torrent as temporal file and return full file path"""
    temp_file = os.path.join(
        tempfile.gettempdir(), str(uuid.uuid4()) + ".torrent"  # Temporal directory
    )  # Random file name
    with open(temp_file, "wb") as f:
        f.write(content)
    log.info(
        "Temporal file path: %s, size: %s",
        temp_file,
        bytes_to_human(os.path.getsize(temp_file)),
    )

    return temp_file


def human_to_bytes(size_human: str) -> int | str | None:
    try:
        size, size_name = size_human[:-2], size_human[-2:]
        if size_name in size_names:
            i = size_names.index(size_name)
            p = math.pow(1024, i)
            return int(float(size) * p)
        return size_human
    except TypeError:
        return None


log = get_logger("function")
