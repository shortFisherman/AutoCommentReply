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
