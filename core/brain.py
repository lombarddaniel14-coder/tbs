"""Claude API wrapper: system prompt, rolling history, and the tool-use loop.

The brain is deliberately transport-agnostic — it takes a string in and gives a
string back. main.py decides whether that string came from a microphone or a
keyboard, and where it goes afterwards.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

# Allow `import config` / `import tools` regardless of how TBS was started.
_CORE_DIR = str(Path(__file__).resolve().parent)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import config  # noqa: E402
import tools as toolkit  # noqa: E402
from streaming import SentenceBuffer, split_sentences  # noqa: E402

try:
    import anthropic  # noqa: E402
except ImportError as exc:  # pragma: no cover - dependency check
    raise config.ConfigError(
        "The `anthropic` package is not installed.\n"
        "Install the dependencies first:\n"
        "    py -3.11 -m pip install -r requirements.txt"
    ) from exc


FALLBACK_SYSTEM_PROMPT = """
You are Buddy, a local assistant running on {user}'s Windows laptop.

Voice and manner: composed, dry, economical. Address him as "Sir". Lead with the
answer, then the detail. Never pad a reply with filler.

You are wired into the machine itself. When a request touches system state,
files, apps, the web, the calendar, or something worth remembering, call the
matching tool rather than guessing or telling him to do it himself. Report what
you actually did.

Your replies are often spoken aloud, so write them to be heard: full sentences,
no markdown, no bullet lists, no code blocks unless he explicitly asks for code.
Say numbers as words a person would say — "battery at thirty four percent",
not "34%".
""".strip()

_STYLE_RULES = """
Operating rules:
- Answer only with the final answer. Do not narrate your reasoning or list steps
  you considered and rejected.
- Prefer one tool call that answers the question over several speculative ones.
- If a tool fails, say plainly what failed and what you would try next.
- Keep spoken replies under about four sentences unless asked for more.
""".strip()


class Brain:
    """Holds the conversation and talks to Claude."""

    def __init__(self, on_tool: Callable[[str, dict], None] | None = None) -> None:
        self.client = anthropic.Anthropic(api_key=config.get_api_key())
        self.messages: list[dict] = []
        self.on_tool = on_tool
        self.system_prompt = self._build_system_prompt()

    # ---------------------------------------------------------- prompting --

    @staticmethod
    def _read_personality_prompt() -> str:
        """Load the persona written by the personality layer, if present."""
        path = config.SYSTEM_PROMPT_PATH
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return ""
        return text

    def _build_system_prompt(self) -> str:
        persona = self._read_personality_prompt()
        if not persona:
            persona = FALLBACK_SYSTEM_PROMPT.format(user=config.USER_NAME)

        sections = [persona, _STYLE_RULES]

        now = datetime.now()
        context = [
            f"Current date and time: {now:%A, %B %d, %Y at %I:%M %p}.",
            "Platform: Windows 11 laptop. Paths use backslashes.",
        ]

        try:
            from tools import memory as memory_tool

            facts = memory_tool.facts_block()
        except Exception:  # noqa: BLE001 - memory must never block startup
            facts = ""
        if facts:
            context.append("What you already know about him:\n" + facts)

        try:
            from tools import schedule as schedule_tool

            today = schedule_tool.today_summary()
        except Exception:  # noqa: BLE001
            today = ""
        if today:
            context.append("On his schedule today: " + today)

        sections.append("\n\n".join(context))
        return "\n\n---\n\n".join(sections)

    def refresh_context(self) -> None:
        """Rebuild the system prompt (e.g. after new facts were remembered)."""
        self.system_prompt = self._build_system_prompt()

    # ------------------------------------------------------------ history --

    def reset(self) -> None:
        self.messages = []

    def _trim_history(self) -> None:
        """Keep the last N user turns, never splitting a tool_use/tool_result pair.

        A "turn boundary" is a user message that carries plain text rather than
        tool results, so cutting there always leaves the transcript valid.
        """
        boundaries = [
            index
            for index, message in enumerate(self.messages)
            if message["role"] == "user" and not _contains_tool_result(message)
        ]
        if len(boundaries) <= config.HISTORY_TURNS:
            return
        cut = boundaries[-config.HISTORY_TURNS]
        self.messages = self.messages[cut:]

    # -------------------------------------------------------------- calls --

    def _request_kwargs(self) -> dict:
        kwargs: dict = {
            "model": config.MODEL,
            "max_tokens": config.MAX_TOKENS,
            "system": [
                {
                    "type": "text",
                    "text": self.system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": self.messages,
            "tools": toolkit.TOOL_SCHEMAS,
            "thinking": config.thinking_param(),
        }
        if config.EFFORT:
            kwargs["output_config"] = {"effort": config.EFFORT}
        return kwargs

    def _create(self) -> "anthropic.types.Message":
        return self.client.messages.create(**self._request_kwargs())

    # ----------------------------------------------------------- streaming --

    def _stream_once(
        self,
        on_delta: Callable[[str], None] | None,
    ) -> "anthropic.types.Message":
        """Run one assistant turn as a stream, pushing text deltas to `on_delta`.

        Only `text_delta` events are forwarded. Thinking deltas and the
        `input_json_delta` events that build a tool_use block are deliberately
        ignored, so tool arguments are never spoken.
        """
        with self.client.messages.stream(**self._request_kwargs()) as stream:
            for event in stream:
                if getattr(event, "type", None) != "content_block_delta":
                    continue
                delta = getattr(event, "delta", None)
                if getattr(delta, "type", None) != "text_delta":
                    continue
                text = getattr(delta, "text", "") or ""
                if text and on_delta is not None:
                    on_delta(text)
            return stream.get_final_message()

    def send(
        self,
        user_text: str,
        *,
        on_sentence: Callable[[str], None] | None = None,
        on_first_token: Callable[[], None] | None = None,
        stream: bool | None = None,
    ) -> str:
        """Send one user message, run any tools Claude asks for, return the reply.

        `on_sentence` is called with each complete sentence the moment it is
        finished, long before the rest of the reply exists — that is the whole
        point of the streaming path. `on_first_token` fires once, on the first
        text token of the turn.

        Set `stream=False` (or TBS_STREAM=off) to fall back to waiting for
        the entire response, which is what TBS used to do.
        """
        user_text = (user_text or "").strip()
        if not user_text:
            return ""

        use_stream = config.STREAMING if stream is None else stream

        self.messages.append({"role": "user", "content": user_text})
        self._trim_history()

        reply_parts: list[str] = []
        used_memory_tool = False

        buffer = SentenceBuffer()
        seen_token = False

        def handle_delta(text: str) -> None:
            nonlocal seen_token
            if not seen_token:
                seen_token = True
                if on_first_token is not None:
                    on_first_token()
            if on_sentence is None:
                return
            for sentence in buffer.feed(text):
                on_sentence(sentence)

        def drain() -> None:
            """Speak the unterminated tail at the end of an assistant turn."""
            if on_sentence is None:
                return
            tail = buffer.flush()
            if tail:
                on_sentence(tail)

        for _ in range(config.MAX_TOOL_ITERATIONS):
            try:
                if use_stream:
                    response = self._stream_once(handle_delta)
                else:
                    response = self._create()
            except anthropic.APIStatusError as exc:
                return self._api_error_text(exc)
            except anthropic.APIConnectionError:
                return "I cannot reach the Anthropic API, Sir. Check the network connection."

            if response.stop_reason == "refusal":
                return "I have to decline that one, Sir."

            self.messages.append({"role": "assistant", "content": response.content})

            text_now = "".join(
                block.text for block in response.content if block.type == "text"
            ).strip()
            if text_now:
                reply_parts.append(text_now)

            if use_stream:
                # A preamble like "One moment, Sir." often ends without a full
                # stop; speak it now rather than holding it behind a tool call.
                drain()
            elif on_sentence is not None and text_now:
                for sentence in split_sentences(text_now):
                    on_sentence(sentence)

            tool_uses = [block for block in response.content if block.type == "tool_use"]
            if not tool_uses:
                break

            results = []
            for block in tool_uses:
                tool_input = dict(block.input or {})
                if self.on_tool:
                    try:
                        self.on_tool(block.name, tool_input)
                    except Exception:  # noqa: BLE001 - UI callback must not break the loop
                        pass
                output, is_error = toolkit.dispatch(block.name, tool_input)
                if block.name in ("remember", "forget"):
                    used_memory_tool = True
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                        "is_error": is_error,
                    }
                )
            self.messages.append({"role": "user", "content": results})
        else:
            giving_up = "I stopped there, Sir — that was taking more steps than it should."
            reply_parts.append(giving_up)
            if on_sentence is not None:
                on_sentence(giving_up)

        if used_memory_tool:
            self.refresh_context()

        reply = " ".join(part for part in reply_parts if part).strip()
        return reply or "Done, Sir."

    # ------------------------------------------------------------- errors --

    @staticmethod
    def _api_error_text(exc: "anthropic.APIStatusError") -> str:
        status = getattr(exc, "status_code", None)
        if status == 401:
            return "My API key was rejected, Sir. Check ANTHROPIC_API_KEY."
        if status == 429:
            return "I am rate limited at the moment, Sir. Give it a minute."
        if status and status >= 500:
            return "The Anthropic API is having trouble, Sir. Worth retrying shortly."
        return f"The API refused that request, Sir: {getattr(exc, 'message', exc)}"


def _contains_tool_result(message: dict) -> bool:
    content = message.get("content")
    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )
