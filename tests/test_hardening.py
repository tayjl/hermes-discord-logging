"""Review fixes: redaction before clipping, controls stripped before redaction, send backoff, flush caps, ownership.
Secrets are built from parts so this file holds no real-looking key."""
import json
import logging
import sys
import time

from fakes import FakeCtx, FakeHttp, stop_service, wait_for
from hdl import collector as col
from hdl import service, style
from hdl.collector import Collector, LogHandler
from hdl.sender import Sender
from test_logging import handler_with_collector, record, text_of

SECRETS = {
    "ghp": "ghp_" + "Zx8cVb2nMa" * 4,
    "sk": "sk-proj-" + "Qw3rTy9uIo" * 5,
    "xoxb": "xoxb-" + "1234567890-" * 2 + "Ab3dEf6hIj" * 3,
    "akia": "AKIA" + "IOSFODNN7EXAMPLE",
    "discord": "MTA4Nzc2NTQzMjEwOTg3NjU0.GaBcDe." + "xY1zW2vU3tS4rQ5pO6nM7lK8jI9hG0fE1dC2bA",
}


def leaked(text, secret):
    """True when any 8 character run of the secret body shows in the text."""
    return any(secret[i:i + 8] in text for i in range(4, len(secret) - 7))


def one_error(msg, exc=None):
    c, h = handler_with_collector()
    h.emit(record(msg=msg, exc=exc))
    return text_of(c.drain())


def test_clip_boundary_does_not_leave_partial_secrets():
    for name, secret in SECRETS.items():
        for before in (200, 250, 280, 290, 295, 299):
            assert not leaked(one_error("x" * before + " " + secret + " tail"), secret), (name, before)
    out = one_error("y" * 250 + "\npassword=hunter2hunter2hunter2hunter2")
    assert "hunter2hunter2hunter2" not in out and "password=hunter" in out  # at a line start the redactor masks it, then we clip


def test_long_pem_key_is_fully_redacted():
    body = "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + str(n) * 10 for n in range(30))
    pem = "-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----"
    assert "MIIEvQIBADAN" not in one_error("loaded " + pem)
    try:
        raise ValueError("bad key " + pem)
    except ValueError:
        assert "MIIEvQIBADAN" not in one_error("trace", sys.exc_info())


def test_secret_in_traceback_is_not_cut_by_the_line_clip():
    try:
        raise ValueError("x" * 170 + " " + SECRETS["sk"])
    except ValueError:
        out = one_error("failed", sys.exc_info())
    assert not leaked(out, SECRETS["sk"])


def test_control_characters_cannot_hide_a_secret():
    for name, secret in SECRETS.items():
        for ch in ("\x00", "\u200b", "\xad", "\x1b", "\u2060", "\ufeff", "\x7f"):
            for cut in (6, 12, 20, 27, 40):
                assert not leaked(one_error("tok " + secret[:cut] + ch + secret[cut:]), secret), (name, repr(ch), cut)


def test_split_secret_in_crash_lines_and_title(monkeypatch):
    secret = SECRETS["ghp"]
    c = Collector()
    c.add_crash([f"1 note={secret[:9]}\x1b{secret[9:]}"])
    assert not leaked(text_of(c.drain()), secret)
    monkeypatch.setattr(col, "lookup_title", lambda sid, home: "t " + secret[:10] + "\u200b" + secret[10:])
    c.start_turn("t", "s")
    c.end_turn("t", "completed", None)
    assert not leaked(text_of(c.drain()), secret)


def test_crash_line_secret_at_clip_boundary():
    c = Collector()
    c.add_crash(["x" * 290 + " key=" + SECRETS["sk"]])
    assert not leaked(text_of(c.drain()), SECRETS["sk"])


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def loaded_sender(http, clock, n=1):
    c, h = handler_with_collector()
    for i in range(n):
        h.emit(record(msg=f"failure {i}"))
    return c, h, Sender("TOK", "1", c, post=http, sleep=lambda s: None, clock=clock, min_gap=0)


def test_auth_failure_backs_off_then_disables():
    for code in (401, 403, 404):
        clock, http = Clock(), FakeHttp([(code, None)] * 50)
        c, h, s = loaded_sender(http, clock)
        s.flush()
        assert len(http.calls) == 1  # no retry loop on a 4xx
        h.emit(record(msg="more"))
        clock.t += 299
        s.flush()
        assert len(http.calls) == 1 and c.drain()  # still quiet, and the backlog was not drained
        h.emit(record(msg="more again"))
        clock.t += 2
        s.flush()
        assert len(http.calls) == 2  # tried again after 5 minutes
        clock.t += 599
        s.flush()
        assert len(http.calls) == 2  # the second pause is 10 minutes
        h.emit(record(msg="third"))
        clock.t += 2
        s.flush()
        assert len(http.calls) == 3 and s.disabled
        h.emit(record(msg="never"))
        clock.t += 10 ** 6
        s.flush()
        assert len(http.calls) == 3


def test_auth_failure_warns_once_per_step(monkeypatch):
    import hdl.sender as sender_mod
    seen = []
    monkeypatch.setattr(sender_mod.logger, "warning", lambda msg, *a: seen.append(msg % a))
    clock, http = Clock(), FakeHttp([(403, None)] * 50)
    c, h, s = loaded_sender(http, clock)
    for _ in range(5):
        h.emit(record(msg="x"))
        s.flush()
        clock.t += 700
    assert len(seen) == 3 and "pausing 5 minutes" in seen[0] and "posting stopped" in seen[2]


def test_success_resets_auth_strikes():
    clock, http = Clock(), FakeHttp([(403, None), (200, None), (403, None)])
    c, h, s = loaded_sender(http, clock)
    s.flush()
    clock.t += 301
    h.emit(record(msg="a"))
    s.flush()
    assert s._strikes == 0
    h.emit(record(msg="b"))
    s.flush()
    assert s._strikes == 1 and not s.disabled


def test_per_flush_message_cap():
    http = FakeHttp()
    c, h, s = loaded_sender(http, Clock(), n=0)
    for i in range(60):
        h.emit(record(msg=f"{i} " + "q" * 250))
    s.flush()
    posts = [json.loads(b)["content"] for _, _, b in http.calls]
    assert len(posts) == 4 and posts[-1].endswith("more messages not shown")
    assert all(style.u16(p) <= 2000 for p in posts)


def test_minimum_gap_between_flushes():
    http, stamps = FakeHttp(), []
    c, h = handler_with_collector()
    s = Sender("TOK", "1", c, post=lambda *a: (stamps.append(time.monotonic()), http(*a))[1], interval=0.01, min_gap=0.4)
    s.start()
    try:
        h.emit(record(msg="one"))
        c.wake.set()
        assert wait_for(lambda: len(stamps) >= 1)
        h.emit(record(msg="two"))
        c.wake.set()
        assert wait_for(lambda: len(stamps) >= 2)
    finally:
        s.stop()
    assert stamps[1] - stamps[0] >= 0.35


def test_stop_interrupts_a_retry_wait():
    http = FakeHttp([(429, 30)] * 5)
    c, h = handler_with_collector()
    s = Sender("TOK", "1", c, post=http, interval=0.01, min_gap=0)
    s.start()
    h.emit(record(msg="x"))
    c.wake.set()
    assert wait_for(lambda: len(http.calls) >= 1)
    started = time.monotonic()
    s.stop()
    assert time.monotonic() - started < 3 and not s.thread.is_alive() and len(http.calls) <= 2


def test_only_the_owning_registration_controls_the_plugin(monkeypatch):
    import agent.secret_scope as scope
    import hdl
    monkeypatch.setattr(scope, "get_secret", lambda name: "TOK")
    monkeypatch.setattr(service, "in_gateway", lambda: True)
    monkeypatch.setattr(service, "Sender", lambda token, channel, collector: Sender(
        token, channel, collector, post=FakeHttp(), interval=3600))
    first, second = FakeCtx(channel="111"), FakeCtx(channel="222")
    try:
        hdl.register(first)
        hdl.register(second)
        assert first.hooks and second.hooks == {} and second.unload == []  # tool calls are not counted twice
        assert service._svc.cfg["channel"] == "111"
        service.stop(second)
        assert service._svc is not None
        first.unload[0]()
        assert service._svc is None
    finally:
        stop_service()


def test_turns_are_capped_with_a_count(monkeypatch):
    monkeypatch.setattr(col, "lookup_title", lambda sid, home: "T")
    c = Collector()
    for n in range(col.MAX_TURNS + 25):
        c.start_turn(f"t{n}", "s")
        c.end_turn(f"t{n}", "completed", None)
    out = text_of(c.drain())
    assert out.count("| completed |") == col.MAX_TURNS
    assert "25 more turns not shown" in out and f"__{col.MAX_TURNS + 25} turns__" in out
    assert c.drain() == []  # the count resets


def test_nothing_is_collected_once_the_sender_is_disabled():
    clock, http = Clock(), FakeHttp([(403, None)] * 50)
    c, h, s = loaded_sender(http, clock)
    for _ in range(3):
        s.flush()
        h.emit(record(msg="again"))
        clock.t += 700
    s.flush()
    assert s.disabled and c.disabled
    h.emit(record(msg="late error"))
    c.start_turn("t", "s")
    c.end_turn("t", "completed", None)
    assert c.drain() == []


def test_cut_traceback_cannot_leave_a_pem_body():
    body = "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + str(n) * 10 for n in range(40))
    pem = "-----BEGIN PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----"
    for pad in range(0, 4000, 137):  # slide the 4000 character cut across the BEGIN line
        try:
            raise ValueError("p" * pad + "\n" + pem + "\n" + "z" * 1500)
        except ValueError:
            out = one_error("failed", sys.exc_info())
        assert "MIIEvQIBADAN" not in out, pad
    cut = col._cap_tail("a\n" * 3000)
    assert len(cut) <= col.RAW_CAP and cut.startswith("a\n")  # cut at a line boundary


def test_long_message_with_cut_pem_is_redacted():
    body = "\n".join("MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC" + str(n) * 10 for n in range(100))
    out = one_error("key: -----BEGIN PRIVATE KEY-----\n" + body)  # the end of the block is past the cap
    assert "MIIEvQIBADAN" not in out
