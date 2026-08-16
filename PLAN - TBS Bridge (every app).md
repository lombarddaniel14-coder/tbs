---
type: plan
tags: [tbs, bridge, forge3d, voice, integration]
created: 2026-08-05
status: AWAITING DANIEL APPROVAL
---

# TBS Bridge — one assistant, every app

## What you'll be able to do

Sit at your desk with Forge 3D projected on the wall. Say *"TBS, open the Marin project."*
*"Spread it out."* *"Isolate the head."* *"What am I looking at?"* — and he answers, in his voice,
because he can both **read** the scene and **act** on it.

In **Settings** there's a TBS icon with a **+ Wake TBS** control. Click it, he wakes. The
state sticks — close the app, reopen it, he's still awake. Say *"TBS, go to sleep"* and he's off
until you wake him again.

Then every app after this gets the same thing, because it's built once as a contract, not
re-invented per app.

## The hard blocker, first

**TBS has no `ANTHROPIC_API_KEY` anywhere** — not in `TBS\.env`, not in `core\.env`. Without
it he cannot think, so nothing below works. This is yours to set (I never handle credentials):
create `Claude Stuff\Projects\Tools\TBS\.env` containing `ANTHROPIC_API_KEY=sk-ant-...` from
console.anthropic.com. Same key everywhere, exactly as you asked.

Optional second key: `PICOVOICE_ACCESS_KEY` (free) turns on the "TBS" wake word and mic input.
Without it, speech **out** already works and input is push-to-talk.

## Architecture — one brain, many surfaces

TBS stays a **single process** with a single key, single voice, single memory. Each app becomes
a *surface* he can reach into. He already works exactly this way: `core\brain.py` runs a Claude
tool-calling loop and `core\tools\*.py` are the tools (apps, files, memory, projects, schedule,
system_info, web). **Adding an app = adding a tool module.** No new AI per app, no duplicated keys.

```
  You (voice) ──▶ TBS (Python, one process, one API key)
                    │  core\tools\forge3d.py   ← new, per app
                    ▼
              localhost control endpoint in the app
                    │
                    ▼
              the app's existing commands/UI
```

### The control channel: a small local HTTP endpoint

Forge 3D's main process opens a server on `127.0.0.1` **only** (never the network), on a random
port, writing `port + token` to a handshake file TBS reads. Every request carries the token.

Two verbs, and the second is the one that makes it feel intelligent:
- `POST /do` — perform an action
- `GET /state` — describe what's on screen, so TBS can *answer questions*, not just obey

**Why not the existing file inbox?** Forge already has a watched `inbox\` and it's proven — but
it's one-way and takes ~400ms+ to settle. Voice needs low latency and needs TBS to *read* state.
The inbox stays exactly as it is for delivering components; this is a separate, live channel.

### What TBS can do in Forge 3D (v1)

**Read:** list projects · list parts in the scene · what's selected · dimensions of a part ·
current mode (spread/isolate/presentation).
**Act:** open/close a project · select a part by name · Spread and set the amount · Isolate ·
Focus/Frame · switch views · enter/exit Presentation · add a primitive · export STL.
**Guarded:** anything destructive (delete, overwrite) requires a **spoken confirmation** — Forge's
existing voice already uses this pattern for delete; TBS reuses it rather than inventing one.

TBS never edits `project.json` directly. He drives the app's real commands, so **undo keeps
working** and the single-writer rule holds.

### Settings UI + persistence

A **TBS** section in Settings: his icon, a **+ Wake TBS** button, and a status line
(*asleep · waking · awake · no API key*). State saves to the existing `settings.json` — which is
already per-machine-aware — so it survives close/reopen and syncs sensibly to the laptop.
Voice command *"TBS, go to sleep"* flips the same setting from inside the app.

### Relationship to the voice Forge already has

Forge's current voice is **Vosk: offline, no key, ~20 fixed commands**. It keeps working exactly as
now and stays the default. TBS is a **second, optional layer** for natural language. Two reasons
that matters: it still works on the ThinkPad with no internet and no key, and if TBS is asleep
or the key is missing, nothing you rely on today breaks.

## Reusable contract — the actual deliverable

The point isn't Forge 3D, it's that app #2 is cheap. Two artifacts:

1. **`TBS-BRIDGE.md`** — the spec any app implements: the two endpoints, the handshake file, the
   token rule, the settings-toggle contract, the confirmation rule for destructive actions.
2. **A `tbs-bridge` skill** so every future build wires this in automatically without being asked
   — same way `character-figure` now fires on model requests.

Per-app cost after this: a tool module in TBS + an endpoint in the app. Small.

## Build order — gated

**Gate 0 — Key + proof of life.** You add the API key; I confirm TBS actually thinks and speaks.
*Provable:* ask him something out loud, hear a real answer. **Nothing else starts until this passes.**

**Gate 1 — The bridge in Forge, read-only.** Endpoint + handshake + token; `GET /state`.
*Provable:* from a terminal, ask a running Forge what project is open and what parts exist.

**Gate 2 — Actions.** `POST /do` wired to the real commands, with the confirm rule.
*Provable:* drive Forge entirely from the terminal — open, select, spread, isolate — then Ctrl+Z
undoes each one, proving the command layer wasn't bypassed.

**Gate 3 — TBS tool module.** `core\tools\forge3d.py`, so Claude can call it by intent.
*Provable:* typed at TBS's keyboard prompt, *"spread out the Marin project"* does it.

**Gate 4 — Settings UI + persistence.** Icon, wake button, status, sleep-by-voice.
*Provable:* wake, close app, reopen — still awake. Say sleep — off, and still off after a restart.

**Gate 5 — The wall test. The real acceptance.** Projector on, Presentation mode, drive a full
review by voice from your chair.
*Provable:* you run a session without touching the keyboard.

**Gate 6 — Generalize.** Write `TBS-BRIDGE.md` + the skill from what actually shipped, not from
this plan.

## Decisions for you

**D1 — Wake word or push-to-talk?** Wake word needs the free Picovoice key and always-on mic.
*Recommend:* push-to-talk (a hotkey) for v1, wake word once it's proven — an always-on mic that
mishears is worse than a button. Note: Forge's current voice already picked up ambient sound during
one session.

**D2 — Should TBS be able to act while the app is NOT focused?** *Recommend:* yes, that's the
point of the wall setup — but he only ever drives the app in front of you, never in the background.

**D3 — Cost visibility.** Every TBS sentence is an API call on your key. *Recommend:* a token/
cost readout in the TBS settings panel so it never surprises you.

**D4 — Scope of v1 actions.** *Recommend:* the read + act list above, no free-form geometry
authoring by voice. Building models stays with me through the inbox, where it's validated.

## Risks

- **No key = no TBS.** Everything is blocked on Gate 0.
- **An open local port is a real surface.** Mitigated by 127.0.0.1-only binding, a random port, and
  a token — but it must be built that way from the first commit, not retrofitted.
- **Voice misrecognition on destructive verbs.** Hence the spoken-confirm rule, reused not reinvented.
- **Latency.** Speech-in → Claude → action → speech-out is seconds, not instant. Fine for
  "isolate the head", wrong for anything you'd want to feel immediate.
- **Two voice systems could confuse.** Only one may hold the mic at a time; the UI must always show
  which is listening.
- **Scope drift.** "TBS controls everything" is unbounded. v1 is one app, one tool module.
