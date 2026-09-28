# it-takes-me

GPT-6 Astra plays one half of _It Takes Two_ while you play the other.

The agent talks to the model through the **Codex app-server** using the official
`openai-codex` Python SDK, so it runs on your ChatGPT/Codex subscription (no API key).
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

Hyperparameters (model, effort, character, budgets) are constants at the top of `run.py`.
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

The Windows backend captures the screen with `mss` and drives a virtual Xbox pad with
`vgamepad`.
