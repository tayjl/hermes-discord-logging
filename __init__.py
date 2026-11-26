"""Hermes Discord Logging: gateway warnings, errors and a one-line summary per turn, posted to a Discord channel."""

import functools
import logging

from . import service

logger = logging.getLogger(__name__)


def _logged(hook):
    """Hermes logs a failing hook at DEBUG only: make it visible. Type only, never the message."""
    @functools.wraps(hook)
    def wrapper(*args, **kwargs):
        try:
            return hook(*args, **kwargs)
        except Exception as e:
            logger.warning("discord-logging: %s failed (%s)", hook.__name__, type(e).__name__)
    return wrapper


@_logged
def pre_llm_call(session_id=None, turn_id=None, parent_session_id=None, **_):
    svc = service.ensure()
    if svc and svc.cfg["summaries"] and turn_id and not parent_session_id:
        svc.collector.start_turn(turn_id, session_id)


@_logged
def post_tool_call(turn_id=None, **_):
    if service._svc is not None:
        service._svc.collector.count_tool(turn_id)


@_logged
def on_session_end(turn_id=None, completed=None, failed=None, interrupted=None, **_):
    svc = service._svc
    if svc is None or not svc.cfg["summaries"]:
        return
    from hermes_constants import get_hermes_home
    outcome = "interrupted" if interrupted else "failed" if failed else "completed"
    svc.collector.end_turn(turn_id, outcome, get_hermes_home())


def register(ctx):
    from agent.secret_scope import get_secret
    try:
        token = get_secret("DISCORD_BOT_TOKEN")  # read here: threads we start have no secret scope
    except Exception as e:
        token = None
        logger.warning("discord-logging: could not read the bot token (%s)", type(e).__name__)
    if not service.configure(ctx, token):
        return  # idle, or another registration owns the plugin: its hooks are the only ones needed
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.on_unload(lambda: service.stop(ctx))
    service.ensure()
