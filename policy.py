import re
from dataclasses import dataclass

from events import Event

PROACTIVE = "proactive"
DIRECT = "direct"
OWNER_CHECKIN = "owner_checkin"
SECRET = re.compile(r"(?i)\b(?:sk-[\w-]{12,}|(?:api[_ -]?key|token|密钥)\s*[:=]\s*[\w-]{8,})")
UNSAFE = re.compile(r"我(?:是|就是真正的)(?:真人|人类)|\[/?PANEL_CUSTOM_PERSONA\]|\[CQ:|忽略(?:之前|以上|所有)(?:的)?指令")


@dataclass(frozen=True, slots=True)
class Decision:
    reason: str
    next_count: int

    @property
    def evaluate(self) -> bool:
        return self.reason in {DIRECT, PROACTIVE, OWNER_CHECKIN}


def decide(event: Event, count: int, every: int) -> Decision:
    if event.platform == "qq" and event.channel_kind == "dm" and event.speaker == "OWNER" and event.text.strip():
        return Decision(DIRECT, count)
    if event.mentions_self:
        return Decision(DIRECT, count)
    if event.mentions_others:
        return Decision("mentions_others", count)
    if event.reply_to and not event.reply_author_id:
        return Decision("unknown_reply", count)
    if not every or event.channel_kind != "group" or not event.text.strip():
        return Decision("no_trigger", count)
    count += 1
    return Decision(PROACTIVE, 0) if count >= every else Decision("counting", count)


def allowed(allowlist: list[str], channel_kind: str, channel_id: str) -> bool:
    return (
        f"{channel_kind}:{channel_id}" in allowlist
        or (channel_kind == "group" and channel_id in allowlist)
    )


def audit_text(text: str, prompt: str, max_chars: int) -> bool:
    clean = text.strip()
    # 完整提示词和较长提示片段不得回显；短人设名称可以自然出现在对话中。
    leaks_prompt = any(
        len(line) >= 12 and (line in clean or (len(clean) >= 12 and clean in line))
        for line in map(str.strip, prompt.splitlines())
    )
    return bool(
        clean and (not max_chars or len(clean) <= max_chars) and not SECRET.search(clean)
        and not UNSAFE.search(clean) and not leaks_prompt
        and not any(ord(char) < 32 and char not in "\n\t" for char in clean)
    )
