import asyncio
import json
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
        self.messages = []
        self.deleted = []
        self.fail_groups = False
        self.fail_photos = 0
        self._next_id = 100

    def _message(self):
        self._next_id += 1
        return SimpleNamespace(message_id=self._next_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def send_photo(self, **kwargs):
        if self.fail_photos > 0:
            self.fail_photos -= 1
            raise RuntimeError("photo upload failed")
        self.photos.append(kwargs)
        return self._message()

    async def send_media_group(self, **kwargs):
        if self.fail_groups:
            raise RuntimeError("album upload failed")
        self.groups.append(kwargs)
        return [self._message() for _ in kwargs["media"]]

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return self._message()

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
        ("FC2PPV 1234567", "1234567"),
        ("FC2PPV5000545", "5000545"),
        ("[FC2-PPV-5000545]", "5000545"),
        ("FC2-5000545", "5000545"),
        ("FC2-PPV（5000545）", "5000545"),
        ("FC2PPV（5000545）", "5000545"),
        ("fc2-ppv-5000545 MixedCase", "5000545"),
        ("【FC2】1234567 素人", "1234567"),
        ("FC2-PPV-1234567【素人】", "1234567"),
        ("FC2", None),
        ("FC2-PPV-abc", None),
    ],
)
def test_extract_number(title, expected):
    assert fc2.extract_number(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        # 黑名单里有 5000545 时，这类非 FC2 名字以前会被误认为该编号
        "Ubuntu.afc25000545b.iso",
        "Ubuntu.fc25000545b.iso",
        "myfc25000545backup.iso",
        "ubuntu-24.04.1-live",
        "FC2-PPV-1234567890123",
    ],
)
def test_extract_number_needs_fc2_boundary(title):
    assert fc2.extract_number(title) is None


# ---------------------------------------------------------------- 官网取图


def test_fetch_official_images_prefers_cover_and_dedupes(monkeypatch):
    responder = FakeResponder({"/article/5000545/": page(OFFICIAL_HTML)})
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._fetch_official("5000545", None, True).images

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

    article = fc2._fetch_official("5000545", None, True)
    assert article.images == []
    assert article.failure_reason
    assert article.not_found is (response.status_code == 404)


def test_preview_images_fill_from_fc2ppvdb_up_to_seven(monkeypatch):
    responder = FakeResponder(
        {
            "/article/5000545/": official_page(2),
            "fc2ppvdb.com/articles/5000545": fc2ppvdb_page(5),
        }
    )
    monkeypatch.setattr(fc2, "_http_get", responder)

    images = fc2._preview_images("5000545", None, True).images

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

    images = fc2._preview_images("5000545", None, True).images

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

    images = fc2._preview_images("5000545", None, True).images

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

    images = fc2._preview_images("5000545", None, True).images

    assert len(images) == 7
    assert all("fc2ppvdb" not in url for url in images)
    assert not any("fc2ppvdb" in url for url in responder.calls)


# ---------------------------------------------------------------- 去重 / 静音 / 回补


@pytest.fixture
def fc2_clock(monkeypatch):
    now = [1_700_000_000.0]
    monkeypatch.setattr(fc2.time, "time", lambda: now[0])
    return now


def make_pusher(tmp_path, monkeypatch, **overrides):
    store = fc2.Fc2Store(str(tmp_path / "fc2_state.json"))
    bot = FakeBot()
    blacklist = fc2.Fc2Blacklist(str(tmp_path / "fc2_blacklist.json"))
    pusher = fc2.Fc2Pusher(bot, FakeConfig(**overrides), store, blacklist)
    return pusher, store, bot


async def test_seen_number_is_not_pushed_and_images_not_fetched(
    tmp_path, monkeypatch
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.remember(
        "5000545", {"hash": "a" * 40, "title": "FC2-PPV-5000545 x", "pub_ts": 10}
    )
    fetched = []
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(fetched.append(a) or [])))

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
    )

    assert fetched == []
    assert bot.photos == [] and bot.groups == []


async def test_muted_number_is_not_pushed(tmp_path, monkeypatch):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    # fc2no 同时写共享黑名单和本容器 state.muted；推送判定以黑名单为准。
    pusher.blacklist.add("5000545", "FC2-PPV-5000545 x", "fc2no")
    store.mute("5000545")
    fetched = []
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(fetched.append(a) or [])))

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
    )

    assert fetched == []
    assert bot.photos == [] and bot.groups == []
    assert store.entry("5000545") is None


async def test_muted_state_alone_does_not_block_push(tmp_path, monkeypatch):
    # state.muted 是每个容器各一份的兼容记录，不能成为跨容器解不开的第二真相源。
    pusher, store, _bot = make_pusher(tmp_path, monkeypatch)
    store.mute("5000545")
    assert pusher._blocked("5000545") is False


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
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/x.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"bytes"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    await pusher._poll_once(-1001234)

    assert len(bot.photos) == 10
    pushed = [call["caption"].split("\n")[0] for call in bot.photos]
    assert pushed[0] == "<b>FC2-PPV-5000020</b>"
    assert pushed[-1] == "<b>FC2-PPV-5000029</b>"
    assert len(store.seen) == 30
    assert store.last_ts == 1029


# ---------------------------------------------------------------- 状态 / 磁力 / 未配置


def test_unblocked_number_is_not_written_back_by_resident_store(tmp_path):
    # /fc2unblock 在回调实例里解黑后，常驻推送实例手里那份旧 muted
    # 不能再被下一次落盘写回 state（否则解黑当场失效）。
    path = str(tmp_path / "fc2_state.json")
    resident = fc2.Fc2Store(path)
    resident.remember("5000545", {"hash": "a" * 40, "title": "t", "pub_ts": 5})
    resident.mute("5000545")
    assert resident.is_muted("5000545")

    callback = fc2.Fc2Store(path)
    callback.unmute("05000545")  # 前导零变体也要一并清掉
    assert not callback.is_muted("5000545")

    resident.save()  # 常驻实例下一次落盘

    fresh = fc2.Fc2Store(path)
    assert fresh.muted == set()
    assert not resident.is_muted("5000545")


def test_noop_unmute_does_not_erase_later_mute(tmp_path):
    # A 对未静音编号做 no-op unmute 不能留下 False delta，否则会抹掉 B 后来的 mute。
    path = str(tmp_path / "fc2_state.json")
    first = fc2.Fc2Store(path)
    second = fc2.Fc2Store(path)

    first.unmute("5000545")
    second.mute("5000545")
    first.save()

    fresh = fc2.Fc2Store(path)
    assert "5000545" in fresh.muted
    assert fresh.is_muted("5000545")


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


async def test_card_send_failure_marks_retry_and_retries_next_poll(
    tmp_path, monkeypatch, fc2_clock
):
    # sol 的复现：last_ts=10，RSS 有 pub_ts=11 的条目，首次发卡片失败。
    # 游标照推 11（失败条目单独记在 retry 里），下一轮靠 retry 重新推。
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.last_ts = 10
    store.save()
    entries = [
        {
            "title": "FC2-PPV-5000545 x",
            "guid": "https://sukebei.nyaa.si/view/5000545",
            "hash": "a" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        }
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    bot.fail_photos = 1
    await pusher._poll_once(-1001234)

    assert bot.photos == []
    assert store.seen == {}
    assert set(store.retry) == {"5000545"}
    assert store.last_ts == 11
    assert store.retry["5000545"] == {
        "count": 1,
        "next_ts": fc2_clock[0] + 600,
        "last_reason": "stage=send_photo exception=RuntimeError",
    }

    await pusher._poll_once(-1001234)
    assert bot.photos == []
    assert store.retry["5000545"]["count"] == 1
    fc2_clock[0] = store.retry["5000545"]["next_ts"]
    await pusher._poll_once(-1001234)

    assert len(bot.photos) == 1
    assert bot.photos[0]["reply_markup"].inline_keyboard[0][0].callback_data == "fc2dl:5000545"
    assert store.entry("5000545")["messages"]
    assert store.retry == {}
    assert store.last_ts == 11


async def test_same_timestamp_failure_retried_after_cursor_moves(
    tmp_path, monkeypatch, fc2_clock
):
    # sol 复现：last_ts=10，A、B 都是 pub_ts=11；A 成功、B 封面发送失败。
    # 游标被 A 推到 11 之后，B 不能再被 pub_ts > last_ts 过滤掉。
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.last_ts = 10
    store.save()
    entries = [
        {
            "title": "FC2-PPV-5000545 a",
            "guid": "https://sukebei.nyaa.si/view/5000545",
            "hash": "a" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        },
        {
            "title": "FC2-PPV-5000546 b",
            "guid": "https://sukebei.nyaa.si/view/5000546",
            "hash": "b" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        },
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    original_send_photo = bot.send_photo
    fail = {"5000546": 1}

    async def flaky_send_photo(**kwargs):
        for number, count in fail.items():
            if count and number in kwargs.get("caption", ""):
                fail[number] = count - 1
                raise RuntimeError("photo upload failed")
        return await original_send_photo(**kwargs)

    monkeypatch.setattr(bot, "send_photo", flaky_send_photo)

    await pusher._poll_once(-1001234)

    # A 出去了，B 失败、单独挂在 retry 上，游标已经到 11
    assert store.entry("5000545") is not None
    assert store.entry("5000546") is None
    assert set(store.retry) == {"5000546"}
    assert store.last_ts == 11

    # 重启重载后 retry 仍在磁盘上
    reloaded = fc2.Fc2Store(str(store.path))
    assert reloaded.retry == store.retry
    assert set(reloaded.retry) == {"5000546"}

    fc2_clock[0] = store.retry["5000546"]["next_ts"]
    await pusher._poll_once(-1001234)

    # 第二轮：B 补推成功，A 不重推
    assert len(bot.photos) == 2
    assert store.entry("5000546")["messages"]
    assert store.retry == {}
    assert store.last_ts == 11


async def test_first_run_failure_not_swallowed_by_backfill(
    tmp_path, monkeypatch, fc2_clock
):
    # sol 复现：空状态、backfill_limit=2；A(11) 封面发送失败、B(12) 成功，
    # RSS 新增 C(13) 而 A 仍在 RSS 里。回补如果每轮重做，A 会被划进
    # backlog 记成 seen，从没发出去的卡片永久丢失。
    pusher, store, bot = make_pusher(tmp_path, monkeypatch, backfill_limit=2)
    entries = [
        {
            "title": "FC2-PPV-5000545 a",
            "guid": "https://sukebei.nyaa.si/view/5000545",
            "hash": "a" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        },
        {
            "title": "FC2-PPV-5000546 b",
            "guid": "https://sukebei.nyaa.si/view/5000546",
            "hash": "b" * 40,
            "pub_ts": 12,
            "size": "1 GiB",
        },
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    original_send_photo = bot.send_photo
    fail = {"5000545": 1}

    async def flaky_send_photo(**kwargs):
        for number, count in fail.items():
            if count and number in kwargs.get("caption", ""):
                fail[number] = count - 1
                raise RuntimeError("photo upload failed")
        return await original_send_photo(**kwargs)

    monkeypatch.setattr(bot, "send_photo", flaky_send_photo)

    await pusher._poll_once(-1001234)

    assert store.entry("5000545") is None  # A 失败，绝不能被回补记成 seen
    assert store.entry("5000546") is not None
    assert set(store.retry) == {"5000545"}
    assert store.backfill_done is True

    entries.append(
        {
            "title": "FC2-PPV-5000547 c",
            "guid": "https://sukebei.nyaa.si/view/5000547",
            "hash": "c" * 40,
            "pub_ts": 13,
            "size": "1 GiB",
        }
    )

    fc2_clock[0] = store.retry["5000545"]["next_ts"]
    await pusher._poll_once(-1001234)

    # 第二轮：A 被重试推出去（不是被当 backlog 记 seen），C 也推了
    assert store.entry("5000545")["messages"]
    assert store.entry("5000547")["messages"]
    assert store.retry == {}
    assert store.last_ts == 13


async def test_failed_entry_retried_without_blocking_later_entries(
    tmp_path, monkeypatch, fc2_clock
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.last_ts = 10
    store.save()
    entries = [
        {
            "title": "FC2-PPV-5000546 bad",
            "guid": "https://sukebei.nyaa.si/view/5000546",
            "hash": "a" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        },
        {
            "title": "FC2-PPV-5000547 good",
            "guid": "https://sukebei.nyaa.si/view/5000547",
            "hash": "b" * 40,
            "pub_ts": 12,
            "size": "1 GiB",
        },
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    original_send_photo = bot.send_photo

    async def flaky_send_photo(**kwargs):
        if "5000546" in kwargs.get("caption", ""):
            raise RuntimeError("photo upload failed")
        return await original_send_photo(**kwargs)

    monkeypatch.setattr(bot, "send_photo", flaky_send_photo)

    await pusher._poll_once(-1001234)

    # 失败的那条不在 seen 里、挂在 retry 上；后面的条目照常推、游标照走
    assert store.entry("5000546") is None
    assert store.entry("5000547")["messages"]
    assert set(store.retry) == {"5000546"}
    assert store.last_ts == 12

    photos_after_first = len(bot.photos)
    fc2_clock[0] = store.retry["5000546"]["next_ts"]
    await pusher._poll_once(-1001234)

    # 第二轮仍会重试失败条目（这次还是失败），已成功的条目不会重推
    assert len(bot.photos) == photos_after_first
    assert set(store.retry) == {"5000546"}
    assert store.last_ts == 12


async def test_store_mute_returns_messages_and_forget_drops_entry(tmp_path):
    store = fc2.Fc2Store(str(tmp_path / "fc2_state.json"))
    store.remember(
        "5000545", {"hash": "a" * 40, "title": "t", "pub_ts": 5}, messages=[7, 8]
    )
    store.save()

    assert store.mute("5000545") == [7, 8]
    assert store.mute("4999999") == []
    assert {"5000545", "4999999"} <= store.muted

    store.forget("5000545")
    assert store.entry("5000545") is None
    fresh = fc2.Fc2Store(str(store.path))
    assert "5000545" in fresh.muted
    assert fresh.entry("5000545") is None


async def test_reload_after_failure_keeps_retry_entry(tmp_path, monkeypatch):
    # 失败后重启进程：重试标记落在磁盘上，新实例照样认得这条
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.last_ts = 10
    store.save()
    entries = [
        {
            "title": "FC2-PPV-5000545 x",
            "guid": "https://sukebei.nyaa.si/view/5000545",
            "hash": "a" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        }
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=([])))
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    await pusher._poll_once(-1001234)
    assert set(store.retry) == {"5000545"}

    reloaded = fc2.Fc2Store(str(store.path))
    assert reloaded.retry == store.retry
    assert set(reloaded.retry) == {"5000545"}
    assert reloaded.seen == {}


def test_finalize_keeps_muted_written_by_other_instance(tmp_path):
    # 推送器和回调（真实运行时）是两个 store 实例：回调 new 一个实例写 muted，
    # 推送器手里那个旧实例落盘时不能把 muted 用旧内存整份覆盖掉。
    path = str(tmp_path / "fc2_state.json")
    pusher_store = fc2.Fc2Store(path)
    pusher_store.last_ts = 11
    pusher_store.save()
    pusher_store.remember(
        "5000545", {"hash": "a" * 40, "title": "t", "pub_ts": 11}, messages=[2001]
    )
    pusher_store.save()

    callback_store = fc2.Fc2Store(path)
    assert callback_store.mute("5000545") == [2001]

    assert (
        pusher_store.finalize(
            "5000545", {"hash": "a" * 40, "title": "t", "pub_ts": 11}, [2001, 2002]
        )
        is False
    )
    fresh = fc2.Fc2Store(path)
    assert "5000545" in fresh.muted
    assert fresh.entry("5000545")["messages"] == [2001]


async def test_window_state_has_card_before_extras_and_finalize_keeps_muted(
    tmp_path, monkeypatch
):
    # 封面成功即落盘（按钮期间可下载/可静音）；附图期间被 muted 则删图、
    # 不记正常推送、muted 不被覆盖。
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fc2,
        "_preview_images",
        lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"])),
    )
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1", b"2", b"3"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")

    gate = asyncio.Event()
    started = asyncio.Event()

    async def slow_group(**kwargs):
        started.set()
        await gate.wait()
        return [bot._message() for _ in kwargs["media"]]

    monkeypatch.setattr(bot, "send_media_group", slow_group)
    task = asyncio.create_task(
        pusher._push_entry(
            {
                "title": "FC2-PPV-5000545 x",
                "hash": "a" * 40,
                "pub_ts": 11,
                "guid": "https://sukebei.nyaa.si/view/1",
            },
            -1001234,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)

    # 窗口期：卡片已经在状态里，附图还挂着
    card_id = 101
    assert store.entry("5000545")["messages"] == [card_id]

    store.mute("5000545")
    pusher.blacklist.add("5000545", "FC2-PPV-5000545 x", "fc2no")
    gate.set()
    await asyncio.wait_for(task, timeout=2)

    # 附图传完后：muted 保住，不记成正常推送
    assert "5000545" in store.muted
    assert store.entry("5000545") is None
    fresh = fc2.Fc2Store(str(store.path))
    assert "5000545" in fresh.muted
    assert fresh.entry("5000545") is None


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


# ---------------------------------------------------------------- 私聊推送目标

def test_target_chat_id_prefers_chat_id_then_channel_then_super_user(monkeypatch):
    class Cfg:
        def __init__(self, fc2, super_user=None):
            self.fc2 = fc2
            self.super_user = super_user

        def get(self, path, default=None):
            if str(path).startswith("fc2."):
                return self.fc2.get(str(path)[4:], default)
            if path == "bot.super_user":
                return self.super_user
            return default

    bot = FakeBot()
    assert fc2.Fc2Pusher(bot, config=Cfg({"chat_id": 111}))._target_chat_id() == 111
    assert fc2.Fc2Pusher(bot, config=Cfg({"channel_id": -100222}))._target_chat_id() == -100222
    assert fc2.Fc2Pusher(bot, config=Cfg({"channel_id": "<channel_id>"}), )._target_chat_id() is None
    pusher = fc2.Fc2Pusher(bot, config=Cfg({}, super_user=555164303))
    assert pusher._target_chat_id() == 555164303


async def test_run_skipped_when_enabled_false(tmp_path, monkeypatch, caplog):
    pusher, _store, _bot = make_pusher(tmp_path, monkeypatch, channel_id=-1001234)
    monkeypatch.setattr(pusher, "_setting", lambda key, default=None: False if key == "enabled" else pusher.config.get(f"fc2.{key}", default))
    calls = []
    monkeypatch.setattr(fc2, "_http_get", lambda *a, **k: calls.append(a))
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(pusher.run(), timeout=1)
    assert calls == []


async def test_backfill_does_not_let_old_duplicate_block_new_push(tmp_path, monkeypatch):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch, backfill_limit=2)
    entries = [
        {"title": "FC2-PPV-5000545 old", "guid": "https://sukebei.nyaa.si/view/1",
         "hash": "a" * 40, "pub_ts": 1000, "size": "1 GiB"},
        {"title": "FC2-PPV-5000001", "guid": "https://sukebei.nyaa.si/view/2",
         "hash": "b" * 40, "pub_ts": 1001, "size": "1 GiB"},
        {"title": "FC2-PPV-5000545 new", "guid": "https://sukebei.nyaa.si/view/3",
         "hash": "c" * 40, "pub_ts": 1002, "size": "1 GiB"},
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/x.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"bytes"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    await pusher._poll_once(-1001234)

    # 要推的最后两条都推了，包括新版 5000545；旧版只记不推、且不能先占 seen
    assert len(bot.photos) == 2
    assert store.entry("5000545")["messages"] != []
    assert store.last_ts == 1002


async def test_card_keyboard_rides_on_single_photo_not_album(tmp_path, monkeypatch):
    # 媒体组客户端不渲染内联键盘（见 Fc2Channel._push_entry 注释），
    # 所以按钮必须长在单张封面卡片上，其余图另发一个不带键盘的媒体组。
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fc2,
        "_preview_images",
        lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"])),
    )
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1", b"2", b"3"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11, "guid": "https://sukebei.nyaa.si/view/1"},
        -1001234,
    )

    # 卡片：单张封面 + caption + 键盘，客户端才会画按钮
    assert len(bot.photos) == 1
    card = bot.photos[0]
    assert card["photo"] == b"1"
    assert "<b>FC2-PPV-5000545</b>" in card["caption"]
    assert card["parse_mode"] == fc2.ParseMode.HTML
    kb = card["reply_markup"]
    assert kb.inline_keyboard[0][0].callback_data == "fc2dl:5000545"
    assert kb.inline_keyboard[0][1].callback_data == "fc2no:5000545"
    assert kb.inline_keyboard[1][0].url == "https://adult.contents.fc2.com/article/5000545/"

    # 其余预览图：独立媒体组，不带 caption / 键盘
    assert len(bot.groups) == 1
    media = bot.groups[0]["media"]
    assert [item.media.input_file_content for item in media] == [b"2", b"3"]
    assert all(item.caption is None for item in media)
    assert "reply_markup" not in bot.groups[0]

    # 不再往媒体组消息上 edit 键盘，也不发组外文字按钮消息
    assert bot.markups == []
    assert bot.messages == []
    assert len(store.entry("5000545")["messages"]) == 3


async def test_single_photo_card_carries_keyboard_without_album(tmp_path, monkeypatch):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"only"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11, "guid": "https://sukebei.nyaa.si/view/1"},
        -1001234,
    )

    assert len(bot.photos) == 1
    assert bot.photos[0]["reply_markup"].inline_keyboard[0][0].callback_data == "fc2dl:5000545"
    assert bot.groups == []
    assert bot.markups == []
    assert len(store.entry("5000545")["messages"]) == 1


async def test_two_photos_send_extra_as_single_photo_not_one_item_album(tmp_path, monkeypatch):
    # 媒体组最少 2 张，附图只剩 1 张时不能塞进媒体组
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg", "https://img/2.jpg"]))
    )
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1", b"2"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11, "guid": "https://sukebei.nyaa.si/view/1"},
        -1001234,
    )

    assert len(bot.photos) == 2
    assert bot.photos[0]["photo"] == b"1"
    assert bot.photos[0]["reply_markup"] is not None
    assert bot.photos[1]["photo"] == b"2"
    assert "reply_markup" not in bot.photos[1]
    assert bot.groups == []
    assert len(store.entry("5000545")["messages"]) == 2


async def test_preview_album_failure_keeps_card_and_state(tmp_path, monkeypatch, caplog):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    bot.fail_groups = True
    monkeypatch.setattr(
        fc2,
        "_preview_images",
        lambda *a, **k: fc2.OfficialArticle(images=(["https://img/1.jpg", "https://img/2.jpg", "https://img/3.jpg"])),
    )
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"1", b"2", b"3"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *a, **k: "")

    with caplog.at_level(logging.WARNING):
        await pusher._push_entry(
            {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11, "guid": "https://sukebei.nyaa.si/view/1"},
            -1001234,
        )

    # 卡片带按钮发出去了就不该因为附图失败丢掉状态（否则下一轮重推整张卡）
    assert len(bot.photos) == 1
    assert len(store.entry("5000545")["messages"]) == 1
    assert any(record.levelno == logging.WARNING for record in caplog.records)


async def test_blacklisted_number_is_not_pushed_and_does_not_touch_state(
    tmp_path, monkeypatch
):
    # 黑名单独立于 fc2_state.json：state 被重置后黑名单照样拦推送。
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    pusher.blacklist.add("5000545", "FC2-PPV-5000545 x", "fc2no")
    fetched = []
    monkeypatch.setattr(fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=(fetched.append(a) or [])))

    await pusher._push_entry(
        {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
    )

    assert fetched == []
    assert bot.photos == [] and bot.groups == []
    assert store.entry("5000545") is None
    assert store.muted == set()


async def test_poll_skips_blacklisted_entry(tmp_path, monkeypatch):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    pusher.blacklist.add("5000545", "FC2-PPV-5000545 x", "fc2no")
    entries = [
        {
            "title": "FC2-PPV-5000545 x",
            "guid": "https://sukebei.nyaa.si/view/5000545",
            "hash": "a" * 40,
            "pub_ts": 11,
            "size": "1 GiB",
        }
    ]
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    await pusher._poll_once(-1001234)

    assert bot.photos == [] and bot.groups == []
    assert store.seen == {}


# ---------------------------------------------------------------- 已完成删卡


def test_message_ids_is_read_only(tmp_path):
    # 完成删卡只能读消息 id；绝不能像 mute() 那样写 muted / 黑名单。
    store = fc2.Fc2Store(str(tmp_path / "fc2_state.json"))
    store.remember(
        "5000545",
        {"hash": "a" * 40, "title": "FC2-PPV-5000545 x", "pub_ts": 1},
        messages=[111, 112],
    )

    ids = store.message_ids("05000545")

    assert ids == [111, 112]
    assert store.muted == set()
    # 再读一次也不会落盘、不会改 seen
    assert store.message_ids("5000545") == [111, 112]


async def test_done_deletion_consumer_deletes_own_card_and_acks(
    tmp_path, monkeypatch
):
    pending_path = tmp_path / "done_pending.jsonl"
    pusher, store, bot = make_pusher(
        tmp_path, monkeypatch, done_pending_file=str(pending_path)
    )
    store.remember(
        "5000545",
        {"hash": "a" * 40, "title": "FC2-PPV-5000545 x", "pub_ts": 1},
        messages=[111, 112],
    )
    queue = fc2.DoneDeletionQueue(str(pending_path))
    assert queue.request("05000545", title="FC2-PPV-5000545 x") is True

    handled = await pusher._consume_done_deletions()

    assert handled == 1
    assert bot.deleted == [(-1001234, 111), (-1001234, 112)]
    assert queue.pending() == []
    # 只删卡，不写 muted / 黑名单
    assert store.muted == set()
    assert pusher.blacklist.contains("5000545") is False


async def test_done_deletion_consumer_skips_blacklisted_card(tmp_path, monkeypatch):
    pending_path = tmp_path / "done_pending.jsonl"
    pusher, store, bot = make_pusher(
        tmp_path, monkeypatch, done_pending_file=str(pending_path)
    )
    pusher.blacklist.add("5000545", "FC2-PPV-5000545 x", "fc2no")
    store.remember(
        "5000545",
        {"hash": "a" * 40, "title": "FC2-PPV-5000545 x", "pub_ts": 1},
        messages=[111],
    )
    queue = fc2.DoneDeletionQueue(str(pending_path))
    queue.request("5000545")

    await pusher._consume_done_deletions()

    assert bot.deleted == []
    assert queue.pending() == []


async def test_done_deletion_consumer_acks_missing_card(tmp_path, monkeypatch):
    pending_path = tmp_path / "done_pending.jsonl"
    pusher, _store, bot = make_pusher(
        tmp_path, monkeypatch, done_pending_file=str(pending_path)
    )
    queue = fc2.DoneDeletionQueue(str(pending_path))
    queue.request("5000999")

    handled = await pusher._consume_done_deletions()

    assert handled == 1
    assert bot.deleted == []
    assert queue.pending() == []


async def test_poll_consumes_done_deletions_even_without_new_feed(
    tmp_path, monkeypatch
):
    pending_path = tmp_path / "done_pending.jsonl"
    pusher, store, bot = make_pusher(
        tmp_path, monkeypatch, done_pending_file=str(pending_path)
    )
    store.remember(
        "5000545",
        {"hash": "a" * 40, "title": "FC2-PPV-5000545 x", "pub_ts": 1},
        messages=[111],
    )
    fc2.DoneDeletionQueue(str(pending_path)).request("5000545")
    # RSS 拉取失败 / 空列表也必须先删完成卡
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: [])

    await pusher._poll_once(-1001234)

    assert bot.deleted == [(-1001234, 111)]


async def test_push_entry_success_records_actual_chat_and_info(
    tmp_path, monkeypatch, caplog
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    monkeypatch.setattr(fc2, "_preview_images", lambda *args, **kwargs: fc2.OfficialArticle(images=(["https://image.invalid/cover.jpg"])))
    monkeypatch.setattr(fc2, "_download_images", lambda *args, **kwargs: ([b"image"], []))
    monkeypatch.setattr(fc2, "_translate_ja_to_zh", lambda *args, **kwargs: "")
    with caplog.at_level(logging.INFO):
        pushed = await pusher._push_entry(
            {"title": "FC2-PPV-5000545 sample", "hash": "a" * 40, "pub_ts": 1},
            -100987654321,
        )

    assert pushed is True
    record = store.entry("5000545")
    assert record is not None
    assert record["chat_id"] == -100987654321
    assert record["messages"] == [101]
    assert "FC2 卡片已推送 number=5000545 msg=101 chat=已脱敏" in caplog.text
    assert "987654321" not in caplog.text
    assert len(bot.photos) == 1


async def test_official_404_enters_persistent_dead_and_is_not_requested_again(
    tmp_path, monkeypatch, fc2_clock, caplog
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.last_ts = 10
    store.backfill_done = True
    entries = [{"title": "FC2-PPV-4972315 sample", "hash": "a" * 40, "pub_ts": 11}]
    responder = FakeResponder({"/article/4972315/": page("", status=404)})
    monkeypatch.setattr(fc2, "_http_get", responder)
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    with caplog.at_level(logging.INFO):
        await pusher._poll_once(-1001234)
        fc2_clock[0] += 365 * 24 * 60 * 60
        entries[0]["pub_ts"] = 12
        await pusher._poll_once(-1001234)
        assert await pusher._push_entry(entries[0], -1001234) is True

    assert responder.calls == [fc2.ARTICLE_URL.format("4972315")]
    assert store.retry == {}
    assert store.dead == {
        "4972315": {"count": 1, "last_reason": "stage=official_fetch status=404"}
    }
    assert fc2.Fc2Store(str(store.path)).dead == store.dead
    assert bot.photos == []
    assert "number=4972315 stage=official_fetch status=404" in caplog.text
    dead_logs = [record for record in caplog.records if "进入死信" in record.getMessage()]
    assert len(dead_logs) == 1
    assert dead_logs[0].levelno == logging.INFO


async def test_retry_seventh_failure_dead_once_and_exponential_deadlines(
    tmp_path, monkeypatch, fc2_clock, caplog
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    store.last_ts = 10
    store.backfill_done = True
    entries = [{"title": "FC2-PPV-1698878 sample", "hash": "b" * 40, "pub_ts": 11}]
    responder = FakeResponder({"/article/1698878/": page("", status=503)})
    monkeypatch.setattr(fc2, "_http_get", responder)
    monkeypatch.setattr(fc2, "_fetch_entries", lambda *a, **k: entries)
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)

    with caplog.at_level(logging.INFO):
        for count in range(1, 7):
            attempted_at = fc2_clock[0]
            await pusher._poll_once(-1001234)
            assert store.retry["1698878"] == {
                "count": count,
                "next_ts": attempted_at + 600 * 2 ** (count - 1),
                "last_reason": "stage=official_fetch status=503",
            }
            assert store.dead == {}
            assert len(responder.calls) == count
            next_ts = store.retry["1698878"]["next_ts"]
            fc2_clock[0] = next_ts - 1
            await pusher._poll_once(-1001234)
            assert len(responder.calls) == count
            assert store.retry["1698878"]["count"] == count
            fc2_clock[0] = next_ts
        await pusher._poll_once(-1001234)
        await pusher._poll_once(-1001234)
        store.mark_retry("1698878", "stage=official_fetch status=503")

    assert len(responder.calls) == 7
    assert store.retry == {}
    assert store.dead["1698878"] == {
        "count": 7,
        "last_reason": "stage=official_fetch status=503",
    }
    assert fc2.Fc2Store(str(store.path)).dead == store.dead
    assert bot.photos == []
    dead_logs = [record for record in caplog.records if "进入死信" in record.getMessage()]
    assert len(dead_logs) == 1
    assert dead_logs[0].levelno == logging.INFO
    assert "number=1698878 count=7 stage=official_fetch status=503" in caplog.text


async def test_legacy_retry_list_loads_and_resumes_with_counted_backoff(
    tmp_path, monkeypatch, fc2_clock
):
    path = tmp_path / "fc2_state.json"
    path.write_text(
        json.dumps({"last_ts": 11, "retry": ["5000545"], "backfill_done": True}),
        encoding="utf-8",
    )
    store = fc2.Fc2Store(str(path))
    assert store.retry == {
        "5000545": {"count": 0, "next_ts": 0.0, "last_reason": "legacy_retry"}
    }
    pusher = fc2.Fc2Pusher(
        FakeBot(), FakeConfig(), store, fc2.Fc2Blacklist(str(tmp_path / "blacklist.json"))
    )
    responder = FakeResponder({"/article/5000545/": page("", status=503)})
    monkeypatch.setattr(fc2, "_http_get", responder)
    monkeypatch.setattr(
        fc2,
        "_fetch_entries",
        lambda *a, **k: [{"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}],
    )
    monkeypatch.setattr(fc2, "PUSH_DELAY", 0)
    await pusher._poll_once(-1001234)
    await pusher._poll_once(-1001234)

    assert responder.calls == [fc2.ARTICLE_URL.format("5000545")]
    assert store.retry == {
        "5000545": {
            "count": 1,
            "next_ts": fc2_clock[0] + 600,
            "last_reason": "stage=official_fetch status=503",
        }
    }
    assert fc2.Fc2Store(str(path)).retry == store.retry
    assert json.loads(path.read_text(encoding="utf-8"))["retry"] == store.retry


async def test_push_entry_fetches_official_once_for_images_and_details(
    tmp_path, monkeypatch
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    html = official_page(6).text.replace(
        "</head>",
        '<meta property="og:description" content="FC2-PPV-5000545 日本語説明" /></head>',
    ).replace("</body>", '<a data-tag="無修正">tag</a></body>')
    responder = FakeResponder({"/article/5000545/": page(html)})
    monkeypatch.setattr(fc2, "_http_get", responder)
    monkeypatch.setattr(fc2, "_download_images", lambda *a, **k: ([b"cover"], []))
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])
    translated = []
    monkeypatch.setattr(
        fc2, "_translate_ja_to_zh", lambda text, proxy: translated.append(text) or "中文说明"
    )

    assert await pusher._push_entry(
        {"title": "FC2-PPV-5000545 sample", "hash": "a" * 40, "pub_ts": 11}, -1001234
    ) is True

    assert responder.calls == [fc2.ARTICLE_URL.format("5000545")]
    assert translated == ["日本語説明"]
    assert "无码" in bot.photos[0]["caption"]
    assert "中文说明" in bot.photos[0]["caption"]
    assert store.entry("5000545") is not None


async def test_push_entry_official_exception_warning_is_sanitized(
    tmp_path, monkeypatch, fc2_clock, caplog
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    sensitive = "https://api.telegram.org/botTEST_SECRET/987654321"
    responder = FakeResponder(errors={"/article/": TimeoutError(sensitive)})
    monkeypatch.setattr(fc2, "_http_get", responder)
    with caplog.at_level(logging.WARNING):
        assert await pusher._push_entry(
            {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
        ) is False

    reason = "stage=official_fetch exception=TimeoutError"
    assert store.retry["5000545"]["last_reason"] == reason
    assert "number=5000545 " + reason in caplog.text
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    assert "TEST_SECRET" not in caplog.text
    assert "987654321" not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert bot.photos == []


@pytest.mark.parametrize(
    "response, error, expected_reason",
    [
        (page("", status=502), None, "status=502"),
        (None, TimeoutError("https://api.telegram.org/botTEST_SECRET/987654321"), "exception=TimeoutError"),
        (SimpleNamespace(status_code=200, content=b"html", headers={"Content-Type": "text/html"}), None, "status=200 reason=not_image"),
        (SimpleNamespace(status_code=200, content=b"x" * (fc2.IMAGE_MAX_BYTES + 1), headers={"Content-Type": "image/jpeg"}), None, "status=200 reason=too_large"),
        (SimpleNamespace(status_code=200, content=b"", headers={"Content-Type": "image/jpeg"}), None, "status=200 reason=empty_image"),
    ],
    ids=["http", "exception", "content-type", "size", "empty"],
)
async def test_push_entry_image_failure_records_safe_stage(
    tmp_path, monkeypatch, fc2_clock, caplog, response, error, expected_reason
):
    pusher, store, bot = make_pusher(tmp_path, monkeypatch)
    monkeypatch.setattr(
        fc2, "_preview_images", lambda *a, **k: fc2.OfficialArticle(images=["https://images.invalid/TEST_SECRET.jpg"])
    )
    monkeypatch.setattr(fc2, "_nyaa_detail_images", lambda *a, **k: [])

    def image_response(*args):
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(fc2, "_http_get", image_response)
    with caplog.at_level(logging.WARNING):
        assert await pusher._push_entry(
            {"title": "FC2-PPV-5000545 x", "hash": "a" * 40, "pub_ts": 11}, -1001234
        ) is False

    reason = "stage=image_download image=1 " + expected_reason
    assert store.retry["5000545"]["last_reason"] == reason
    assert "number=5000545 " + reason in caplog.text
    assert "TEST_SECRET" not in caplog.text
    assert "987654321" not in caplog.text
    assert "api.telegram.org" not in caplog.text
    assert "images.invalid" not in caplog.text
    assert bot.photos == []


def test_download_images_retains_valid_bytes_and_failure_reasons(monkeypatch):
    responder = FakeResponder(
        {
            "good": SimpleNamespace(status_code=200, content=b"image", headers={"Content-Type": "image/png"}),
            "bad": page("", status=503),
        }
    )
    monkeypatch.setattr(fc2, "_http_get", responder)
    photos, failures = fc2._download_images(["https://image.invalid/good", "https://image.invalid/bad"])
    assert photos == [b"image"]
    assert failures == ["stage=image_download image=2 status=503"]
