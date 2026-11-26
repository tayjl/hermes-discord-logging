"""House look for Discord posts: the daily digest frame, a yellow-bordered ansi box, and 2000-unit splitting."""

import re

LIMIT = 2000  # Discord's message limit, counted in UTF-16 units
WIDTH = 38

# Same escapes as scripts/gmail-digest/digest-blocks.json. They count toward the limit.
YELLOW, RESET, REVERSED = "\x1b[1;38;2;255;216;0m", "\x1b[0m", "\x1b[7;37m"
RULE = "\u2500"
TITLE = "HERMES DISCORD LOGGING"


def u16(text):
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def header(title=TITLE):
    left = (WIDTH - len(title)) // 2
    right = WIDTH - len(title) - left
    return (f"```ansi\n{YELLOW}\u256d{RULE * (WIDTH - 2)}\u256e\n"
            f"{REVERSED}{'-' * left}{RESET}{YELLOW}{title}{REVERSED}{'-' * right}{RESET}{YELLOW}\n"
            f"\u2570{RULE * (WIDTH - 2)}\u256f{RESET}\n```")


HEADER = header()
FOOTER = f"```ansi\n{YELLOW}\u2570{RULE * (WIDTH - 2)}\u256f{RESET}\n```"
BOX_TOP = f"```ansi\n{YELLOW}\u250c{RULE * (WIDTH - 1)}{RESET}\n"
BOX_BOTTOM = f"\n{YELLOW}\u2514{RULE * (WIDTH - 1)}{RESET}\n```"

# Control characters (ESC included) except newline and tab, plus invisible direction and separator characters.
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad\u034f\u061c\u180e]")


def strip_controls(text):
    return _CONTROL.sub("", text)


def plain(text):
    """Safe inside an ansi block: no fake colours, and no run of 3 backticks that could close the box."""
    text = strip_controls(text)
    return re.sub(r"`(?=``)", "`\u200b", text)  # a zero-width space breaks every run of 3 backticks


def clip(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def render(kind, secondary, lines):
    """Split lines into framed messages that each fit Discord's limit.

    Message 1 holds the header and the kind line, later ones start with a lone U+200B so they stack under it,
    and the footer closes the last one. Every message reserves room for the footer.
    """
    head = f"{HEADER}\n**{kind}** | __{secondary}__\n"
    cont = "\u200b\n"
    fixed = u16(BOX_TOP) + u16(BOX_BOTTOM) + 1 + u16(FOOTER)
    messages, current, size, prefix = [], [], 0, head
    for line in lines:
        line = plain(line)
        room = LIMIT - fixed - u16(prefix)
        while u16(line) > room - 1:  # one oversized line can't be split further, so trim it
            line = line[:-1]
        if current and size + u16(line) + 1 > room:
            messages.append((prefix, current))
            current, size, prefix = [], 0, cont
        current.append(line)
        size += u16(line) + 1
    if current:
        messages.append((prefix, current))
    last = len(messages) - 1
    return [prefix + BOX_TOP + "\n".join(chunk) + BOX_BOTTOM + ("\n" + FOOTER if n == last else "")
            for n, (prefix, chunk) in enumerate(messages)]
