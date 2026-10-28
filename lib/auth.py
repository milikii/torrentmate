from functools import wraps
import pydash as _
from telegram import Update
from telegram.ext import ContextTypes

from models.BotConfigurator import BotConfigurator
from lib.func import get_logger, trans

bot_config = BotConfigurator()
# config = bot_config.config

# log_file = _.get(bot_config.config, 'bot.log_file')
log = get_logger("Authentication")


def restricted(func):
    @wraps(func)
    async def wrapped(
        update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs
    ):
        user_id = update.effective_user.id
        super_user = bot_config.get("bot.super_user")
        # YAML 里 super_user 可能被写成字符串，int 与 str 直接比较永远
        # 不相等，超级用户会被自己挡在门外。
        try:
            super_user = int(super_user) if super_user is not None else None
        except (TypeError, ValueError):
            pass
        if not super_user:
            log.critical("Access denied because bot.super_user is not configured")
            await context.bot.send_message(
                chat_id=user_id,
                text=trans("ACCESS_RESTRICTED", update.effective_user.language_code),
            )
            return
        # Check if user is allowed
        allowed_users = _.get(bot_config.config, "bot.allowed_users", []) or []
        is_allowed_user = user_id in allowed_users or str(user_id) in [
            str(x) for x in allowed_users
        ]
        if not is_allowed_user and user_id != super_user:
            log.debug(update)

            await context.bot.send_message(
                chat_id=user_id,
                text=trans("ACCESS_RESTRICTED", update.effective_user.language_code),
            )
            log.error("User %s is not authorized", user_id)
            return
        # If user is authorized, then execute wrapped function
        return await func(update, context, *args, **kwargs)

    return wrapped
