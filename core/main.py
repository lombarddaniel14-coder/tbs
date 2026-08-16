"""TBS entry point.

Runs the loop: listen -> understand -> route to a tool or plain conversation
-> respond.

    py -3.11 main.py            # voice if available, keyboard if not
    py -3.11 main.py --text     # keyboard only (no microphone needed)
    py -3.11 main.py --once "battery?"
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

import config  # noqa: E402
from brain import Brain  # noqa: E402
from streaming import SpeechQueue, TurnTimings  # noqa: E402

EXIT_WORDS = {"exit", "quit", "goodbye", "shut down", "shutdown", "stand down", "bye"}
RESET_WORDS = {"reset", "new conversation", "start over", "forget this conversation"}


# ---------------------------------------------------------------------------
# Voice bridge — owned by the personality layer, loaded defensively.
# ---------------------------------------------------------------------------

class VoiceBridge:
    """Thin adapter over ..\\personality\\voice.py.

    That module belongs to the personality agent, so this only *probes* it for
    a speak-like and a listen-like callable. If the module is missing, broken,
    or shaped differently, TBS falls back to the keyboard instead of dying.
    """

    SPEAK_NAMES = ("speak", "say", "talk", "output", "speak_text")
    LISTEN_NAMES = ("listen", "listen_once", "hear", "transcribe", "record", "get_input")
    CLASS_NAMES = ("TBSVoice", "Voice", "VoiceEngine", "TBS", "Speech", "VoiceIO")

    # Optional extras. Present -> sentence N+1 is synthesised while N plays and
    # a wake word can cut TBS off. Absent -> plain speak(), same as before.
    SYNTH_NAMES = ("synth_to_file", "synthesize_to_file", "render_to_file")
    PLAY_NAMES = ("play_file", "play_wav", "play")
    STOP_NAMES = ("stop_speaking", "stop_playback", "stop", "shut_up")
    RESET_NAMES = ("reset_speaking", "reset_playback")
    WATCH_NAMES = ("start_barge_in_watch",)

    def __init__(self) -> None:
        self.module = None
        self.error: str | None = None
        self._init_error: str | None = None
        self._speak = None
        self._listen = None
        self._synth = None
        self._play = None
        self._stop = None
        self._reset = None
        self._watch_factory = None
        self._load()

    def _load(self) -> None:
        path = config.PERSONALITY_DIR / "voice.py"
        if not path.is_file():
            self.error = f"no voice module at {path}"
            return
        try:
            spec = importlib.util.spec_from_file_location("tbs_personality_voice", path)
            if spec is None or spec.loader is None:
                self.error = "voice.py could not be loaded"
                return
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - a broken voice layer must not stop TBS
            self.error = f"{type(exc).__name__}: {exc}"
            return

        self.module = module
        self._speak = self._find(module, self.SPEAK_NAMES)
        self._listen = self._find(module, self.LISTEN_NAMES)
        self._bind_streaming_hooks(module)

        if self._speak is None or self._listen is None:
            instance = self._instantiate(module)
            if instance is not None:
                self._speak = self._speak or self._find(instance, self.SPEAK_NAMES)
                self._listen = self._listen or self._find(instance, self.LISTEN_NAMES)
                self._bind_streaming_hooks(instance)

        if self._speak is None and self._listen is None:
            self.error = self._init_error or "voice.py exposes no speak/listen functions"
        elif self._speak is not None and self._listen is not None:
            self.error = None  # a later candidate worked; drop earlier attempts

    @staticmethod
    def _find(obj, names):
        for name in names:
            candidate = getattr(obj, name, None)
            if callable(candidate):
                return candidate
        return None

    def _instantiate(self, module):
        """Build a voice object from the module: known names first, then any
        class in it that exposes a speak-like and a listen-like method."""
        candidates = [
            cls
            for cls in (getattr(module, name, None) for name in self.CLASS_NAMES)
            if isinstance(cls, type)
        ]
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and attr not in candidates
                and self._find(attr, self.SPEAK_NAMES)
                and self._find(attr, self.LISTEN_NAMES)
            ):
                candidates.append(attr)

        for cls in candidates:
            try:
                return cls()
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                self._init_error = f"{cls.__name__}() could not start — {exc}"
                continue
        return None

    def _bind_streaming_hooks(self, obj) -> None:
        """Pick up the optional queued-playback and barge-in callables."""
        self._synth = self._synth or self._find(obj, self.SYNTH_NAMES)
        self._play = self._play or self._find(obj, self.PLAY_NAMES)
        self._stop = self._stop or self._find(obj, self.STOP_NAMES)
        self._reset = self._reset or self._find(obj, self.RESET_NAMES)
        self._watch_factory = self._watch_factory or self._find(obj, self.WATCH_NAMES)

    @property
    def can_speak(self) -> bool:
        return self._speak is not None

    @property
    def can_listen(self) -> bool:
        return self._listen is not None

    def speak(self, text: str) -> None:
        if not text or self._speak is None:
            return
        try:
            self._speak(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[voice] speak failed: {exc}")

    # -- streaming speech ---------------------------------------------------

    @property
    def pipelined(self) -> bool:
        """True when the backend can render one sentence while another plays."""
        if self._synth is None or self._play is None:
            return False
        probe = getattr(self.module, "supports_queued_playback", None)
        if callable(probe):
            try:
                return bool(probe())
            except Exception:  # noqa: BLE001
                return False
        return True

    def begin_stream(self, on_first_audio=None) -> SpeechQueue | None:
        """Open a playback queue for one turn, or None if speech is unavailable.

        Falls back through three levels, quietly:
          1. pipelined  - synth thread + play thread, no gap between sentences
          2. simple     - one thread calling the backend's blocking speak()
          3. None       - caller speaks the whole reply at the end, as before
        """
        if not self.can_speak:
            return None
        if self._reset is not None:
            try:
                self._reset()
            except Exception:  # noqa: BLE001
                pass

        def on_error(exc: BaseException) -> None:
            print(f"[voice] speech backend failed mid-reply: {exc}")

        try:
            if self.pipelined:
                return SpeechQueue(
                    speak_fn=self.speak,
                    synth_fn=self._synth,
                    play_fn=self._play,
                    stop_fn=self._stop,
                    cleanup_fn=_delete_quietly,
                    on_first_audio=on_first_audio,
                    on_error=on_error,
                )
            return SpeechQueue(
                speak_fn=self.speak,
                stop_fn=self._stop,
                on_first_audio=on_first_audio,
                on_error=on_error,
            )
        except Exception as exc:  # noqa: BLE001 - never lose the turn over TTS
            print(f"[voice] queued playback unavailable ({exc}); "
                  "falling back to speaking the whole reply")
            return None

    def start_barge_in(self, on_wake) -> object | None:
        """Wake-word interrupt while TBS is talking. None if unsupported."""
        if self._watch_factory is None:
            return None
        try:
            return self._watch_factory(on_wake)
        except Exception:  # noqa: BLE001 - no mic / no key / not supported
            return None

    def listen(self) -> str | None:
        if self._listen is None:
            return None
        try:
            heard = self._listen()
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[voice] listen failed: {exc}")
            return None
        return "" if heard is None else str(heard).strip()


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

def _delete_quietly(path) -> None:
    """Remove a temp WAV once it has been played (or dropped by a barge-in)."""
    try:
        Path(str(path)).unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _announce_tool(name: str, tool_input: dict) -> None:
    detail = ", ".join(f"{k}={v}" for k, v in list(tool_input.items())[:3])
    print(f"  [tool] {name}({detail})")


def _normalise(text: str) -> str:
    """Lowercase and strip padding, BOMs, and trailing punctuation.

    Transcribed speech and piped stdin both arrive with stray characters; this
    keeps 'Exit.' and '\\ufeffexit' matching the exit words.
    """
    cleaned = text.replace("﻿", "").replace("​", "").strip()
    return cleaned.strip(".!?,;:").strip().lower()


def _take_turn(brain: Brain, voice, speaking: bool, user_text: str,
               timings: TurnTimings) -> str:
    """One user message -> spoken reply, streamed sentence by sentence.

    Sentences go to the speakers the moment they are complete, so TBS starts
    talking while Claude is still writing. Ctrl+C or a wake word mid-reply
    aborts playback immediately.
    """
    queue = voice.begin_stream(on_first_audio=lambda: timings.mark("first_audio")) \
        if speaking else None
    watch = voice.start_barge_in(queue.abort) if (queue is not None) else None

    try:
        reply = brain.send(
            user_text,
            on_sentence=(queue.put if queue is not None else None),
            on_first_token=lambda: timings.mark("first_token"),
        )
        print(f"TBS: {reply}\n")
        if queue is not None:
            queue.close()
            # The backend died before saying anything: say it the old way.
            if queue.error is not None and not queue.spoken and not queue.aborted:
                voice.speak(reply)
        elif speaking:
            voice.speak(reply)
    except KeyboardInterrupt:
        if queue is not None:
            queue.abort()
            queue.close(timeout=2.0)
        print("\n[interrupted]\n")
        raise
    finally:
        if watch is not None:
            try:
                watch.stop()
            except Exception:  # noqa: BLE001
                pass

    if queue is not None and queue.aborted:
        print("[barge-in] stopped speaking.\n")
    timings.mark("done")
    return reply


def run(text_only: bool, once: str | None) -> int:
    try:
        brain = Brain(on_tool=_announce_tool)
    except config.ConfigError as exc:
        print(f"\n{exc}\n")
        return 1

    voice = VoiceBridge() if not text_only else None
    speaking = bool(voice and voice.can_speak)
    listening = bool(voice and voice.can_listen)

    if once is not None:
        timings = TurnTimings()
        timings.mark("wake")
        timings.mark("transcript")
        try:
            _take_turn(brain, voice, speaking, once, timings)
        except KeyboardInterrupt:
            return 0
        if config.SHOW_TIMINGS:
            print(timings.report())
        return 0

    print("=" * 64)
    print("  J A R V I S   —   online")
    print("=" * 64)
    print(f"  model   : {config.MODEL}")
    print(f"  tools   : {len(brain_tool_names())} available")
    if text_only:
        print("  mode    : keyboard (--text)")
    else:
        mode = []
        mode.append("mic" if listening else "keyboard")
        mode.append("speech out" if speaking else "text out")
        print(f"  mode    : {' + '.join(mode)}")
        if voice and voice.error:
            print(f"  note    : voice layer unavailable ({voice.error})")
    if config.STREAMING:
        how = "sentence-by-sentence"
        if speaking:
            how += " + gapless playback" if voice.pipelined else " (queued playback)"
        print(f"  stream  : {how}")
    else:
        print("  stream  : off (TBS_STREAM=off) — waits for the full reply")
    print("  say 'exit' to quit, 'reset' to clear the conversation")
    print("=" * 64 + "\n")

    while True:
        try:
            if listening:
                print("Listening...")
                timings = TurnTimings()
                timings.mark("wake")
                heard = voice.listen()
                timings.mark("transcript")
                if not heard:
                    continue
                user_text = heard
                print(f"YOU: {user_text}")
            else:
                user_text = input("YOU: ").strip()
                timings = TurnTimings()
                timings.mark("wake")
                timings.mark("transcript")
        except (KeyboardInterrupt, EOFError):
            print("\nTBS: Standing down, Sir.")
            return 0

        if not user_text:
            continue

        command = _normalise(user_text)
        if command in EXIT_WORDS:
            farewell = "Standing down, Sir."
            print(f"TBS: {farewell}")
            if speaking:
                voice.speak(farewell)
            return 0
        if command in RESET_WORDS:
            brain.reset()
            brain.refresh_context()
            print("TBS: Conversation cleared, Sir.\n")
            continue

        try:
            _take_turn(brain, voice, speaking, user_text, timings)
        except KeyboardInterrupt:
            # Ctrl+C mid-reply is a barge-in, not a shutdown: keep the loop.
            continue
        if config.SHOW_TIMINGS:
            print(timings.report())


def brain_tool_names() -> tuple[str, ...]:
    import tools as toolkit

    return toolkit.TOOL_NAMES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tbs",
        description="Local TBS assistant (Claude-powered).",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="keyboard-only mode; never touch the microphone or speakers",
    )
    parser.add_argument(
        "--once",
        metavar="MESSAGE",
        help="send a single message, print the reply, and exit",
    )
    args = parser.parse_args(argv)
    return run(text_only=args.text, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
