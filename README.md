# Hermes Discord Logging

See what your gateway is doing from your phone. Warnings and errors, and a one-line summary of every agent turn, arrive in a Discord channel of your choice.

It uses the bot token Hermes already has and Discord's REST API, so it opens no second connection.

## What you get

- **Errors.** Records at WARNING or above from the gateway, batched about every 10 seconds. Identical repeats become one line with a count (`x4`). Tracebacks are clipped to the part that matters.
- **Turns.** One compact line per turn: time, session title, outcome (completed, failed or interrupted), duration and tool call count. The assistant's reply is never sent.
- **Crash report.** If the gateway exited badly, the next start posts one summary from `gateway-exit-diag.log`. Each crash is posted once, and only marked as posted after Discord accepted it. CRITICAL log records are posted live, so they are not read again.
- **Safe by design.** Every line goes through Hermes' redactor first. Messages never ping anyone. Control characters and triple backticks are neutralised, and long batches split into several messages under Discord's 2000 character limit.

The plugin only runs inside the gateway process. Anywhere else it does nothing.

## Install

Run these in the default profile only. `DISCORD_BOT_TOKEN` must be set. The bot needs permission to view and send messages in the channel.

```
mklink /J <hermes home>\plugins\discord-logging <path to this repo>
hermes plugins enable discord-logging
hermes config set plugins.entries.discord-logging.settings.channel 1575066588099649636
hermes gateway restart
```

On macOS and Linux, use a symlink instead of `mklink /J`. Replace the channel id with your own.

Do not enable the plugin in other profiles. The gateway serves every profile from one process, so the default profile's copy already sees all of them.

## Settings

Set under `plugins.entries.discord-logging.settings`. Changes apply after a gateway restart.

| Setting | Default | Meaning |
| --- | --- | --- |
| `channel` | empty | Channel id (digits). Empty keeps the plugin idle. |
| `level` | `WARNING` | Lowest level to post: `WARNING`, `ERROR` or `CRITICAL`. |
| `summaries` | `true` | Post the one-line turn summaries. |

To turn it off, run `hermes config set plugins.entries.discord-logging.settings.channel ""` or `hermes plugins disable discord-logging`, then restart the gateway.

## Limitations

- A hard crash can't be posted live, because the process is gone. It is reported at the next start.
- A restart that Hermes records as an unclean exit is also reported as a crash.
- If Discord is unreachable, batches are retried a few times and then dropped. One warning is logged.
- A 401, 403 or 404 (bad token, no access or wrong channel) pauses posting for 5 minutes, then 10, and the third repeat stops posting until the next start. This protects the invalid-request limit the bot's gateway connection shares.
- At most 3 messages go out per flush, plus a line saying how many more were not shown. Flushes are at least 5 seconds apart.
- Raw log text is redacted first and only then clipped, with control and invisible characters removed beforehand. The Hermes redactor decides what counts as a secret, so a password written mid-sentence may not be masked.
- The gateway serves every profile from one process. The first profile that enables the plugin owns it, and other registrations do nothing.
- Records from `discord`, `urllib3` and `http` loggers, and from this plugin, are never posted, so a failed post can't cause a loop.
- The plugin starts once the process owns the gateway lock. Errors logged before the first turn may be missed if that happens after load.

## Tests

No network is used. Run with Hermes' venv python:

```
<hermes venv python> tests/run.py
```
