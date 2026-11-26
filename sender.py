"""One background daemon thread: flushes the collector every few seconds and posts through Discord's REST API."""

import json
import logging
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

API = "https://discord.com/api/v10/channels/{}/messages"
THREAD_NAME = "hdl-sender"
FLUSH_S = 10
ATTEMPTS = 3
MAX_WAIT_S = 30  # longest we sleep for one retry, whatever Discord asks
MAX_MESSAGES = 3  # per flush: a flood is summarised, not posted
MIN_GAP_S = 5  # least time between two flushes, so an early wake cannot send back to back
AUTH_CODES = (401, 403, 404)  # bad token, no access or wrong channel: retrying only burns the shared invalid-request limit
AUTH_BACKOFF_S = 300  # doubles with each repeat
AUTH_STRIKES = 3  # repeats before the sender gives up for this run


def http_post(url, token, body):
    """POST once. Returns (status, retry_after seconds or None). Status 0 means the network failed."""
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bot {token}", "Content-Type": "application/json",
        "User-Agent": "DiscordBot (hermes-discord-logging, 1.0.0)"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, float(json.loads(e.read().decode("utf-8", "replace")).get("retry_after"))
        except Exception:
            return e.code, None
    except Exception:
        return 0, None


class Sender:
    def __init__(self, token, channel, collector, post=http_post, sleep=None, interval=FLUSH_S, clock=time.monotonic,
                 min_gap=MIN_GAP_S):
        self._token = token  # never logged
        self._url = API.format(channel)
        self._collector = collector
        self._post = post
        self._stop = threading.Event()
        self._sleep = sleep or self._stop.wait  # a stop wakes a waiting retry
        self._interval = interval
        self._clock = clock
        self._min_gap = min_gap
        self._last_flush = None
        self._failing = False
        self._strikes = 0
        self._quiet_until = 0
        self.disabled = False
        self.thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._collector.wake.set()
        if self.thread.is_alive():
            self.thread.join(5)

    def _run(self):
        while not self._stop.is_set():
            self._collector.wake.wait(self._interval)
            self._collector.wake.clear()
            if self._last_flush is not None:
                self._stop.wait(max(0, self._min_gap - (self._clock() - self._last_flush)))
            if not self._stop.is_set():
                self.flush()
        self.flush()

    def flush(self):
        if self.disabled or self._clock() < self._quiet_until:
            return  # nothing is drained: the collector keeps a bounded backlog meanwhile
        self._last_flush = self._clock()
        try:
            messages = self._collector.drain()
            extra = len(messages) - MAX_MESSAGES
            if extra > 0:
                messages = messages[:MAX_MESSAGES] + [f"{extra} more message{'' if extra == 1 else 's'} not shown"]
            for n, text in enumerate(messages):
                if not self.send(text):
                    break  # the rest would fail the same way
                if n == 0 and self._collector.sent_ack:
                    self._collector.sent_ack()  # a crash report goes first, so its post is the first one
                    self._collector.sent_ack = None
        except Exception as e:
            logger.warning("discord-logging: flush failed (%s)", type(e).__name__)

    def send(self, text):
        # An empty parse list means no mention in the text can ever ping anyone.
        body = json.dumps({"content": text, "allowed_mentions": {"parse": []}}).encode("utf-8")
        status = 0
        for attempt in range(ATTEMPTS):
            status, retry = self._post(self._url, self._token, body)
            if 200 <= status < 300:
                self._failing, self._strikes = False, 0
                return True
            if status in AUTH_CODES:
                break
            if status == 429 or status >= 500 or status == 0:
                if attempt < ATTEMPTS - 1 and self._sleep(min(retry or 2, MAX_WAIT_S)):
                    break  # stopping
                continue
            break  # another 4xx will not get better by retrying
        if status in AUTH_CODES:
            self._strikes += 1
            if self._strikes >= AUTH_STRIKES:
                self.disabled = self._collector.disabled = True
                logger.warning("discord-logging: status %s again and again, posting stopped until the next start", status)
            else:
                self._quiet_until = self._clock() + AUTH_BACKOFF_S * 2 ** (self._strikes - 1)
                logger.warning("discord-logging: could not post (status %s), pausing %d minutes", status,
                               AUTH_BACKOFF_S * 2 ** (self._strikes - 1) // 60)
        elif not self._failing:  # one line per run of failures, so a bad channel cannot flood the log
            self._failing = True
            logger.warning("discord-logging: could not post (status %s)", status)
        return False
