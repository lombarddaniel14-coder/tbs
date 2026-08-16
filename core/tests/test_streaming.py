"""Tests for the sentence splitter, the playback queue, and the streaming brain.

No API key, no speakers, no network:

    py -3.11 core\\tests\\test_streaming.py

The brain test drives `Brain._stream_once` with a stubbed Anthropic client that
emits realistic token-by-token deltas whose chunk boundaries land mid-word and
mid-number, which is where a naive splitter breaks.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from streaming import (  # noqa: E402
    SentenceBuffer,
    SpeechQueue,
    TurnTimings,
    split_sentences,
)

PASS = "  ok   "
FAIL = "  FAIL "
_failures = 0


def check(name: str, got, want) -> None:
    global _failures
    if got == want:
        print(f"{PASS} {name}")
    else:
        _failures += 1
        print(f"{FAIL} {name}\n         got  {got!r}\n         want {want!r}")


# ---------------------------------------------------------------------------
# 1. Sentence splitting - the tricky cases
# ---------------------------------------------------------------------------

SPLIT_CASES = [
    (
        "plain sentences",
        "Battery is at thirty four percent. The machine is otherwise fine.",
        ["Battery is at thirty four percent.", "The machine is otherwise fine."],
    ),
    (
        "abbreviation: Dr.",
        "Dr. Kelso called. He wants the report.",
        ["Dr. Kelso called.", "He wants the report."],
    ),
    (
        "abbreviation: Mr. and Mrs.",
        "Mr. and Mrs. Stark are downstairs waiting.",
        ["Mr. and Mrs. Stark are downstairs waiting."],
    ),
    (
        "abbreviation: e.g.",
        "Bring something warm, e.g. a coat. It is freezing.",
        ["Bring something warm, e.g. a coat.", "It is freezing."],
    ),
    (
        "abbreviation: i.e.",
        "The obvious one, i.e. the red button, is disabled.",
        ["The obvious one, i.e. the red button, is disabled."],
    ),
    (
        "acronym: U.S.",
        "He flew to the U.S. yesterday. Landed at noon.",
        ["He flew to the U.S. yesterday.", "Landed at noon."],
    ),
    (
        "decimal number",
        "Your temperature is 98.6 degrees. Perfectly normal.",
        ["Your temperature is 98.6 degrees.", "Perfectly normal."],
    ),
    (
        "decimal at end of clause",
        "Disk usage sits at 72.4 percent.",
        ["Disk usage sits at 72.4 percent."],
    ),
    (
        "ellipsis",
        "I would not advise that... but it is your call. Proceed?",
        ["I would not advise that... but it is your call.", "Proceed?"],
    ),
    (
        "time with p.m.",
        "Your class is at 3:30 p.m. in Smith 210.",
        ["Your class is at 3:30 p.m. in Smith 210."],
    ),
    (
        "time with p.m. then a new sentence",
        "It starts at 3:30 p.m. Do not be late.",
        ["It starts at 3:30 p.m. Do not be late."],
    ),
    (
        "initials",
        "J. R. R. Tolkien wrote it. In 1937.",
        ["J. R. R. Tolkien wrote it.", "In 1937."],
    ),
    (
        "question and exclamation",
        "Shall I open Spotify? Of course, Sir! Right away.",
        ["Shall I open Spotify?", "Of course, Sir!", "Right away."],
    ),
    (
        "quoted terminator",
        'He said "stand down." Then he left.',
        ['He said "stand down."', "Then he left."],
    ),
    (
        "version numbers stay put",
        "You are on version 3.11.9 of Python. That is current.",
        ["You are on version 3.11.9 of Python.", "That is current."],
    ),
    (
        "no trailing punctuation",
        "Working on it",
        ["Working on it"],
    ),
    (
        "empty",
        "",
        [],
    ),
]


def test_splitter_whole_strings() -> None:
    print("\n[1] sentence splitter - whole strings")
    for name, text, want in SPLIT_CASES:
        check(name, split_sentences(text), want)


def _stream_chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_splitter_streamed() -> None:
    """The same cases, fed a few characters at a time.

    Chunk sizes of 1, 3 and 7 guarantee boundaries land inside "98.6", "Dr.",
    and "p.m." - the exact places a per-chunk splitter fires early.
    """
    print("\n[2] sentence splitter - fed in chunks (never splits early)")
    for size in (1, 3, 7):
        bad = []
        for name, text, want in SPLIT_CASES:
            buf = SentenceBuffer()
            got: list[str] = []
            for chunk in _stream_chunks(text, size):
                got.extend(buf.feed(chunk))
            tail = buf.flush()
            if tail:
                got.append(tail)
            if got != want:
                bad.append((name, got, want))
        if bad:
            global _failures
            _failures += 1
            print(f"{FAIL} chunk size {size}")
            for name, got, want in bad:
                print(f"         {name}: got {got!r} want {want!r}")
        else:
            print(f"{PASS} chunk size {size}: all {len(SPLIT_CASES)} cases identical")


# ---------------------------------------------------------------------------
# 2. Playback queue
# ---------------------------------------------------------------------------

def test_speech_queue_simple() -> None:
    print("\n[3] SpeechQueue - simple (blocking speak_fn)")
    said: list[str] = []
    first = []

    def speak(text: str) -> None:
        time.sleep(0.02)
        said.append(text)

    q = SpeechQueue(speak_fn=speak, on_first_audio=lambda: first.append(True))
    t0 = time.perf_counter()
    for s in ["One.", "Two.", "Three."]:
        q.put(s)
    put_cost = time.perf_counter() - t0
    q.close()
    check("spoken in order", said, ["One.", "Two.", "Three."])
    check("first-audio callback fired", first, [True])
    check("put() did not block on audio", put_cost < 0.02, True)


def test_speech_queue_pipelined() -> None:
    print("\n[4] SpeechQueue - pipelined (synth thread + play thread)")
    order: list[str] = []
    stopped: list[bool] = []

    def synth(text: str):
        time.sleep(0.03)
        order.append(f"synth:{text}")
        return text

    def play(item) -> None:
        time.sleep(0.05)
        order.append(f"play:{item}")

    q = SpeechQueue(synth_fn=synth, play_fn=play, stop_fn=lambda: stopped.append(True))
    t0 = time.perf_counter()
    for s in ["One.", "Two.", "Three."]:
        q.put(s)
    q.close()
    total = time.perf_counter() - t0

    played = [o.split(":", 1)[1] for o in order if o.startswith("play:")]
    check("played in order", played, ["One.", "Two.", "Three."])
    # Serial would be 3*(0.03+0.05)=0.24s; pipelined is ~0.03+3*0.05=0.18s.
    check("synthesis overlapped playback", total < 0.22, True)
    check("sentence 2 synthesised before sentence 1 finished playing",
          order.index("synth:Two.") < order.index("play:One."), True)


def test_speech_queue_abort() -> None:
    print("\n[5] SpeechQueue - barge-in")
    played: list[str] = []
    stopped: list[bool] = []

    def play(item) -> None:
        for _ in range(20):
            if stopped:
                return
            time.sleep(0.01)
        played.append(item)

    q = SpeechQueue(
        synth_fn=lambda t: t,
        play_fn=play,
        stop_fn=lambda: stopped.append(True),
    )
    for s in ["One.", "Two.", "Three.", "Four."]:
        q.put(s)
    time.sleep(0.05)
    q.abort()
    q.close(timeout=2.0)
    check("stop_fn called", bool(stopped), True)
    check("queue flushed (nothing finished playing)", played, [])
    check("aborted flag set", q.aborted, True)


def test_speech_queue_degrades() -> None:
    print("\n[6] SpeechQueue - backend failure is reported, not raised")
    def boom(_text: str) -> None:
        raise RuntimeError("no audio device")

    q = SpeechQueue(speak_fn=boom)
    q.put("One.")
    q.put("Two.")
    q.close()
    check("error captured", isinstance(q.error, RuntimeError), True)


# ---------------------------------------------------------------------------
# 3. The streaming brain, against a stubbed SDK
# ---------------------------------------------------------------------------

class _Delta:
    def __init__(self, type_: str, text: str = "", partial_json: str = "") -> None:
        self.type = type_
        self.text = text
        self.partial_json = partial_json


class _Event:
    def __init__(self, type_: str, **kw) -> None:
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Block:
    def __init__(self, **kw) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


class _Message:
    def __init__(self, content, stop_reason="end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason


class _StubStream:
    """Mimics `client.messages.stream(...)` closely enough for brain.py.

    Tokens are emitted a few characters at a time with deliberately awkward
    boundaries, and tool_use arrives as `input_json_delta` events that must
    never reach the speaker.
    """

    def __init__(self, turn: dict, delay: float = 0.0) -> None:
        self._turn = turn
        self._delay = delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        text = self._turn.get("text", "")
        if text:
            yield _Event("content_block_start", index=0)
            step = 4
            for i in range(0, len(text), step):
                if self._delay:
                    time.sleep(self._delay)
                yield _Event(
                    "content_block_delta", index=0, delta=_Delta("text_delta", text[i:i + step])
                )
            yield _Event("content_block_stop", index=0)
        for call in self._turn.get("tools", []):
            yield _Event("content_block_start", index=1)
            for piece in ('{"query":', '"battery"}'):
                yield _Event(
                    "content_block_delta",
                    index=1,
                    delta=_Delta("input_json_delta", partial_json=piece),
                )
            yield _Event("content_block_stop", index=1)

    def get_final_message(self):
        content = []
        if self._turn.get("text"):
            content.append(_Block(type="text", text=self._turn["text"]))
        for call in self._turn.get("tools", []):
            content.append(
                _Block(type="tool_use", id=call["id"], name=call["name"], input=call["input"])
            )
        return _Message(content, self._turn.get("stop_reason", "end_turn"))


class _StubMessages:
    def __init__(self, turns) -> None:
        self._turns = list(turns)
        self.calls = 0

    def stream(self, **_kwargs):
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return _StubStream(turn, delay=_kwargs.pop("_delay", 0.0))

    def create(self, **_kwargs):
        """The non-streaming path, for the TBS_STREAM=off fallback."""
        turn = self._turns[min(self.calls, len(self._turns) - 1)]
        self.calls += 1
        return _StubStream(turn).get_final_message()


class _StubClient:
    def __init__(self, turns) -> None:
        self.messages = _StubMessages(turns)


def _make_brain(turns):
    """Build a Brain without touching config, the network, or an API key."""
    import brain as brain_mod

    obj = object.__new__(brain_mod.Brain)
    obj.client = _StubClient(turns)
    obj.messages = []
    obj.on_tool = None
    obj.system_prompt = "test"
    return obj


def test_brain_streaming_plain() -> None:
    print("\n[7] Brain.send - streaming, plain reply")
    reply_text = (
        "Battery is at 98.6 percent, Sir. Dr. Banner called at 3:30 p.m. "
        "I would not read too much into it..."
    )
    brain = _make_brain([{"text": reply_text}])
    heard: list[str] = []
    tokens: list[int] = []

    reply = brain.send(
        "status",
        on_sentence=heard.append,
        on_first_token=lambda: tokens.append(1),
    )
    check("full reply preserved", reply, reply_text)
    check("first-token callback fired", tokens, [1])
    check(
        "sentences handed to TTS",
        heard,
        [
            "Battery is at 98.6 percent, Sir.",
            "Dr. Banner called at 3:30 p.m. I would not read too much into it...",
        ],
    )
    check("nothing spoken twice", " ".join(heard).count("98.6"), 1)


def test_brain_streaming_with_tools() -> None:
    print("\n[8] Brain.send - streaming with a tool call")
    import tools as toolkit

    original = toolkit.dispatch
    toolkit.dispatch = lambda name, args: ("Battery at 41 percent, discharging.", False)
    try:
        brain = _make_brain([
            {
                "text": "One moment, Sir.",
                "tools": [{"id": "tu_1", "name": "battery", "input": {}}],
                "stop_reason": "tool_use",
            },
            {"text": "Forty one percent and falling."},
        ])
        heard: list[str] = []
        seen_tools: list[str] = []
        brain.on_tool = lambda name, args: seen_tools.append(name)
        reply = brain.send("battery?", on_sentence=heard.append)
    finally:
        toolkit.dispatch = original

    check("tool ran", seen_tools, ["battery"])
    check("final text returned", reply, "One moment, Sir. Forty one percent and falling.")
    check("spoke text only, no tool JSON", heard,
          ["One moment, Sir.", "Forty one percent and falling."])
    check("no JSON leaked to TTS", any("{" in s for s in heard), False)


def test_latency_win() -> None:
    """Same stub, timed: blocking waits for the last token, streaming does not."""
    print("\n[9] measured latency - streamed vs. blocking")
    text = (
        "Battery is at forty one percent, Sir. "
        "The machine is otherwise healthy. "
        "Disk is at 72.4 percent and memory is comfortable."
    )
    delay = 0.004  # per 4-character token, ~0.13s for this reply

    brain = _make_brain([{"text": text}])
    brain.client.messages.stream = (
        lambda **kw: _StubStream({"text": text}, delay=delay)
    )

    timings = TurnTimings()
    first_sentence: list[float] = []

    def on_sentence(_s: str) -> None:
        if not first_sentence:
            first_sentence.append(time.perf_counter() - timings.t0)

    brain.send("status", on_sentence=on_sentence)
    total = time.perf_counter() - timings.t0

    saved = total - first_sentence[0]
    print(f"         first sentence ready at {first_sentence[0]*1000:.0f} ms, "
          f"full reply at {total*1000:.0f} ms")
    print(f"         -> speech can start {saved*1000:.0f} ms "
          f"({saved/total*100:.0f}%) earlier")
    check("first sentence arrives before the reply completes",
          first_sentence[0] < total * 0.6, True)


def test_blocking_fallback() -> None:
    """TBS_STREAM=off: no streaming call, but sentences still reach TTS."""
    print("\n[11] fallback - stream=False still speaks, via messages.create")
    text = "Battery is at 98.6 percent, Sir. Nothing else to report."
    brain = _make_brain([{"text": text}])
    heard: list[str] = []
    reply = brain.send("status", on_sentence=heard.append, stream=False)
    check("reply intact", reply, text)
    check("sentences delivered after the fact", heard,
          ["Battery is at 98.6 percent, Sir.", "Nothing else to report."])


def test_timings_report() -> None:
    print("\n[10] TurnTimings report")
    t = TurnTimings()
    for name, value in [
        ("wake", 0.0), ("transcript", 0.62), ("first_token", 0.94),
        ("first_audio", 1.11), ("done", 2.30),
    ]:
        t.marks[name] = value
    line = t.report()
    print(line)
    check("mentions every stage",
          all(k in line for k in ("wake", "transcript", "first token", "first audio")), True)


def main() -> int:
    test_splitter_whole_strings()
    test_splitter_streamed()
    test_speech_queue_simple()
    test_speech_queue_pipelined()
    test_speech_queue_abort()
    test_speech_queue_degrades()
    test_brain_streaming_plain()
    test_brain_streaming_with_tools()
    test_latency_win()
    test_blocking_fallback()
    test_timings_report()
    print("\n" + ("-" * 62))
    if _failures:
        print(f"{_failures} check(s) FAILED")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
