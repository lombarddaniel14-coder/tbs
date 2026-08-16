# TBS — core

The brain and hands of a local Iron-Man-style assistant that runs on Daniel's
Windows 11 ThinkPad. It listens (or reads typed input), decides what to do,
actually does it on the machine, and answers.

This folder is the engine. The **voice and personality layer lives in
`..\personality\`** (`voice.py`, `system-prompt.md`) and is built separately —
core only *reads* from it, never writes to it.

---

## What it is

```
TBS\
├── core\                  <- this folder
│   ├── main.py            entry point + the listen→think→act→respond loop
│   ├── brain.py           Claude wrapper: system prompt, history, tool loop
│   ├── streaming.py       sentence splitter, playback queue, turn timings
│   ├── config.py          settings, paths, API key loading
│   ├── requirements.txt
│   ├── tests\             splitter + queue + streaming tests (no API key needed)
│   └── tools\             the things TBS can actually DO
│       ├── __init__.py    tool registry + safe dispatch
│       ├── system_info.py battery, CPU, RAM, disk, wifi
│       ├── apps.py        launch / close Windows apps, open URLs
│       ├── files.py       search and open files
│       ├── web.py         web search + page fetch
│       ├── schedule.py    local JSON calendar (classes, deadlines, events)
│       └── memory.py      persistent facts about Daniel
├── personality\           <- built by the personality layer (voice.py, system-prompt.md)
└── data\                  <- created on first use: schedule.json, memory.json
```

**How a turn works.** `main.py` gets a line of input (mic or keyboard) and hands
it to `Brain.send()`. The brain sends the conversation to Claude with all 14
tool schemas attached. If Claude replies with `tool_use` blocks, the brain runs
those handlers locally, feeds the results back, and loops — up to 8 rounds —
until Claude produces a plain answer. That answer goes back to `main.py`, which
prints it and, if the personality layer provides a `speak()`, says it out loud.

**TBS speaks while Claude is still writing.** The reply is streamed, and
`streaming.SentenceBuffer` cuts it at real sentence boundaries the moment each
one is complete. Sentence 1 goes to the speakers while sentence 2 is still
being generated. With Piper, synthesis and playback run on separate threads, so
sentence N+1 is already rendered when N stops — no gap. Measured on this
machine: **first audio at 0.47 s instead of 1.75 s, a 1.3 s (73%) cut in dead
air**, and the whole turn finishes ~1.5 s sooner.

The splitter does not fire on `Dr.`, `Mr.`, `e.g.`, `i.e.`, `U.S.`, initials
(`J. R. R. Tolkien`), decimals (`98.6`, `3.11.9`), ellipses, or times
(`3:30 p.m.`) — and because it only commits to a boundary once the character
*after* the terminator has arrived, a chunk that ends mid-number never triggers
it either. `py -3.11 tests\test_streaming.py` checks all of that.

**The system prompt** is assembled fresh each session from three pieces: the
persona in `..\personality\system-prompt.md` (a built-in TBS persona is used
if that file does not exist yet), a short block of operating rules, and live
context — today's date, everything in `memory.json`, and what is on the schedule
today. So TBS starts every session already knowing who he is talking to.

---

## Install

Requires **Python 3.11+** on Windows.

```powershell
cd "C:\Users\Daniel\OneDrive - Bentley University\Claude Stuff\Projects\Tools\TBS\core"
py -3.11 -m pip install -r requirements.txt
```

Then give it an API key — either create `TBS\.env`:

```
ANTHROPIC_API_KEY=sk-ant-...
```

or set it in the shell:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

Without a key, TBS exits immediately and prints exactly what to do.

For the voice, install the personality layer's dependencies and the offline
Piper voice (free, no key, ~60 MB one-time download):

```powershell
cd "C:\Users\Daniel\OneDrive - Bentley University\Claude Stuff\Projects\Tools\TBS\personality"
py -3.11 -m pip install -r requirements.txt
py -3.11 setup_piper.py
```

---

## Run

```powershell
py -3.11 main.py            # voice if the personality layer is ready, keyboard otherwise
py -3.11 main.py --text     # keyboard only — no mic, no speakers (best for testing)
py -3.11 main.py --once "how much battery do I have"
```

In the loop, say or type `exit` to quit and `reset` to clear the conversation.
Tool calls are echoed as `[tool] name(args)` so you can see what he touched.

Try these:

- "What's my battery at?" / "How's the machine doing?"
- "Open Spotify" · "Close Chrome" · "Pull up the Bentley registrar page"
- "Find my resume" · "Where's that econ essay?"
- "Add GB 110 on Mondays at 9:30 in Smith 210" · "What do I have this week?"
- "Remember that my roommate is Malik" · "What do you know about me?"

---

## What works today

| Capability | Status |
|---|---|
| Claude tool-use loop (`claude-opus-4-8`, up to 8 tool rounds per turn) | working |
| Rolling conversation window (12 user turns, never splits a tool pair) | working |
| System info — CPU, RAM, disk, wifi | working |
| System info — battery | working where a battery is present; reports cleanly when not |
| Launch / close apps, open URLs | working (with a protected-process blocklist) |
| File search and open across Desktop/Documents/Downloads/Pictures/Videos/Music | working |
| Web search + page fetch | working — keyless DuckDuckGo HTML + `urllib`, no second API key |
| Local JSON schedule: classes, deadlines, events | working |
| Persistent memory injected into the system prompt each session | working |
| `--text` keyboard mode | working, no mic needed |
| Speech out (Piper `en_GB-alan-medium`, offline) | **working** — `personality\setup_piper.py` installs it |
| Sentence-boundary streaming + gapless playback | working |
| Barge-in (Ctrl+C, or a wake word if Porcupine is set up) | working — silence in ~35 ms |
| Per-turn latency breakdown | printed each turn |
| Voice **in** (mic → Whisper) | still needs `PICOVOICE_ACCESS_KEY`; keyboard until then |
| Prompt caching on the system prompt | on |

**Graceful degradation is deliberate.** No `psutil` → system info still reports
disk and wifi. No `personality\voice.py`, or a broken one → TBS prints why
and drops to the keyboard. A tool that throws → the exception comes back to
Claude as a tool error instead of crashing the process. And speech falls back
in three steps, silently: gapless queued playback → one blocking `speak()` per
sentence → speak the whole reply at the end, exactly as it used to.

**Verified:** every file byte-compiles under Python 3.11; the tool registry,
all six tool modules, the main loop, the exit/reset commands, and the voice
bridge (present, broken, and missing cases) were run end-to-end. The streaming
path was exercised against a stubbed token-by-token stream
(`tests\test_streaming.py`, 40+ checks) and end-to-end through real Piper
audio (`tests\test_end_to_end.py`). The live Claude API call is still *not*
exercised — there is no API key on this machine. That is the one path to
smoke-test first.

```powershell
py -3.11 tests\test_streaming.py            # splitter, queue, brain (no audio)
py -3.11 tests\test_end_to_end.py           # streamed vs. blocking, real speakers
py -3.11 tests\test_end_to_end.py --silent  # same, measured but quiet
py -3.11 tests\test_end_to_end.py --bargein # interrupt mid-reply
```

---

## Voice contract

`main.py` loads `..\personality\voice.py` by file path and probes it for:

- a speak-like callable: `speak` / `say` / `talk` / `output` / `speak_text`
- a listen-like callable: `listen` / `listen_once` / `hear` / `transcribe` / `record` / `get_input`

and, optionally, for the hooks that make streamed speech gapless and
interruptible:

- `synth_to_file(text) -> path` and `play_file(path)` — render one sentence
  while another is playing. Present → no gap between sentences.
- `stop_speaking()` / `reset_speaking()` — barge-in.
- `supports_queued_playback() -> bool` — say no and TBS uses plain `speak()`.
- `start_barge_in_watch(on_wake)` — listen for "TBS" *during* playback.

All of them are optional. Missing → TBS still streams sentence by sentence
through `speak()`; a backend that fails mid-reply is caught and the reply is
spoken the old way.

Module-level functions are preferred; failing that, it instantiates a class —
`TBSVoice` first, then `Voice` / `VoiceEngine` / `TBS` / `Speech` /
`VoiceIO`, then any class in the module exposing both method names — and uses
the instance. `listen()` should return a string (empty or `None` if nothing was
heard). Anything else is treated as "voice unavailable" and TBS falls back to
the keyboard rather than failing.

As of this build **speech out works**: `voice.py` exposes module-level `speak`,
`synth_to_file`, `play_file` and `stop_speaking` backed by Piper, so core gets a
voice without constructing the full pipeline (no mic, no Whisper, no Porcupine
key). Speech *in* is still keyboard-only — `TBSVoice()` needs
`PICOVOICE_ACCESS_KEY` for the wake word, and until it is set the bridge's
attempt to build that class costs ~3 s of Whisper loading at startup for
nothing. Set the key, or run `--text`, to skip it.

---

## Settings

Everything is in `config.py`, overridable by environment variable or `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | required |
| `TBS_THINKING` | `off` | `adaptive` makes Claude reason before answering (slower, smarter) |
| `TBS_EFFORT` | `low` | `low` / `medium` / `high` / `xhigh` / `max` |
| `TBS_USER_NAME` | `Daniel` | used by the built-in fallback persona |
| `TBS_STREAM` | `on` | `off` waits for the whole reply before speaking (the old behaviour) |
| `TBS_TIMINGS` | `on` | print the wake→transcript→first token→first audio→done breakdown |
| `TBS_TTS` | `piper` | `piper` / `elevenlabs` / `sapi` / `auto` — read by the personality layer too |

`MODEL`, `MAX_TOKENS`, `HISTORY_TURNS`, and `MAX_TOOL_ITERATIONS` are constants
at the top of `config.py`.

---

## What's next

1. **Smoke-test a live call** — `py -3.11 main.py --text --once "system check"`
   with a real key, and confirm a tool actually fires.
2. **Wake word** — set `PICOVOICE_ACCESS_KEY` (free at console.picovoice.ai) to
   turn on mic input *and* wake-word barge-in; both are already wired.
3. ~~Streaming replies~~ — done. Sentence-boundary streaming with gapless
   queued playback; see `streaming.py`.
4. **More hands** — volume and media control, screenshots, clipboard, Bentley
   portal / Canvas scraping, email triage.
5. **A real calendar bridge** — sync `schedule.json` with Google Calendar
   instead of keeping a separate list.
6. **Confirmation gate** for destructive actions (`close_app --force`, deleting
   files) before the tool surface grows any further.
7. **Auto-start** as a Windows scheduled task or tray app so TBS is always
   resident.
