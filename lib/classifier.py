"""按任务名称自动分类，决定下载到哪个目录。

规则全部来自配置文件 ``auto_classify.rules``，按顺序匹配，先命中者优先。
未命中任何规则时落到 ``auto_classify.fallback`` 目录（默认「未分类」）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import unquote_plus

from lib.func import escape_html, get_logger

log = get_logger("Classifier")

# 常见 JAV 番号：ABC-123 / SSIS001 / hhd800.com@SSIS-123 等
# 排除编码、分辨率、容器等误判前缀
_JAV_STOP_PREFIX = (
    "MP4|MKV|AVI|WMV|MOV|TS|M2TS|RMVB|FLV|WEBM|"
    "X264|X265|H264|H265|HEVC|AVC|AV1|VP9|DIVX|XVID|"
    "AAC|AC3|DTS|DDP|DD|FLAC|MP3|ATMOS|"
    "HD|FHD|UHD|SD|HDR|SDR|HDRIP|BDRIP|WEBRIP|HDTV|BLURAY|"
    "CD|DVD|BD|DISC|PART|VOL|EP|SE|SP|"
    "FPS|BIT|KBPS|MBPS|CH|AUDIO|SUB|CHS|CHT"
)

JAV_CODE_PATTERN = (
    rf"(?<![A-Za-z0-9])(?!(?:{_JAV_STOP_PREFIX})(?![A-Za-z0-9]))"
    r"([A-Za-z]{2,6})[\s._-]?((?!(?:19|20)\d{2}(?!\d))\d{2,5})(?![0-9A-Za-z])"
)

# 站点前缀常写成 hhd800.com@SSIS-123，需要跳过这类"假番号"
_DOMAIN_SUFFIXES = (
    ".com",
    ".net",
    ".cc",
    ".org",
    ".me",
    ".tv",
    ".xyz",
    ".info",
    " com",
    " net",
    " cc",
    " org",
    " me",
    " tv",
    " xyz",
    " info",
)

_DOMESTIC_STUDIOS_PATTERN = "麻豆|天美|果冻|精东|星空|蜜桃|糖心|含羞草|香蕉|探花|司机社"
_DOMESTIC_91_PATTERNS = (
    r"(?<![A-Za-z0-9])91[\s._-]*(?:制片[厂廠]|[传傳]媒|原[创創]|大神|"
    r"(?:夯|沈|秦)?先生|视[频頻]|自拍|精品|"
    r"porn(?:[\s._-]*com)?(?![A-Za-z0-9]))",
    r"(?<![A-Za-z0-9])91[\s._-]*CM[\s._-]*\d{2,5}(?![A-Za-z0-9])",
)

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "name": "fc2",
        "dir": "FC2",
        "patterns": [
            r"\bFC2[\s._-]*(?:PPV)?[\s._-]*\d{3,8}\b",
            r"\bFC2\b",
        ],
    },
    {
        "name": "国产",
        "dir": "国产",
        "patterns": [
            r"国产|國產|国内自拍|自拍流出",
            _DOMESTIC_STUDIOS_PATTERN,
            *_DOMESTIC_91_PATTERNS,
            r"MD[\s._-]?\d{3,5}",
            r"SWAG|ONLYFANS|FANSLY|JVID",
            r"(?:^|[^A-Za-z])GD[\s._-]?\d{3,5}",
        ],
    },
    {
        "name": "无码",
        "dir": "无码",
        "patterns": [
            r"无码|無碼|無修正|无修正|破解|流出|leak(?:ed)?|uncensored|decensored",
            r"1pondo|10musume|pacopacomama|caribbeancom|heydouga|tokyo[-_ ]?hot",
            r"(?<![A-Za-z0-9])\d{3,5}[-_ ]?UC(?![A-Za-z0-9])",
        ],
    },
    {
        "name": "有码",
        "dir": "有码",
        "patterns": [JAV_CODE_PATTERN],
    },
]

# 配置里缺少目录定义时使用的兜底路径
DEFAULT_DIRECTORIES: dict[str, str] = {
    "FC2": "/downloads/FC2",
    "有码": "/downloads/有码",
    "无码": "/downloads/无码",
    "国产": "/downloads/国产",
    "未分类": "/downloads/未分类",
}

_CLEANUP = re.compile(r"[\[\]【】()（）_]+")


@dataclass(frozen=True)
class Classification:
    category: str
    directory: str  # 目录名（对应配置 directories 的 key）
    rule: str
    matched: str | None = None
    uncertain: bool = False

    @property
    def label(self) -> str:
        return self.directory


@dataclass(frozen=True)
class ClassifyRule:
    name: str
    directory: str
    patterns: tuple[re.Pattern, ...] = field(default=())


def _clean_name(name: str) -> str:
    """把常见命名里的分隔符统一成空格，便于正则匹配。"""
    text = unquote_plus(name or "")
    text = _CLEANUP.sub(" ", text)
    text = re.sub(r"[.\u3000]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _best_match(text: str, pattern: re.Pattern) -> str | None:
    """在同一规则里挑最可信的命中：跳过站点域名前缀，优先靠后的番号。"""
    candidates = []
    for match in pattern.finditer(text):
        end = match.end()
        if text[end : end + 5].lower().startswith(_DOMAIN_SUFFIXES):
            continue
        candidates.append(match.group(0))
    if not candidates:
        return None
    return candidates[-1]


class Classifier:
    def __init__(
        self,
        rules: Sequence[dict[str, Any]] | None = None,
        directories: dict[str, str] | None = None,
        fallback: str = "未分类",
    ) -> None:
        self.fallback = fallback
        self.directories = dict(directories or {})
        self.rules = self._build_rules(rules if rules is not None else DEFAULT_RULES)

    @staticmethod
    def _build_rules(rules: Iterable[dict[str, Any]]) -> list[ClassifyRule]:
        compiled: list[ClassifyRule] = []
        for raw in rules:
            if not isinstance(raw, dict):
                continue
            if raw.get("enabled") is False:
                continue
            name = str(raw.get("name") or raw.get("dir") or "").strip()
            directory = str(raw.get("dir") or raw.get("directory") or name).strip()
            patterns = raw.get("patterns") or []
            if isinstance(patterns, str):
                patterns = [patterns]
            regexes = []
            for pattern in patterns:
                candidates = [pattern]
                if name == "国产" and pattern == f"{_DOMESTIC_STUDIOS_PATTERN}|91":
                    # 兼容已经写入配置的旧默认规则，只在内存中收窄裸 91，
                    # 不改配置文件，也不改变其它自定义规则的含义。
                    candidates = [_DOMESTIC_STUDIOS_PATTERN, *_DOMESTIC_91_PATTERNS]
                for candidate in candidates:
                    try:
                        regexes.append(re.compile(str(candidate), re.IGNORECASE))
                    except re.error as err:
                        log.error("分类规则 %s 的正则无效: %s (%s)", name, candidate, err)
            if name and regexes:
                compiled.append(
                    ClassifyRule(
                        name=name, directory=directory or name, patterns=tuple(regexes)
                    )
                )
        return compiled

    def resolve_path(self, directory: str) -> str | None:
        """把目录名解析为 Transmission 上的真实路径。"""
        if not directory:
            return None
        path = self.directories.get(directory)
        if path:
            return str(path)
        return DEFAULT_DIRECTORIES.get(directory)

    def classify(self, name: str | None) -> Classification:
        text = _clean_name(name or "")
        if text:
            for rule in self.rules:
                for pattern in rule.patterns:
                    matched = _best_match(text, pattern)
                    if matched:
                        return Classification(
                            category=rule.name,
                            directory=rule.directory,
                            rule=rule.name,
                            matched=matched.strip(),
                        )
        return Classification(
            category="未分类",
            directory=self.fallback or "未分类",
            rule="fallback",
            uncertain=True,
        )

    def describe(self) -> list[str]:
        """给 /rules 命令用的可读规则说明。"""
        lines = []
        for rule in self.rules:
            patterns = " | ".join(escape_html(p.pattern) for p in rule.patterns)
            lines.append(
                f"• <b>{escape_html(rule.name)}</b> → {escape_html(rule.directory)}\n"
                f"  <code>{patterns}</code>"
            )
        lines.append(f"• <b>兜底</b> → {escape_html(self.fallback)}")
        return lines


_classifier_cache: tuple[str, Classifier] | None = None


def get_classifier() -> Classifier:
    """根据当前配置构建分类器；配置变化后自动重建。"""
    from models.BotConfigurator import BotConfigurator

    config = BotConfigurator()
    rules = config.get("auto_classify.rules", DEFAULT_RULES)
    directories = dict(config.get("directories", {}) or {})
    fallback = config.get("auto_classify.fallback", "未分类")
    signature = f"{rules!r}|{directories!r}|{fallback!r}"

    global _classifier_cache
    if _classifier_cache and _classifier_cache[0] == signature:
        return _classifier_cache[1]
    classifier = Classifier(rules=rules, directories=directories, fallback=fallback)
    _classifier_cache = (signature, classifier)
    return classifier
