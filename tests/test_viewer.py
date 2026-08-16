"""M2 offline acceptance: local auth session and viewer identity (A1-A6)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from _helpers import (
    BVID,
    IMG_URL,
    SUB_URL,
    UNIQUE_SECRET,
    VIEWER_MID,
    api_response,
    discussion_reply_payload,
    login_nav_data,
    make_client,
    raw_comment,
    share_url,
    view_data,
)

from auto_comment_reply.adapter import BilibiliAdapter
from auto_comment_reply.errors import AuthenticationError, ResponseParseError
from auto_comment_reply.models import FetchResult
from auto_comment_reply.output import build_output_document

try:
    from auto_comment_reply.models import Viewer
except ImportError:  # pragma: no cover - M2 source not landed yet
    Viewer = None  # type: ignore[assignment]


def require_viewer() -> None:
    if Viewer is None:
        pytest.fail("M2 Viewer 模型尚未实现（source 未就绪）")


def _without(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return {name: value for name, value in payload.items() if name != key}


def test_viewer_model_exposes_credential_free_fact_shape() -> None:
    require_viewer()

    anonymous = Viewer(
        platform="bilibili",
        authenticated=False,
        platform_user_id=None,
        username=None,
    )
    assert anonymous.to_dict() == {
        "platform": "bilibili",
        "authenticated": False,
        "platform_user_id": None,
        "username": None,
    }

    logged_in = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    assert logged_in.to_dict() == {
        "platform": "bilibili",
        "authenticated": True,
        "platform_user_id": VIEWER_MID,
        "username": "display-only",
    }
    assert UNIQUE_SECRET not in repr(logged_in)


def test_anonymous_discussion_viewer_is_explicit_and_nav_is_never_requested() -> None:
    require_viewer()
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(raw_comment(100)),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch_discussion(
            share_url(100)
        )

    assert result.viewer is not None
    assert result.viewer.authenticated is False
    assert result.viewer.platform_user_id is None
    assert result.viewer.username is None
    assert "/x/web-interface/nav" not in paths
    assert "/x/v2/reply/wbi/main" not in paths
    assert result.stats.root_pages_fetched == 0

    document = build_output_document(result)
    assert document["viewer"] == {
        "platform": "bilibili",
        "authenticated": False,
        "platform_user_id": None,
        "username": None,
    }
    assert document["schema_version"] == "1.2"


def test_authenticated_discussion_resolves_nav_before_comment_read_and_caches_it() -> None:
    require_viewer()
    paths: list[str] = []
    cookie_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        cookie_headers.append(request.headers.get("Cookie"))
        assert UNIQUE_SECRET not in str(request.url)
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, login_nav_data())
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(raw_comment(100)),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(
            client=client,
            cookie=UNIQUE_SECRET,
            request_delay=0,
            retries=0,
        )
        first = adapter.fetch_discussion(share_url(100))
        second = adapter.fetch_discussion(share_url(100))

    assert paths.count("/x/web-interface/nav") == 1
    nav_index = paths.index("/x/web-interface/nav")
    reply_indexes = [index for index, path in enumerate(paths) if path == "/x/v2/reply/reply"]
    assert nav_index < min(reply_indexes)
    assert all(header == UNIQUE_SECRET for header in cookie_headers)

    for result in (first, second):
        assert result.viewer is not None
        assert result.viewer.authenticated is True
        assert result.viewer.platform_user_id == VIEWER_MID
        assert result.viewer.username == "display-only"
        assert result.stats.root_pages_fetched == 0
    assert "/x/v2/reply/wbi/main" not in paths


def test_authenticated_nav_accepts_numeric_string_mid_and_nullable_username() -> None:
    require_viewer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, login_nav_data(mid="987654", uname=None))
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(raw_comment(100)),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(
            client=client,
            cookie=UNIQUE_SECRET,
            request_delay=0,
            retries=0,
        ).fetch_discussion(share_url(100))

    assert result.viewer is not None
    assert result.viewer.authenticated is True
    assert result.viewer.platform_user_id == 987654
    assert result.viewer.username is None


def test_authenticated_adapter_shares_one_nav_between_legacy_wbi_and_discussion() -> None:
    require_viewer()
    nav_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/x/web-interface/nav":
            nonlocal nav_calls
            nav_calls += 1
            return api_response(request, login_nav_data())
        if path == "/x/web-interface/view":
            return api_response(request, view_data())
        if path == "/x/v2/reply/wbi/main":
            assert "w_rid" in request.url.params
            return api_response(
                request,
                {"cursor": {"is_end": True, "all_count": 0}, "top_replies": [], "replies": []},
            )
        if path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(raw_comment(100)),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(
            client=client,
            cookie=UNIQUE_SECRET,
            request_delay=0,
            retries=0,
        )
        legacy = adapter.fetch(BVID)
        first_discussion = adapter.fetch_discussion(share_url(100))
        second_discussion = adapter.fetch_discussion(share_url(100))

    assert nav_calls == 1
    for result in (legacy, first_discussion, second_discussion):
        assert result.viewer is not None
        assert result.viewer.authenticated is True
        assert result.viewer.platform_user_id == VIEWER_MID
    assert first_discussion.stats.root_pages_fetched == 0


def test_anonymous_legacy_fetch_also_has_explicit_anonymous_viewer() -> None:
    require_viewer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/web-interface/nav":
            return api_response(
                request,
                {"wbi_img": {"img_url": IMG_URL, "sub_url": SUB_URL}},
                code=-101,
            )
        if request.url.path == "/x/v2/reply/wbi/main":
            return api_response(
                request,
                {"cursor": {"is_end": True, "all_count": 0}, "top_replies": [], "replies": []},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(client=client, request_delay=0, retries=0).fetch(BVID)

    assert result.viewer is not None
    assert result.viewer.authenticated is False
    assert result.viewer.platform_user_id is None
    assert result.viewer.username is None


@pytest.mark.parametrize(
    ("nav_payload", "nav_code"),
    [
        pytest.param(
            login_nav_data(),
            -101,
            id="code-101",
        ),
        pytest.param(
            login_nav_data(is_login=False),
            0,
            id="islogin-false",
        ),
        pytest.param(
            _without(login_nav_data(), "isLogin"),
            0,
            id="islogin-absent",
        ),
        pytest.param(
            login_nav_data(mid=None),
            0,
            id="mid-absent",
        ),
        pytest.param(
            login_nav_data(mid=0),
            0,
            id="mid-zero",
        ),
        pytest.param(
            login_nav_data(mid=-5),
            0,
            id="mid-negative",
        ),
        pytest.param(
            login_nav_data(mid="abc"),
            0,
            id="mid-non-numeric",
        ),
        pytest.param(
            login_nav_data(mid="12.5"),
            0,
            id="mid-non-integer-string",
        ),
        pytest.param(
            login_nav_data(mid=1.5),
            0,
            id="mid-float",
        ),
        pytest.param(
            login_nav_data(mid=True),
            0,
            id="mid-bool",
        ),
        pytest.param(
            None,
            0,
            id="data-missing",
        ),
        pytest.param(
            [],
            0,
            id="data-not-mapping",
        ),
    ],
)
def test_authenticated_nav_invalid_fails_closed_before_comment_read(
    nav_payload: Any,
    nav_code: int,
) -> None:
    require_viewer()
    reply_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, nav_payload, code=nav_code)
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            nonlocal reply_calls
            reply_calls += 1
            raise AssertionError("评论读取不得在认证失败后开始")
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(
            client=client,
            cookie=UNIQUE_SECRET,
            request_delay=0,
            retries=0,
        )
        with pytest.raises((AuthenticationError, ResponseParseError)) as excinfo:
            adapter.fetch_discussion(share_url(100))

    assert reply_calls == 0
    error = excinfo.value
    assert UNIQUE_SECRET not in str(error)
    assert UNIQUE_SECRET not in repr(error)
    assert UNIQUE_SECRET not in json.dumps(error.details, ensure_ascii=False)


def test_authenticated_discussion_derives_is_self_tristate_in_output() -> None:
    require_viewer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, login_nav_data())
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(
                    raw_comment(100, mid=VIEWER_MID),
                    replies=[
                        raw_comment(110, root=100, parent=100, mid=999),
                        raw_comment(111, root=100, parent=110, include_mid=False),
                    ],
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        result = BilibiliAdapter(
            client=client,
            cookie=UNIQUE_SECRET,
            request_delay=0,
            retries=0,
        ).fetch_discussion(share_url(100))

    document = build_output_document(result)
    by_id = {item["comment_id"]: item for item in document["comments"]}
    assert by_id[100]["is_self"] is True
    assert by_id[110]["is_self"] is False
    assert by_id[111]["is_self"] is None
    assert "user_id" not in by_id[100]
    assert result.complete is False  # unknown author keeps missing-field diagnostics


def test_authenticated_success_path_never_echoes_secret() -> None:
    require_viewer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, login_nav_data())
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(
                    raw_comment(100, mid=VIEWER_MID),
                    replies=[raw_comment(110, root=100, parent=100, mid=999)],
                ),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    with make_client(handler) as client:
        adapter = BilibiliAdapter(
            client=client,
            cookie=UNIQUE_SECRET,
            request_delay=0,
            retries=0,
        )
        result = adapter.fetch_discussion(share_url(100))

    document = build_output_document(result)
    payloads = [
        json.dumps(document, ensure_ascii=False),
        repr(document),
        repr(result),
        repr(result.viewer),
        repr(result.video),
        repr(result.comments),
        repr(result.diagnostics),
        repr(result.stats),
        repr(adapter),
    ]
    if result.discussion is not None:
        payloads.append(json.dumps(result.discussion.to_dict(), ensure_ascii=False))
    for text in payloads:
        assert UNIQUE_SECRET not in text


def test_result_type_annotates_viewer_on_fetch_result() -> None:
    require_viewer()
    assert "viewer" in FetchResult.__dataclass_fields__


@pytest.mark.parametrize(
    ("left_username", "right_username"),
    [
        pytest.param("display-only", None, id="str-vs-null"),
        pytest.param(None, "renamed", id="null-vs-str"),
        pytest.param("display-only", "renamed", id="str-vs-str"),
    ],
)
def test_viewer_equality_and_hash_ignore_username(
    left_username: str | None,
    right_username: str | None,
) -> None:
    require_viewer()
    left = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username=left_username,
    )
    right = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username=right_username,
    )
    assert left.identity == right.identity
    assert left == right
    assert hash(left) == hash(right)
    assert left.username == left_username
    assert right.username == right_username
    assert left.to_dict()["username"] == left_username
    assert right.to_dict()["username"] == right_username


def test_viewer_equality_requires_same_platform_authenticated_and_mid() -> None:
    require_viewer()
    base = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    different_mid = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID + 1,
        username="display-only",
    )
    different_platform = Viewer(
        platform="weibo",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    anonymous = Viewer(
        platform="bilibili",
        authenticated=False,
        platform_user_id=None,
        username=None,
    )
    assert base != different_mid
    assert base != different_platform
    assert base != anonymous
    assert different_mid != anonymous


def test_viewer_equality_with_non_viewer_returns_not_implemented() -> None:
    require_viewer()
    viewer = Viewer(
        platform="bilibili",
        authenticated=True,
        platform_user_id=VIEWER_MID,
        username="display-only",
    )
    assert viewer.__eq__(object()) is NotImplemented
