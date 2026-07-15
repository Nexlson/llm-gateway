from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RequestFeatures:
    input_tokens: int
    has_tools: bool
    system_prompt: str
    headers: dict[str, str]


def estimate_tokens(text: str) -> int:
    # Routing-only heuristic (~4 chars/token). NOT used for billing — cost comes
    # from provider `usage`. Isolated here so a real tokenizer can replace it.
    if not text:
        return 0
    return (len(text) + 3) // 4


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            p["text"]
            for p in content
            if isinstance(p, dict) and isinstance(p.get("text"), str)
        ]
        return " ".join(parts)
    return ""


def extract_features(body: dict, headers: dict[str, str]) -> RequestFeatures:
    messages = body.get("messages") or []
    all_text: list[str] = []
    system_text: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        text = _content_text(message.get("content"))
        all_text.append(text)
        if message.get("role") == "system":
            system_text.append(text)

    has_tools = bool(body.get("tools")) or bool(body.get("functions"))
    return RequestFeatures(
        input_tokens=estimate_tokens("".join(all_text)),
        has_tools=has_tools,
        system_prompt="\n".join(system_text),
        headers={k.lower(): v for k, v in headers.items()},
    )
