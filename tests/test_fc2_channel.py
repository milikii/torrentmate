import asyncio
import logging
from types import SimpleNamespace

import pytest

from models import Fc2Channel as fc2

FEED_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<rss version="2.0" xmlns:nyaa="https://sukebei.nyaa.si/xmlns/nyaa">'
    "<channel><title>sukebei</title>"
)


def make_feed(*items):
    return FEED_HEAD + "".join(items) + "</channel></rss>"


def make_item(number, pub, hash_=None, size="1.2 GiB", title=None, guid=None):
    ident = number if isinstance(number, int) else int(number)
    return (
        "<item>"
        f"<title>{title or f'+++ FC2-PPV-{ident} sample'}</title>"
        f"<guid>{guid or f'https://sukebei.nyaa.si/view/{ident}'}</guid>"
        f"<pubDate>{pub}</pubDate>"
        f"<nyaa:infoHash>{'a' * 40 if hash_ is None else hash_}</nyaa:infoHash>"
        f"<nyaa:size>{size}</nyaa:size>"
        "</item>"
    )


class FakeConfig:
    def __init__(self, **overrides):
        self.fc2 = {
            "channel_id": -1001234,
            "interval": 600,
            "backfill_limit": 10,
            "proxy": None,
            "verify_tls": True,
        }
        self.fc2.update(overrides)

    def get(self, path, default=None):
        if str(path).startswith("fc2."):
            return self.fc2.get(str(path)[4:], default)
        return default


class FakeResponder:
    """把 URL 映射到假 response，记录所有请求。"""

    def __init__(self, pages=None, errors=None):
        self.pages = pages or {}
        self.errors = errors or {}
        self.calls = []

    def __call__(self, url, proxy, verify_tls):
        self.calls.append(url)
        for fragment, error in self.errors.items():
            if fragment in url:
                raise error
        for fragment, page in self.pages.items():
            if fragment in url:
                return page
        return SimpleNamespace(status_code=404, text="", content=b"", headers={})


def page(html, status=200):
    return SimpleNamespace(status_code=status, text=html, content=b"", headers={})


class FakeBot:
    def __init__(self):
        self.photos = []
        self.groups = []
        self.markups = []
        self._next_id = 100

    def _message(self):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return self._message()

    async def send_media_group(self, **kwargs):
        self.groups.append(kwargs)
        return [self._message() for _ in kwargs["media"]]

    async def edit_message_reply_markup(self, **kwargs):
        self.markups.append(kwargs)
        return True


OFFICIAL_HTML = """
<html><head>
<title>FC2-PPV-5000545 タイトル</title>
<meta property="og:image" content="//storage.fc2.com/a/cover.jpg" />
</head><body>
<img src="//contents-thumbnail2.fc2.com/w480/storage.fc2.com/a/1.jpg">
<img src="//contents-thumbnail2.fc2.com/w480/storage.fc2.com/a/1.jpg">
<img src="//contents-thumbnail2.fc2.com/w480/storage.fc2.com/a/2.jpg">
<img src="//contents-thumbnail2.fc2.com/w480/storage.fc2.com/a/3.jpg">
</body></html>
"""


def official_page(count, number="5000545"):
    thumbs = "".join(
        f'<img src="//contents-thumbnail2.fc2.com/w480/host.fc2.com/{index}.jpg">'
        for index in range(count)
    )
    return page(
        f"<html><head><title>FC2-PPV-{number} title</title>"
        f'<meta property="og:image" content="https://host.fc2.com/cover.jpg" />'
        f"</head><body>{thumbs}</body></html>"
    )


def fc2ppvdb_page(count, start=0):
    images = "".join(
        f'<img src="https://img.fc2ppvdb.com/{index}.jpg">'
        for index in range(start, start + count)
    )
    return page(f"<html><head><title>fc2ppvdb</title></head><body>{images}</body></html>")


# ---------------------------------------------------------------- RSS 解析


def test_parse_feed_sorts_ascending_and_keeps_fields():
    text = make_feed(
        make_item(5000002, "Wed, 29 Oct 2026 12:00:00 +0800", size="2 GiB"),
        make_item(5000001, "Wed, 29 Oct 2026 10:00:00 +0800", size="1 GiB"),
    )

    items = fc2.parse_feed(text)

    assert [item["title"] for item in items] == [
        "+++ FC2-PPV-5000001 sample",
        "+++ FC2-PPV-5000002 sample",
    ]
    first = items[0]
    assert first["guid"] == "https://sukebei.nyaa.si/view/5000001"
    assert first["hash"] == "a" * 40
    assert first["size"] == "1 GiB"
    assert first["pub_ts"] < items[1]["pub_ts"]


def test_parse_feed_drops_items_without_pubdate_or_hash():
    text = make_feed(
        make_item(5000001, "Wed, 29 Oct 2026 10:00:00 +0800"),
        make_item(5000002, "", hash_="b" * 40),
        make_item(5000003, "Wed, 29 Oct 2026 11:00:00 +0800", hash_=""),
        make_item(5000004, "Wed, 29 Oct 2026 12:00:00 +0800", hash_="c" * 40),
    )

    items = fc2.parse_feed(text)

    assert [fc2.extract_number(item["title"]) for item in items] == [
        "5000001",
        "5000004",
    ]


@pytest.mark.parametrize(
    "title, expected",
    [
        ("FC2-PPV-5000545", "5000545"),
        ("+++ FC2-PPV-1234567 some title", "1234567"),
        ("fc2ppv 12345", "12345"),
        ("FC2_123456", "123456"),
        ("FC2-PPV-00012345", "12345"),
        ("FC2", None),
        ("FC2-PPV-abc", None),
    ],
)
def test_extract_number(title, expected):
    assert fc2.extract_number(title) == expected


# ---------------------------------------------------------------- 官网取图


def test_fetch_official_images_prefers_cover_and_dedupes(monkeypatch):
    responder = FakeResponder({"/article/5000545/": page(OFFICIAL_HTML)})
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._fetch_official("5000545", None, True)

    assert images == [
        "https://storage.fc2.com/a/cover.jpg",
        "https://storage.fc2.com/a/1.jpg",
        "https://storage.fc2.com/a/2.jpg",
        "https://storage.fc2.com/a/3.jpg",
    ]
    assert responder.calls == ["https://adult.contents.fc2.com/article/5000545/"]


@pytest.mark.parametrize(
    "response",
    [
        page("<html><body>not found</body></html>", status=404),
        page("<html><head><title>FC2-PPV-5999999 other</title></head></html>"),
        page(
            "<html><head><title>FC2-PPV-5000545 title</title></head>"
            "<body>no images</body></html>"
        ),
    ],
)
def test_fetch_official_rejects_unconfirmed_article(monkeypatch, response):
    responder = FakeResponder({"/article/": response})
    monkeypatch.setattr(fc2, "_http_get", responder)

    assert fc2._fetch_official("5000545", None, True) == []


def test_preview_images_fill_from_fc2ppvdb_up_to_seven(monkeypatch):
    responder = FakeResponder(
        {
            "/article/5000545/": official_page(2),
            "fc2ppvdb.com/articles/5000545": fc2ppvdb_page(5),
        }
    )
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._preview_images("5000545", None, True)

    assert len(images) == fc2.PREVIEW_LIMIT == 7
    assert images[:3] == [
        "https://host.fc2.com/cover.jpg",
        "https://host.fc2.com/0.jpg",
        "https://host.fc2.com/1.jpg",
    ]
    assert images[3:] == [f"https://img.fc2ppvdb.com/{i}.jpg" for i in range(4)]


def test_preview_images_survive_fc2ppvdb_failure(monkeypatch):
    responder = FakeResponder(
        {"/article/5000545/": official_page(3)},
        errors={"fc2ppvdb": RuntimeError("proxy down")},
    )
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._preview_images("5000545", None, True)

    assert images == [
        "https://host.fc2.com/cover.jpg",
        "https://host.fc2.com/0.jpg",
        "https://host.fc2.com/1.jpg",
        "https://host.fc2.com/2.jpg",
    ]


def test_preview_images_dedupes_fc2ppvdb_duplicates(monkeypatch):
    responder = FakeResponder(
        {
            "/article/5000545/": official_page(3),
            "fc2ppvdb.com/articles/5000545": page(
                '<img src="https://host.fc2.com/0.jpg">'
                '<img src="https://img.fc2ppvdb.com/9.jpg">'
            ),
        }
    )
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._preview_images("5000545", None, True)

    assert images == [
        "https://host.fc2.com/cover.jpg",
        "https://host.fc2.com/0.jpg",
        "https://host.fc2.com/1.jpg",
        "https://host.fc2.com/2.jpg",
        "https://img.fc2ppvdb.com/9.jpg",
    ]


def test_preview_images_with_seven_official_skips_fc2ppvdb(monkeypatch):
    responder = FakeResponder(
        {
            "/article/5000545/": official_page(6),
            "fc2ppvdb.com/articles/5000545": fc2ppvdb_page(5),
        }
    )
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._preview_images("5000545", None, True)

    assert len(images) == 7
    assert all("fc2ppvdb" not in url for url in images)
    assert not any("fc2ppvdb" in url for url in responder.calls)


# ---------------------------------------------------------------- 去重 / 静音 / 回补


def make_pusher(tmp_path, monkeypatch, **overrides):
    store = fc2.Fc2Store(str(tmp_path / "fc2_state.json"))
    bot = FakeBot()
    pusher = fc2.Fc2Pusher(bot, FakeConfig(**overrides), store)
    return pusher, store, bot


async def test_seen_number_is_not_pushed_and_images_not_fetched(
    tmp_path, monkeypatch
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.remember(
        "5000545", {"hash": "a" * 40, "title": "FC2-PPV-5000545 x", "pub_ts": 10}
    )
    fetched = []
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fetched.append(a) or [])

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
    )

    assert fetched == []
    assert bot.photos == [] and bot.groups == []


async def test_muted_number_is_not_pushed(tmp_path, monkeypatch):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.mute("5000545")
    fetched = []
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fetched.append(a) or [])

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
    )

    assert fetched == []
    assert bot.photos == [] and bot.groups == []
    assert store.entry("5000545") is None


async def test_first_run_backfills_only_last_ten(tmp_path, monkeypatch):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch, backfill_limit=10)
    entries = [
        {
            "title": f"FC2-PPV-{5000000 + index}",
            "guid": f"https://sukebei.nyaa.si/view/{5000000 + index}",
            "hash": f"{index:040x}",
            "pub_ts": 1000 + index,
            "size": "1 GiB",
        }
        for index in range(30)
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: ["https://img/x.jpg"])
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: [b"bytes"])
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    await pusher._poll_once(-1001234)

    assert len(bot.photos) == 10
    pushed = [call["caption"].split("\n")[0] for call in bot.photos]
    assert pushed[0] == "<b>FC2-PPV-5000020</b>"
    assert pushed[-1] == "<b>FC2-PPV-5000029</b>"
    assert len(store.seen) == 30
    assert store.last_ts == 1029


# ---------------------------------------------------------------- 状态 / 磁力 / 未配置


def test_store_roundtrip_and_corruption(tmp_path):
    path = tmp_path / "fc2_state.json"
    store = fc2.Fc2Store(str(path))
    store.last_ts = 123.0
    store.remember(
        "5000545", {"hash": "b" * 40, "title": "t", "pub_ts": 5}, messages=[1, 2]
    )
    store.mute("4999999")
    store.save()

    reloaded = fc2.Fc2Store(str(path))
    assert reloaded.last_ts == 123.0
    assert reloaded.entry("5000545")["hash"] == "b" * 40
    assert reloaded.entry("5000545")["messages"] == [1, 2]
    assert reloaded.muted == {"4999999"}

    path.write_text("{not json", encoding="utf-8")
    broken = fc2.Fc2Store(str(path))
    assert broken.last_ts == 0
    assert broken.seen == {}
    assert broken.muted == set()


def test_magnet_assembly():
    magnet = fc2._magnet("d00c0ffee", "FC2-PPV-5000545 日本語 title")

    assert magnet.startswith("magnet:?xt=urn:btih:d00c0ffee")
    assert "dn=FC2-PPV-5000545%20%E6%97%A5%E6%9C%AC%E8%AA%9E%20title" in magnet
    assert magnet.count("&tr=") == 3
    assert "日本語" not in magnet


async def test_run_without_channel_id_only_warns(tmp_path, monkeypatch, caplog):
    pusher, _store, _bot = make_pusher(tmp_path, monkeypatch, channel_id=None)
    calls = []
    monkeypatch.setattr(fc2, "_http_get", lambda *a, **k: calls.append(a))

    with caplog.at_level(logging.WARNING):
        await asyncio.wait_for(pusher.run(), timeout=1)

    assert calls == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


# ---------------------------------------------------------------- 码标签 + 详情翻译

DETAIL_HTML = """
<html><head>
<title>FC2-PPV-4974501 title</title>
<meta property="og:description" content="FC2-PPV-4974501 可愛い素人の高画質版です" />
</head><body>
<a class="tag tagTag" data-article-tag href="/search/?tag=%E7%84%A1%E4%BF%AE%E6%AD%A3" data-tag="無修正">無修正</a>
<a class="tag tagTag" data-article-tag href="/search/?tag=%E7%B4%A0%E4%BA%BA" data-tag="素人">素人</a>
</body></html>
"""


def test_official_tags_and_description():
    assert fc2._official_tags(DETAIL_HTML) == ["無修正", "素人"]
    assert fc2._official_description(DETAIL_HTML) == "可愛い素人の高画質版です"


@pytest.mark.parametrize(
    "tags, expected",
    [
        (["無修正"], "无码"),
        (["薄モザイク"], "薄码"),
        (["薄消し"], "薄码"),
        (["素人"], "有码"),
        ([], "有码"),
    ],
)
def test_censor_label_only_tags_seller_text_ignored(tags, expected):
    # 卖家详情文案不算：详情里写薄モザイク也不影响，只看标签
    assert fc2._censor_label(tags) == expected


def test_caption_contains_censor_and_detail():
    caption = fc2._caption(
        {"title": "+++ FC2-PPV-4974501 sample", "size": "1 GiB", "pub_ts": 1000, "guid": "https://sukebei.nyaa.si/view/4974501"},
        "4974501",
        censor_label="无码",
        detail="这是翻译后的详情",
    )
    assert "🏷️ 无码" in caption
    assert "这是翻译后的详情" in caption
    assert len(caption) <= 1024


def test_keyboard_has_fc2_official_url_button():
    keyboard = fc2._keyboard("4974501")
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data == "fc2dl:4974501" for b in buttons)
    assert any(b.callback_data == "fc2no:4974501" for b in buttons)
    assert any(b.url == "https://adult.contents.fc2.com/article/4974501/" for b in buttons)


def test_translate_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(fc2.requests, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert fc2._translate_ja_to_zh("可愛い", None) == ""


# ---------------------------------------------------------------- nyaa 附图 + 谷歌翻译优先

NYAA_VIEW_HTML = """
<html><body>
<div id="torrent-description">
[![poster](https://s3.example.com/poster/FC2-PPV-5000545.jpg)](https://manko.fun)
https://imagetwist.com/abc123/240107020601c1b44.jpg#gallery
[![flyer](https://apiplayer.b-cdn.net/images/static_flyer.jpg)](https://manko.fun)
</div>
</body></html>
"""


def test_nyaa_detail_images_resolves_imagetwist_and_filters_ads(monkeypatch):
    def fake_http(url, proxy, verify_tls):
        if "imagetwist.com" in url:
            return page('<img src="https://img400.imagetwist.com/i/80069/abc123.jpg/240107020601c1b44.jpg">')
        return page(NYAA_VIEW_HTML)

    monkeypatch.setattr(fc2, "_http_get", fake_http)
    images = fc2._nyaa_detail_images("https://sukebei.nyaa.si/view/4748878", None, True)
    assert images == [
        "https://s3.example.com/poster/FC2-PPV-5000545.jpg",
        "https://img400.imagetwist.com/i/80069/abc123.jpg/240107020601c1b44.jpg",
    ]


def test_nyaa_detail_images_empty_guid():
    assert fc2._nyaa_detail_images(None, None, True) == []


def test_translate_prefers_google_over_mymemory(monkeypatch):
    class GoogleResp:
        status_code = 200

        def json(self):
            return [[["谷歌翻译结果", "元サッカー", None, None, 3]], None, "ja"]

    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return GoogleResp()

    monkeypatch.setattr(fc2.requests, "get", fake_get)
    assert fc2._translate_ja_to_zh("元サッカー", "http://proxy:7890") == "谷歌翻译结果"
    assert "translate.googleapis.com" in calls[0]
