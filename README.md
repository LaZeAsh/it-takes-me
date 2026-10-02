# it-takes-me

GPT-6.1 Sol plays one half of _It Takes Two_ while you play the other, using low reasoning effort
by default.

The default harness talks to the model through the **Codex app-server** using the official
`openai-codex` Python SDK, so it runs on your ChatGPT/Codex subscription (no API key). An optional
**Responses API** harness runs the same player and tools with API models.
Game actions are exposed to the model as dynamic tools on one persistent thread. The model
acts in **chunks**: one `act` call plans up to 10 s of play as a list of skills (`run`, `jump`,
`double_jump`, `dash`, ...) or raw pad segments, which `it_takes_me/game/chunks.py` executes
locally with exact timing before returning the resulting frame(s).

When the model loses track of its partner, it can use `locate_partner` in an action chunk to
click the right stick and reveal the partner's location, then plan its route from the returned frame.

## On-screen markers and tasks

The prompt tells the model to treat the game's on-screen icons as its task list, in priority
order: yellow circles on objects (a ring around a white dot, or a `Y` when in range), then
tutorial prompts such as `X Dash`, then the distant white objective hexagon, and never the
partner's colored location dot. There is no walkthrough: the model decides what to do next
from what it sees.

The model works on one task at a time. Every `act` names its `task`; changing it is refused (with
nothing pressed) unless the call also marks the previous task `done` or `blocked`. Each turn's
message repeats the current task. The model defines each task from the current frame, since play
can resume from any checkpoint. A chunk that stops
on a scene cut (respawn, checkpoint reload, cutscene) clears the task so it is defined again.

## Action timing

The model chooses `ms` (total step duration) for every skill. Button skills also accept `hold_ms`:
hold the button, then release it while continuing movement for the remaining time. `double_jump`
and `jump_dash` accept `gap_ms`, the release time between the two presses. Each press lasts
`hold_ms`, so the total must cover both holds and the gap. All durations are positive integer
milliseconds; omitted timings preserve the original skill defaults. `interact`, `ability`, and
`skip_cutscene` hold for the whole step by default. `press` can tap any controller button.

Movement is camera-relative. Besides the 8 `dir` words, any moving step accepts `heading` (degrees:
0 forward, 90 right, 180 back) for exact angles and `speed` (0.1-1, default 1) for how hard the
stick is pushed; `look` accepts `look_speed` for small camera turns. The prompt asks the model to
walk ledges forward at speed 0.3-0.5 after turning the camera to face along them, because moving
sideways makes the camera swing and curves the path off the edge. Jumps stay at full speed.

A directional jump, double jump, jump-dash, or dash must keep steering until it lands: if the next
step is not another directional move (or a ground pound or grapple), its `ms` must cover the skill's
default airtime, or `act` rejects the chunk before pressing anything. Releasing the stick mid-air
(a `wait` or the chunk ending right after a short jump) made the character drop short.

`skip_cutscene` holds B with neutral sticks to skip a cutscene. Its default duration is 2,000 ms;
the model can choose a longer `ms` if needed and inspects the returned frame before moving again.

For example, this jumps with a 100 ms press and moves forward for 800 ms total:

```json
{"intent": "jump across the gap", "steps": [{"skill": "jump", "dir": "forward", "ms": 800, "hold_ms": 100}]}
```

For a clear stretch of traversal, the model can request a single 7,000 ms run rather than many
short runs separated by inference. Runs longer than 3,000 ms automatically check low-resolution
snapshots locally for a stuck view or abrupt scene change. A check stops the chunk, skips later
steps, releases the controller, and reports why. These checks cannot detect arrival at a target
or every hazard, so the model should shorten runs near edges and destinations. The pad releases
at the end of every chunk unless the model sets `keep_moving` on a chunk that ends with a
directional `run`. Then that run continues in the background while the model plans, until its next
tool call or turn, a stuck view, a scene cut, or `CARRY_MAX_MS` (6 s); the next result says how long
it kept moving. The prompt asks for 4-8 s chunks when travelling, since every call costs seconds of
planning time standing still. `MAX_CHUNK_MS` in `run.py`
sets the overall chunk limit.

## Luna + Jev player (separate technique)

`run_jev.py` runs a second, independent player: Luna (a vision model, on your Codex subscription
in Fast mode) describes each frame and Jev (TypeSafe's decision model, via OpenRouter) picks the
next skill from the same skill set. It needs `OPENROUTER_API_KEY` in `.env` for Jev and records to
`runs/jev/`. See `it_takes_me/jev/README.md`.

## Setup

```bash
uv sync
```

Uses your existing Codex login (`~/.codex/auth.json`).

The Python SDK installs the bundled Codex runtime used by this project. Run `uv sync` after
dependency updates; updating a separately installed global Codex CLI does not update this runtime.

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
`look_at_screen` tool only returns a 1,536 px high-detail observation for small prompts or objects,
at most once between actions; turns and `act` already return fresh navigation frames.

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
uv run python replay.py runs/20260927-232307 --runtime codex --turns 2
uv run python replay.py runs/20260927-232307 --runtime responses --turns 2
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
