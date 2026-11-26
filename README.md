# Hermes Discord Logging

See what your gateway is doing from your phone. Warnings, errors and a one-line summary of each agent turn go to a Discord channel you choose.

It uses the bot token Hermes already has and Discord's REST API, so it opens no second connection.

## Quick setup

Paste this prompt into Hermes to begin setup:

```text
Set up the discord-logging plugin for me. Do this in the default profile only.

1. Clone https://github.com/tayjl/hermes-discord-logging.git and note the local path.
2. Link the repo into the default profile's plugins dir as "discord-logging".
   On Windows, use a junction: mklink /J <hermes home>\plugins\discord-logging <repo path>
   Elsewhere, use a symlink.
3. Check that DISCORD_BOT_TOKEN is set in the default profile's env. It must be the same bot the Discord platform uses.
   If it is missing, ask me for it.
4. Run: hermes plugins enable discord-logging
5. Run: hermes config set plugins.entries.discord-logging.settings.channel <logs channel id>
6. Check that the bot can View Channel and Send Messages in that channel. If not, ask me to fix it.
7. Run: hermes gateway restart
```

### Manual install

Run these in the default profile only. Set `DISCORD_BOT_TOKEN` first. It must be the same bot the Discord platform uses. The bot needs View Channel and Send Messages on the channel.

```
mklink /J <hermes home>\plugins\discord-logging <path to this repo>
hermes plugins enable discord-logging
hermes config set plugins.entries.discord-logging.settings.channel <logs channel id>
hermes gateway restart
```

On macOS and Linux, use a symlink instead of `mklink /J`. Replace `<logs channel id>` with your channel id.

Do not enable the plugin in other profiles. The gateway serves every profile from one process, so the default profile's copy already sees all of them.

## Settings

Set under `plugins.entries.discord-logging.settings`. Changes apply after a gateway restart.

| Setting | Default | Meaning |
| --- | --- | --- |
| `channel` | empty | Channel id (digits only). Empty keeps the plugin idle. |
| `level` | `WARNING` | Lowest level to post: `WARNING`, `ERROR` or `CRITICAL`. |
| `summaries` | `true` | Post the one-line turn summaries. |

## What you get

- **Errors.** Gateway records at WARNING or above, batched about every 10 seconds. Identical repeats merge into one line with a count (`x4`). Tracebacks are clipped to the useful part.
- **Turns.** One line per turn: time, session title, outcome (completed, failed or interrupted), duration and tool call count. The reply is never sent.
- **Crash report.** If the gateway exited badly, the next start posts one summary from `gateway-exit-diag.log`. Each crash posts once, and is marked posted only after Discord accepts it. CRITICAL records post live, so they are not read again.
- **Safe by design.** Every line goes through the Hermes redactor. Control characters are stripped before redaction. Messages never ping anyone. Triple backticks are neutralised. Long batches split under Discord's 2000 character limit.

The plugin only runs inside the gateway process. Anywhere else it does nothing.

## If something breaks

Turn it off with `hermes config set plugins.entries.discord-logging.settings.channel ""` or `hermes plugins disable discord-logging`. Then restart the gateway.

## Limitations

- A hard crash can't be posted live, because the process is gone. It is reported at the next start.
- A restart that Hermes records as an unclean exit is also reported as a crash.
- If Discord is unreachable, batches are retried a few times, then dropped. One warning is logged.
- A 401, 403 or 404 (bad token, no access or wrong channel) pauses posting for 5 minutes, then 10. A third repeat stops posting until the next start.
- At most 3 messages go out per flush, plus a line saying how many were not shown. Flushes are at least 5 seconds apart.
- The redactor decides what counts as a secret, so a key=value secret written mid-line may not be masked.
- The gateway serves every profile from one process. The first profile that enables the plugin owns it.
- Logs from the `discord`, `urllib3` and `http` loggers, and from this plugin, are never posted. A failed post can't cause a loop.
- The plugin starts once the process owns the gateway lock. Errors logged before that point may be missed.

## Development

No network is used in tests. Run them with the Hermes venv Python:

```
<hermes venv python> tests/run.py
```
