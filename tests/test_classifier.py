import pytest

from lib.classifier import DEFAULT_RULES, Classifier

DIRECTORIES = {
    "FC2": "/downloads/FC2",
    "有码": "/downloads/有码",
    "无码": "/downloads/无码",
    "国产": "/downloads/国产",
    "未分类": "/downloads/未分类",
}


@pytest.fixture
def classifier() -> Classifier:
    return Classifier(rules=DEFAULT_RULES, directories=DIRECTORIES)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("[ThZu.Cc]FC2-PPV-1234567 无码流出 1080p", "FC2"),
        ("fc2_ppv_4567890", "FC2"),
        ("SSIS-123-C 中文字幕 1080p", "有码"),
        ("hhd800.com@IPX-999.mp4", "有码"),
        ("麻豆传媒MD-0123 国产剧情", "国产"),
        ("糖心VLOG 约啪 2024", "国产"),
        ("MD-0123 高清剧情", "国产"),
        ("91制片厂 高清剧情", "国产"),
        ("91CM-123 高清剧情", "国产"),
        ("91 原创 合集", "国产"),
        ("91大神 高清合集", "国产"),
        ("91沈先生 高清合集", "国产"),
        ("[91porn] 自拍合集", "国产"),
        ("91porn.com@自拍合集", "国产"),
        ("SSIS-091 中文字幕 1080p", "有码"),
        ("hhd800.com@IPX-191.mp4", "有码"),
        ("[无码] ABC-123 uncensored", "无码"),
        ("1pondo 123456_001", "无码"),
    ],
)
def test_classification(classifier: Classifier, name: str, expected: str):
    assert classifier.classify(name).directory == expected


@pytest.mark.parametrize(
    "name",
    [
        "The.Office.US.S01E01.1080p.WEB-DL.x264",
        "Some.Movie.2024.2160p.HEVC.AAC",
        "HEVC-10bit",
        "DD5.1",
        "Some.Movie.1991.1080p.WEB-DL.x264",
        "普通电影 91 1080p",
        "91",
        "0123456789abcdef0123456789abcdef01234591",
        "",
    ],
)
def test_common_video_names_are_not_misclassified(classifier: Classifier, name: str):
    result = classifier.classify(name)
    assert result.directory == "未分类"
    assert result.uncertain is True


def test_unknown_falls_back_to_configured_folder():
    classifier = Classifier(rules=[], directories={}, fallback="其它")
    assert classifier.classify("something").directory == "其它"


def test_custom_rules_and_disabled_rule():
    rules = [
        {"name": "skip", "dir": "有码", "enabled": False, "patterns": [r"abc"]},
        {"name": "custom", "dir": "国产", "patterns": [r"abc-123"]},
    ]
    classifier = Classifier(rules=rules, directories=DIRECTORIES)
    assert classifier.classify("abc-123").directory == "国产"


def test_legacy_default_91_rule_is_narrowed_without_mutating_config():
    legacy_pattern = "麻豆|天美|果冻|精东|星空|蜜桃|糖心|含羞草|香蕉|探花|司机社|91"
    rules = [
        {"name": "国产", "dir": "国产", "patterns": [legacy_pattern]},
        DEFAULT_RULES[-1],
    ]
    classifier = Classifier(rules=rules, directories=DIRECTORIES)

    assert classifier.classify("SSIS-091").directory == "有码"
    assert classifier.classify("91制片厂 高清剧情").directory == "国产"
    assert classifier.classify("91CM-123").directory == "国产"
    assert rules[0]["patterns"] == [legacy_pattern]


def test_resolve_path_falls_back_to_defaults():
    classifier = Classifier(rules=DEFAULT_RULES, directories={})
    assert classifier.resolve_path("FC2") == "/downloads/FC2"
    assert classifier.resolve_path("不存在的目录") is None


def test_broken_regex_is_ignored():
    rules = [{"name": "bad", "dir": "有码", "patterns": ["("]}]
    classifier = Classifier(rules=rules, directories=DIRECTORIES)
    assert classifier.classify("SSIS-123").directory == "未分类"
