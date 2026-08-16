"""setup_piper.py — install the offline TBS voice.

Piper is the default TTS backend: free, offline, and fast enough to keep up
with a streamed reply. It ships without any voice, so this script fetches one
pair of files from the official rhasspy/piper-voices repository on Hugging
Face and proves the whole path works before TBS ever needs it.

    py -3.11 setup_piper.py              # download, verify, speak a test phrase
    py -3.11 setup_piper.py --check      # report what is installed, change nothing
    py -3.11 setup_piper.py --force      # re-download even if the files exist
    py -3.11 setup_piper.py --no-play    # synthesise the test WAV but stay silent
    py -3.11 setup_piper.py --voice en_US-ryan-high

A voice is always a PAIR: `<name>.onnx` (the model) and `<name>.onnx.json` (the
phoneme and audio config). Either one alone is useless, which is exactly how
this install was broken before.

Files land in `personality\\voices\\`, which is where voice.py already looks
(TBS_PIPER_VOICE overrides the path).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
VOICES_DIR = HERE / "voices"

# Official source. `resolve/main` serves the file itself rather than the HTML
# page; the layout is <lang>/<locale>/<speaker>/<quality>/<name>.onnx[.json].
REPO_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

DEFAULT_VOICE = "en_GB-alan-medium"

TEST_PHRASE = (
    "Good evening, Sir. Piper is installed and the offline voice is working. "
    "Battery is at ninety eight point six percent."
)

# A model that downloaded correctly is tens of megabytes; anything smaller is
# an error page or a truncated transfer.
MIN_MODEL_BYTES = 5_000_000
MIN_CONFIG_BYTES = 500
MIN_WAV_BYTES = 20_000       # ~0.5 s of 22 kHz 16-bit mono


class SetupError(RuntimeError):
    """Something the user can fix, phrased so they can fix it."""


# ---------------------------------------------------------------------------
# Working out where a voice lives in the repo
# ---------------------------------------------------------------------------

def voice_urls(voice: str) -> tuple[str, str]:
    """Map `en_GB-alan-medium` to its two download URLs.

    en_GB-alan-medium -> en/en_GB/alan/medium/en_GB-alan-medium.onnx[.json]
    """
    try:
        locale, speaker, quality = voice.split("-", 2)
        language = locale.split("_")[0]
    except ValueError as exc:
        raise SetupError(
            f"'{voice}' is not a Piper voice name.\n"
            "    Expected <locale>-<speaker>-<quality>, e.g. en_GB-alan-medium.\n"
            "    Full list: https://huggingface.co/rhasspy/piper-voices/tree/main"
        ) from exc
    path = f"{language}/{locale}/{speaker}/{quality}/{voice}.onnx"
    return f"{REPO_BASE}/{path}", f"{REPO_BASE}/{path}.json"


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def download(url: str, dest: Path, *, force: bool = False, min_bytes: int = 1) -> Path:
    """Fetch `url` to `dest` via a temp file, so a failed download leaves nothing."""
    if dest.exists() and not force:
        if dest.stat().st_size >= min_bytes:
            print(f"  [skip] {dest.name} already present ({_human(dest.stat().st_size)})")
            return dest
        print(f"  [redo] {dest.name} is too small ({dest.stat().st_size} B) - refetching")

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "tbs-setup-piper/1.0"})

    print(f"  [get ] {dest.name}")
    print(f"         {url}")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                out.write(block)
                done += len(block)
                if total:
                    pct = done * 100 // total
                    print(f"\r         {pct:3d}%  {_human(done)} / {_human(total)}",
                          end="", flush=True)
            if total:
                print()
    except urllib.error.HTTPError as exc:
        tmp.unlink(missing_ok=True)
        if exc.code == 404:
            raise SetupError(
                f"404 - no such voice at {url}\n"
                "    Check the voice name against\n"
                "    https://huggingface.co/rhasspy/piper-voices/tree/main"
            ) from exc
        raise SetupError(f"Download failed ({exc.code} {exc.reason}): {url}") from exc
    except urllib.error.URLError as exc:
        tmp.unlink(missing_ok=True)
        raise SetupError(
            f"Could not reach Hugging Face: {exc.reason}\n"
            "    Check the network / proxy and try again."
        ) from exc

    size = tmp.stat().st_size
    if size < min_bytes:
        tmp.unlink(missing_ok=True)
        raise SetupError(
            f"{dest.name} came back as only {size} B - that is an error page, "
            "not the file.\n    Try again, or download it manually from\n"
            f"    {url}"
        )
    tmp.replace(dest)
    rate = size / max(time.perf_counter() - started, 1e-6) / 1024 / 1024
    print(f"         saved {_human(size)} ({rate:.1f} MB/s) -> {dest}")
    return dest


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def verify_files(model: Path, config: Path) -> dict:
    """Both files exist, are plausibly sized, and the config actually parses."""
    print("\nVerifying")
    for path, minimum in ((model, MIN_MODEL_BYTES), (config, MIN_CONFIG_BYTES)):
        if not path.exists():
            raise SetupError(f"Missing after download: {path}")
        size = path.stat().st_size
        if size < minimum:
            raise SetupError(f"{path.name} is only {_human(size)} - truncated. Re-run with --force.")
        print(f"  [ok  ] {path.name:<32} {_human(size)}")

    try:
        meta = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"{config.name} is not valid JSON ({exc}). Re-run with --force.") from exc

    sample_rate = (meta.get("audio") or {}).get("sample_rate")
    if not sample_rate:
        raise SetupError(f"{config.name} has no audio.sample_rate - wrong file?")
    phonemes = len(meta.get("phoneme_id_map") or {})
    print(f"  [ok  ] config parses: {sample_rate} Hz, {phonemes} phoneme ids, "
          f"espeak voice '{(meta.get('espeak') or {}).get('voice', '?')}'")
    return meta


def _load_piper_voice(model: Path):
    """Load the model through the Python API, or return None to use the CLI."""
    try:
        from piper import PiperVoice
    except ImportError:
        return None
    try:
        return PiperVoice.load(str(model))
    except Exception as exc:  # noqa: BLE001 - fall through to the CLI
        print(f"  [note] Python API could not load the model ({exc}); trying the CLI.")
        return None


def synthesize_test(model: Path, out_wav: Path, phrase: str = TEST_PHRASE) -> Path:
    """Render the test phrase and return the WAV path."""
    print("\nSynthesising a test phrase")
    print(f"  \"{phrase}\"")
    started = time.perf_counter()

    voice = _load_piper_voice(model)
    if voice is not None:
        with wave.open(str(out_wav), "wb") as wav_file:
            voice.synthesize_wav(phrase, wav_file)
        how = "piper python api"
    else:
        if shutil.which("piper") is None:
            raise SetupError(
                "Piper is installed as neither a working Python package nor a CLI.\n"
                "    Fix:  py -3.11 -m pip install piper-tts\n"
                "    (needs Python 3.10+; the wheel bundles onnxruntime and espeak-ng data)"
            )
        subprocess.run(
            ["piper", "-m", str(model), "-f", str(out_wav)],
            input=phrase.encode("utf-8"), check=True, capture_output=True,
        )
        how = "piper cli"
    elapsed = time.perf_counter() - started

    if not out_wav.exists():
        raise SetupError(f"No WAV was produced at {out_wav}.")
    size = out_wav.stat().st_size
    with wave.open(str(out_wav), "rb") as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
        duration = frames / float(rate or 1)

    print(f"  [ok  ] {how}: {_human(size)}, {duration:.2f}s of audio at {rate} Hz")
    print(f"  [ok  ] rendered in {elapsed:.2f}s "
          f"({duration / max(elapsed, 1e-6):.1f}x realtime)")

    if size < MIN_WAV_BYTES or duration < 0.5:
        raise SetupError(
            f"The WAV is suspiciously small ({_human(size)}, {duration:.2f}s) - "
            "synthesis ran but produced nothing audible."
        )
    return out_wav


def play(wav_path: Path) -> bool:
    """Play the test WAV so the user can actually hear it. Never fatal."""
    print("\nPlaying it back")
    try:
        if sys.platform == "win32":
            import winsound

            winsound.PlaySound(str(wav_path), winsound.SND_FILENAME)
        elif sys.platform == "darwin":
            subprocess.run(["afplay", str(wav_path)], check=False)
        else:
            subprocess.run(["aplay", str(wav_path)], check=False, capture_output=True)
        print("  [ok  ] playback finished (if you heard nothing, check the output device)")
        return True
    except Exception as exc:  # noqa: BLE001 - a silent machine is not a failed install
        print(f"  [warn] could not play audio here ({exc}); the WAV itself is fine:")
        print(f"         {wav_path}")
        return False


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def paths_for(voice: str) -> tuple[Path, Path]:
    model = VOICES_DIR / f"{voice}.onnx"
    return model, model.with_suffix(".onnx.json")


def check_only(voice: str) -> int:
    model, config = paths_for(voice)
    print(f"Piper voice: {voice}")
    print(f"  folder : {VOICES_DIR}")
    ok = True
    for path in (model, config):
        if path.exists():
            print(f"  [ok  ] {path.name} ({_human(path.stat().st_size)})")
        else:
            ok = False
            print(f"  [MISS] {path.name}")
    try:
        import piper  # noqa: F401
        print("  [ok  ] piper-tts package importable")
    except ImportError:
        ok = False
        print("  [MISS] piper-tts  ->  py -3.11 -m pip install piper-tts")
    print("\n" + ("Piper is ready." if ok else "Run this script without --check to install."))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="setup_piper",
        description="Download and verify the offline Piper voice TBS speaks with.",
    )
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help=f"voice name (default: {DEFAULT_VOICE})")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--no-play", action="store_true", help="synthesise but do not play")
    parser.add_argument("--check", action="store_true", help="report status and exit")
    parser.add_argument("--keep-wav", action="store_true",
                        help="leave the test WAV in the voices folder")
    args = parser.parse_args(argv)

    if args.check:
        return check_only(args.voice)

    print("=" * 68)
    print(f"  Piper voice install - {args.voice}")
    print("=" * 68)

    try:
        import piper  # noqa: F401
    except ImportError:
        print("\n[!] The piper-tts package is not installed.")
        print("    Fix:  py -3.11 -m pip install piper-tts")
        print("    Then run this script again.\n")
        return 1

    model_url, config_url = voice_urls(args.voice)
    model_path, config_path = paths_for(args.voice)

    try:
        print(f"\nDownloading into {VOICES_DIR}")
        download(model_url, model_path, force=args.force, min_bytes=MIN_MODEL_BYTES)
        download(config_url, config_path, force=args.force, min_bytes=MIN_CONFIG_BYTES)

        verify_files(model_path, config_path)

        wav_path = (VOICES_DIR if args.keep_wav
                    else Path(tempfile.gettempdir())) / f"piper_test_{args.voice}.wav"
        synthesize_test(model_path, wav_path)
        if not args.no_play:
            play(wav_path)
        if not args.keep_wav:
            print(f"\n  test WAV: {wav_path}")
    except SetupError as exc:
        print(f"\n[!] {exc}\n")
        return 1

    print("\n" + "=" * 68)
    print("  Piper is installed and audible.")
    print("=" * 68)
    print(f"  model  : {model_path}")
    print(f"  config : {config_path}")
    print("\n  voice.py finds this automatically. To be explicit:")
    print(f'      $env:TBS_PIPER_VOICE = "{model_path}"')
    print('      $env:TBS_TTS = "piper"      # already the default')
    print("\n  Try it:")
    print('      py -3.11 voice.py --say "Good evening, Sir."')
    print("      py -3.11 voice.py --check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
