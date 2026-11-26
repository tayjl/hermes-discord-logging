import json
import logging
import pathlib
import threading
import time

from fakes import FakeCtx, FakeHttp, start_service, stop_service, wait_for
from hdl import collector as col
from hdl import crash, service, style
from hdl.collector import Collector, LogHandler
from hdl.sender import Sender

DIGEST = pathlib.Path.home() / "AppData/Local/hermes/scripts/gmail-digest/digest-blocks.json"


def record(name="agent.x", level=logging.WARNING, msg="boom", exc=None, thread=None):
    r = logging.LogRecord(name, level, __file__, 1, msg, None, exc)
    if thread:
        r.threadName = thread
    return r


def handler_with_collector():
    c = Collector()
    return c, LogHandler(c, logging.WARNING, "hdl")


def text_of(messages):
    return "\n".join(messages)


def test_repeats_merge_with_count():
    c, h = handler_with_collector()
    for _ in range(3):
        h.emit(record(msg="db locked"))
    h.emit(record(msg="other"))
    out = text_of(c.drain())
    assert out.count("db locked") == 1 and "db locked x3" in out
    assert "other" in out and "other x" not in out
    assert "**Errors** | __gateway__" in out


def test_drain_empties_and_idle_drain_is_empty():
    c, h = handler_with_collector()
    assert c.drain() == []
    h.emit(record())
    assert c.drain() and c.drain() == []


def test_traceback_is_clipped():
    c, h = handler_with_collector()
    try:
        def deep(n):
            if n == 0:
                raise ValueError("bottom")
            deep(n - 1)
        deep(30)
    except ValueError:
        import sys
        h.emit(record(msg="failed", exc=sys.exc_info()))
    out = text_of(c.drain())
    assert "ValueError: bottom" in out and "..." in out
    assert out.count("deep") <= 8


def test_near_limit_wakes_flusher():
    c, h = handler_with_collector()
    h.emit(record(msg="a"))
    assert not c.wake.is_set()
    for n in range(10):
        h.emit(record(msg=f"line {n} " + "x" * 200))
    assert c.wake.is_set()


def test_tool_executor_excluded_but_not_siblings():
    c, h = handler_with_collector()
    for name in ("agent.tool_executor", "agent.tool_executor.terminal"):
        h.emit(record(name=name))
    assert c.drain() == []
    for name in ("agent.tool_executor_x", "agent.foo"):
        h.emit(record(name=name, msg=f"from {name}"))
    out = "\n".join(c.drain())
    assert "agent.tool_executor_x" in out and "agent.foo" in out


def test_excluded_loggers_and_thread():
    c, h = handler_with_collector()
    for name in ("discord.client", "discord", "urllib3.connectionpool", "http.client", "hdl", "hdl.sender"):
        h.emit(record(name=name))
    h.emit(record(thread="hdl-sender"))
    assert c.drain() == []
    h.emit(record(name="hdlother"))  # prefix lookalike is not ours
    h.emit(record(name="hermes_plugins.platforms__discord.adapter"))
    assert "hdlother" in text_of(c.drain())


def test_level_filter():
    c, h = handler_with_collector()
    root = logging.getLogger("hdl-test-level")
    root.propagate = False
    root.addHandler(h)
    root.setLevel(logging.DEBUG)
    root.info("quiet")
    root.error("loud")
    out = text_of(c.drain())
    assert "loud" in out and "quiet" not in out
    root.removeHandler(h)


def test_reentrancy_guard():
    c, h = handler_with_collector()
    root = logging.getLogger()
    root.addHandler(h)
    original = c.add_error

    def noisy(*args):
        logging.getLogger("somewhere.else").error("logged while handling")
        original(*args)
    c.add_error = noisy
    try:
        logging.getLogger("agent.y").error("first")
    finally:
        root.removeHandler(h)
    out = text_of(c.drain())
    assert "first" in out and "logged while handling" not in out


def test_redaction_uses_hermes_redactor():
    c, h = handler_with_collector()
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    h.emit(record(msg=f"auth failed token={secret} Authorization: Bearer abcdefghijklmnop1234567890"))
    out = text_of(c.drain())
    assert secret not in out and "abcdefghijklmnop1234567890" not in out
    assert "auth failed" in out


def test_redaction_fails_closed(monkeypatch):
    import agent.redact
    monkeypatch.setattr(agent.redact, "redact_for_egress", lambda *a, **k: 1 / 0)
    assert col.redact("sk-secret") == "[redaction unavailable]"


def test_control_chars_and_backticks_neutralised():
    c, h = handler_with_collector()
    h.emit(record(msg="evil \x1b[31mred\x1b[0m ``` close \u202e"))
    out = text_of(c.drain())
    body = out.split(style.BOX_TOP, 1)[1].split(style.BOX_BOTTOM, 1)[0]
    assert "\x1b" not in body and "```" not in body and "\u202e" not in body


def test_every_payload_has_allowed_mentions():
    http = FakeHttp()
    s = Sender("TOK", "123", Collector(), post=http, sleep=lambda s: None)
    assert s.send("hi @everyone <@123>")
    url, token, body = http.calls[0]
    payload = json.loads(body)
    assert payload["allowed_mentions"] == {"parse": []}
    assert url == "https://discord.com/api/v10/channels/123/messages"
    assert token == "TOK" and b"TOK" not in body


def test_retry_after_is_honoured():
    http, slept = FakeHttp([(429, 0.75), (429, 1.5), (204, None)]), []
    s = Sender("TOK", "1", Collector(), post=http, sleep=slept.append)
    assert s.send("x") and slept == [0.75, 1.5] and len(http.calls) == 3


def test_retry_gives_up_and_client_error_is_not_retried():
    http, slept = FakeHttp([(429, 1)] * 5), []
    s = Sender("TOK", "1", Collector(), post=http, sleep=slept.append)
    assert not s.send("x") and len(http.calls) == 3
    http = FakeHttp([(403, None)])
    s = Sender("TOK", "1", Collector(), post=http, sleep=slept.append)
    assert not s.send("x") and len(http.calls) == 1


def test_split_stays_within_2000_utf16_units():
    c, h = handler_with_collector()
    for n in range(60):
        h.emit(record(msg=f"{n} " + "\U0001F600" * 140))  # astral characters count twice
    h.emit(record(msg="y" * 5000))
    messages = c.drain()
    assert len(messages) > 2
    assert all(style.u16(m) <= 2000 for m in messages)
    assert messages[0].startswith(style.HEADER) and style.HEADER not in messages[1]
    assert messages[1].startswith("\u200b\n")
    assert messages[-1].endswith(style.FOOTER)
    assert not any(m.endswith(style.FOOTER) for m in messages[:-1])


def test_header_only_on_first_drain():
    c, h = handler_with_collector()
    h.emit(record(msg="one"))
    assert c.drain()[0].startswith(style.HEADER)
    h.emit(record(msg="two"))
    second = c.drain()
    assert second and style.HEADER not in second[0] and second[0].startswith("**Errors**")
    assert c.drain() == []


def test_header_not_used_up_by_empty_drain_and_only_first_kind_gets_it():
    c, h = handler_with_collector()
    assert c.drain() == []
    h.emit(record(msg="boom"))
    c.add_crash(["crashed"])
    c.end_turn("t", "completed", None)  # unknown turn: ignored
    messages = c.drain()
    assert len(messages) == 2
    assert messages[0].startswith(style.HEADER) and style.HEADER not in messages[1]


def test_header_returns_after_reactivation():
    first, h = handler_with_collector()
    h.emit(record(msg="one"))
    assert style.HEADER in first.drain()[0]
    second, h2 = handler_with_collector()  # stop() then ensure() builds a fresh Collector
    h2.emit(record(msg="two"))
    assert style.HEADER in second.drain()[0]


def test_header_matches_digest_escapes():
    if DIGEST.exists():
        digest = json.loads(DIGEST.read_text(encoding="utf-8"))
        assert style.header("HERMES DAILY DIGEST") == digest["header"]
        assert style.FOOTER == digest["footer"]
    assert "HERMES DISCORD LOGGING" in style.HEADER
    assert style.u16(style.HEADER) < 400


def test_turn_summaries(monkeypatch):
    monkeypatch.setattr(col, "lookup_title", lambda sid, home: "Fix the build" if sid == "s1" else col.UNTITLED)
    c = Collector()
    c.start_turn("t1", "s1")
    c.count_tool("t1")
    c.count_tool("t1")
    c.count_tool("nope")
    c.end_turn("t1", "completed", None)
    c.start_turn("t2", "s2")
    c.end_turn("t2", "failed", None)
    c.end_turn("never-started", "completed", None)
    out = text_of(c.drain())
    assert "**Turns** | __2 turns__" in out
    assert "Fix the build | completed | 0s | 2 tools" in out
    assert "Untitled session | failed | 0s | 0 tools" in out


def test_duration_format():
    assert [col.duration(s) for s in (0, 59, 60, 125, 3600, 7260)] == ["0s", "59s", "1m 0s", "2m 5s", "1h 0m", "2h 1m"]


def test_hooks_collect_turns_and_skip_subagents(monkeypatch):
    import hdl
    monkeypatch.setattr(col, "lookup_title", lambda sid, home: "T")
    ctx, svc = start_service(monkeypatch, FakeHttp())
    try:
        hdl.pre_llm_call(session_id="s", turn_id="a")
        hdl.pre_llm_call(session_id="sub", turn_id="b", parent_session_id="s")
        hdl.post_tool_call(turn_id="a")
        hdl.on_session_end(turn_id="a", completed=True, failed=False, interrupted=False)
        hdl.on_session_end(turn_id="b", completed=True, failed=False, interrupted=False)
        out = text_of(svc.collector.drain())
        assert "__1 turn__" in out and "1 tool" in out and "completed" in out
    finally:
        stop_service()


def test_summaries_off(monkeypatch):
    import hdl
    ctx, svc = start_service(monkeypatch, FakeHttp(), summaries=False)
    try:
        hdl.pre_llm_call(session_id="s", turn_id="a")
        hdl.on_session_end(turn_id="a", completed=True)
        assert svc.collector.drain() == []
    finally:
        stop_service()


def test_no_assistant_text_is_kept(monkeypatch):
    import hdl
    ctx, svc = start_service(monkeypatch, FakeHttp())
    try:
        hdl.pre_llm_call(session_id="s", turn_id="a")
        hdl.on_session_end(turn_id="a", completed=True, assistant_response="PRIVATE REPLY")
        assert "PRIVATE REPLY" not in text_of(svc.collector.drain())
    finally:
        stop_service()


def test_gateway_only_install(monkeypatch):
    root = logging.getLogger()
    before = list(root.handlers)
    monkeypatch.setattr(service, "in_gateway", lambda: False)
    service.configure(FakeCtx(channel="123"), "TOK")
    try:
        assert service.ensure() is None and root.handlers == before
    finally:
        stop_service()
    ctx, svc = start_service(monkeypatch, FakeHttp())
    try:
        assert service.ensure() is svc and service.ensure() is svc
        assert [h for h in root.handlers if isinstance(h, LogHandler)] == [svc.handler]
        assert sum(t.name == "hdl-sender" for t in threading.enumerate()) == 1
    finally:
        stop_service()
    assert root.handlers == before


def test_in_gateway_follows_runtime_lock(monkeypatch):
    import gateway.status
    monkeypatch.setattr(service, "_gateway", False)
    monkeypatch.setattr(gateway.status, "owns_gateway_runtime_lock", lambda: False)
    assert service.in_gateway() is False
    monkeypatch.setattr(gateway.status, "owns_gateway_runtime_lock", lambda: True)
    assert service.in_gateway() is True


def test_idle_when_channel_empty_or_bad(monkeypatch):
    monkeypatch.setattr(service, "in_gateway", lambda: True)
    root = logging.getLogger()
    before = list(root.handlers)
    for channel in ("", None, "abc", "12 3"):
        assert service.configure(FakeCtx(channel=channel), "TOK") is None
        assert service.ensure() is None
    assert service.configure(FakeCtx(channel="123"), None) is None  # no token
    assert root.handlers == before


def test_channel_may_be_a_yaml_int_and_level_setting(monkeypatch):
    cfg = service.configure(FakeCtx(channel=1575066588099649636, level="error"), "TOK")
    assert cfg["channel"] == "1575066588099649636" and cfg["level"] == logging.ERROR and cfg["summaries"] is True
    stop_service()
    assert service.configure(FakeCtx(channel="1", level="nonsense"), "TOK")["level"] == logging.WARNING
    stop_service()


def test_register_wires_hooks_and_stays_idle(monkeypatch):
    import hdl
    import agent.secret_scope as scope
    monkeypatch.setattr(scope, "get_secret", lambda name: "TOK")
    monkeypatch.setattr(service, "in_gateway", lambda: True)
    ctx = FakeCtx()
    hdl.register(ctx)
    assert ctx.hooks == {} and service.ensure() is None  # idle: no channel, nothing registered
    ctx = FakeCtx(channel="123")
    try:
        hdl.register(ctx)
        assert set(ctx.hooks) == {"pre_llm_call", "post_tool_call", "on_session_end"} and ctx.unload
    finally:
        stop_service()


def _home():
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def _diag(path, *events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def test_crash_find_is_newer_only(tmp_path):
    now = time.time()
    _diag(tmp_path / "gateway-exit-diag.log",
          {"ts": _iso(now - 7200), "tag": "gateway.previous_unclean_exit", "pid": 2, "prior_pid": 1},
          {"ts": _iso(now - 60), "tag": "gateway.exit_nonzero", "pid": 3},
          {"ts": _iso(now - 30), "tag": "gateway.exit_clean", "pid": 4})
    lines, newest = crash.find(tmp_path, None, now)
    assert len(lines) == 2 and "previous_unclean_exit" in lines[0] and "exit_nonzero" in lines[1]
    assert abs(newest - (now - 60)) < 1
    assert crash.find(tmp_path, newest, now) == ([], newest)
    assert crash.find(tmp_path / "missing", None, now) == ([], None)


def test_crash_first_run_ignores_old_events(tmp_path):
    now = time.time()
    _diag(tmp_path / "gateway-exit-diag.log", {"ts": _iso(now - 3 * 86400), "tag": "gateway.exit_nonzero"})
    assert crash.find(tmp_path, None, now) == ([], None)


def test_crash_ignores_critical_lines_in_errors_log(tmp_path):
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 5))
    (tmp_path / "errors.log").write_text(f"{stamp},002 CRITICAL gateway.run: fatal\nTraceback\n", encoding="utf-8")
    assert crash.find(tmp_path, None) == ([], None)  # CRITICAL records are posted live, so not read again


def _exit_log(secret=""):
    logs = _home() / "logs"
    logs.mkdir(exist_ok=True)
    _diag(logs / "gateway-exit-diag.log",
          {"ts": _iso(time.time() - 10), "tag": "gateway.exit_nonzero", "pid": 9, "note": f"token={secret}"})
    return logs / "gateway-exit-diag.log"


def test_crash_reported_once_across_starts_and_redacted(monkeypatch):
    http = FakeHttp()
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    log = _exit_log(secret)
    ctx, svc = start_service(monkeypatch, http)
    try:
        assert ctx.state.get("last_crash") is None  # not before the post
        svc.sender.flush()
        sent = json.loads(http.calls[0][2])["content"]
        assert "**Crash report** | __gateway__" in sent and "exit_nonzero" in sent and secret not in sent
        assert ctx.state.get("last_crash") is not None
    finally:
        stop_service()
    # A restart with the same persisted marker posts nothing.
    monkeypatch.setattr(service, "in_gateway", lambda: True)
    service.configure(ctx, "TOK")
    try:
        assert service.ensure().collector.drain() == []
    finally:
        stop_service()
        log.unlink()


def test_crash_marker_is_kept_when_the_post_fails(monkeypatch):
    http = FakeHttp([(500, None)] * 3)
    log = _exit_log()
    ctx, svc = start_service(monkeypatch, http)
    try:
        svc.sender.flush()
        assert len(http.calls) == 3 and ctx.state.get("last_crash") is None
    finally:
        stop_service()
        log.unlink()


def test_end_to_end_flush_posts_framed_message(monkeypatch):
    http = FakeHttp()
    ctx, svc = start_service(monkeypatch, http)
    try:
        logging.getLogger("agent.e2e").error("pipeline broke")
        svc.collector.wake.set()
        assert wait_for(lambda: len(http.calls) >= 1)  # woken early, before the 1 hour timer
    finally:
        stop_service()
    payload = json.loads(http.calls[0][2])
    assert "pipeline broke" in payload["content"] and payload["allowed_mentions"] == {"parse": []}
    assert style.u16(payload["content"]) <= 2000
