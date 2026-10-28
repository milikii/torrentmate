"""界面按钮与命令描述的多语言文案。

Telegram 的回复键盘按钮是「纯文本消息」，所以处理器必须同时接受各语言按钮文案；
这里统一维护，避免新增语言时到处改正则。
"""

from __future__ import annotations

from lib.func import normalize_lang, trans

BUTTON_LABELS: dict[str, dict[str, str]] = {
    "menu.torrents": {"zh": "📁 任务", "zh-hant": "📁 任務", "en": "📁 Torrents"},
    "menu.search": {"zh": "🔍 最近搜索", "zh-hant": "🔍 最近搜尋", "en": "🔍 Search"},
    "menu.status": {"zh": "📊 状态", "zh-hant": "📊 狀態", "en": "📊 Status"},
    "menu.help": {"zh": "❓ 帮助", "zh-hant": "❓ 幫助", "en": "❓ Help"},
    "menu.stop_all": {
        "zh": "⏹ 全部暂停",
        "zh-hant": "⏹ 全部暫停",
        "en": "⏹ Stop all Torrents",
    },
    "menu.start_all": {
        "zh": "⏩ 全部开始",
        "zh-hant": "⏩ 全部開始",
        "en": "⏩ Start all Torrents",
    },
    "menu.history": {
        "zh": "🕑 下载历史",
        "zh-hant": "🕑 下載歷史",
        "en": "🕑 Download history",
    },
    "menu.rules": {
        "zh": "🏷 分类规则",
        "zh-hant": "🏷 分類規則",
        "en": "🏷 Classify rules",
    },
    "menu.adduser": {
        "zh": "👤 添加用户",
        "zh-hant": "👤 新增使用者",
        "en": "👤 Add new user",
    },
    "menu.auto": {
        "zh": "🤖 自动分类",
        "zh-hant": "🤖 自動分類",
        "en": "🤖 Auto classify",
    },
}

# 命令 → 描述（/help 正文与 Telegram 命令菜单共用）
COMMAND_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "torrents": {"zh": "任务列表", "en": "List torrents"},
    "status": {"zh": "服务器状态与磁盘剩余", "en": "Server status and free space"},
    "history": {"zh": "下载历史", "en": "Download history"},
    "last_search": {"zh": "上次搜索结果", "en": "Last search results"},
    "start_all": {"zh": "全部开始", "en": "Start all"},
    "stop_all": {"zh": "全部暂停", "en": "Stop all"},
    "rules": {"zh": "查看自动分类规则", "en": "Show classify rules"},
    "classify": {
        "zh": "测试名称分类，如 /classify SSIS-123",
        "en": "Test classification",
    },
    "speed": {"zh": "限速，如 /speed 5M 1M，/speed off 取消", "en": "Set speed limit"},
    "adduser": {"zh": "生成邀请链接", "en": "Invite a new user"},
    "help": {"zh": "帮助", "en": "Help"},
}


def button_label(key: str, lang_code: str | None = None) -> str:
    labels = BUTTON_LABELS.get(key, {})
    code = normalize_lang(lang_code)
    for candidate in (code, code.split("-")[0], "zh", "en"):
        if labels.get(candidate):
            return labels[candidate]
    return trans(key, lang_code)


def button_regex(key: str) -> str:
    """匹配任意语言下该按钮的完整文案。"""
    import re

    variants = {value for value in BUTTON_LABELS.get(key, {}).values() if value}
    if not variants:
        return r"$^"
    pattern = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    return rf"^(?:{pattern})$"


def command_list(lang_code: str | None = None) -> list[tuple[str, str]]:
    return [
        (name, button_label_key_text(name, lang_code)) for name in COMMAND_DESCRIPTIONS
    ]


def button_label_key_text(command: str, lang_code: str | None = None) -> str:
    descriptions = COMMAND_DESCRIPTIONS.get(command, {})
    code = normalize_lang(lang_code)
    for candidate in (code, code.split("-")[0], "zh", "en"):
        if descriptions.get(candidate):
            return descriptions[candidate]
    return command
