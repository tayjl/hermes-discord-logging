# Changelog

## Unreleased

- Stops posting logs from `agent.tool_executor` and its children. Routine tool warnings were flooding the channel.
- Shows the yellow title header only on the first post after the plugin starts, instead of on every post.

## 1.0.0

- First release. Posts gateway warnings and errors to a Discord channel, batched about every 10 seconds, with identical repeats merged into one line (xN) and tracebacks clipped.
- One summary line per agent turn (time, session title, outcome, duration, tool calls). No assistant text is ever sent.
- Reports a gateway crash once, at the next start, from the exit log. The marker is saved only after the post succeeded.
- Sends through Discord's REST API from one background thread, honours rate limits, and never allows mentions.
- Redacts every line with Hermes' own redactor before it is clipped, after removing control and invisible characters.
- Backs off after 401, 403 or 404 and stops after three repeats. Caps posts per flush and spaces flushes at least 5 seconds apart.
- Only runs inside the gateway. Idle until a channel is set.
