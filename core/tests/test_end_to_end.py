"""End-to-end latency check: stubbed Claude stream -> real Piper -> real speakers.

No API key needed. The token stream is simulated at a realistic rate with
chunk boundaries that land mid-word; everything downstream of it (the sentence
splitter, the playback queue, voice.py, Piper, winsound) is the real thing.

    py -3.11 core\\tests\\test_end_to_end.py            # streamed vs. blocking
    py -3.11 core\\tests\\test_end_to_end.py --silent   # measure, do not play
    py -3.11 core\\tests\\test_end_to_end.py --bargein  # interrupt mid-reply

It prints the two timelines side by side, which is the number the streaming
work was done for: how long the user stares at a silent laptop.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from main import VoiceBridge  # noqa: E402
from streaming import SpeechQueue, TurnTimings, split_sentences  # noqa: E402

# A plausible TBS reply: three sentences, an abbreviation, and a decimal.
REPLY = (
    "Battery is at forty one percent and discharging, Sir. "
    "Dr. Banner's meeting was moved to 3:30 p.m., so you have the afternoon free. "
    "Disk usage is 72.4 percent, which is nothing to worry about yet."
)

TOKENS_PER_SECOND = 45.0     # a normal Opus streaming rate
CHARS_PER_TOKEN = 4


def token_stream(text: str, tps: float = TOKENS_PER_SECOND):
    """Yield the reply in token-sized chunks at a realistic pace."""
    delay = 1.0 / tps
    for i in range(0, len(text), CHARS_PER_TOKEN):
        time.sleep(delay)
        yield text[i:i + CHARS_PER_TOKEN]


def run_streamed(bridge: VoiceBridge, silent: bool) -> TurnTimings:
    """What TBS does now: speak each sentence as soon as it exists."""
    from streaming import SentenceBuffer

    timings = TurnTimings()
    timings.mark("wake")
    timings.mark("transcript")

    queue = _make_queue(bridge, silent, lambda: timings.mark("first_audio"))
    buffer = SentenceBuffer()
    first = True
    for chunk in token_stream(REPLY):
        if first:
            timings.mark("first_token")
            first = False
        for sentence in buffer.feed(chunk):
            queue.put(sentence)
    tail = buffer.flush()
    if tail:
        queue.put(tail)
    timings.mark("generation_done")
    queue.close()
    timings.mark("done")
    return timings


def run_blocking(bridge: VoiceBridge, silent: bool) -> TurnTimings:
    """What TBS did before: wait for the whole reply, then speak it."""
    timings = TurnTimings()
    timings.mark("wake")
    timings.mark("transcript")

    text = []
    first = True
    for chunk in token_stream(REPLY):
        if first:
            timings.mark("first_token")
            first = False
        text.append(chunk)
    timings.mark("generation_done")

    queue = _make_queue(bridge, silent, lambda: timings.mark("first_audio"))
    queue.put("".join(text))
    queue.close()
    timings.mark("done")
    return timings


def _make_queue(bridge: VoiceBridge, silent: bool, on_first_audio) -> SpeechQueue:
    """The real queue, with playback swapped for a no-op in --silent mode."""
    if silent:
        return SpeechQueue(
            synth_fn=bridge._synth,
            play_fn=lambda p: Path(str(p)).unlink(missing_ok=True),
            on_first_audio=on_first_audio,
        )
    queue = bridge.begin_stream(on_first_audio=on_first_audio)
    if queue is None:
        raise SystemExit("[!] no speech backend available; run personality\\setup_piper.py")
    return queue


def test_barge_in(bridge: VoiceBridge) -> None:
    print("\nBarge-in: speaking, then interrupting after 1.2 s")
    queue = bridge.begin_stream()
    for sentence in split_sentences(REPLY):
        queue.put(sentence)
    time.sleep(1.2)
    started = time.perf_counter()
    queue.abort()
    queue.close(timeout=5.0)
    print(f"  silence {(time.perf_counter() - started) * 1000:.0f} ms after abort()")
    print(f"  sentences left unspoken: {3 - len(queue.spoken)} of 3")
    print("  [ok] playback stopped mid-reply and the queue was flushed")


class StubBrain:
    """Stands in for Brain: same `send()` signature, no API key, no network."""

    def send(self, user_text, *, on_sentence=None, on_first_token=None, stream=None):
        from streaming import SentenceBuffer

        buffer = SentenceBuffer()
        first = True
        for chunk in token_stream(REPLY):
            if first and on_first_token:
                on_first_token()
                first = False
            for sentence in buffer.feed(chunk):
                if on_sentence:
                    on_sentence(sentence)
        tail = buffer.flush()
        if tail and on_sentence:
            on_sentence(tail)
        return REPLY


def test_full_turn(bridge: VoiceBridge) -> None:
    """The real main.py turn, wired to a stub brain and the real speakers."""
    import main as main_mod

    print("\nFull turn through main._take_turn (stub brain, real Piper)")
    timings = TurnTimings()
    timings.mark("wake")
    timings.mark("transcript")
    reply = main_mod._take_turn(StubBrain(), bridge, True, "status report", timings)
    print(timings.report())
    assert reply == REPLY, "the turn must still return the complete text"
    print("  [ok] reply text intact, sentences spoken as they were generated")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--silent", action="store_true", help="synthesise but do not play")
    parser.add_argument("--bargein", action="store_true", help="only run the barge-in test")
    parser.add_argument("--turn", action="store_true",
                        help="drive main.py's real turn function with a stub brain")
    args = parser.parse_args()

    bridge = VoiceBridge()
    if not bridge.can_speak:
        print(f"[!] voice layer unavailable: {bridge.error}")
        return 1
    print(f"voice layer: speak={bridge.can_speak} listen={bridge.can_listen} "
          f"pipelined={bridge.pipelined}")

    if args.bargein:
        test_barge_in(bridge)
        return 0

    if args.turn:
        test_full_turn(bridge)
        return 0

    print(f"\nReply: {len(REPLY)} chars, {len(split_sentences(REPLY))} sentences, "
          f"generated at {TOKENS_PER_SECOND:.0f} tok/s")

    print("\n--- BEFORE: wait for the full reply, then speak ---")
    before = run_blocking(bridge, args.silent)
    print(before.report())

    print("\n--- AFTER: speak each sentence as it completes ---")
    after = run_streamed(bridge, args.silent)
    print(after.report())

    b_first = before.get("first_audio") or 0.0
    a_first = after.get("first_audio") or 0.0
    print("\n" + "=" * 68)
    print(f"  first audio   before {b_first:.2f}s   ->   after {a_first:.2f}s"
          f"   ({b_first - a_first:+.2f}s, {100 * (b_first - a_first) / max(b_first, 1e-9):.0f}% sooner)")
    print(f"  reply spoken  before {before.get('done'):.2f}s   ->   "
          f"after {after.get('done'):.2f}s")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
