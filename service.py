"""Process-wide state. register() runs once per enabled profile in one multiplexed gateway process, so everything here
is a lazy module-level singleton that starts once, and only in the gateway."""

import logging
import threading

from . import collector as collector_mod
from . import crash
from .sender import Sender

logger = logging.getLogger(__name__)

DEFAULT_LEVEL = "WARNING"

_lock = threading.Lock()
_cfg = None  # settings read by configure(), or None while idle
_svc = None
_gateway = False  # cached once True: the lock may be claimed after the plugin loads
_gateway_logged = None


class Service:
    def __init__(self, cfg):
        self.cfg = cfg
        self.collector = collector_mod.Collector()
        self.sender = Sender(cfg["token"], cfg["channel"], self.collector)
        self.handler = collector_mod.LogHandler(self.collector, cfg["level"], __package__ or __name__)


def in_gateway():
    global _gateway, _gateway_logged
    if _gateway:
        return True
    try:
        from gateway.status import owns_gateway_runtime_lock
        _gateway = bool(owns_gateway_runtime_lock())
    except Exception:
        pass
    if _gateway_logged != _gateway:
        _gateway_logged = _gateway
        logger.info("discord-logging: this process %s the gateway", "is" if _gateway else "is not")
    return _gateway


def _level(raw):
    level = logging.getLevelName(str(raw or DEFAULT_LEVEL).strip().upper())
    if isinstance(level, int):
        return level
    logger.warning("discord-logging: unknown level %r, using %s", raw, DEFAULT_LEVEL)
    return logging.getLevelName(DEFAULT_LEVEL)


def configure(ctx, token):
    """Read the settings. A missing or non-numeric channel leaves the plugin idle."""
    global _cfg
    if _cfg is not None:
        logger.debug("discord-logging: already configured by another registration")
        return None  # one gateway process serves every profile: the first registration owns the plugin
    channel = str(ctx.get_config("channel", "") or "").strip()
    if not channel.isdigit():
        if channel:
            logger.warning("discord-logging: channel must be digits, staying idle")
        else:
            logger.info("discord-logging: no channel set, staying idle")
        return None
    if not token:
        logger.warning("discord-logging: no DISCORD_BOT_TOKEN, staying idle")
        return None
    with _lock:
        _cfg = {"ctx": ctx, "channel": channel, "token": token, "level": _level(ctx.get_config("level", DEFAULT_LEVEL)),
                "summaries": bool(ctx.get_config("summaries", True))}
    return _cfg


def ensure():
    """Start posting once. Returns the Service, or None while idle or outside the gateway."""
    global _svc
    if _svc is not None:
        return _svc
    if _cfg is None or not in_gateway():
        return None
    with _lock:
        if _svc is None:
            svc = Service(_cfg)
            logging.getLogger().addHandler(svc.handler)
            svc.sender.start()
            _svc = svc
            logger.info("discord-logging: posting to channel %s at %s",
                        svc.cfg["channel"], logging.getLevelName(svc.cfg["level"]))
            _report_crash(svc)
    return _svc


def _report_crash(svc):
    try:
        from hermes_constants import get_hermes_home
        state = svc.cfg["ctx"].state
        lines, newest = crash.find(get_hermes_home() / "logs", state.get("last_crash"))
        if lines:  # the marker moves only after the post succeeded
            svc.collector.add_crash(lines, ack=lambda: state.set("last_crash", newest))
    except Exception as e:
        logger.warning("discord-logging: crash check failed (%s)", type(e).__name__)


def stop(ctx):
    """Only the owning registration may stop the plugin: another profile unloading must not silence the gateway."""
    global _svc, _cfg
    with _lock:
        if _cfg is None or _cfg["ctx"] is not ctx:
            return
        svc, _svc, _cfg = _svc, None, None
    if svc is not None:
        logging.getLogger().removeHandler(svc.handler)
        svc.sender.stop()
