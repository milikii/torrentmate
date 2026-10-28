from lib.magnet import (
    build_magnet,
    contains_magnet,
    extract_magnets,
    normalize_infohash,
    parse_magnet,
)

HASH_HEX = "0123456789abcdef0123456789abcdef01234567"
HASH_HEX_2 = "89abcdef0123456789abcdef0123456789abcdef"
HASH_BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def test_extracts_several_links_from_one_message():
    text = (
        "这几部一起下\n"
        f"magnet:?xt=urn:btih:{HASH_HEX}&dn=SSIS-123\n"
        f"magnet:?xt=urn:btih:{HASH_HEX_2}&dn=%E5%9B%BD%E4%BA%A7MD-001\n"
    )
    links = extract_magnets(text)

    assert [link.info_hash for link in links] == [HASH_HEX, HASH_HEX_2]
    assert links[0].name == "SSIS-123"
    assert links[1].name == "国产MD-001"


def test_deduplicates_by_info_hash():
    text = f"magnet:?xt=urn:btih:{HASH_HEX}&dn=a\nmagnet:?xt=urn:btih:{HASH_HEX.upper()}&dn=b"
    assert len(extract_magnets(text)) == 1


def test_base32_hash_is_normalized_to_hex():
    assert normalize_infohash(HASH_BASE32) == "00443214c74254b635cf84653a56d7c675be77df"
    link = parse_magnet(f"magnet:?xt=urn:btih:{HASH_BASE32}")
    assert link is not None
    assert link.info_hash == "00443214c74254b635cf84653a56d7c675be77df"


def test_trailing_chinese_punctuation_is_removed():
    link = parse_magnet(f"magnet:?xt=urn:btih:{HASH_HEX}，")
    assert link is not None
    assert link.raw.endswith(HASH_HEX)


def test_invalid_or_non_btih_links_are_ignored():
    assert extract_magnets("magnet:?xt=urn:sha1:abc") == []
    assert parse_magnet("https://example.com/a.torrent") is None
    assert contains_magnet("just a text") is False


def test_size_and_trackers_are_parsed():
    link = parse_magnet(
        f"magnet:?xt=urn:btih:{HASH_HEX}&xl=12345&tr=udp://tracker:80&tr=http://t2/announce"
    )
    assert link is not None
    assert link.size == 12345
    assert link.trackers == ("udp://tracker:80", "http://t2/announce")


def test_limit_and_build_magnet():
    text = "\n".join(f"magnet:?xt=urn:btih:{index:040x}" for index in range(1, 6))
    assert len(extract_magnets(text, limit=3)) == 3
    assert build_magnet(HASH_HEX).endswith(HASH_HEX)
