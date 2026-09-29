# it-takes-me

GPT-6 Astra plays one half of _It Takes Two_ while you play the other.

The default harness talks to the model through the **Codex app-server** using the official
`openai-codex` Python SDK, so it runs on your ChatGPT/Codex subscription (no API key). An optional
**Responses API** harness runs the same player and tools with API models.
Game actions are exposed to the model as dynamic tools on one persistent thread. The model
acts in **chunks**: one `act` call plans 0.5-3 s of play as a list of skills (`run`, `jump`,
`double_jump`, `dash`, ...) or raw pad segments, which `it_takes_me/game/chunks.py` executes
locally with exact timing before returning the resulting frame(s).

## Setup

```bash
uv sync
```

Uses your existing Codex login (`~/.codex/auth.json`).

## Run

Hyperparameters (runtime, model, effort, character, budgets) are constants at the top of `run.py`.
On the Windows gaming PC with the game on screen:

```bash
uv run python run.py
```

Start the pad first and leave it open for the whole session:

```bash
uv run python pad.py   # plugs in one virtual controller + shows it on screen
uv run python run.py   # connects to that pad; restart freely, the controller stays the same
```

A virtual controller only lives as long as the process that created it, so `pad.py` owns it and
`run.py` streams its inputs there over localhost. The window shows what the agent is pressing,
and you can click it to add your own input (it never takes focus from the game). Without
`pad.py`, `run.py` plugs in its own controller for that run.

Every session is recorded to `runs/<timestamp>/` (frames + `events.jsonl`).

Model observations are cropped to the controlled character's half and resized separately from the
recording. `frames/` contains the original full-resolution captures; `observations/` contains the
exact JPEGs sent to the model. Navigation uses a 512 px observation by default. The
`look_at_screen` tool can request a 1,536 px high-detail observation for small prompts or objects.

## Responses API

Set `RUNTIME = "responses"` in `run.py`, choose the model and effort, and provide an API key:

```powershell
$env:OPENAI_API_KEY = "..."
uv run python run.py
```

The Responses runtime uses the same game tools and recordings, chains calls with
`previous_response_id`, records the full usage breakdown, and enables server-side compaction. Codex
remains the default runtime.

## Replay and compare

Run recorded frames through either harness without starting the game or waiting for controller
timings:

```bash
uv run python replay.py runs/20260927-232307 --runtime codex --model gpt-6-astra --turns 2
uv run python replay.py runs/20260927-232307 --runtime responses --model gpt-6-sol --turns 2
```

Compare existing or replay runs:

```bash
uv run python summarize.py runs/20260927-232307 runs/<new-run>
```

The summary reports turn and decision latency, action/look counts, observations, errors, cumulative
tokens, and tokens per action. The implementation sequence and acceptance checks are in
[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md).

The Windows backend captures the screen with `mss` and drives a virtual Xbox pad with
`vgamepad`.
