"""Test helpers: a fake plugin context and a fake HTTP sender. No network."""
import time

from hdl import service
from hdl.sender import Sender


class FakeState:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


class FakeCtx:
    def __init__(self, **settings):
        self.settings = settings
        self.state = FakeState()
        self.hooks = {}
        self.unload = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback, **_):
        self.hooks[name] = callback

    def on_unload(self, callback):
        self.unload.append(callback)


class FakeHttp:
    """Stands in for http_post: records every call, answers from a script of (status, retry_after)."""

    def __init__(self, script=()):
        self.calls = []
        self.script = list(script)

    def __call__(self, url, token, body):
        self.calls.append((url, token, body))
        return self.script.pop(0) if self.script else (200, None)


def start_service(monkeypatch, http, **settings):
    """configure() and ensure() as the gateway would, with the fake sender and a slow flush timer."""
    monkeypatch.setattr(service, "in_gateway", lambda: True)
    monkeypatch.setattr(service, "Sender", lambda token, channel, collector: Sender(
        token, channel, collector, post=http, sleep=lambda s: None, interval=3600))
    ctx = FakeCtx(channel="1575066588099649636", **settings)
    service.configure(ctx, "TOKEN-NOT-REAL")
    return ctx, service.ensure()


def wait_for(predicate, seconds=5):
    end = time.time() + seconds
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def stop_service():
    """service.stop() as the owning registration's unload would call it."""
    if service._cfg is not None:
        service.stop(service._cfg["ctx"])
