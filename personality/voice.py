"""
voice.py — the TBS voice pipeline.

    wake word ("buddy")  ->  record until silence  ->  transcribe
    ->  Claude (TBS system prompt)  ->  speak

Standalone and importable. Nothing here depends on ..\core\ — if the scaffold
wants to drive the pipeline itself, it can:

    from voice import TBSVoice, load_system_prompt

    jv = TBSVoice()           # warms models, picks a TTS backend
    jv.run()                     # blocking wake-word loop
    # ...or drive the stages individually:
    text  = jv.listen()          # record + transcribe one utterance
    reply = jv.think(text)       # Claude call, keeps conversation history
    jv.speak(reply)

Every dependency is optional at import time and checked at construction, so a
missing package produces one clear sentence telling you what to install rather
than a stack trace three frames deep.

    python voice.py --check      # verify environment, exit
    python voice.py              # run the assistant
    python voice.py --say "..."  # TTS smoke test
    python voice.py --text       # keyboard input, spoken output (no mic)

Design notes live in voice-stack.md; the persona lives in system-prompt.md.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

# --------------------------------------------------------------------------
# Configuration. Environment variables override every default.
# --------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent

# --- Models -----------------------------------------------------------------
BRAIN_MODEL = os.getenv("TBS_BRAIN_MODEL", "claude-opus-4-8")
AMBIENT_MODEL = os.getenv("TBS_AMBIENT_MODEL", "claude-haiku-4-5")
MAX_TOKENS = int(os.getenv("TBS_MAX_TOKENS", "1024"))
EFFORT = os.getenv("TBS_EFFORT", "medium")  # low | medium | high | xhigh | max

# --- Wake word --------------------------------------------------------------
WAKE_KEYWORD = "buddy"          # NOT a Porcupine built-in — needs a custom .ppn if Porcupine is enabled
WAKE_SENSITIVITY = float(os.getenv("TBS_WAKE_SENSITIVITY", "0.6"))

# --- Speech-to-text ---------------------------------------------------------
STT_MODEL = os.getenv("TBS_STT_MODEL", "small.en")   # base.en if the CPU is weak
STT_COMPUTE = os.getenv("TBS_STT_COMPUTE", "int8")
STT_THREADS = int(os.getenv("TBS_STT_THREADS", "4"))

# --- Recording / VAD --------------------------------------------------------
SAMPLE_RATE = 16000               # Porcupine and Whisper both want 16 kHz mono
SILENCE_MS = int(os.getenv("TBS_SILENCE_MS", "500"))   # hangover before we stop
MAX_UTTERANCE_S = float(os.getenv("TBS_MAX_UTTERANCE_S", "20"))
MIN_UTTERANCE_S = 0.35            # anything shorter is a cough, not a command
# RMS threshold on int16 samples. Raise in a noisy room, lower if it clips you off.
SILENCE_RMS = float(os.getenv("TBS_SILENCE_RMS", "500"))

# --- Text-to-speech ---------------------------------------------------------
# Piper is the default: free, offline, no key, and fast enough to keep up with
# a streamed reply. Install it with `py -3.11 setup_piper.py`.
# "auto" -> elevenlabs if a key is present, else piper, else sapi.
TTS_BACKEND = os.getenv("TBS_TTS", "piper")

ELEVEN_VOICE_ID = os.getenv("TBS_ELEVEN_VOICE", "onwK4e9ZLuTAKqWW03F9")  # "the user", British
ELEVEN_MODEL = os.getenv("TBS_ELEVEN_MODEL", "eleven_flash_v2_5")       # ~75 ms latency
ELEVEN_FORMAT = os.getenv("TBS_ELEVEN_FORMAT", "mp3_22050_32")          # small = fast

PIPER_VOICE = os.getenv(
    "TBS_PIPER_VOICE",
    str(HERE / "voices" / "en_GB-alan-medium.onnx"),
)

HISTORY_TURNS = int(os.getenv("TBS_HISTORY_TURNS", "12"))  # user+assistant messages kept


class TBSError(RuntimeError):
    """A problem the user can actually fix, phrased so they can fix it."""


# --------------------------------------------------------------------------
# System prompt loading
# --------------------------------------------------------------------------

_PROMPT_RE = re.compile(r"##\s*BEGIN SYSTEM PROMPT\s*```(?:\w+)?\n(.*?)```", re.S)

_FALLBACK_PROMPT = (
    "You are Buddy, a voice assistant. Dry British wit, address the user as 'sir', "
    "never break character, keep spoken replies to at most two sentences unless asked "
    "to elaborate, report status crisply, and push back plainly when the user is about "
    "to do something ill-advised."
)


def load_system_prompt(path: Optional[Path] = None) -> str:
    """Extract the fenced prompt block from system-prompt.md.

    Editing the markdown changes the running assistant — no code change needed.
    Falls back to a compressed inline version if the file is missing.
    """
    path = path or (HERE / "system-prompt.md")
    try:
        md = path.read_text(encoding="utf-8")
    except OSError:
        print(f"[tbs] warning: {path} not found; using the fallback persona.",
              file=sys.stderr)
        return _FALLBACK_PROMPT

    m = _PROMPT_RE.search(md)
    if not m:
        print(f"[tbs] warning: no '## BEGIN SYSTEM PROMPT' fenced block in {path.name}; "
              "using the fallback persona.", file=sys.stderr)
        return _FALLBACK_PROMPT
    return m.group(1).strip()


# --------------------------------------------------------------------------
# Dependency probing — every import is deferred and reported in plain English.
# --------------------------------------------------------------------------

def _need(module: str, pip_name: str, why: str):
    """Import `module` or raise a TBSError telling the user exactly what to run."""
    try:
        return __import__(module)
    except ImportError as exc:
        raise TBSError(
            f"{why} needs the '{module}' package, which isn't installed.\n"
            f"    Fix:  pip install {pip_name}\n"
            f"    (original error: {exc})"
        ) from exc


# --------------------------------------------------------------------------
# TTS backends
# --------------------------------------------------------------------------

class TTSBackend:
    """One spoken utterance at a time.

    `speak()` is the only method a backend must implement. A backend that can
    also render to a file *without* playing it sets `supports_queued_playback`
    and implements `synthesize_to_file` / `play_file`; the streaming layer then
    synthesises the next sentence while the current one is still playing, which
    is what removes the gap between sentences. Backends that cannot do that
    still stream sentence-by-sentence, just with a short synthesis pause.
    """

    name = "none"
    supports_queued_playback = False

    def speak(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    # -- optional, for gapless queued playback ------------------------------

    def synthesize_to_file(self, text: str) -> Optional[Path]:
        """Render `text` to a temp WAV and return its path (no audio output)."""
        return None

    def play_file(self, path: Path) -> None:
        """Play a file produced by `synthesize_to_file`."""
        raise NotImplementedError

    # -- barge-in -----------------------------------------------------------

    def stop(self) -> None:
        """Cut playback immediately. Safe to call when nothing is playing."""
        stop_playback()

    def reset(self) -> None:
        """Clear a previous stop so the next utterance can play."""
        reset_playback()


class ElevenLabsTTS(TTSBackend):
    """Streaming cloud TTS. Best quality, ~75 ms to first byte with Flash v2.5.

    Note: ElevenLabs *default* voices expire 2026-12-31. Clone the voice into your
    own workspace and set TBS_ELEVEN_VOICE to the copy's ID before then.
    """

    name = "elevenlabs"

    def __init__(self) -> None:
        key = os.getenv("ELEVENLABS_API_KEY")
        if not key:
            raise TBSError(
                "ELEVENLABS_API_KEY is not set.\n"
                '    Fix:  setx ELEVENLABS_API_KEY "your-key"   (then reopen PowerShell)\n'
                "    Or set TBS_TTS=piper to use the free offline voice instead."
            )
        _need("elevenlabs", "elevenlabs", "The ElevenLabs TTS backend")
        from elevenlabs.client import ElevenLabs

        self._client = ElevenLabs(api_key=key)
        self._play = _make_mp3_player()

    def speak(self, text: str) -> None:
        stream = self._client.text_to_speech.stream(
            voice_id=ELEVEN_VOICE_ID,
            model_id=ELEVEN_MODEL,
            text=text,
            output_format=ELEVEN_FORMAT,
            voice_settings={"stability": 0.45, "similarity_boost": 0.8, "speed": 1.05},
        )
        # Collect then play. Chunk-by-chunk playback needs a decoder in the loop;
        # for two-sentence replies the whole payload arrives in well under 200 ms.
        audio = b"".join(chunk for chunk in stream if chunk)
        self._play(audio)


class PiperTTS(TTSBackend):
    """Free, offline, ~10x realtime on a desktop CPU. Clearly synthetic but instant.

    Tries the Python API first, then falls back to the `piper` CLI, because the
    Python surface has changed across 1.2 -> 1.4 and the CLI has not.
    """

    name = "piper"
    supports_queued_playback = True

    def __init__(self) -> None:
        self._model_path = Path(PIPER_VOICE)
        if not self._model_path.exists():
            raise TBSError(
                f"Piper voice model not found at {self._model_path}\n"
                "    Fix:  py -3.11 setup_piper.py\n"
                "    (downloads en_GB-alan-medium.onnx and .onnx.json from\n"
                "     https://huggingface.co/rhasspy/piper-voices, verifies both,\n"
                "     and speaks a test phrase)"
            )
        if not self._model_path.with_suffix(".onnx.json").exists():
            raise TBSError(
                f"Piper config file missing: {self._model_path.with_suffix('.onnx.json')}\n"
                "    Every Piper voice is a PAIR of files — the .onnx and the .onnx.json.\n"
                "    Download the .onnx.json alongside the model."
            )

        self._voice = None
        try:
            from piper import PiperVoice  # piper-tts >= 1.2
            self._voice = PiperVoice.load(str(self._model_path))
        except Exception:
            # Fall through to the CLI. Verify it exists now, not mid-sentence.
            import shutil
            if shutil.which("piper") is None:
                raise TBSError(
                    "Piper is unusable: the Python API failed to load and the 'piper' "
                    "CLI is not on PATH.\n"
                    "    Fix:  pip install piper-tts   (requires Python 3.10+)\n"
                    "    Or set TBS_TTS=sapi for the built-in Windows voice."
                )

        self._counter = 0
        self._counter_lock = threading.Lock()

    def _next_path(self) -> Path:
        """A unique temp name — queued playback has several WAVs in flight."""
        with self._counter_lock:
            self._counter += 1
            n = self._counter
        return Path(tempfile.gettempdir()) / f"tbs_{os.getpid()}_{n:04d}.wav"

    def synthesize_to_file(self, text: str) -> Optional[Path]:
        """Render without playing, so the next sentence can be prepared early."""
        wav_path = self._next_path()
        if self._voice is not None:
            import wave
            with wave.open(str(wav_path), "wb") as wf:
                self._voice.synthesize_wav(text, wf)
        else:
            import subprocess
            subprocess.run(
                ["piper", "-m", str(self._model_path), "-f", str(wav_path)],
                input=text.encode("utf-8"),
                check=True,
                capture_output=True,
            )
        return wav_path

    def play_file(self, path: Path) -> None:
        _play_wav_file(Path(path))

    def speak(self, text: str) -> None:
        wav_path = self.synthesize_to_file(text)
        try:
            if wav_path is not None:
                self.play_file(wav_path)
        finally:
            if wav_path is not None:
                wav_path.unlink(missing_ok=True)


class SapiTTS(TTSBackend):
    """Windows SAPI5. Zero install, zero latency, sounds like a 2007 GPS.

    Present purely so TBS is never mute. Not a shipping choice.
    """

    name = "sapi"

    def __init__(self) -> None:
        _need("pyttsx3", "pyttsx3", "The Windows SAPI fallback voice")
        import pyttsx3

        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", 175)
        # Prefer a British voice if one is installed (Settings -> Speech -> Add voices).
        for v in self._engine.getProperty("voices"):
            blob = f"{getattr(v, 'name', '')} {getattr(v, 'id', '')}".lower()
            if "en-gb" in blob or "george" in blob or "hazel" in blob:
                self._engine.setProperty("voice", v.id)
                break

    def speak(self, text: str) -> None:
        self._engine.say(text)
        self._engine.runAndWait()


# Set by stop_playback(); checked while a WAV is playing so a wake word or a
# Ctrl+C cuts TBS off mid-sentence instead of after it.
_PLAYBACK_STOP = threading.Event()


def stop_playback() -> None:
    """Barge-in: silence whatever is playing right now."""
    _PLAYBACK_STOP.set()
    if sys.platform == "win32":
        try:
            import winsound
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001 - nothing playing is fine
            pass
    else:
        try:
            import sounddevice as sd
            sd.stop()
        except Exception:  # noqa: BLE001
            pass


def reset_playback() -> None:
    """Clear a previous barge-in so the next reply is allowed to play."""
    _PLAYBACK_STOP.clear()


def _wav_duration(path: Path) -> float:
    import wave
    try:
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate() or 1
            return wf.getnframes() / float(rate)
    except Exception:  # noqa: BLE001 - unknown length: fall back to blocking play
        return -1.0


def _play_wav_file(path: Path) -> None:
    """Play a WAV. winsound is stdlib on Windows; sounddevice covers everything else.

    Playback is asynchronous and polled so `stop_playback()` can cut it short;
    if the duration cannot be read we fall back to a plain blocking play.
    """
    if _PLAYBACK_STOP.is_set():
        return
    if sys.platform == "win32":
        import winsound
        duration = _wav_duration(path)
        if duration <= 0:
            winsound.PlaySound(str(path), winsound.SND_FILENAME)
            return
        winsound.PlaySound(
            str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
        )
        deadline = time.monotonic() + duration + 0.05
        while time.monotonic() < deadline:
            if _PLAYBACK_STOP.is_set():
                winsound.PlaySound(None, winsound.SND_PURGE)
                return
            time.sleep(0.02)
        return
    try:
        import soundfile as sf
        import sounddevice as sd
        data, sr = sf.read(str(path), dtype="float32")
        sd.play(data, sr)
        while sd.get_stream().active:
            if _PLAYBACK_STOP.is_set():
                sd.stop()
                return
            time.sleep(0.02)
    except ImportError as exc:
        raise TBSError(
            "Playing audio off Windows needs soundfile + sounddevice.\n"
            f"    Fix:  pip install soundfile sounddevice   ({exc})"
        ) from exc


def _make_mp3_player():
    """Return a callable that plays raw MP3 bytes.

    Decoding MP3 in-process needs an extra dependency on every platform, so we
    write a temp file and hand it to the OS. Windows' winsound is WAV-only, so we
    decode with miniaudio/pydub if available and otherwise ask ElevenLabs for PCM.
    """
    try:
        import miniaudio  # tiny, wheels on Windows, no ffmpeg
        import sounddevice as sd
        import numpy as np

        def play(mp3_bytes: bytes) -> None:
            decoded = miniaudio.decode(mp3_bytes)
            samples = np.frombuffer(decoded.samples, dtype=np.int16)
            if decoded.nchannels > 1:
                samples = samples.reshape(-1, decoded.nchannels)
            sd.play(samples, decoded.sample_rate)
            sd.wait()

        return play
    except ImportError:
        pass

    # Fallback: hand the file to the default system player. Blocking on Windows.
    def play_via_file(mp3_bytes: bytes) -> None:
        path = Path(tempfile.gettempdir()) / f"tbs_{os.getpid()}.mp3"
        path.write_bytes(mp3_bytes)
        try:
            if sys.platform == "win32":
                # PowerShell's MediaPlayer plays MP3 without extra packages.
                import subprocess
                ps = (
                    "Add-Type -AssemblyName presentationCore; "
                    "$p = New-Object System.Windows.Media.MediaPlayer; "
                    f"$p.Open([uri]'{path}'); $p.Play(); "
                    "Start-Sleep -Milliseconds 400; "
                    "while ($p.NaturalDuration.HasTimeSpan -eq $false) "
                    "{ Start-Sleep -Milliseconds 50 }; "
                    "Start-Sleep -Seconds $p.NaturalDuration.TimeSpan.TotalSeconds"
                )
                subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                               check=False, capture_output=True)
            else:
                import subprocess
                subprocess.run(["afplay" if sys.platform == "darwin" else "mpg123",
                                str(path)], check=False, capture_output=True)
        finally:
            path.unlink(missing_ok=True)

    print("[tbs] note: install 'miniaudio' for smoother ElevenLabs playback "
          "(pip install miniaudio).", file=sys.stderr)
    return play_via_file


def make_tts(backend: str = TTS_BACKEND) -> TTSBackend:
    """Pick a TTS backend, degrading gracefully and saying why."""
    order: list[str]
    if backend == "auto":
        order = (["elevenlabs"] if os.getenv("ELEVENLABS_API_KEY") else []) + ["piper", "sapi"]
    else:
        order = [backend]

    errors: list[str] = []
    for name in order:
        try:
            if name == "elevenlabs":
                return ElevenLabsTTS()
            if name == "piper":
                return PiperTTS()
            if name == "sapi":
                return SapiTTS()
            raise TBSError(
                f"Unknown TTS backend '{name}'. Valid: auto, elevenlabs, piper, sapi."
            )
        except TBSError as exc:
            errors.append(f"  [{name}] {exc}")

    raise TBSError("No usable TTS backend.\n" + "\n".join(errors))


# --------------------------------------------------------------------------
# Module-level speech API
#
# ..\core\main.py loads this file by path and probes it for a `speak`. Keeping
# these at module level means core gets a voice without constructing the whole
# pipeline — no microphone, no Whisper, no Porcupine AccessKey. The extra hooks
# (synth_to_file / play_file / stop_speaking) let core's SpeechQueue run
# gapless queued playback when the backend supports it, and quietly fall back
# to plain `speak()` when it does not.
# --------------------------------------------------------------------------

_TTS_LOCK = threading.Lock()
_TTS: Optional[TTSBackend] = None
_TTS_ERROR: Optional[TBSError] = None


def get_tts(backend: str = TTS_BACKEND) -> TTSBackend:
    """Lazily build (and cache) the process-wide TTS backend."""
    global _TTS, _TTS_ERROR
    with _TTS_LOCK:
        if _TTS is not None:
            return _TTS
        if _TTS_ERROR is not None:
            raise _TTS_ERROR
        try:
            _TTS = make_tts(backend)
        except TBSError as exc:
            _TTS_ERROR = exc
            raise
        print(f"[tbs] tts backend: {_TTS.name}")
        return _TTS


def speak(text: str) -> None:
    """Say one thing, blocking until it has been said."""
    if not (text or "").strip():
        return
    get_tts().speak(text)


def supports_queued_playback() -> bool:
    """True when synthesis and playback can be pipelined (gapless sentences)."""
    try:
        tts = get_tts()
    except TBSError:
        return False
    return bool(getattr(tts, "supports_queued_playback", False))


def synth_to_file(text: str):
    """Render a sentence to a temp WAV without playing it. None if unsupported."""
    if not (text or "").strip():
        return None
    return get_tts().synthesize_to_file(text)


def play_file(path) -> None:
    get_tts().play_file(Path(path))


def stop_speaking() -> None:
    """Barge-in: cut the current sentence."""
    stop_playback()


def reset_speaking() -> None:
    """Allow playback again after a barge-in."""
    reset_playback()


class BargeInWatch:
    """Listens for "TBS" *while he is talking* and cuts him off.

    Needs a microphone and a Porcupine AccessKey. If either is missing this is
    simply never started — Ctrl+C remains the interrupt — so no caller has to
    care whether wake-word barge-in is available.
    """

    def __init__(self, on_wake=None) -> None:
        self.event = threading.Event()
        self._on_wake = on_wake
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._porcupine = None

    def start(self) -> "BargeInWatch":
        import pvporcupine  # raises ImportError if absent - caller handles it
        import sounddevice  # noqa: F401 - fail here rather than in the thread

        key = os.getenv("PICOVOICE_ACCESS_KEY")
        if not key:
            raise TBSError("PICOVOICE_ACCESS_KEY is not set; wake-word barge-in is off.")
        self._porcupine = pvporcupine.create(
            access_key=key, keywords=[WAKE_KEYWORD], sensitivities=[WAKE_SENSITIVITY]
        )
        self._thread = threading.Thread(target=self._run, name="barge-in", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        import sounddevice as sd

        frame_len = self._porcupine.frame_length
        try:
            with sd.RawInputStream(samplerate=self._porcupine.sample_rate,
                                   blocksize=frame_len, dtype="int16",
                                   channels=1) as stream:
                while not self._stop.is_set():
                    data, overflowed = stream.read(frame_len)
                    if overflowed:
                        continue
                    pcm = struct.unpack_from("h" * frame_len, data)
                    if self._porcupine.process(pcm) >= 0:
                        self.event.set()
                        stop_playback()
                        if self._on_wake:
                            try:
                                self._on_wake()
                            except Exception:  # noqa: BLE001
                                pass
                        return
        except Exception as exc:  # noqa: BLE001 - a dead watcher must not kill speech
            print(f"[tbs] barge-in watcher stopped: {exc}", file=sys.stderr)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._porcupine is not None:
            try:
                self._porcupine.delete()
            except Exception:  # noqa: BLE001
                pass
            self._porcupine = None

    @property
    def triggered(self) -> bool:
        return self.event.is_set()


def start_barge_in_watch(on_wake=None) -> Optional[BargeInWatch]:
    """Start wake-word barge-in, or return None if this machine cannot do it."""
    try:
        return BargeInWatch(on_wake).start()
    except Exception:  # noqa: BLE001 - no mic, no key, no package: all fine
        return None


# --------------------------------------------------------------------------
# The pipeline
# --------------------------------------------------------------------------

class TBSVoice:
    """The whole loop. Construct once (it warms the models), then call run()."""

    def __init__(self, *, tts_backend: str = TTS_BACKEND, load_stt: bool = True,
                 load_wake: bool = True) -> None:
        self.system_prompt = load_system_prompt()
        self.history: list[dict] = []
        self._stop = threading.Event()

        # --- TTS (cheapest to build, fail here first) ---
        self.tts = get_tts(tts_backend)

        # --- Claude ---
        _need("anthropic", "anthropic", "Talking to Claude")
        import anthropic

        if not os.getenv("ANTHROPIC_API_KEY"):
            # An unset key isn't fatal — the SDK also reads an `ant auth login` profile.
            print("[tbs] note: ANTHROPIC_API_KEY is unset; relying on an "
                  "`ant auth login` profile. Run `ant auth status` if calls fail.",
                  file=sys.stderr)
        self.client = anthropic.Anthropic()

        # --- STT ---
        self.stt = None
        if load_stt:
            _need("faster_whisper", "faster-whisper", "Speech-to-text")
            from faster_whisper import WhisperModel

            print(f"[tbs] loading whisper '{STT_MODEL}' ({STT_COMPUTE})… ", end="", flush=True)
            t0 = time.perf_counter()
            self.stt = WhisperModel(
                STT_MODEL, device="cpu", compute_type=STT_COMPUTE, cpu_threads=STT_THREADS
            )
            # Warm the graph so the FIRST real utterance isn't 4 seconds slow.
            import numpy as np
            list(self.stt.transcribe(np.zeros(SAMPLE_RATE, dtype="float32"),
                                     language="en", beam_size=1)[0])
            print(f"ready in {time.perf_counter() - t0:.1f}s")

        # --- Wake word (OPTIONAL) ---
        # Porcupine only powers the spoken "TBS" trigger. Voice input itself
        # (record -> transcribe) needs no AccessKey, so a missing engine/key must
        # NOT stop TBS from listening — it just falls back to listening each
        # turn. This keeps voice working with no Picovoice account at all.
        self.porcupine = None
        if load_wake:
            key = os.getenv("PICOVOICE_ACCESS_KEY")
            try:
                import pvporcupine
                if not key:
                    raise RuntimeError("PICOVOICE_ACCESS_KEY not set")
                self.porcupine = pvporcupine.create(
                    access_key=key,
                    keywords=[WAKE_KEYWORD],          # "buddy" needs a custom .ppn keyword file
                    sensitivities=[WAKE_SENSITIVITY],
                )
            except Exception as exc:  # noqa: BLE001 - wake word is a nicety, never fatal
                print(
                    f"[tbs] wake word off ({exc}). Voice input still works - "
                    "TBS listens each turn. To enable saying 'TBS', "
                    "install pvporcupine and set PICOVOICE_ACCESS_KEY.",
                    file=sys.stderr,
                )
                self.porcupine = None

        # --- Microphone ---
        _need("sounddevice", "sounddevice", "Microphone capture")
        _need("numpy", "numpy", "Audio processing")

    # -- stage 1: wake word ------------------------------------------------

    def wait_for_wake(self) -> None:
        """Block until someone says 'TBS'. ~30 ms frames, negligible CPU."""
        import sounddevice as sd

        assert self.porcupine is not None
        frame_len = self.porcupine.frame_length

        with sd.RawInputStream(samplerate=self.porcupine.sample_rate,
                               blocksize=frame_len, dtype="int16",
                               channels=1) as stream:
            print('[tbs] listening for "TBS"…')
            while not self._stop.is_set():
                data, overflowed = stream.read(frame_len)
                if overflowed:
                    continue
                pcm = struct.unpack_from("h" * frame_len, data)
                if self.porcupine.process(pcm) >= 0:
                    return

    # -- stage 2: record until silence -------------------------------------

    def record_until_silence(self) -> "object":
        """Capture one utterance. Returns float32 mono @16 kHz for Whisper.

        Simple RMS gate rather than a neural VAD: on a laptop mic in a quiet room
        it is indistinguishable, and it costs nothing. Whisper's own `vad_filter`
        cleans up the edges afterwards.
        """
        import numpy as np
        import sounddevice as sd

        block = 512                      # 32 ms — same granularity as Porcupine
        silence_blocks = max(1, int(SILENCE_MS / (block / SAMPLE_RATE * 1000)))
        max_blocks = int(MAX_UTTERANCE_S * SAMPLE_RATE / block)

        chunks: list[bytes] = []
        quiet_run = 0
        heard_speech = False
        q: "queue.Queue[bytes]" = queue.Queue()

        def cb(indata, frames, time_info, status):
            q.put(bytes(indata))

        with sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=block, dtype="int16",
                               channels=1, callback=cb):
            for _ in range(max_blocks):
                try:
                    data = q.get(timeout=1.0)
                except queue.Empty:
                    break
                chunks.append(data)

                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0

                if rms > SILENCE_RMS:
                    heard_speech = True
                    quiet_run = 0
                elif heard_speech:
                    quiet_run += 1
                    if quiet_run >= silence_blocks:
                        break

        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.frombuffer(b"".join(chunks), dtype=np.int16).astype(np.float32) / 32768.0
        return audio

    # -- stage 3: transcribe -----------------------------------------------

    def transcribe(self, audio) -> str:
        if self.stt is None:
            raise TBSError("Speech-to-text was not loaded (constructed with load_stt=False).")
        if len(audio) < MIN_UTTERANCE_S * SAMPLE_RATE:
            return ""
        segments, _info = self.stt.transcribe(
            audio,
            language="en",
            beam_size=1,          # greedy: ~40% faster, no meaningful accuracy loss here
            vad_filter=True,
            condition_on_previous_text=False,   # stops hallucinated continuations
        )
        return " ".join(s.text.strip() for s in segments).strip()

    def listen(self) -> str:
        """Record one utterance and return its transcript."""
        return self.transcribe(self.record_until_silence())

    # -- stage 4: Claude ---------------------------------------------------

    def think(self, user_text: str, *, model: Optional[str] = None) -> str:
        """One conversational turn. Keeps a rolling history."""
        model = model or self._route(user_text)
        self.history.append({"role": "user", "content": user_text})

        kwargs: dict = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": self.system_prompt,
            "messages": self.history[-HISTORY_TURNS:],
        }
        # Adaptive thinking is off unless requested on Opus 4.8, and Haiku 4.5
        # doesn't support it at all — so only set it for the brain model.
        if model.startswith("claude-opus") or model.startswith("claude-sonnet-5"):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": EFFORT}

        try:
            resp = self.client.messages.create(**kwargs)
        except Exception as exc:
            self.history.pop()
            raise TBSError(f"Claude call failed: {exc}") from exc

        if resp.stop_reason == "refusal":
            reply = "I'm afraid I can't help with that one, sir."
        else:
            reply = " ".join(b.text for b in resp.content if b.type == "text").strip()
        if not reply:
            reply = "I've nothing useful to say to that, sir."

        self.history.append({"role": "assistant", "content": reply})
        del self.history[:-HISTORY_TURNS]
        return reply

    @staticmethod
    def _route(text: str) -> str:
        """Cheap heuristic router: trivial one-liners go to Haiku, everything else Opus.

        Saves ~500 ms and most of the token bill on 'what time is it'-class turns.
        See voice-stack.md § latency budget.
        """
        t = text.lower().strip()
        trivial_starts = (
            "what time", "what's the time", "set a timer", "set an alarm",
            "how long", "remind me", "what's the date", "what day",
            "thanks", "thank you", "never mind", "cancel", "stop", "hello", "hi ",
        )
        if len(t.split()) <= 8 and t.startswith(trivial_starts):
            return AMBIENT_MODEL
        return BRAIN_MODEL

    # -- stage 5: speak ----------------------------------------------------

    def speak(self, text: str) -> None:
        if not text.strip():
            return
        try:
            self.tts.speak(text)
        except Exception as exc:
            # Never let a TTS failure kill the loop — print and carry on.
            print(f"[tbs] tts failed ({exc}); text was: {text}", file=sys.stderr)

    # -- the loop ----------------------------------------------------------

    def turn(self) -> None:
        """One full wake -> reply cycle, with timings."""
        self.wait_for_wake()
        t_wake = time.perf_counter()
        print("[tbs] awake.")

        audio = self.record_until_silence()
        t_rec = time.perf_counter()

        text = self.transcribe(audio)
        t_stt = time.perf_counter()
        if not text:
            print("[tbs] (nothing heard)")
            return
        print(f"  you  > {text}")

        reply = self.think(text)
        t_llm = time.perf_counter()
        print(f"  tbs> {reply}")

        self.speak(reply)
        t_tts = time.perf_counter()

        print(f"  [rec {t_rec - t_wake:.2f}s | stt {t_stt - t_rec:.2f}s | "
              f"llm {t_llm - t_stt:.2f}s | tts {t_tts - t_llm:.2f}s | "
              f"wake->speech {t_tts - t_rec:.2f}s]")

    def run(self) -> None:
        print("[tbs] online. Ctrl+C to shut down.")
        try:
            while not self._stop.is_set():
                try:
                    self.turn()
                except TBSError as exc:
                    print(f"[tbs] {exc}", file=sys.stderr)
                    time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n[tbs] shutting down, sir.")
        finally:
            self.close()

    def close(self) -> None:
        self._stop.set()
        if self.porcupine is not None:
            try:
                self.porcupine.delete()
            except Exception:
                pass
            self.porcupine = None

    def __enter__(self) -> "TBSVoice":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _check() -> int:
    """Verify every dependency and key, and report what's missing. Never raises."""
    ok = True

    print("Dependencies")
    for mod, pip_name, required in [
        ("anthropic", "anthropic", True),
        ("pvporcupine", "pvporcupine", True),
        ("sounddevice", "sounddevice", True),
        ("numpy", "numpy", True),
        ("faster_whisper", "faster-whisper", True),
        ("piper", "piper-tts", False),
        ("elevenlabs", "elevenlabs", False),
        ("miniaudio", "miniaudio", False),
        ("pyttsx3", "pyttsx3", False),
    ]:
        try:
            __import__(mod)
            print(f"  [ok]      {mod}")
        except ImportError:
            tag = "MISSING " if required else "optional"
            print(f"  [{tag}] {mod:<16} pip install {pip_name}")
            ok = ok and not required

    print("\nCredentials")
    for var, required, note in [
        ("ANTHROPIC_API_KEY", False, "or an `ant auth login` profile"),
        ("PICOVOICE_ACCESS_KEY", True, "free at console.picovoice.ai"),
        ("ELEVENLABS_API_KEY", False, "omit to use Piper"),
    ]:
        if os.getenv(var):
            print(f"  [ok]      {var}")
        else:
            tag = "MISSING " if required else "optional"
            print(f"  [{tag}] {var:<22} {note}")
            ok = ok and not required

    print("\nAssets")
    p = Path(PIPER_VOICE)
    for f in (p, p.with_suffix(".onnx.json")):
        print(f"  [{'ok' if f.exists() else 'missing':<7}] {f}")
    sp = HERE / "system-prompt.md"
    print(f"  [{'ok' if sp.exists() else 'missing':<7}] {sp}")

    print("\nAudio input")
    try:
        import sounddevice as sd
        default_in = sd.query_devices(kind="input")
        print(f"  [ok]      default mic: {default_in['name']}")
    except Exception as exc:
        print(f"  [MISSING] no usable input device ({exc})")
        ok = False

    print("\nTTS backend selection")
    try:
        backend = make_tts()
        print(f"  [ok]      would use: {backend.name}")
        if backend.supports_queued_playback:
            print("  [ok]      gapless queued playback: yes "
                  "(sentence N+1 renders while N plays)")
        else:
            print(f"  [note]    {backend.name} cannot pre-render; sentences still "
                  "stream, with a short gap between them")
    except TBSError as exc:
        print(f"  [MISSING] {exc}")
        ok = False

    print("\n" + ("All systems nominal, sir." if ok else "Fix the MISSING items above."))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="TBS voice pipeline")
    ap.add_argument("--check", action="store_true",
                    help="verify dependencies, keys and audio devices, then exit")
    ap.add_argument("--say", metavar="TEXT", help="speak TEXT and exit (TTS smoke test)")
    ap.add_argument("--text", action="store_true",
                    help="keyboard input instead of the mic; still speaks the reply")
    ap.add_argument("--tts", default=TTS_BACKEND,
                    choices=["auto", "elevenlabs", "piper", "sapi"],
                    help="force a TTS backend")
    args = ap.parse_args()

    if args.check:
        return _check()

    try:
        if args.say:
            make_tts(args.tts).speak(args.say)
            return 0

        if args.text:
            jv = TBSVoice(tts_backend=args.tts, load_stt=False, load_wake=False)
            print("[tbs] text mode. Blank line to exit.")
            while True:
                try:
                    line = input("  you  > ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not line:
                    break
                reply = jv.think(line)
                print(f"  tbs> {reply}")
                jv.speak(reply)
            jv.close()
            return 0

        with TBSVoice(tts_backend=args.tts) as jv:
            jv.run()
        return 0

    except TBSError as exc:
        print(f"\n[tbs] {exc}\n", file=sys.stderr)
        print("Run `python voice.py --check` for a full environment report.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
