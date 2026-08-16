from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from auto_comment_reply.adapter import BilibiliAdapter
from auto_comment_reply.errors import ParameterError, RateLimitError
from auto_comment_reply.output import build_output_document

IMG_URL = "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png"
SUB_URL = "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png"
BVID = "BV1xx411c7mD"


def api_response(request: httpx.Request, data: Any, *, code: int = 0) -> httpx.Response:
    return httpx.Response(
        200,
        json={"code": code, "message": "OK" if code == 0 else "denied", "data": data},
        request=request,
    )


def raw_comment(
    rpid: int,
    *,
    root: int = 0,
    parent: int = 0,
    rcount: int = 0,
    replies: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "rpid": rpid,
        "root": root,
        "parent": parent,
        "rcount": rcount,
        "ctime": rpid,
        "member": {"mid": str(rpid + 1_000), "uname": f"user-{rpid}"},
        "content": {"message": f"comment-{rpid}"},
    }
    if replies is not None:
        item["replies"] = replies
    return item


def share_url(root: int, *, focus: int | None = None, bvid: str = BVID) -> str:
    url = f"https://www.bilibili.com/video/{bvid}/?comment_root_id={root}"
    if focus is not None:
        url = f"{url}&comment_secondary_id={focus}"
    return url


def view_data() -> dict[str, Any]:
    return {
        "aid": 42,
        "bvid": BVID,
        "title": "fixture video",
        "owner": {"mid": 7, "name": "owner"},
        "stat": {"reply": 7},
    }


def nav_data() -> dict[str, Any]:
    return {"wbi_img": {"img_url": IMG_URL, "sub_url": SUB_URL}}


def make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetches_all_root_and_child_pages_and_builds_multilevel_tree() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/x/web-interface/view":
            assert request.url.params["bvid"] == BVID
            return api_response(request, view_data())
        if path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if path == "/x/v2/reply/wbi/main":
            assert "w_rid" in request.url.params
            assert request.url.params["wts"] == "1700000000"
            offset = json.loads(request.url.params["pagination_str"])["offset"]
            if offset == "":
                root_100 = raw_comment(
                    100,
                    rcount=3,
                    replies=[raw_comment(110, root=100, parent=100)],
                )
                return api_response(
                    request,
                    {
                        "cursor": {
                            "is_end": False,
                            "all_count": 7,
                            "pagination_reply": {"next_offset": "next-page"},
                        },
                        "top_replies": [raw_comment(200)],
                        "replies": [root_100],
                    },
                )
            assert offset == "next-page"
            return api_response(
                request,
                {
                    "cursor": {
                        "is_end": True,
                        "all_count": 7,
                        "pagination_reply": {"next_offset": "unused"},
                    },
                    "top_replies": [],
                    "replies": [raw_comment(300, rcount=1)],
                },
            )
        if path == "/x/v2/reply/reply":
            root = int(request.url.params["root"])
            page_number = int(request.url.params["pn"])
            if root == 100 and page_number == 1:
                return api_response(
                    request,
                    {
                        "page": {"num": 1, "size": 2, "count": 3},
                        "root": raw_comment(100, rcount=3),
                        "replies": [
                            raw_comment(110, root=100, parent=100),
                            raw_comment(111, root=100, parent=110),
                        ],
                    },
                )
            if root == 100 and page_number == 2:
                return api_response(
                    request,
                    {
                        "page": {"num": 2, "size": 2, "count": 3},
                        "root": raw_comment(100, rcount=3),
                        "replies": [raw_comment(112, root=100, parent=111)],
                    },
                )
            assert root == 300 and page_number == 1
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 1},
                    "root": raw_comment(300, rcount=1),
                    "replies": [raw_comment(310, root=300, parent=300)],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(
            client=client,
            request_delay=0,
            retries=0,
            clock=lambda: 1_700_000_000,
        )
        result = adapter.fetch(BVID)

    document = build_output_document(result)
    assert document["complete"] is True
    assert document["schema_version"] == "1.0"
    assert document["stats"]["root_pages_fetched"] == 2
    assert document["stats"]["reply_pages_fetched"] == 3
    assert document["stats"]["root_comments_fetched"] == 3
    assert document["stats"]["reply_comments_fetched"] == 4
    assert document["conversation_chains"] == [[100, 110, 111, 112], [200], [300, 310]]
    assert document["orphan_comment_ids"] == []
    assert sum(request.url.path == "/x/web-interface/nav" for request in requests) == 1


def test_one_branch_failure_does_not_hide_or_abort_other_branches() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/x/web-interface/view":
            return api_response(request, view_data())
        if path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 4},
                    "top_replies": [],
                    "replies": [raw_comment(100, rcount=1), raw_comment(200, rcount=1)],
                },
            )
        if path == "/x/v2/reply/reply":
            root = int(request.url.params["root"])
            if root == 100:
                return api_response(request, None, code=-403)
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 1},
                    "root": raw_comment(200, rcount=1),
                    "replies": [raw_comment(210, root=200, parent=200)],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert {item.comment_id for item in result.comments} == {100, 200, 210}
    assert any(item.scope == "root:100:replies" for item in result.diagnostics)
    assert any(item.category == "access_denied" for item in result.diagnostics)


def test_missing_relationship_field_is_reported_and_skipped() -> None:
    broken = raw_comment(100)
    broken.pop("parent")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 1},
                    "top_replies": [],
                    "replies": [broken],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert result.comments == []
    assert any(item.category == "response_parse" for item in result.diagnostics)


def test_missing_root_reply_count_cannot_silently_skip_branch() -> None:
    root = raw_comment(100)
    root.pop("rcount")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 1},
                    "top_replies": [],
                    "replies": [root],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert result.comments[0].reply_count == 0
    assert any("rcount" in item.details.get("fields", []) for item in result.diagnostics)


def test_network_error_is_retried_with_bounded_backoff() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        return api_response(request, view_data())

    with make_client(handler) as client:
        adapter = BilibiliAdapter(
            client=client,
            request_delay=0,
            retries=1,
            retry_backoff=0.5,
            sleep=sleeps.append,
        )
        video = adapter.resolve_video(BVID)

    assert video.aid == 42
    assert calls == 2
    assert sleeps == [0.5]


def test_b23_short_link_is_followed_but_only_to_bilibili() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "b23.tv":
            return httpx.Response(
                302,
                headers={"Location": f"https://www.bilibili.com/video/{BVID}"},
                request=request,
            )
        if request.url.host == "www.bilibili.com" and request.url.path.startswith("/video/"):
            return httpx.Response(200, text="video page", request=request)
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        video = BilibiliAdapter(client=client, request_delay=0, retries=0).resolve_video(
            "https://b23.tv/example"
        )

    assert video.bvid == BVID


def test_b23_external_redirect_is_rejected_before_external_request() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "b23.tv":
            return httpx.Response(
                302,
                headers={"Location": "https://example.com/not-bilibili"},
                request=request,
            )
        raise AssertionError("external redirect target must never be requested")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError, match="非 Bilibili"):
            adapter.resolve_video("https://b23.tv/example")

    assert requested_hosts == ["b23.tv"]


def test_empty_comment_area_is_a_complete_empty_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            data = view_data()
            data["stat"]["reply"] = 0
            return api_response(request, data)
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 0},
                    "top_replies": [],
                    "replies": None,
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    document = build_output_document(result)
    assert document["complete"] is True
    assert document["comments"] == []
    assert document["trees"] == []
    assert document["conversation_chains"] == []


def test_empty_intermediate_root_page_follows_explicit_next_offset() -> None:
    main_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal main_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            main_calls += 1
            offset = json.loads(request.url.params["pagination_str"])["offset"]
            if offset == "":
                return api_response(
                    request,
                    {
                        "cursor": {
                            "is_end": False,
                            "all_count": 1,
                            "pagination_reply": {"next_offset": "page-2"},
                        },
                        "top_replies": [],
                        "replies": [],
                    },
                )
            assert offset == "page-2"
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 1},
                    "top_replies": [],
                    "replies": [raw_comment(100)],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is True
    assert [item.comment_id for item in result.comments] == [100]
    assert main_calls == 2


def test_missing_main_replies_field_is_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {"cursor": {"is_end": True, "all_count": 0}, "top_replies": []},
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert any(item.category == "response_parse" for item in result.diagnostics)


def test_root_page_safety_limit_keeps_partial_data_and_marks_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {
                        "is_end": False,
                        "all_count": 2,
                        "pagination_reply": {"next_offset": "page-2"},
                    },
                    "top_replies": [],
                    "replies": [raw_comment(100)],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(
            client=client,
            request_delay=0,
            retries=0,
            max_root_pages=1,
        ).fetch(BVID)

    assert result.complete is False
    assert [item.comment_id for item in result.comments] == [100]
    assert any(item.category == "pagination_limit" for item in result.diagnostics)


def test_repeated_child_page_is_detected_as_pagination_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 4},
                    "top_replies": [],
                    "replies": [raw_comment(100, rcount=3)],
                },
            )
        if request.url.path == "/x/v2/reply/reply":
            page_number = int(request.url.params["pn"])
            return api_response(
                request,
                {
                    "page": {"num": page_number, "size": 1, "count": 3},
                    "root": raw_comment(100, rcount=3),
                    "replies": [raw_comment(110, root=100, parent=100)],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert {item.comment_id for item in result.comments} == {100, 110}
    assert any(item.category == "pagination_loop" for item in result.diagnostics)


@pytest.mark.parametrize("missing_field", [False, True])
def test_child_page_cannot_end_before_reported_count(missing_field: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 3},
                    "top_replies": [],
                    "replies": [raw_comment(100, rcount=2)],
                },
            )
        if request.url.path == "/x/v2/reply/reply":
            data: dict[str, Any] = {
                "page": {"num": 1, "size": 20, "count": 2},
                "root": raw_comment(100, rcount=2),
            }
            if not missing_field:
                data["replies"] = []
            return api_response(request, data)
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    expected_category = "response_parse" if missing_field else "pagination_incomplete"
    assert any(item.category == expected_category for item in result.diagnostics)


def test_short_nonempty_child_page_does_not_fake_reaching_total() -> None:
    child_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal child_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 4},
                    "top_replies": [],
                    "replies": [raw_comment(100, rcount=3)],
                },
            )
        if request.url.path == "/x/v2/reply/reply":
            child_calls += 1
            replies = (
                [
                    raw_comment(110, root=100, parent=100),
                    raw_comment(111, root=100, parent=110),
                ]
                if child_calls == 1
                else []
            )
            return api_response(
                request,
                {
                    "page": {"num": child_calls, "size": 20, "count": 3},
                    "root": raw_comment(100, rcount=3),
                    "replies": replies,
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert child_calls == 2
    assert any(item.category == "pagination_incomplete" for item in result.diagnostics)


@pytest.mark.parametrize("access_code", [-403, -352])
def test_wbi_access_denied_refreshes_nav_key_once(access_code: int) -> None:
    nav_calls = 0
    main_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal nav_calls, main_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            nav_calls += 1
            suffix = "a" if nav_calls == 1 else "b"
            data = {
                "wbi_img": {
                    "img_url": f"https://i0.hdslb.com/bfs/wbi/{suffix * 32}.png",
                    "sub_url": f"https://i0.hdslb.com/bfs/wbi/{suffix * 32}.png",
                }
            }
            return api_response(request, data, code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            main_calls += 1
            if main_calls == 1:
                return api_response(request, None, code=access_code)
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 0},
                    "top_replies": [],
                    "replies": [],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is True
    assert nav_calls == 2
    assert main_calls == 2


def test_api_rate_limit_is_classified_without_hiding_partial_state() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(request, None, code=-799)
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.complete is False
    assert result.diagnostics[0].category == "rate_limit"
    assert result.diagnostics[0].details["retryable"] is True


def test_http_412_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(412, request=request)

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=3)
        with pytest.raises(RateLimitError):
            adapter.resolve_video(BVID)

    assert calls == 1


@pytest.mark.parametrize(
    ("reference", "expected_param"),
    [
        ("av42", ("aid", "42")),
        ("42", ("aid", "42")),
        ("https://www.bilibili.com/video/av42", ("aid", "42")),
        (f"https://www.bilibili.com/video/{BVID}", ("bvid", BVID)),
    ],
)
def test_supported_video_reference_forms(reference: str, expected_param: tuple[str, str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params[expected_param[0]] == expected_param[1]
        return api_response(request, view_data())

    with make_client(handler) as client:
        video = BilibiliAdapter(client=client, request_delay=0, retries=0).resolve_video(reference)

    assert video.bvid == BVID


def test_non_bilibili_url_with_bvid_text_is_rejected_without_request() -> None:
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError, match="只接受 Bilibili"):
            adapter.resolve_video(f"https://example.com/{BVID}")

    assert requested is False


def test_fetch_reference_expanded_comment_link_targets_one_root_without_main_or_nav() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == "/x/web-interface/view":
            return api_response(request, view_data())
        if path == "/x/v2/reply/reply":
            assert request.url.params["oid"] == "42"
            assert request.url.params["root"] == "100"
            assert request.url.params["pn"] == "1"
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 0},
                    "root": raw_comment(100),
                    "replies": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_reference(
            share_url(100)
        )

    document = build_output_document(result)
    assert result.complete is True
    assert [item.comment_id for item in result.comments] == [100]
    assert result.discussion is not None
    assert result.discussion.identity == ("bilibili", "video", 42, 100)
    assert document["schema_version"] == "1.2"
    assert document["viewer"]["authenticated"] is False
    assert document["discussion"]["oid"] == 42
    assert document["discussion"]["focus_comment_id"] is None
    assert document["stats"]["root_pages_fetched"] == 0
    assert document["stats"]["reply_pages_fetched"] == 1
    assert len(document["trees"]) == 1
    assert document["trees"][0]["comment"]["comment_id"] == 100
    assert document["trees"][0]["children"] == []
    assert sum(request.url.path == "/x/v2/reply/reply" for request in requests) == 1
    assert not any(
        request.url.path in {"/x/v2/reply/wbi/main", "/x/web-interface/nav"} for request in requests
    )


def test_fetch_discussion_paginates_replies_without_repeating_page_one() -> None:
    page_numbers: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/x/web-interface/view":
            return api_response(request, view_data())
        if path == "/x/v2/reply/reply":
            page_number = int(request.url.params["pn"])
            page_numbers.append(page_number)
            if page_number == 1:
                return api_response(
                    request,
                    {
                        "page": {"num": 1, "size": 2, "count": 3},
                        "root": raw_comment(100, rcount=3),
                        "replies": [
                            raw_comment(110, root=100, parent=100),
                            raw_comment(111, root=100, parent=110),
                        ],
                    },
                )
            assert page_number == 2
            return api_response(
                request,
                {
                    "page": {"num": 2, "size": 20, "count": 3},
                    "root": raw_comment(100, rcount=3),
                    "replies": [raw_comment(112, root=100, parent=111)],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    document = build_output_document(result)
    assert page_numbers == [1, 2]
    assert result.complete is True
    assert {item.comment_id for item in result.comments} == {100, 110, 111, 112}
    assert document["conversation_chains"] == [[100, 110, 111, 112]]
    assert document["stats"]["reply_pages_fetched"] == 2


def test_fetch_discussion_root_echo_mismatch_is_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 0},
                    "root": raw_comment(999),
                    "replies": [],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    document = build_output_document(result)
    assert result.complete is False
    assert result.comments == []
    assert document["trees"] == []
    mismatch = next(item for item in result.diagnostics if item.category == "root_id_mismatch")
    assert mismatch.details["requested_root_comment_id"] == 100
    assert mismatch.details["actual_rpid"] == 999


def test_fetch_discussion_missing_root_metadata_is_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                {"page": {"num": 1, "size": 20, "count": 0}, "replies": []},
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    assert result.complete is False
    assert result.comments == []
    assert any(item.category == "root_metadata_missing" for item in result.diagnostics)


@pytest.mark.parametrize("invisible_flag", [True, 1, "true", "TRUE", "1"])
def test_fetch_discussion_invisible_root_is_incomplete_without_more_pages(
    invisible_flag: object,
) -> None:
    reply_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reply_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            reply_calls += 1
            root_item = raw_comment(100)
            root_item["invisible"] = invisible_flag
            return api_response(
                request,
                {"page": {"num": 1, "size": 20, "count": 0}, "root": root_item, "replies": []},
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    assert result.complete is False
    assert reply_calls == 1
    assert result.comments == []
    assert any(item.category == "root_not_visible" for item in result.diagnostics)


def test_fetch_discussion_non_root_relationship_is_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 0},
                    "root": raw_comment(100, root=7, parent=7),
                    "replies": [],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    document = build_output_document(result)
    assert result.complete is False
    assert result.comments == []
    assert document["trees"] == []
    assert any(item.category == "root_relationship_invalid" for item in result.diagnostics)


@pytest.mark.parametrize("api_code", [12006, -404])
def test_fetch_discussion_api_error_becomes_incomplete_diagnostic(api_code: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(request, None, code=api_code)
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    assert result.complete is False
    assert result.comments == []
    assert result.discussion is not None
    diagnostic = next(item for item in result.diagnostics if item.category == "business")
    assert diagnostic.details["api_code"] == api_code


def test_fetch_discussion_focus_never_affects_relationships() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 1},
                    "root": raw_comment(100, rcount=1),
                    "replies": [raw_comment(110, root=100, parent=100)],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100, focus=999)
        )

    document = build_output_document(result)
    assert result.discussion is not None
    assert result.discussion.focus_comment_id == 999
    assert result.discussion.root_comment_id == 100
    assert [item.parent_id for item in result.comments if not item.is_root] == [100]
    assert document["conversation_chains"] == [[100, 110]]


def test_fetch_discussion_page_one_empty_before_count_is_incomplete() -> None:
    reply_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reply_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            reply_calls += 1
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 3},
                    "root": raw_comment(100, rcount=3),
                    "replies": [],
                },
            )
        raise AssertionError(str(request.url))

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    assert result.complete is False
    assert reply_calls == 1
    assert any(item.category == "pagination_incomplete" for item in result.diagnostics)


def test_fetch_reference_b23_comment_link_skips_final_page_request() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "b23.tv":
            return httpx.Response(
                302,
                headers={"Location": f"https://www.bilibili.com/video/{BVID}/?comment_root_id=100"},
                request=request,
            )
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 0},
                    "root": raw_comment(100),
                    "replies": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_reference(
            "https://b23.tv/example"
        )

    assert result.complete is True
    assert result.discussion is not None
    assert result.discussion.root_comment_id == 100
    assert "www.bilibili.com" not in requested_hosts
    assert requested_hosts.count("b23.tv") == 1


def test_fetch_reference_b23_chain_through_second_b23_is_allowed() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "b23.tv" and request.url.path == "/first":
            return httpx.Response(
                302, headers={"Location": "https://b23.tv/second"}, request=request
            )
        if request.url.host == "b23.tv" and request.url.path == "/second":
            return httpx.Response(
                302,
                headers={"Location": f"https://www.bilibili.com/video/{BVID}/?comment_root_id=100"},
                request=request,
            )
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 0},
                    "root": raw_comment(100),
                    "replies": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_reference(
            "https://b23.tv/first"
        )

    assert result.complete is True
    assert requested_hosts.count("b23.tv") == 2
    assert "www.bilibili.com" not in requested_hosts


def test_fetch_reference_b23_video_link_falls_back_to_legacy_fetch() -> None:
    requested_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.url.host == "b23.tv":
            return httpx.Response(
                302,
                headers={"Location": f"https://www.bilibili.com/video/{BVID}"},
                request=request,
            )
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 0},
                    "top_replies": [],
                    "replies": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_reference(
            "https://b23.tv/example"
        )

    assert result.complete is True
    assert result.discussion is None
    assert result.video.bvid == BVID
    assert result.stats.root_pages_fetched == 1
    assert "www.bilibili.com" not in requested_hosts


def test_fetch_reference_expanded_video_url_without_root_falls_back_to_legacy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 0},
                    "top_replies": [],
                    "replies": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_reference(
            f"https://www.bilibili.com/video/{BVID}"
        )

    assert result.complete is True
    assert result.discussion is None
    assert result.stats.root_pages_fetched == 1


def test_b23_redirect_loop_is_rejected_without_unbounded_requests() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://b23.tv/loop"}, request=request)

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError, match="循环"):
            adapter.resolve_video("https://b23.tv/loop")

    assert calls == 1


def test_b23_sixth_redirect_hop_is_rejected() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        current = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            302, headers={"Location": f"https://b23.tv/{current + 1}"}, request=request
        )

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError, match="安全上限"):
            adapter.resolve_video("https://b23.tv/0")

    assert calls == 5


@pytest.mark.parametrize(
    "location",
    [
        "https://",
        "ftp://bilibili.com/video/BV1xx411c7mD",
    ],
)
def test_b23_malformed_redirect_locations_are_rejected(location: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": location}, request=request)

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError):
            adapter.resolve_video("https://b23.tv/example")


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.bilibili.com/video/{BVID}/?comment_secondary_id=999",
        f"https://www.bilibili.com/video/{BVID}/#reply999",
    ],
)
def test_fetch_reference_secondary_or_fragment_without_root_is_fatal_before_requests(
    url: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError):
            adapter.fetch_reference(url)

    assert calls == 0


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.bilibili.com/video/{BVID}/?COMMENT_ROOT_ID=100",
        f"https://www.bilibili.com/video/{BVID}/?Comment_Secondary_Id=999",
        f"https://www.bilibili.com/video/{BVID}/#Reply999",
    ],
)
def test_fetch_reference_case_variant_markers_fail_closed_without_requests(url: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError):
            adapter.fetch_reference(url)

    assert calls == 0


def test_fetch_reference_b23_original_marker_lost_in_location_is_fatal() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.host == "b23.tv":
            return httpx.Response(
                302,
                headers={"Location": f"https://www.bilibili.com/video/{BVID}"},
                request=request,
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError, match="comment_root_id"):
            adapter.fetch_reference("https://b23.tv/example?comment_root_id=100")

    assert calls == 1


def test_video_reference_extracts_only_path_identifiers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/x/web-interface/view"
        assert request.url.params.get("aid") == "42"
        assert "bvid" not in request.url.params
        return api_response(request, view_data())

    with make_client(handler) as client:
        video = BilibiliAdapter(client=client, request_delay=0, retries=0).resolve_video(
            f"https://www.bilibili.com/video/av42?bvid={BVID}"
        )

    assert video.bvid == BVID


@pytest.mark.parametrize(
    "url",
    [
        "ftp://b23.tv/example",
        "https://user:pass@b23.tv/example",
        "https://b23.tv:8080/example",
        "https://b23.tv:abc/example",
    ],
)
def test_b23_initial_authority_violations_rejected_before_requests(url: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError):
            adapter.resolve_video(url)

    assert calls == 0


@pytest.mark.parametrize(
    "location",
    [
        "ftp://b23.tv/next",
        "https://user:pass@b23.tv/next",
        "https://b23.tv:8080/next",
    ],
)
def test_b23_hop_authority_violations_rejected_after_first_request(location: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": location}, request=request)

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError):
            adapter.resolve_video("https://b23.tv/example")

    assert calls == 1


def test_b23_remote_protocol_error_is_parameter_error() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://b23.tv:bad/next"}, request=request)

    with make_client(handler) as client:
        adapter = BilibiliAdapter(client=client, request_delay=0, retries=0)
        with pytest.raises(ParameterError, match="协议畸形"):
            adapter.resolve_video("https://b23.tv/example")

    assert calls == 1


@pytest.mark.parametrize(
    "initial_url",
    [
        "http://b23.tv:80/example",
        "https://b23.tv:443/example",
    ],
)
def test_b23_default_ports_are_accepted(initial_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "b23.tv":
            return httpx.Response(
                302,
                headers={"Location": f"https://www.bilibili.com/video/{BVID}"},
                request=request,
            )
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        video = BilibiliAdapter(client=client, request_delay=0, retries=0).resolve_video(
            initial_url
        )

    assert video.bvid == BVID


@pytest.mark.parametrize(
    ("reference", "expected_key", "expected_value"),
    [
        ("BV1xx411c7mD", "bvid", "BV1xx411c7mD"),
        ("av42", "aid", "42"),
        ("42", "aid", "42"),
    ],
)
def test_fetch_reference_bare_references_dispatch_to_legacy(
    reference: str,
    expected_key: str,
    expected_value: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            assert request.url.params.get(expected_key) == expected_value
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_data(), code=-101)
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {
                    "cursor": {"is_end": True, "all_count": 0},
                    "top_replies": [],
                    "replies": [],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_reference(
            reference
        )

    assert result.complete is True
    assert result.discussion is None
    assert result.stats.root_pages_fetched == 1


def test_fetch_discussion_wrong_root_excludes_foreign_replies() -> None:
    reply_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reply_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            reply_calls += 1
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 2},
                    "root": raw_comment(999, rcount=2),
                    "replies": [
                        raw_comment(910, root=999, parent=999),
                        raw_comment(911, root=999, parent=910),
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    document = build_output_document(result)
    assert result.complete is False
    assert reply_calls == 1
    assert result.comments == []
    assert document["trees"] == []
    assert document["orphan_comment_ids"] == []
    assert any(item.category == "root_id_mismatch" for item in result.diagnostics)
    excluded = next(
        item for item in result.diagnostics if item.category == "foreign_root_reply_excluded"
    )
    assert excluded.details["excluded_count"] == 2
    assert excluded.details["excluded_root_ids"] == [999]


@pytest.mark.parametrize(
    "root_payload",
    [
        pytest.param(None, id="missing"),
        pytest.param(True, id="invisible"),
        pytest.param(999, id="wrong-id"),
    ],
)
def test_fetch_discussion_invalid_root_keeps_requested_replies_as_orphans(
    root_payload: int | bool | None,
) -> None:
    reply_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reply_calls
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            reply_calls += 1
            data: dict[str, Any] = {
                "page": {"num": 1, "size": 20, "count": 1},
                "replies": [raw_comment(110, root=100, parent=100)],
            }
            if root_payload is None:
                return api_response(request, data)
            if root_payload is True:
                root_item = raw_comment(100)
                root_item["invisible"] = True
                data["root"] = root_item
                return api_response(request, data)
            data["root"] = raw_comment(root_payload)
            return api_response(request, data)
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    document = build_output_document(result)
    assert result.complete is False
    assert reply_calls == 1
    assert [item.comment_id for item in result.comments] == [110]
    assert document["trees"] == []
    assert document["orphan_comment_ids"] == [110]
    assert not any(item.category == "foreign_root_reply_excluded" for item in result.diagnostics)


def test_fetch_discussion_valid_root_parses_page_one_replies_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply_scopes: list[str] = []
    original_parse_comment = BilibiliAdapter._parse_comment

    def counting_parse_comment(
        self: BilibiliAdapter,
        raw_item: Any,
        *,
        video_id: str,
        scope: str,
        diagnostics: list[Any],
    ) -> Any:
        if "reply_page" in scope:
            reply_scopes.append(scope)
        return original_parse_comment(
            self,
            raw_item,
            video_id=video_id,
            scope=scope,
            diagnostics=diagnostics,
        )

    monkeypatch.setattr(BilibiliAdapter, "_parse_comment", counting_parse_comment)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            assert request.url.params["pn"] == "1"
            return api_response(
                request,
                {
                    "page": {"num": 1, "size": 20, "count": 2},
                    "root": raw_comment(100, rcount=2),
                    "replies": [
                        raw_comment(110, root=100, parent=100),
                        raw_comment(111, root=100, parent=110),
                    ],
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    assert result.complete is True
    assert {item.comment_id for item in result.comments} == {100, 110, 111}
    assert reply_scopes == [
        "root:100:reply_page:1",
        "root:100:reply_page:1",
    ]
