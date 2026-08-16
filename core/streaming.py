"""Sentence-boundary streaming: split a token stream into speakable sentences
and get them to the speakers without gaps.

Three pieces, all transport-agnostic and importable on their own:

    SentenceBuffer   feed(text_chunk) -> complete sentences, flush() -> remainder
    SpeechQueue      a background player so generation never waits on audio
    TurnTimings      wake -> transcript -> first token -> first audio -> done

Nothing here imports the Anthropic SDK or any audio library; `brain.py` feeds
the buffer and `main.py` wires the queue to whatever the personality layer
exposes. That keeps this file unit-testable with no API key and no speakers.
"""

from __future__ import annotations

import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

# ---------------------------------------------------------------------------
# Sentence splitting
# ---------------------------------------------------------------------------

TERMINATORS = ".!?…"          # . ! ? and the single-character ellipsis
_CLOSERS = "\"')]}”’"    # quotes/brackets that may trail a terminator

# Lowercased, trailing dot removed. Dotted forms are stored WITH their internal
# dots ("e.g", "u.s") because that is what the lookbehind sees.
_ABBREVIATIONS = {
    # titles
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "rev", "hon", "fr",
    "gen", "col", "sgt", "capt", "lt", "cmdr", "gov", "sen", "rep", "supt",
    "messrs", "mme", "mmes", "mssr",
    # latin and general
    "e.g", "i.e", "etc", "vs", "viz", "cf", "al", "ibid", "approx", "est",
    "dept", "univ", "inc", "ltd", "co", "corp", "bros", "assn", "dist",
    # units and references
    "no", "vol", "vols", "fig", "figs", "eq", "pp", "ed", "eds", "min", "max",
    "sec", "hr", "hrs", "yr", "yrs", "oz", "lb", "lbs", "km", "cm", "mm", "kg",
    "ft", "in", "mi", "mt", "rd", "ave", "blvd", "apt", "dept",
    # times, places, degrees
    "a.m", "p.m", "u.s", "u.s.a", "u.k", "u.n", "e.u", "d.c", "ph.d", "m.d",
    "b.a", "m.a", "b.s", "m.s", "b.sc", "m.sc", "jd", "esq",
    # calendar
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri",
    "sat", "sun",
}

# "no" is in the list for "No. 5"; a bare "no." at the end of a clause is rare
# enough that under-splitting there is the cheaper mistake.

_WORD_BEFORE = re.compile(r"([A-Za-z][A-Za-z.]*)$")
_HAS_SPEECH = re.compile(r"[A-Za-z0-9]")

# The shortest fragment worth handing to TTS on its own. Below this we wait for
# more text rather than firing the synthesiser at "Yes."
MIN_SENTENCE_CHARS = 2


def _is_abbreviation(text: str, dot: int) -> bool:
    """Is the period at `dot` part of an abbreviation rather than a full stop?"""
    match = _WORD_BEFORE.search(text[:dot])
    if not match:
        return False
    token = match.group(1)
    bare = token.replace(".", "")
    # Initials: "J." in "J. Robert Oppenheimer", and the U/S of "U.S."
    if len(bare) == 1 and bare.isupper():
        return True
    return token.lower() in _ABBREVIATIONS


def _is_decimal_point(text: str, dot: int) -> bool:
    """98.6 - digit, dot, digit."""
    return (
        dot > 0
        and text[dot - 1].isdigit()
        and dot + 1 < len(text)
        and text[dot + 1].isdigit()
    )


def _in_dot_run(text: str, dot: int) -> bool:
    """Part of an ellipsis ("..."), which is a pause, not an end."""
    if text[dot] != ".":
        return False
    before = dot > 0 and text[dot - 1] == "."
    after = dot + 1 < len(text) and text[dot + 1] == "."
    return before or after


class SentenceBuffer:
    """Accumulates streamed text and yields sentences the moment they complete.

    A boundary is only reported once the character *after* the terminator has
    arrived, so a chunk that ends mid-number ("98.") or mid-abbreviation
    ("Dr.") never fires early. Anything still buffered when the stream ends
    comes out of `flush()`.
    """

    def __init__(self, min_chars: int = MIN_SENTENCE_CHARS) -> None:
        self._buf = ""
        self._start = 0          # start of the sentence under construction
        self._scan = 0           # next index to examine
        self.min_chars = min_chars

    # -- feeding ------------------------------------------------------------

    def feed(self, chunk: str) -> list[str]:
        """Add streamed text; return every sentence that is now complete."""
        if not chunk:
            return []
        self._buf += chunk
        out: list[str] = []

        i = max(self._scan, self._start)
        n = len(self._buf)
        while i < n:
            ch = self._buf[i]
            if ch not in TERMINATORS:
                i += 1
                continue

            if _in_dot_run(self._buf, i) or _is_decimal_point(self._buf, i):
                i += 1
                continue
            if ch == "." and _is_abbreviation(self._buf, i):
                i += 1
                continue

            # Swallow any run of terminators and closing quotes/brackets.
            end = i
            while end + 1 < n and self._buf[end + 1] in TERMINATORS + _CLOSERS:
                end += 1

            if end + 1 >= n:
                # The next character has not streamed in yet - do not guess.
                break
            if not self._buf[end + 1].isspace():
                # "3.5" style run-on, or a terminator glued to more text.
                i = end + 1
                continue

            candidate = self._buf[self._start:end + 1].strip()
            if len(candidate) < self.min_chars or not _HAS_SPEECH.search(candidate):
                i = end + 1
                continue

            out.append(candidate)
            self._start = end + 1
            i = self._start

        self._scan = max(self._start, min(i, len(self._buf)))
        # Reclaim memory on long replies.
        if self._start > 0 and self._start == len(self._buf):
            self._buf = ""
            self._start = self._scan = 0
        return out

    # -- draining -----------------------------------------------------------

    def flush(self) -> str:
        """Return whatever is left (an unterminated final clause) and reset."""
        remainder = self._buf[self._start:].strip()
        self._buf = ""
        self._start = self._scan = 0
        return remainder if _HAS_SPEECH.search(remainder or "") else ""

    @property
    def pending(self) -> str:
        return self._buf[self._start:]


def split_sentences(text: str) -> list[str]:
    """Split a complete string. Convenience wrapper over SentenceBuffer."""
    buf = SentenceBuffer()
    out = buf.feed(text)
    tail = buf.flush()
    if tail:
        out.append(tail)
    return out


def iter_sentences(chunks: Iterable[str]) -> Iterable[str]:
    """Stream sentences out of an iterable of text chunks."""
    buf = SentenceBuffer()
    for chunk in chunks:
        yield from buf.feed(chunk)
    tail = buf.flush()
    if tail:
        yield tail


# ---------------------------------------------------------------------------
# Playback queue
# ---------------------------------------------------------------------------

_SENTINEL = object()


class SpeechQueue:
    """Speaks sentences in order on background threads.

    Two modes:

    * **pipelined** - given `synth_fn(text) -> path` and `play_fn(path)`, one
      thread synthesises while another plays, so sentence N+1 is already
      rendered when sentence N stops. This is what removes the gap.
    * **simple** - given only a blocking `speak_fn(text)`, a single thread
      speaks each sentence in turn. Generation still never blocks on audio;
      there is just a short synthesis gap between sentences.

    Either way `put()` returns immediately. `abort()` drops everything queued
    and asks the backend to stop the sentence in progress (barge-in).
    """

    def __init__(
        self,
        speak_fn: Optional[Callable[[str], None]] = None,
        *,
        synth_fn: Optional[Callable[[str], object]] = None,
        play_fn: Optional[Callable[[object], None]] = None,
        stop_fn: Optional[Callable[[], None]] = None,
        cleanup_fn: Optional[Callable[[object], None]] = None,
        on_first_audio: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        if speak_fn is None and not (synth_fn and play_fn):
            raise ValueError("SpeechQueue needs speak_fn, or synth_fn + play_fn")

        self.pipelined = bool(synth_fn and play_fn)
        self._speak_fn = speak_fn
        self._synth_fn = synth_fn
        self._play_fn = play_fn
        self._stop_fn = stop_fn
        self._cleanup_fn = cleanup_fn
        self._on_first_audio = on_first_audio
        self._on_error = on_error

        self._stop = threading.Event()
        self._first_audio_done = False
        self._failed: BaseException | None = None
        self.spoken: list[str] = []

        self._in: "queue.Queue[object]" = queue.Queue()
        self._threads: list[threading.Thread] = []

        if self.pipelined:
            self._mid: "queue.Queue[object]" = queue.Queue()
            self._threads.append(
                threading.Thread(target=self._synth_worker, name="tts-synth", daemon=True)
            )
            self._threads.append(
                threading.Thread(target=self._play_worker, name="tts-play", daemon=True)
            )
        else:
            self._threads.append(
                threading.Thread(target=self._speak_worker, name="tts-speak", daemon=True)
            )
        for thread in self._threads:
            thread.start()

    # -- producer side ------------------------------------------------------

    def put(self, sentence: str) -> None:
        """Hand a finished sentence to the speakers. Never blocks."""
        if self._stop.is_set() or self._failed is not None:
            return
        sentence = (sentence or "").strip()
        if sentence:
            self._in.put(sentence)

    def close(self, timeout: float | None = 60.0) -> None:
        """Finish everything queued, then stop the threads."""
        self._in.put(_SENTINEL)
        for thread in self._threads:
            thread.join(timeout=timeout)

    def abort(self) -> None:
        """Barge-in: drop the queue and cut the sentence in progress."""
        self._stop.set()
        for q in (self._in, getattr(self, "_mid", None)):
            if q is None:
                continue
            while True:
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    break
                if item is not _SENTINEL and self.pipelined and isinstance(item, tuple):
                    self._safe_cleanup(item[1])
        if self._stop_fn:
            try:
                self._stop_fn()
            except Exception:  # noqa: BLE001 - stopping must not raise
                pass
        self._in.put(_SENTINEL)
        if self.pipelined:
            self._mid.put(_SENTINEL)

    @property
    def aborted(self) -> bool:
        return self._stop.is_set()

    @property
    def error(self) -> BaseException | None:
        """Set if the backend blew up; callers fall back to plain speak()."""
        return self._failed

    # -- workers ------------------------------------------------------------

    def _note_first_audio(self) -> None:
        if not self._first_audio_done:
            self._first_audio_done = True
            if self._on_first_audio:
                try:
                    self._on_first_audio()
                except Exception:  # noqa: BLE001
                    pass

    def _fail(self, exc: BaseException) -> None:
        if self._failed is None:
            self._failed = exc
            if self._on_error:
                try:
                    self._on_error(exc)
                except Exception:  # noqa: BLE001
                    pass

    def _safe_cleanup(self, item: object) -> None:
        if self._cleanup_fn is None:
            return
        try:
            self._cleanup_fn(item)
        except Exception:  # noqa: BLE001
            pass

    def _speak_worker(self) -> None:
        while True:
            item = self._in.get()
            if item is _SENTINEL:
                return
            if self._stop.is_set() or self._failed is not None:
                continue
            try:
                self._note_first_audio()
                self._speak_fn(item)  # type: ignore[misc]
                self.spoken.append(item)
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

    def _synth_worker(self) -> None:
        while True:
            item = self._in.get()
            if item is _SENTINEL:
                self._mid.put(_SENTINEL)
                return
            if self._stop.is_set() or self._failed is not None:
                continue
            try:
                rendered = self._synth_fn(item)  # type: ignore[misc]
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
                continue
            if rendered is None:
                continue
            self._mid.put((item, rendered))

    def _play_worker(self) -> None:
        while True:
            entry = self._mid.get()
            if entry is _SENTINEL:
                return
            text, rendered = entry
            if self._stop.is_set():
                self._safe_cleanup(rendered)
                continue
            try:
                self._note_first_audio()
                self._play_fn(rendered)  # type: ignore[misc]
                # `spoken` means heard, not merely synthesised - the barge-in
                # and fallback logic both depend on that distinction. A
                # sentence cut short by abort() does not count.
                if not self._stop.is_set():
                    self.spoken.append(text)
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)
            finally:
                self._safe_cleanup(rendered)


# ---------------------------------------------------------------------------
# Latency accounting
# ---------------------------------------------------------------------------

@dataclass
class TurnTimings:
    """Stopwatch for one turn, printed so the streaming win is measurable."""

    label: str = "turn"
    t0: float = field(default_factory=time.perf_counter)
    marks: dict = field(default_factory=dict)

    def mark(self, name: str) -> float:
        """Record `name` at now (first write wins) and return seconds since t0."""
        elapsed = time.perf_counter() - self.t0
        self.marks.setdefault(name, elapsed)
        return self.marks[name]

    def get(self, name: str) -> Optional[float]:
        return self.marks.get(name)

    def report(self) -> str:
        order = [
            ("wake", "wake"),
            ("transcript", "transcript"),
            ("first_token", "first token"),
            ("first_audio", "first audio"),
            ("done", "done"),
        ]
        parts = []
        previous = 0.0
        for key, label in order:
            value = self.marks.get(key)
            if value is None:
                continue
            parts.append(f"{label} +{value - previous:.2f}s")
            previous = value
        total = self.marks.get("done", time.perf_counter() - self.t0)
        line = "  [" + " | ".join(parts) + f" | total {total:.2f}s]"

        # Dead air is the wait between the transcript being ready and the first
        # sound coming out. Everything after that is TBS talking.
        first_audio = self.marks.get("first_audio")
        transcript = self.marks.get("transcript")
        if first_audio is not None and transcript is not None:
            line += f"\n  [dead air {first_audio - transcript:.2f}s"
            generation = self.marks.get("generation_done")
            if generation is not None and generation > first_audio:
                line += (
                    f" - speech started {generation - first_audio:.2f}s before "
                    "the reply had finished generating"
                )
            line += "]"
        return line
