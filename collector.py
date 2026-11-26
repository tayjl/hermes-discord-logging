"""What gets posted: batched log records, one line per turn, crash reports. Turns text into framed messages."""

import logging
import threading
import time
import traceback
from collections import OrderedDict

from . import style

UNTITLED = "Untitled session"
NEAR_LIMIT = 1200  # pending characters that trigger an early flush, well under one message
MAX_ENTRIES = 100  # distinct pending lines per kind: a flood is counted, not stored
LINE_CHARS = 300
TRACE_LINE_CHARS = 200
MAX_TURNS = 200  # pending turn lines: a paused sender gets a count, not a backlog
RAW_CAP = 4000  # raw characters kept per record part: clipping happens only after redaction
TRACE_TAIL = 6  # the last lines of a traceback hold the exception
TITLE_CHARS = 60
MAX_AGE_S = 24 * 3600  # turns that never end are dropped after this

# Loggers that send or relay our posts: logging from them could loop.
EXCLUDED_PREFIXES = ("discord", "urllib3", "urllib", "http", "requests")
OWN_THREAD = "hdl-sender"


def redact(text):
    """Hermes' egress scrub (strict, URL credentials too). Never returns the raw text if it can't run."""
    try:
        from agent.redact import redact_for_egress
        return redact_for_egress(text, urls=True)
    except Exception:
        return "[redaction unavailable]"


def scrub(text):
    """Strip controls and invisible characters, redact, then clip: a clip before redaction would leave a partial secret,
    and a control character inside a secret would hide it from the redactor."""
    return redact(style.strip_controls(text))


def lookup_title(session_id, home):
    try:
        from persistence_assembly import open_persistence_reader
        with open_persistence_reader(home) as persistence:
            return persistence.sessions.get_title(session_id) or UNTITLED
    except Exception:
        return UNTITLED


def duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


def _clock(epoch):
    return time.strftime("%H:%M:%S", time.localtime(epoch))


class Collector:
    def __init__(self):
        self.wake = threading.Event()  # set when enough is pending to flush early
        self._lock = threading.Lock()
        self._errors = OrderedDict()  # (level, logger, body) -> [first time, count]
        self._dropped = 0
        self._turns = []
        self._turns_dropped = 0
        self.disabled = False  # set by the sender when it gives up: nothing more is collected
        self._crash = []
        self.crash_ack = None  # set while a crash report is pending, taken by drain()
        self.sent_ack = None  # the pending crash report's ack, for the sender to call after a successful post
        self._starts = {}  # turn_id -> [monotonic start, session_id, tool calls]
        self._size = 0

    def _grew(self, chars):
        self._size += chars
        if self._size >= NEAR_LIMIT:
            self.wake.set()

    def add_error(self, level, name, body, trace, created):
        with self._lock:
            if self.disabled:
                return
            key = (level, name, body, trace)
            if key in self._errors:
                self._errors[key][1] += 1
            elif len(self._errors) >= MAX_ENTRIES:
                self._dropped += 1
            else:
                self._errors[key] = [created, 1]
                self._grew(min(len(body), LINE_CHARS) + len(name) + 20)

    def add_crash(self, lines, ack=None):
        """ack runs once the post that carries these lines succeeded (the sender calls it)."""
        with self._lock:
            self._crash.extend(lines)
            self.crash_ack = ack
            self._grew(sum(min(len(line), LINE_CHARS) for line in lines))

    def start_turn(self, turn_id, session_id):
        now = time.monotonic()
        with self._lock:
            for key in [k for k, v in self._starts.items() if now - v[0] > MAX_AGE_S]:
                del self._starts[key]
            self._starts.setdefault(turn_id, [now, session_id, 0])

    def count_tool(self, turn_id):
        with self._lock:
            if turn_id in self._starts:
                self._starts[turn_id][2] += 1

    def end_turn(self, turn_id, outcome, home):
        with self._lock:
            started = self._starts.pop(turn_id, None)
            if started is None or self.disabled:
                return
            if len(self._turns) >= MAX_TURNS:
                self._turns_dropped += 1
                return
            self._turns.append((time.time(), started[1], home, outcome, time.monotonic() - started[0], started[2]))
            self._grew(80)

    def drain(self):
        """Take everything pending and return the finished messages (redacted, framed, within the limit)."""
        with self._lock:
            errors, self._errors = self._errors, OrderedDict()
            dropped, self._dropped = self._dropped, 0
            turns, self._turns = self._turns, []
            turns_dropped, self._turns_dropped = self._turns_dropped, 0
            crash, self._crash = self._crash, []
            self.sent_ack, self.crash_ack = (self.crash_ack if crash else None), None
            self._size = 0
            self.wake.clear()
        messages = []
        if crash:
            messages += style.render("Crash report", "gateway", [style.clip(scrub(line), LINE_CHARS) for line in crash])
        if errors:
            messages += style.render("Errors", "gateway", self._error_lines(errors, dropped))
        if turns or turns_dropped:
            lines = [self._turn_line(*turn) for turn in turns]
            if turns_dropped:
                lines.append(f"{turns_dropped} more turns not shown")
            count = len(turns) + turns_dropped
            messages += style.render("Turns", f"{count} turn{'' if count == 1 else 's'}", lines)
        return messages

    @staticmethod
    def _error_lines(errors, dropped):
        merged = OrderedDict()  # redaction can make two different records identical
        for (level, name, body, trace), (created, count) in errors.items():
            key = (level, name, scrub(body), scrub(trace))
            if key in merged:
                merged[key][1] += count
            else:
                merged[key] = [created, count]
        lines = []
        for (level, name, body, trace), (created, count) in merged.items():
            body = style.clip(body.strip(), LINE_CHARS)
            first, _, rest = body.partition("\n")
            lines.append(f"{_clock(created)} {level} {name}: {first}" + (f" x{count}" if count > 1 else ""))
            lines.extend(rest.split("\n") if rest else [])
            lines.extend(_trace_lines(trace))
        if dropped:
            lines.append(f"{dropped} more lines not shown")
        return lines

    @staticmethod
    def _turn_line(ended, session_id, home, outcome, seconds, tools):
        title = " ".join(style.plain(style.clip(scrub(lookup_title(session_id, home)), TITLE_CHARS)).split())
        return (f"{_clock(ended)} {title} | {outcome} | {duration(seconds)}"
                f" | {tools} tool{'' if tools == 1 else 's'}")


def _trace_lines(trace):
    """Clip an already redacted traceback to its first line and its tail, as indented lines."""
    lines = [line for line in trace.strip().splitlines() if line.strip(" ~^")]  # caret underlines only add length
    if len(lines) > TRACE_TAIL + 1:
        lines = lines[:1] + ["..."] + lines[-TRACE_TAIL:]
    return ["  " + style.clip(line.rstrip(), TRACE_LINE_CHARS) for line in lines]


def _cap_tail(text):
    """Keep the last RAW_CAP characters from a line boundary. A PEM block whose BEGIN line was cut off would leave its
    body for the redactor to miss, so everything up to that block's END line goes too."""
    if len(text) <= RAW_CAP:
        return text
    text = text[-RAW_CAP:].partition("\n")[2]
    end = text.find("-----END ")
    if end != -1 and "-----BEGIN " not in text[:end]:
        text = text[end:].partition("\n")[2]
    return text


def _parts(record):
    """The record's message and traceback as raw text (capped, not clipped to display size)."""
    trace = ""
    if record.exc_info and record.exc_info[0] is not None:
        trace = _cap_tail("".join(traceback.format_exception(*record.exc_info)))
    return record.getMessage()[:RAW_CAP], trace


class LogHandler(logging.Handler):
    def __init__(self, collector, level, own_prefix):
        super().__init__(level)
        self._collector = collector
        self._own = own_prefix
        self._busy = threading.local()

    def _excluded(self, record):
        name = record.name
        return (name.startswith(EXCLUDED_PREFIXES) or name == self._own or name.startswith(self._own + ".")
                or record.threadName == OWN_THREAD)

    def emit(self, record):
        if getattr(self._busy, "on", False) or self._excluded(record):
            return
        self._busy.on = True  # anything logged while we work on a record is ignored
        try:
            self._collector.add_error(record.levelname, record.name, *_parts(record), record.created)
        except Exception:
            pass
        finally:
            self._busy.on = False
