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

from auto_comment_reply import cli, storage
from auto_comment_reply.adapter import BilibiliAdapter
from auto_comment_reply.errors import ParameterError
from auto_comment_reply.models import (
    ANONYMOUS_VIEWER,
    Comment,
    Diagnostic,
    FetchResult,
    FetchStats,
    VideoInfo,
)
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


def make_targeted_result(*, complete: bool) -> FetchResult:
    return make_result(
        complete=complete,
        discussion=DiscussionReference(
            platform="bilibili",
            object_type="video",
            aid=42,
            bvid="BV1xx411c7mD",
            root_comment_id=100,
            focus_comment_id=None,
        ),
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


def test_cli_database_enabled_persists_final_document_before_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []
    captured: dict[str, object] = {}
    result = make_targeted_result(complete=True)

    class TracingAdapter:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def __enter__(self) -> TracingAdapter:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def fetch_reference(self, reference: str) -> FetchResult:
            events.append("fetch")
            captured["reference"] = reference
            return result

    monkeypatch.setattr(cli, "BilibiliAdapter", TracingAdapter)

    real_build = cli.build_output_document

    def traced_build(*args: object, **kwargs: object) -> dict[str, object]:
        events.append("build")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(cli, "build_output_document", traced_build)

    real_dumps = json.dumps

    def traced_dumps(*args: object, **kwargs: object) -> str:
        if kwargs.get("sort_keys"):
            return real_dumps(*args, **kwargs)
        events.append("dump")
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(cli.json, "dumps", traced_dumps)

    persisted_paths: list[Path] = []
    persisted_documents: list[dict[str, object]] = []

    def fake_persist(
        database_path: Path, result_arg: FetchResult, document: dict[str, object]
    ) -> None:
        events.append("persist")
        persisted_paths.append(database_path)
        persisted_documents.append(document)

    monkeypatch.setattr(cli, "persist_discussion_sync", fake_persist)

    real_write = cli._write_output

    def traced_write(*args: object, **kwargs: object) -> None:
        events.append("write")
        real_write(*args, **kwargs)

    monkeypatch.setattr(cli, "_write_output", traced_write)

    output = tmp_path / "out.json"
    database = tmp_path / "sync.db"
    exit_code = cli.main(
        [
            "https://www.bilibili.com/video/BV1xx411c7mD/?comment_root_id=100",
            "--database",
            str(database),
            "-o",
            str(output),
            "--compact",
            "--quiet",
        ]
    )

    assert exit_code == 0
    assert events == ["fetch", "build", "persist", "dump", "write"]
    assert persisted_paths == [database]
    persisted_document = persisted_documents[0]
    assert persisted_document["complete"] is True
    assert persisted_document["schema_version"] == "1.2"
    assert "generated_at" in persisted_document
    raw = output.read_text(encoding="utf-8")
    written_document = json.loads(raw)
    assert written_document["complete"] == persisted_document["complete"]
    assert written_document["generated_at"] == persisted_document["generated_at"]
    assert str(database) not in raw
    forbidden_keys = {
        "sync_run_id",
        "sync_run",
        "baseline",
        "diff",
        "newly_observed",
        "not_currently_visible",
        "database",
        "database_path",
    }
    assert not forbidden_keys.intersection(written_document)


def test_cli_database_persists_final_document_complete_not_result_complete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    result = make_targeted_result(complete=True)
    result.comments.append(
        Comment(
            comment_id=110,
            user_id=9,
            username="child",
            content="broken",
            root_id=100,
            parent_id=999,
            created_at=2,
            video_id="BV1xx411c7mD",
        )
    )
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    persisted_documents: list[dict[str, object]] = []
    monkeypatch.setattr(
        cli,
        "persist_discussion_sync",
        lambda database_path, result_arg, document: persisted_documents.append(document),
    )

    output = tmp_path / "out.json"
    exit_code = cli.main(
        [
            share_url(100),
            "--database",
            str(tmp_path / "sync.db"),
            "-o",
            str(output),
            "--compact",
            "--quiet",
        ]
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert result.complete is True
    assert exit_code == 2
    assert document["complete"] is False
    assert persisted_documents[0]["complete"] is False


def test_cli_incomplete_result_with_database_writes_schema_1_2_and_returns_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}
    result = make_targeted_result(complete=False)
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    persist_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli,
        "persist_discussion_sync",
        lambda *args: persist_calls.append(args),
    )

    output = tmp_path / "out.json"
    exit_code = cli.main(
        [
            share_url(100),
            "--database",
            str(tmp_path / "sync.db"),
            "-o",
            str(output),
            "--compact",
            "--quiet",
        ]
    )

    document = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert document["schema_version"] == "1.2"
    assert document["complete"] is False
    assert len(persist_calls) == 1
    assert persist_calls[0][2]["complete"] is False


def test_cli_database_persistence_error_exits_one_without_json_or_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict[str, object] = {}
    result = make_targeted_result(complete=True)
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))

    def fail_persist(*_args: object, **_kwargs: object) -> None:
        raise cli.PersistenceError("lock_timeout", "SQLite 数据库正被其他进程占用，等待写锁超时。")

    monkeypatch.setattr(cli, "persist_discussion_sync", fail_persist)

    output = tmp_path / "out.json"
    database = tmp_path / "sync.db"
    exit_code = cli.main(
        [
            share_url(100),
            "--database",
            str(database),
            "-o",
            str(output),
            "--quiet",
        ]
    )

    streams = capsys.readouterr()
    assert exit_code == 1
    assert streams.out == ""
    assert not output.exists()
    assert not database.exists()
    assert "SQLite 数据库正被其他进程占用，等待写锁超时。" in caplog.text


def test_cli_legacy_reference_with_database_exits_one_before_persist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict[str, object] = {}
    result = make_result(complete=True)
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    persist_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli,
        "persist_discussion_sync",
        lambda *args: persist_calls.append(args),
    )

    output = tmp_path / "out.json"
    database = tmp_path / "sync.db"
    exit_code = cli.main(
        ["BV1xx411c7mD", "--database", str(database), "-o", str(output), "--quiet"]
    )

    streams = capsys.readouterr()
    assert exit_code == 1
    assert persist_calls == []
    assert streams.out == ""
    assert not output.exists()
    assert not database.exists()
    assert "legacy" in caplog.text


def test_cli_without_database_never_calls_persistence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, object] = {}
    result = make_result(complete=True)
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    persist_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli,
        "persist_discussion_sync",
        lambda *args: persist_calls.append(args),
    )
    env_database = tmp_path / "auto-discovered.db"
    monkeypatch.setenv("BILIBILI_DB", str(env_database))
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)

    exit_code = cli.main(["BV1xx411c7mD", "--compact", "--quiet"])

    document = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert document["schema_version"] == "1.0"
    assert persist_calls == []
    assert not env_database.exists()


def test_cli_database_keeps_output_precheck_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    captured: dict[str, object] = {}
    result = make_targeted_result(complete=True)
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    persist_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli,
        "persist_discussion_sync",
        lambda *args: persist_calls.append(args),
    )
    output = tmp_path / "out.json"
    output.write_text("existing", encoding="utf-8")

    exit_code = cli.main(
        [
            share_url(100),
            "--database",
            str(tmp_path / "sync.db"),
            "-o",
            str(output),
            "--quiet",
        ]
    )

    assert exit_code == 1
    assert captured == {}
    assert persist_calls == []
    assert output.read_text(encoding="utf-8") == "existing"
    assert "输出文件已存在" in caplog.text


def test_cli_database_stdout_does_not_leak_database_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    result = make_targeted_result(complete=True)
    monkeypatch.setattr(cli, "BilibiliAdapter", fake_adapter_class(result, captured))
    persist_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        cli,
        "persist_discussion_sync",
        lambda *args: persist_calls.append(args),
    )
    database = tmp_path / "sync.db"

    exit_code = cli.main([share_url(100), "--database", str(database), "--compact", "--quiet"])

    streams = capsys.readouterr()
    assert exit_code == 0
    assert len(persist_calls) == 1
    assert str(database) not in streams.out
    assert str(database) not in streams.err


def test_cli_relationship_conflict_writes_degraded_document_and_returns_two(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def comment_document(
        comment_id: int,
        *,
        root_id: int = 0,
        parent_id: int = 0,
        created_at: int,
    ) -> Comment:
        return Comment(
            comment_id=comment_id,
            user_id=8,
            username=f"user-{comment_id}",
            content=f"content-{comment_id}",
            root_id=root_id,
            parent_id=parent_id,
            created_at=created_at,
            video_id="BV1xx411c7mD",
        )

    first = make_targeted_result(complete=True)
    first.comments = [
        comment_document(100, created_at=1),
        comment_document(110, root_id=100, parent_id=100, created_at=2),
    ]
    second = make_targeted_result(complete=True)
    second.comments = [
        comment_document(100, created_at=1),
        comment_document(50, root_id=100, parent_id=100, created_at=2),
        comment_document(110, root_id=100, parent_id=50, created_at=3),
    ]
    call_count = 0

    class SequenceAdapter:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self) -> SequenceAdapter:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def fetch_reference(self, reference: str) -> FetchResult:
            nonlocal call_count
            call_count += 1
            assert reference == share_url(100)
            return first if call_count == 1 else second

    monkeypatch.setattr(cli, "BilibiliAdapter", SequenceAdapter)
    database = tmp_path / "sync.db"
    output = tmp_path / "out.json"
    arguments = [
        share_url(100),
        "--database",
        str(database),
        "-o",
        str(output),
        "--force",
        "--compact",
        "--quiet",
    ]

    assert cli.main(arguments) == 0
    assert cli.main(arguments) == 2

    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema_version"] == "1.2"
    assert document["complete"] is False
    assert any(item.get("category") == "relationship_conflict" for item in document["diagnostics"])

    stored_discussion = DiscussionReference(
        platform="bilibili",
        object_type="video",
        aid=42,
        bvid="BV1xx411c7mD",
        root_comment_id=100,
        focus_comment_id=None,
    )
    state = storage.get_viewer_discussion_state(database, ANONYMOUS_VIEWER, stored_discussion)
    assert state is not None
    assert state["last_complete_visible_ids"] == [100, 110]
    facts = {
        item["comment_id"]: item for item in storage.list_comments(database, stored_discussion)
    }
    assert (facts[110]["root_id"], facts[110]["parent_id"]) == (100, 100)
    runs = storage.list_sync_runs(database, ANONYMOUS_VIEWER, stored_discussion)
    assert [run["complete"] for run in runs] == [True, False]


def test_cli_database_open_failure_never_echoes_path_or_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "BilibiliAdapter", fake_adapter_class(make_targeted_result(complete=True), captured)
    )
    monkeypatch.delenv("BILIBILI_COOKIE", raising=False)
    database = tmp_path / UNIQUE_SECRET
    database.mkdir()

    exit_code = cli.main([share_url(100), "--database", str(database), "--quiet"])

    streams = capsys.readouterr()
    assert exit_code == 1
    assert streams.out == ""
    assert str(database) not in streams.err
    assert UNIQUE_SECRET not in streams.err
    assert Path.home().name not in streams.err
