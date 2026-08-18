# TBS Voice Stack — Windows 11 / Lenovo ThinkPad (CPU-only)

Researched July 2026. Opinionated: one recommendation per layer, one free fallback,
and the reasoning for each. Target: **wake word → first spoken syllable in under 2 seconds.**

---

## TL;DR — the stack

| Layer | Pick | Fallback | Why |
|---|---|---|---|
| Wake word | **Porcupine** (`pvporcupine`), built-in keyword `"tbs"` | openWakeWord | "TBS" is a *shipped* keyword. Zero training. ~30 ms frames, <5% of one core. |
| STT | **faster-whisper**, `small.en`, `int8` | `base.en` if the ThinkPad is a U-series | Best CPU accuracy-per-millisecond on x86. ~0.6 s for a 4 s utterance. |
| TTS | **ElevenLabs Flash v2.5**, voice `the user` (`onwK4e9ZLuTAKqWW03F9`) | **Piper** `en_GB-alan-medium` | Flash is ~75 ms to first byte and actually sounds like the character. Piper is free, offline, ~10× realtime. |
| Playback | `winsound` (stdlib) for WAV; `sounddevice` for streamed PCM | — | No extra deps on Windows. |
| LLM | `claude-opus-4-8` brain / `claude-haiku-4-5` ambient | — | See `system-prompt.md` § Model routing. |

---

## 1. Wake word — Porcupine

**Verified: `"tbs"` is a built-in Porcupine keyword.** The shipped keyword set is
`alexa, americano, blueberry, bumblebee, computer, grapefruit, grasshopper, hey google,
hey siri, tbs, ok google, picovoice, porcupine, terminator`
([Porcupine repo](https://github.com/picovoice/porcupine),
[Picovoice docs](https://picovoice.ai/docs/api/porcupine-java/)).

No custom model to train, no `.ppn` file to download, no Picovoice Console round-trip
for the keyword itself. You **do** need a free AccessKey from
[console.picovoice.ai](https://console.picovoice.ai) — free tier covers personal use.

```powershell
pip install pvporcupine sounddevice
setx PICOVOICE_ACCESS_KEY "your-key-here"
```

```python
import pvporcupine
porcupine = pvporcupine.create(
    access_key=os.environ["PICOVOICE_ACCESS_KEY"],
    keywords=["tbs"],
    sensitivities=[0.6],          # 0.5 default; 0.6–0.7 if it misses you
)
# porcupine.sample_rate == 16000, porcupine.frame_length == 512  (32 ms frames)
```

**Latency: ~30–60 ms** from the end of the spoken word to detection. Effectively free.

Tuning: sensitivity `0.5` is the default. Raise toward `0.7` if it misses you across a
dorm room; drop to `0.4` if the TV sets it off. TBS is a fairly distinctive
two-syllable word — false accepts are rare.

### Fallback: openWakeWord
`pip install openwakeword`. Fully open, no AccessKey, no cloud registration. But there
is **no pretrained "tbs" model** — you'd train one from synthetic data (an afternoon
of work). Only worth it if you object to the Picovoice key on principle.

**Not recommended:** Snowboy (dead since 2020), Mycroft Precise (unmaintained),
Windows Speech Recognition wake-up (poor, and it hijacks the whole OS).

---

## 2. STT — faster-whisper, `small.en`, int8

`faster-whisper` wraps CTranslate2 rather than PyTorch. On CPU it is roughly **2× faster
than reference Whisper** and it int8-quantizes cleanly, which is what makes a ThinkPad
viable ([codersera 2026 comparison](https://codersera.com/blog/faster-whisper-vs-whisper-cpp-speech-to-text-2026/),
[promptquorum 2026 benchmarks](https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026)).

```powershell
pip install faster-whisper
```

```python
from faster_whisper import WhisperModel
model = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=4)
segments, _ = model.transcribe(audio_float32, language="en", beam_size=1, vad_filter=True)
```

First call downloads ~470 MB to `%USERPROFILE%\.cache\huggingface`. Warm it at startup —
cold load is 3–5 s, and you don't want that on the first "TBS".

### Expected latency on a ThinkPad CPU (4 threads, int8, `beam_size=1`)

| Model | RAM | 3 s utterance | 6 s utterance | Accuracy |
|---|---|---|---|---|
| `tiny.en` | ~200 MB | ~0.15 s | ~0.3 s | Drops proper nouns constantly |
| `base.en` | ~290 MB | ~0.3 s | ~0.6 s | Fine for commands, shaky on names |
| **`small.en`** | **~700 MB** | **~0.6 s** | **~1.1 s** | **Reliable — the sweet spot** |
| `medium.en` | ~2.1 GB | ~2.0 s | ~4.0 s | Blows the latency budget |

Use `small.en` on any modern ThinkPad (P/H-series or 12th-gen+ U-series). Drop to
`base.en` only if it's an older low-TDP U-series and you feel the lag. Always use the
`.en` variants — English-only models are meaningfully faster and more accurate than
multilingual at the same size.

`beam_size=1` (greedy) instead of the default 5 costs almost nothing in accuracy on short
command utterances and saves ~40% of the decode time. Keep it.

### Why not whisper.cpp
whisper.cpp is genuinely competitive on CPU and wins outright at `tiny` — it's the right
call for embedded or no-Python environments. Here you're already in Python for Porcupine
and the Anthropic SDK, and `pip install faster-whisper` beats compiling a C++ binary and
managing a subprocess pipe on Windows. If you later want zero Python, whisper.cpp is the
port target.

### Why not cloud STT
Deepgram/AssemblyAI are ~200–300 ms and excellent, but they add a network hop, an API
key, a per-minute bill, and they break when the campus wifi does. Local wins for a
personal assistant. Revisit only if `small.en` accuracy disappoints you.

---

## 3. TTS — the hard part

Three real options, ranked.

### Recommended: ElevenLabs Flash v2.5
`eleven_flash_v2_5` — **~75 ms model latency**, 32 languages, built for exactly this
(real-time agents) ([ElevenLabs models](https://elevenlabs.io/docs/overview/models),
[cheat sheet 2026](https://www.webfuse.com/elevenlabs-cheat-sheet)).

**Voice: `the user` — voice ID `onwK4e9ZLuTAKqWW03F9`.** Male, middle-aged, British,
authoritative, originally a news-presenter voice. It is the closest default to the
film TBS: RP, measured, faintly amused. Second choice: `George` —
`JBFqnCBsd6RMkjVDRZzb`, warmer, narration-styled, slightly less crisp.

```python
from elevenlabs.client import ElevenLabs
client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
audio = client.text_to_speech.stream(
    voice_id="onwK4e9ZLuTAKqWW03F9",
    model_id="eleven_flash_v2_5",
    text=reply,
    output_format="mp3_22050_32",   # small = fast first byte
    voice_settings={"stability": 0.45, "similarity_boost": 0.8, "speed": 1.05},
)
```

**Cost:** free tier is ~10k chars/month — roughly 100 TBS replies, enough to evaluate.
Starter is $5/mo for 30k. A two-sentence reply is ~120 chars, so $5 ≈ 250 replies/month.
The two-sentence rule in the system prompt is doing double duty here as a cost control.

⚠️ **ElevenLabs default voices expire 31 December 2026.** Before then, clone `the user`
into your own workspace voice (Voices → Add → save a copy) and pin *that* ID, or you'll
wake up one January to a silent TBS.

### The default: Piper  ← installed and working
`en_GB-alan-medium` — Alan (UK), male, British, the best British male in the Piper
catalogue ([Piper VOICES.md](https://github.com/rhasspy/piper/blob/master/VOICES.md),
[samples](https://rhasspy.github.io/piper-samples/)). MIT-licensed, fully offline,
no key, no bill. **This is now the default backend** (`TBS_TTS=piper`).

Install is one command — it downloads the voice pair, verifies both files, and
speaks a test phrase:

```powershell
py -3.11 -m pip install piper-tts     # requires Python >= 3.10; v1.5.0 as of July 2026
py -3.11 setup_piper.py               # downloads + verifies + speaks
py -3.11 setup_piper.py --check       # status only
```

The pair lands in `personality\voices\` (60.3 MB `.onnx` + 4.8 KB `.onnx.json`,
both required). `TBS_PIPER_VOICE` overrides the path.

**Measured on this ThinkPad (July 2026):** a 2.9-second sentence synthesizes in
**~0.08–0.22 s**, i.e. ~13–35× realtime — comfortably faster than it plays, which
is what makes gapless sentence-by-sentence playback possible. The earlier "10×
realtime" estimate was about right. Cold start (model load) adds ~2 s once.

Honest assessment: Piper is *clearly* synthetic — flat prosody, no emotional range, an
audible seam between clauses. It will not give you goosebumps. But it is instant, free,
private, and works on a plane. Run it as the default while you're iterating on the
prompt and behaviour; flip to ElevenLabs when you want the thing to feel real.

Alternatives inside Piper: `en_GB-northern_english_male-medium` (regional, less butler),
`en_GB-semaine-medium` (multi-speaker, mixed quality). `alan` is the pick.

### Not recommended: Windows SAPI
`pip install pyttsx3` → SAPI5 → "Microsoft George" / "Microsoft Hazel" (British voices,
installable via Settings → Time & Language → Speech). Zero install, zero latency, and it
sounds like a 2007 GPS unit. It is a *last-resort* fallback so the assistant is never
mute — not a shipping choice. `voice.py` includes it as backend `sapi` for exactly that.

### Also considered
- **Kokoro-82M** — new open TTS, better prosody than Piper, but ~1–2 s on CPU for a short
  reply and the British voices are weak. Worth re-checking in six months.
- **XTTS-v2 / Coqui** — voice cloning quality, way too slow on CPU (5–10 s). Dead on arrival
  for realtime.
- **Azure Neural TTS** — `en-GB-RyanNeural` is excellent and cheap, but the SDK on Windows
  is heavier than ElevenLabs' and the latency is worse (~250 ms). Fine plan C.

---

## 4. Full latency budget

Measured from the end of the wake word to the first audible syllable of the reply.

### With ElevenLabs (recommended)

| Stage | Time | Notes |
|---|---:|---|
| Wake word detect | 40 ms | Porcupine, 32 ms frames |
| Record until silence | *n/a* | Excluded — this is the user talking, not lag |
| End-of-speech detection | 550 ms | 500 ms silence threshold + frame slack. **The single biggest lever.** |
| STT (`small.en`, int8, 4 s clip) | 620 ms | |
| Claude first token (Haiku, `effort=low`) | 400 ms | Opus w/ adaptive thinking: 900–1500 ms |
| TTS first byte (Flash v2.5) | 90 ms | |
| Audio buffer + playback start | 60 ms | |
| **Total (ambient / Haiku)** | **≈ 1.76 s** | ✅ under target |
| **Total (brain / Opus)** | **≈ 2.4 s** | ⚠️ over — mitigations below |

### With Piper (offline)

Swap the TTS row: 90 ms → ~160 ms for a two-sentence reply. **≈ 1.83 s** on Haiku.
Piper is not the bottleneck; the silence threshold and the LLM are.

### Getting Opus under 2 s

1. **Stream the reply and speak the first sentence while the second generates.**
   Sentence-boundary chunking is the highest-leverage change in the whole pipeline —
   it cuts perceived latency by whatever the LLM's remaining generation time is.
2. **Route aggressively.** Anything answerable in one clause goes to Haiku. Only tool
   use, code, and planning touch Opus. See `system-prompt.md` § Model routing.
3. **Drop the silence threshold to 400 ms** once you're used to the rhythm. Saves 150 ms
   at the cost of occasionally cutting you off mid-thought.
4. **Fire a filler ack immediately on wake** ("Sir?" — pre-rendered WAV, 0 ms synthesis).
   Doesn't reduce real latency, but it removes the dead-air feeling entirely, which is
   what "under 2 seconds" is actually a proxy for.
5. **Prompt-cache the conversation history** — put the `cache_control` breakpoint on the
   last block of the newest turn. Saves 100–300 ms of prefill on long sessions.
6. Keep `effort="medium"` on Opus for conversational turns. `high`/`xhigh` are for
   agentic work, not for "what's on my calendar".

### Things that will silently wreck the budget

- **Cold model load.** Warm faster-whisper *and* the Porcupine handle at startup.
- **Default `beam_size=5`.** Set it to 1.
- **Large `output_format` on ElevenLabs.** `mp3_44100_128` triples the first-byte time
  over `mp3_22050_32` for no audible gain over a laptop speaker.
- **Windows power plan.** On Balanced with the lid on battery, a ThinkPad will downclock
  and STT can double. Set the plan to High Performance while TBS is running, or accept it.
- **Writing the TTS output to disk before playing it.** Stream it.

---

## 5. Install, start to finish

```powershell
cd "<VAULT_ROOT>\Projects\Tools\TBS\personality"
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Keys (persist across sessions)
setx ANTHROPIC_API_KEY   "sk-ant-..."
setx PICOVOICE_ACCESS_KEY "..."
setx ELEVENLABS_API_KEY  "..."          # optional — omit to use Piper

# Piper voice — the default backend (downloads, verifies, and speaks a test line)
py -3.11 setup_piper.py

python voice.py --check      # verify every dependency and key
python voice.py              # run
```

`setx` only affects *new* shells — reopen PowerShell after setting keys.

---

## Sources

- [Porcupine — GitHub](https://github.com/picovoice/porcupine)
- [Porcupine built-in keywords — Picovoice Docs](https://picovoice.ai/docs/api/porcupine-java/)
- [Wake Word Detection Guide 2026 — Picovoice](https://picovoice.ai/blog/complete-guide-to-wake-word/)
- [faster-whisper vs whisper.cpp (2026) — Codersera](https://codersera.com/blog/faster-whisper-vs-whisper-cpp-speech-to-text-2026/)
- [Local Whisper STT benchmarks 2026 — PromptQuorum](https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026)
- [Choosing between Whisper variants — Modal](https://modal.com/blog/choosing-whisper-variants)
- [ElevenLabs Models (Flash v2.5)](https://elevenlabs.io/docs/overview/models)
- [ElevenLabs Cheat Sheet 2026 — Webfuse](https://www.webfuse.com/elevenlabs-cheat-sheet)
- [ElevenLabs default voices](https://elevenlabs.io/docs/product/voices/default-voices)
- [ElevenLabs voice IDs with samples — json2video](https://json2video.com/ai-voices/elevenlabs/)
- [Piper VOICES.md](https://github.com/rhasspy/piper/blob/master/VOICES.md)
- [Piper voice samples](https://rhasspy.github.io/piper-samples/)
- [Piper TTS setup 2026 — Local AI Master](https://localaimaster.com/blog/piper-tts-setup-guide)
