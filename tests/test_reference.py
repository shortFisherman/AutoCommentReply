"""Offline tests for the pure Bilibili comment reference layer."""

from __future__ import annotations

import pytest

from auto_comment_reply.errors import ParameterError
from auto_comment_reply.models import VideoInfo
from auto_comment_reply.reference import (
    CommentReference,
    DiscussionReference,
    build_discussion_reference,
    parse_comment_reference,
)

BVID = "BV1xx411c7mD"
AID = 42
ROOT_COMMENT_ID = 123456789
SECONDARY_COMMENT_ID = 987654321
OTHER_COMMENT_ID = 555666777


def video_info(*, bvid: str = BVID, aid: int = AID) -> VideoInfo:
    return VideoInfo(
        aid=aid,
        bvid=bvid,
        title="fixture",
        owner_id=7,
        owner_name="owner",
    )


def bilibili_url(
    path: str = f"/video/{BVID}",
    *,
    query: str = "",
    fragment: str = "",
) -> str:
    url = f"https://www.bilibili.com{path}"
    if query:
        url = f"{url}?{query}"
    if fragment:
        url = f"{url}#{fragment}"
    return url


def test_parses_bv_link_with_secondary_focus() -> None:
    reference = parse_comment_reference(
        bilibili_url(
            query=(f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}")
        )
    )

    assert reference == CommentReference(
        bvid=BVID,
        aid=None,
        root_comment_id=ROOT_COMMENT_ID,
        secondary_comment_id=SECONDARY_COMMENT_ID,
        fragment_comment_id=None,
        focus_comment_id=SECONDARY_COMMENT_ID,
    )


def test_parses_av_link_with_reply_fragment_focus() -> None:
    reference = parse_comment_reference(
        bilibili_url(
            path=f"/video/av{AID}",
            query=f"comment_root_id={ROOT_COMMENT_ID}",
            fragment=f"reply{SECONDARY_COMMENT_ID}",
        )
    )

    assert reference.aid == AID
    assert reference.bvid is None
    assert reference.fragment_comment_id == SECONDARY_COMMENT_ID
    assert reference.focus_comment_id == SECONDARY_COMMENT_ID
    assert reference.secondary_comment_id is None


def test_parses_numeric_aid_path() -> None:
    reference = parse_comment_reference(
        bilibili_url(path=f"/video/{AID}", query=f"comment_root_id={ROOT_COMMENT_ID}")
    )

    assert reference.aid == AID
    assert reference.bvid is None


def test_allows_www_mobile_and_bare_bilibili_hosts() -> None:
    urls = [
        f"https://www.bilibili.com/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}",
        f"https://m.bilibili.com/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}",
        f"https://bilibili.com/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}",
    ]

    for url in urls:
        assert parse_comment_reference(url).root_comment_id == ROOT_COMMENT_ID


def test_tracking_parameters_are_ignored_and_parsing_is_idempotent() -> None:
    clean = parse_comment_reference(bilibili_url(query=f"comment_root_id={ROOT_COMMENT_ID}"))
    tracked = parse_comment_reference(
        bilibili_url(
            query=(
                f"comment_root_id={ROOT_COMMENT_ID}"
                "&share_tag=phone&unique_k=abc123&vd_source=xyz&p=1"
            )
        )
    )
    reordered = parse_comment_reference(
        bilibili_url(
            query=(f"comment_secondary_id={SECONDARY_COMMENT_ID}&comment_root_id={ROOT_COMMENT_ID}")
        )
    )

    assert clean == tracked
    assert reordered == parse_comment_reference(
        bilibili_url(
            query=(f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}")
        )
    )


def test_secondary_and_fragment_are_equivalent_focus_candidates() -> None:
    secondary_only = parse_comment_reference(
        bilibili_url(
            query=(f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}")
        )
    )
    fragment_only = parse_comment_reference(
        bilibili_url(
            query=f"comment_root_id={ROOT_COMMENT_ID}",
            fragment=f"reply{SECONDARY_COMMENT_ID}",
        )
    )

    assert secondary_only.focus_comment_id == SECONDARY_COMMENT_ID
    assert fragment_only.focus_comment_id == SECONDARY_COMMENT_ID


def test_equal_secondary_and_fragment_focus_is_accepted() -> None:
    reference = parse_comment_reference(
        bilibili_url(
            query=(
                f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}"
            ),
            fragment=f"reply{SECONDARY_COMMENT_ID}",
        )
    )

    assert reference.focus_comment_id == SECONDARY_COMMENT_ID


def test_conflicting_secondary_and_fragment_focus_is_rejected() -> None:
    with pytest.raises(ParameterError, match="不一致"):
        parse_comment_reference(
            bilibili_url(
                query=(
                    f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}"
                ),
                fragment=f"reply{OTHER_COMMENT_ID}",
            )
        )


def test_focus_is_kept_separate_from_root() -> None:
    reference = parse_comment_reference(
        bilibili_url(
            query=(f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}")
        )
    )
    discussion = build_discussion_reference(video_info(), reference)

    assert discussion.root_comment_id == ROOT_COMMENT_ID
    assert discussion.focus_comment_id == SECONDARY_COMMENT_ID
    assert discussion.root_comment_id != discussion.focus_comment_id


def test_no_focus_candidate_is_allowed() -> None:
    reference = parse_comment_reference(bilibili_url(query=f"comment_root_id={ROOT_COMMENT_ID}"))

    assert reference.secondary_comment_id is None
    assert reference.fragment_comment_id is None
    assert reference.focus_comment_id is None
    assert build_discussion_reference(video_info(), reference).focus_comment_id is None


def test_leading_zero_positive_ints_are_normalized() -> None:
    reference = parse_comment_reference(
        bilibili_url(
            query=(
                f"comment_root_id=0{ROOT_COMMENT_ID}&comment_secondary_id=0{SECONDARY_COMMENT_ID}"
            )
        )
    )

    assert reference.root_comment_id == ROOT_COMMENT_ID
    assert reference.secondary_comment_id == SECONDARY_COMMENT_ID
    assert reference.focus_comment_id == SECONDARY_COMMENT_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "BV1xx411c7mD?comment_root_id=123",
        "www.bilibili.com/video/BV1xx411c7mD/?comment_root_id=123",
        "https://b23.tv/BV1xx411c7mD?comment_root_id=123",
        "https://example.com/video/BV1xx411c7mD/?comment_root_id=123",
        "ftp://www.bilibili.com/video/BV1xx411c7mD/?comment_root_id=123",
    ],
)
def test_rejects_missing_or_non_bilibili_urls(url: str) -> None:
    with pytest.raises(ParameterError):
        parse_comment_reference(url)


def test_rejects_non_video_or_identifierless_urls() -> None:
    urls = [
        bilibili_url(path="/video/", query=f"comment_root_id={ROOT_COMMENT_ID}"),
        bilibili_url(path="/read/cv123", query=f"comment_root_id={ROOT_COMMENT_ID}"),
        f"https://www.bilibili.com/?comment_root_id={ROOT_COMMENT_ID}",
    ]

    for url in urls:
        with pytest.raises(ParameterError):
            parse_comment_reference(url)


@pytest.mark.parametrize(
    ("query", "fragment"),
    [
        ("", ""),
        ("comment_root_id=", ""),
        ("comment_root_id=0", ""),
        ("comment_root_id=-1", ""),
        ("comment_root_id=abc", ""),
        ("comment_root_id=1.5", ""),
        ("comment_root_id=123&comment_secondary_id=abc", ""),
        ("comment_root_id=123&comment_secondary_id=0", ""),
        ("comment_root_id=123&comment_secondary_id=", ""),
        ("comment_root_id=123", "replyabc"),
        ("comment_root_id=123", "reply0"),
        ("comment_root_id=123", "reply-1"),
        ("comment_root_id=123", "comment123"),
        ("comment_root_id=123&comment_root_id=456", ""),
        (
            "comment_root_id=123&comment_secondary_id=1&comment_secondary_id=2",
            "",
        ),
    ],
)
def test_rejects_invalid_comment_parameters(query: str, fragment: str) -> None:
    with pytest.raises(ParameterError):
        parse_comment_reference(bilibili_url(query=query, fragment=fragment))


def test_error_messages_do_not_leak_query_values() -> None:
    secret_value = "top-secret-token"
    url = f"https://www.bilibili.com/video/{BVID}/?comment_root_id=abc&access_token={secret_value}"

    with pytest.raises(ParameterError) as error:
        parse_comment_reference(url)

    assert secret_value not in str(error.value)


def test_discussion_reference_identity_is_focus_independent() -> None:
    video = video_info()
    reference_a = parse_comment_reference(
        bilibili_url(
            path=f"/video/av{AID}",
            query=(
                f"comment_root_id={ROOT_COMMENT_ID}&comment_secondary_id={SECONDARY_COMMENT_ID}"
            ),
        )
    )
    reference_b = parse_comment_reference(
        bilibili_url(
            path=f"/video/{BVID}",
            query=f"comment_root_id={ROOT_COMMENT_ID}&vd_source=xyz",
            fragment=f"reply{OTHER_COMMENT_ID}",
        )
    )

    discussion_a = build_discussion_reference(video, reference_a)
    discussion_b = build_discussion_reference(video, reference_b)

    assert isinstance(discussion_a, DiscussionReference)
    assert discussion_a.identity == ("bilibili", "video", AID, ROOT_COMMENT_ID)
    assert discussion_a.identity == discussion_b.identity
    assert discussion_a.focus_comment_id == SECONDARY_COMMENT_ID
    assert discussion_b.focus_comment_id == OTHER_COMMENT_ID


def test_discussion_reference_rejects_video_identifier_mismatch() -> None:
    wrong_aid = parse_comment_reference(
        bilibili_url(path=f"/video/av{AID + 1}", query=f"comment_root_id={ROOT_COMMENT_ID}")
    )
    with pytest.raises(ParameterError, match="不一致"):
        build_discussion_reference(video_info(), wrong_aid)

    wrong_bvid = parse_comment_reference(
        bilibili_url(path="/video/BV1xx411c7mA", query=f"comment_root_id={ROOT_COMMENT_ID}")
    )
    with pytest.raises(ParameterError, match="不一致"):
        build_discussion_reference(video_info(), wrong_bvid)


def test_comment_reference_is_immutable() -> None:
    reference = parse_comment_reference(bilibili_url(query=f"comment_root_id={ROOT_COMMENT_ID}"))

    with pytest.raises(AttributeError):
        reference.root_comment_id = 1


@pytest.mark.parametrize(
    "url",
    [
        f"https://user:pass@www.bilibili.com/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}",
        f"https://www.bilibili.com:8080/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}",
        f"https://www.bilibili.com:abc/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}",
    ],
)
def test_comment_reference_rejects_credentials_and_nonstandard_ports(url: str) -> None:
    with pytest.raises(ParameterError):
        parse_comment_reference(url)


@pytest.mark.parametrize("port", ["80", "443"])
def test_comment_reference_allows_default_ports(port: str) -> None:
    url = f"https://www.bilibili.com:{port}/video/{BVID}/?comment_root_id={ROOT_COMMENT_ID}"

    assert parse_comment_reference(url).root_comment_id == ROOT_COMMENT_ID
