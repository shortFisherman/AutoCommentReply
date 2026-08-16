from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from _helpers import (
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

from auto_comment_reply import cli
from auto_comment_reply.adapter import BilibiliAdapter
from auto_comment_reply.errors import ParameterError
from auto_comment_reply.models import Comment, Diagnostic, FetchResult, FetchStats, VideoInfo
from auto_comment_reply.reference import DiscussionReference


def make_result(
    *,
    complete: bool,
    discussion: DiscussionReference | None = None,
) -> FetchResult:
    diagnostics = []
    if not complete:
        diagnostics.append(
            Diagnostic(
                severity="error",
                category="pagination",
                scope="test",
                message="partial",
            )
        )
    return FetchResult(
        video=VideoInfo(
            aid=42,
            bvid="BV1xx411c7mD",
            title="fixture",
            owner_id=7,
            owner_name="owner",
            visible_comment_count_hint=1,
        ),
        comments=[
            Comment(
                comment_id=100,
                user_id=8,
                username="user",
                content="hello",
                root_id=0,
                parent_id=0,
                created_at=1,
                video_id="BV1xx411c7mD",
            )
        ],
        complete=complete,
        diagnostics=diagnostics,
        stats=FetchStats(
            expected_total_comments=1,
            root_comments_fetched=1,
            total_comments_fetched=1,
        ),
        discussion=discussion,
    )


def fake_adapter_class(result: FetchResult | Exception, captured: dict[str, object]):
    class FakeAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def fetch_reference(self, reference: str) -> FetchResult:
            captured["reference"] = reference
            if isinstance(result, Exception):
                raise result
            return result

    return FakeAdapter


def real_adapter_factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[..., BilibiliAdapter]:
    """Build a real adapter on MockTransport for end-to-end offline CLI tests."""

    def factory(**kwargs: object) -> BilibiliAdapter:
        return BilibiliAdapter(
            **{
                **kwargs,
                "client": make_client(handler),
                "request_delay": 0,
                "retries": 0,
            }
        )

    return factory


def test_cli_complete_result_prints_json_and_returns_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=True), captured)
    )
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)

    exit_code = cli.main(["BV1xx411c7mD", "--compact", "--quiet"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["complete"] is True
    assert captured["reference"] == "BV1xx411c7mD"
    assert captured["cookie"] is None


def test_cli_share_link_dispatches_targeted_fetch_and_exposes_discussion(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    result = make_result(
        complete=True,
        discussion=DiscussionReference(
            platform="bilibili",
            object_type="video",
            aid=42,
            bvid="BV1xx411c7mD",
            root_comment_id=100,
            focus_comment_id=None,
        ),
    )
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)

    link = "https://www.bilibili.com/video/BV1xx411c7mD/?comment_root_id=100"
    exit_code = cli.main([link, "--compact", "--quiet"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["reference"] == link
    assert document["discussion"]["root_comment_id"] == 100
    assert document["discussion"]["identity"] == {
        "platform": "bilibili",
        "object_type": "video",
        "oid": 42,
        "root_comment_id": 100,
    }


def test_cli_incomplete_result_is_written_and_returns_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=False), captured)
    )
    output = tmp_path / "comments.json"

    exit_code = cli.main(["BV1xx411c7mD", "-o", str(output), "--quiet"])

    assert exit_code == 2
    assert json.loads(output.read_text(encoding="utf-8"))["complete"] is False


def test_cli_fatal_adapter_error_returns_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "BilibiliAdapter",
        fake_adapter_class(ParameterError("bad video"), captured),
    )

    exit_code = cli.main(["invalid", "--quiet"])

    assert exit_code == 1
    assert "bad video" in caplog.text


def test_cookie_file_takes_precedence_and_is_not_echoed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    secret = "SESSDATA=super-secret; DedeUserID=7"
    cookie_file = tmp_path / "private.cookie"
    cookie_file.write_text(secret, encoding="utf-8-sig")
    monkeypatch.setenv("BILIBILI_COOKIE", "SESSDATA=environment")
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=True), captured)
    )

    exit_code = cli.main(
        ["BV1xx411c7mD", "--cookie-file", str(cookie_file), "--compact", "--quiet"]
    )

    streams = capsys.readouterr()
    assert exit_code == 0
    assert captured["cookie"] == secret
    assert secret not in streams.out
    assert secret not in streams.err


def test_write_output_requires_force_to_replace_existing_file(tmp_path: Path) -> None:
    output = tmp_path / "comments.json"
    output.write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError):
        cli._write_output(output, "new", force=False)

    cli._write_output(output, "new", force=True)
    assert output.read_text(encoding="utf-8") == "new"


def test_cli_existing_output_returns_one_before_fetch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_result(complete=True), captured)
    )
    output = tmp_path / "comments.json"
    output.write_text("existing", encoding="utf-8")

    exit_code = cli.main(["BV1xx411c7mD", "-o", str(output), "--quiet"])

    assert exit_code == 1
    assert captured == {}
    assert "输出文件已存在" in caplog.text


def test_cli_authenticated_cookie_file_run_writes_schema_1_2_without_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = UNIQUE_SECRET
    cookie_file = tmp_path / "private.cookie"
    cookie_file.write_text(secret, encoding="utf-8-sig")
    output = tmp_path / "out.json"
    request_paths: list[str] = []
    cookie_headers: list[str | None] = []
    env_decoy = "SESSDATA=env-decoy-secret-5f1a2b3c"

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        cookie_headers.append(request.headers.get("Cookie"))
        assert secret not in str(request.url)
        if request.url.path == "/x/web-interface/nav":
            assert request.headers.get("Cookie") == secret
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

    monkeypatch.setattr(cli, "BilibiliAdapter", real_adapter_factory(handler))
    monkeypatch.setenv("BILIBILI_COOKIE", env_decoy)

    exit_code = cli.main(
        [
            share_url(100),
            "--cookie-file",
            str(cookie_file),
            "-o",
            str(output),
            "--force",
            "--verbose",
        ]
    )

    streams = capsys.readouterr()
    document = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert document["schema_version"] == "1.2"
    assert document["viewer"]["authenticated"] is True
    assert document["viewer"]["platform_user_id"] == VIEWER_MID
    assert request_paths.count("/x/web-interface/nav") == 1
    assert all(header == secret for header in cookie_headers)
    for text in (
        output.read_text(encoding="utf-8"),
        streams.out,
        streams.err,
    ):
        assert secret not in text
        assert env_decoy not in text


def test_cli_auth_failure_exits_1_without_json_on_stdout_or_disk(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = UNIQUE_SECRET
    cookie_file = tmp_path / "private.cookie"
    cookie_file.write_text(secret, encoding="utf-8-sig")
    output = tmp_path / "out.json"
    reply_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            return api_response(request, login_nav_data(is_login=False))
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            nonlocal reply_calls
            reply_calls += 1
            raise AssertionError("评论读取不得在认证失败后开始")
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(cli, "BilibiliAdapter", real_adapter_factory(handler))

    exit_code = cli.main(
        [
            share_url(100),
            "--cookie-file",
            str(cookie_file),
            "-o",
            str(output),
            "--verbose",
        ]
    )
    streams = capsys.readouterr()

    assert exit_code == 1
    assert reply_calls == 0
    assert streams.out == ""
    assert not output.exists()
    assert secret not in streams.out
    assert secret not in streams.err

    stdout_exit_code = cli.main([share_url(100), "--cookie-file", str(cookie_file), "--verbose"])
    stdout_streams = capsys.readouterr()
    assert stdout_exit_code == 1
    assert stdout_streams.out == ""
    assert secret not in stdout_streams.out
    assert secret not in stdout_streams.err


def test_cli_env_cookie_is_used_when_no_cookie_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = UNIQUE_SECRET

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/x/web-interface/nav":
            assert request.headers.get("Cookie") == secret
            return api_response(request, login_nav_data())
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(raw_comment(100, mid=VIEWER_MID)),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(cli, "BilibiliAdapter", real_adapter_factory(handler))
    monkeypatch.setenv("BILIBILI_COOKIE", secret)

    exit_code = cli.main([share_url(100), "--compact", "--quiet"])

    streams = capsys.readouterr()
    document = json.loads(streams.out)
    assert exit_code == 0
    assert document["viewer"]["authenticated"] is True
    assert document["viewer"]["platform_user_id"] == VIEWER_MID
    assert secret not in streams.out
    assert secret not in streams.err


def test_cli_anonymous_run_emits_anonymous_viewer_without_nav(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_paths.append(request.url.path)
        if request.url.path == "/x/web-interface/view":
            return api_response(request, view_data())
        if request.url.path == "/x/v2/reply/reply":
            return api_response(
                request,
                discussion_reply_payload(raw_comment(100)),
            )
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(cli, "BilibiliAdapter", real_adapter_factory(handler))
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)

    exit_code = cli.main([share_url(100), "--compact", "--quiet"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["viewer"] == {
        "platform": "bilibili",
        "authenticated": False,
        "platform_user_id": None,
        "username": None,
    }
    assert "/x/web-interface/nav" not in request_paths


def test_cli_empty_cookie_file_exits_1_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    cookie_file = tmp_path / "empty.cookie"
    cookie_file.write_text("", encoding="utf-8")
    output = tmp_path / "out.json"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(cli, "BilibiliAdapter", real_adapter_factory(handler))

    exit_code = cli.main(
        [
            "BV1xx411c7mD",
            "--cookie-file",
            str(cookie_file),
            "-o",
            str(output),
            "--verbose",
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 1
    assert streams.out == ""
    assert not output.exists()
    assert "Cookie 文件为空" in caplog.text
