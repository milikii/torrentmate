import copy
import os
import yaml
import sys
import pydash as _
from typing import Dict, Any
import argparse
import shutil
from lib.func import get_logger
from telegram import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from lib.constants import CONFIG_FILE, BOT_FOLDER

log = get_logger("BotConfigurator")


class BotConfigurator:

    config_file = CONFIG_FILE
    args = None
    _runtime_overrides: Dict[str, Any] = {}

    def __init__(self) -> None:
        if not BotConfigurator.config_file:
            log.critical(
                "Please set BotConfigurator.config_file before instantiating class objects"
            )
            raise ValueError("BotConfigurator.config_file not set")
        self.commands = None
        self._config = None
        # 磁盘原始配置（未注入环境变量/命令行运行时值），写盘时以它为底，
        # 避免运行时 token、RPC 密码被写回 YAML。
        self._disk_config = None
        self._config_mtime: float | None = None

    @staticmethod
    def argparser():
        """
        Method is used to parse CLI input and environment variables and merge
        input data with existing values in configuration file.

        NOTE: Most properties can be changed only inside configuration file.

        Values precedence:
        - CLI
        - Environemnt variables
        - Configuration file
        """

        parser = argparse.ArgumentParser(
            prog="Torrentino",
            description="Torrentino configuration options: ",
            epilog="Passed as command line arguments or environment variables"
            "are stored to configuration file and available for next run.",
        )
        parser.add_argument(
            "--config-file",
            type=str,
            default=os.getenv("CONFIG_FILE", CONFIG_FILE),
            help="Configuration file location",
        )
        parser.add_argument(
            "--token",
            type=str,
            default=os.getenv("TOKEN"),
            help="Token received from https://t.me/Botfather!",
        )
        parser.add_argument(
            "--super-user",
            type=int,
            default=os.getenv("SUPER_USER"),
            help="Telegram user ID allowed to administer the bot",
        )
        parser.add_argument(
            "--transmission-host",
            type=str,
            default=os.getenv("TRANSMISSION_HOST"),
            help="Transmission server host",
        )
        parser.add_argument(
            "--transmission-port",
            type=str,
            default=os.getenv("TRANSMISSION_PORT"),
            help="Transmission server port",
        )
        parser.add_argument(
            "--transmission-user",
            type=str,
            default=os.getenv("TRANSMISSION_USER"),
            help="User name for remote transmission authentication",
        )
        parser.add_argument(
            "--transmission-password",
            type=str,
            default=os.getenv("TRANSMISSION_PASSWORD"),
            help="Password for remote transmission user",
        )
        parser.add_argument(
            "--log",
            type=str,
            default=os.getenv("LOG_FILE", "logs/torrentino.log"),
            help="Log file location",
        )
        parser.add_argument(
            "--log-level", type=str, default=os.getenv("LOG_LEVEL"), help="Log level"
        )
        parser.add_argument(
            "--download-log",
            type=str,
            default=os.getenv(
                "DOWNLOAD_LOG",
            ),
            help="Download history log file",
        )
        BotConfigurator.args = parser.parse_args()
        BotConfigurator.config_file = BotConfigurator.args.config_file or CONFIG_FILE
        BotConfigurator._runtime_overrides = {
            "bot.token": BotConfigurator.args.token,
            "bot.super_user": BotConfigurator.args.super_user,
            "bot.log_file": BotConfigurator.args.log,
            "bot.log_level": BotConfigurator.args.log_level,
            "bot.download_log_file": BotConfigurator.args.download_log,
            "transmission.host": BotConfigurator.args.transmission_host,
            "transmission.port": BotConfigurator.args.transmission_port,
            "transmission.user": BotConfigurator.args.transmission_user,
            "transmission.password": BotConfigurator.args.transmission_password,
        }

    def init_args(self):
        """Apply CLI/environment values in memory without persisting secrets."""
        for path, value in BotConfigurator._runtime_overrides.items():
            if value is not None:
                _.set_(self._config, path, value)

    @property
    def config(self) -> Dict:
        """
        Return configuration file as dict:
        - if file doesn't exist, create new file from template
        - 配置文件被别的 BotConfigurator 实例改写（如 add_user）后，
          下一次访问会按 mtime 重新加载，而不是一直用旧缓存，
          否则邀请新用户后鉴权模块仍不认他。
        """
        if self._config is not None:
            try:
                current_mtime = os.path.getmtime(BotConfigurator.config_file)
            except OSError:
                current_mtime = None
            if current_mtime == self._config_mtime:
                return self._config
        if not os.path.exists(BotConfigurator.config_file):
            log.info("Configuration file %s not found", BotConfigurator.config_file)
            try:
                template_file = os.path.join(BOT_FOLDER, "templates", "torrentino.yaml")
                os.makedirs(os.path.dirname(BotConfigurator.config_file), exist_ok=True)
                shutil.copy(template_file, BotConfigurator.config_file)
                log.info(
                    "Created new configuration file from template: %s",
                    BotConfigurator.config_file,
                )
            except Exception as e:
                log.critical(
                    "Failed to create configuration file %s from template: %s due to error: %s",
                    BotConfigurator.config_file,
                    template_file,
                    e,
                )
                log.critical("Stopping bot startup...")
                sys.exit(1)
        with open(BotConfigurator.config_file, "r", encoding="utf-8") as config_file:
            self._config = yaml.safe_load(config_file) or {}
        self._disk_config = copy.deepcopy(self._config)
        try:
            self._config_mtime = os.path.getmtime(BotConfigurator.config_file)
        except OSError:
            self._config_mtime = None
        self.init_args()

        return self._config

    def set(self, path: str, value: str) -> None:
        # 之前用 `not value` 判断，False、0、空字符串都写不进去，
        # 例如把开关关掉就永远改不动。
        if value is None or value == _.get(self.config, path):
            return
        _.set_(self._config, path, value)
        if path in {"bot.token", "transmission.password"} or path.endswith("password"):
            log.info("Updated sensitive configuration value: %s", path)
        else:
            log.info("Updated configuration value: %s = %s", path, value)
        self.save_config()

    def get(self, path: str | list[str], default: Any = None) -> Any:
        return _.get(self.config, path, default)

    def _persistable_config(self) -> Dict:
        """可写盘的配置：以磁盘原始配置为底，只合并不属于运行时注入
        的改动（如 allowed_users、label），运行时注入的 token、RPC 密码
        等一律不写回，写回时置为 None，避免旧密钥残留误导。"""
        if self._config is None:
            _ = self.config
        disk = copy.deepcopy(self._disk_config or {})
        runtime = BotConfigurator._runtime_overrides or {}

        def _merge(dst: Dict, src: Dict, prefix: str = "") -> None:
            for key, value in src.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if path in runtime and runtime[path] is not None:
                    continue
                if isinstance(value, dict) and isinstance(dst.get(key), dict):
                    _merge(dst[key], value, path)
                else:
                    dst[key] = copy.deepcopy(value)

        _merge(disk, self._config or {})
        for path, value in runtime.items():
            if value is not None:
                try:
                    _.set_(disk, path, None)
                except Exception:  # noqa: BLE001
                    pass
        return disk

    def save_config(self) -> None:
        log.info("Updating configuration file: %s", self.config_file)
        data = self._persistable_config()
        directory = os.path.dirname(self.config_file) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{self.config_file}.tmp"
        try:
            mode = os.stat(self.config_file).st_mode & 0o777 if os.path.exists(
                self.config_file
            ) else 0o600
        except OSError:
            mode = 0o600
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        # 原子替换：中途崩溃也不会把配置文件写成半截。
        os.replace(tmp, self.config_file)
        self._disk_config = copy.deepcopy(data)
        try:
            self._config_mtime = os.path.getmtime(self.config_file)
        except OSError:
            self._config_mtime = None

    def validate(self) -> bool:
        failed_checks = []
        warning_checks = []
        if not self.get("bot.token"):
            failed_checks.append(
                "You must pass the token you received from https://t.me/Botfather!"
            )
        if not self.get("bot.super_user"):
            failed_checks.append(
                "Set bot.super_user, SUPER_USER, or --super-user before starting the bot"
            )
        if not all(
            [
                self.get("transmission.host"),
                self.get("transmission.port"),
                self.get("transmission.user"),
                self.get("transmission.password"),
            ]
        ):
            warning_checks.append("Provide Transmission host, port, user, and password")
        if warning_checks:
            for check in warning_checks:
                log.warning(check)
        if failed_checks:
            for check in failed_checks:
                log.critical(check)
            return False
        return True

    def get_actions_keyboard(self, actions, lang_code: str | None = None):
        """构建底部快捷键盘，actions 可传文案或 i18n 按钮 key。"""
        from lib.i18n import BUTTON_LABELS, button_label

        if not actions:
            return ReplyKeyboardRemove()
        labels = []
        for action in actions:
            if action in BUTTON_LABELS:
                labels.append(button_label(action, lang_code))
            else:
                labels.append(str(action))
        rows = [labels[index : index + 2] for index in range(0, len(labels), 2)]
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=text) for text in row] for row in rows],
            resize_keyboard=True,
        )

    def get_downloads_keyboard(
        self, lang_code: str | None = None, with_auto: bool | None = None
    ) -> InlineKeyboardMarkup:
        # Download directories
        # Transmission server needs write access to these directories
        from lib.constants import AUTO_DIR_CALLBACK
        from lib.i18n import button_label

        directories = dict(self.config.get("directories") or {})
        if with_auto is None:
            with_auto = bool(self.get("auto_classify.enabled", True))

        rows = []
        if with_auto:
            rows.append(
                [
                    InlineKeyboardButton(
                        button_label("menu.auto", lang_code),
                        callback_data=AUTO_DIR_CALLBACK,
                    )
                ]
            )
        buttons = [
            InlineKeyboardButton(str(name), callback_data=str(path))
            for name, path in directories.items()
        ]
        for index in range(0, len(buttons), 2):
            rows.append(buttons[index : index + 2])
        if rows:
            return InlineKeyboardMarkup(rows)
        return InlineKeyboardMarkup([[InlineKeyboardButton("—", callback_data="x")]])

    def set_bot_commands(self, commands) -> "BotConfigurator":
        """Store commands to synchronize during application startup."""
        self.commands = commands
        return self

    def add_user(self, user_id: int) -> "BotConfigurator":
        cfg = self.config
        bot_section = cfg.setdefault("bot", {})
        allowed = bot_section.get("allowed_users") or []
        bot_section["allowed_users"] = allowed
        if user_id not in allowed and str(user_id) not in [str(x) for x in allowed]:
            log.info("Adding user_id %s to allowed users", user_id)
            allowed.append(user_id)
            self.save_config()
        return self
