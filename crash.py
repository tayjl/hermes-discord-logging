"""Finds a gateway crash that happened since the last one we reported, from Hermes' exit log."""

import json
import time
from datetime import datetime

# The exit log holds the crashes. CRITICAL records are posted live by the log handler, so they are not read again here.
CRASH_TAGS = ("gateway.exit_nonzero", "gateway.previous_unclean_exit")
HIDDEN_KEYS = {"ts", "tag", "python", "platform", "argv"}
TAIL_BYTES = 64 * 1024
FIRST_RUN_WINDOW_S = 24 * 3600  # with no marker yet, ignore crashes older than this
MAX_EVENTS = 3  # lines per report: the newest ones
RAW_LINE_CHARS = 4000  # raw cap only: the collector redacts first and clips after


def _tail(path):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def _diag_events(path):
    events = []
    for line in _tail(path):
        try:
            record = json.loads(line)
            if record.get("tag") not in CRASH_TAGS:
                continue
            stamp = datetime.fromisoformat(record["ts"]).timestamp()
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        extra = " ".join(f"{k}={v}" for k, v in record.items() if k not in HIDDEN_KEYS)
        events.append((stamp, [f"{time.strftime('%H:%M:%S', time.localtime(stamp))} {record['tag']} {extra}"]))
    return events


def find(logs_dir, after, now=None):
    """Return (lines, newest timestamp) for crashes newer than `after`, or ([], after) when there are none."""
    now = time.time() if now is None else now
    floor = after if after is not None else now - FIRST_RUN_WINDOW_S
    events = [e for e in _diag_events(logs_dir / "gateway-exit-diag.log") if e[0] > floor]
    if not events:
        return [], after
    events.sort(key=lambda e: e[0])
    lines = [line[:RAW_LINE_CHARS] for _, chunk in events[-MAX_EVENTS:] for line in chunk]
    return lines, events[-1][0]
