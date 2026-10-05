# it-takes-me

GPT-6.1 Sol plays one half of _It Takes Two_ while you play the other, using low reasoning effort
by default.

The default harness talks to the model through the **Codex app-server** using the official
`openai-codex` Python SDK, so it runs on your ChatGPT/Codex subscription (no API key). An optional
**Responses API** harness runs the same player and tools with API models.
Game actions are exposed to the model as dynamic tools on one persistent thread. The model
acts in **chunks**: one `act` call plans up to 10 s of play as a line of skills (`run`, `jump`,
`double_jump`, `dash`, ...) or raw pad segments, which `it_takes_me/game/chunks.py` executes
locally with exact timing before returning the resulting frame(s).

## Inference latency

Every second the model spends planning, the character stands still, so the Codex thread is
stripped to what the game needs (`it_takes_me/sol/runtime.py`):

- **Direct tool calls.** Codex's catalog marks current models `code_mode_only`, which puts
  dynamic tools behind a JavaScript `exec` tool: the model wrote ~70 tokens of script on every
  call to unwrap the result, and the first call printed its frame as base64 text (~27k tokens
  kept for the session). The runtime writes a copy of `~/.codex/models_cache.json` with
  `tool_mode: "direct"` and passes it as `model_catalog_json`.
- **No coding-agent context.** A short `baseInstructions` replaces Codex's system prompt, and
  `-c` overrides turn off the features, MCP servers from your `config.toml`, and context sections
  (permissions, apps, environment) a game player never uses.
- **Compact steps.** `act` takes its steps as one line (`run f 1200; jump f; run f 300`), parsed
  by `it_takes_me/game/shorthand.py`, and `task` is sent only when it changes.

Together these took the first request from ~24.5k to ~8.5k input tokens, output from 120-160 to
20-65 tokens per call, and the median act-to-act time in replay to about 2.5 s.

When the model loses track of its partner, it can use `locate_partner` in an action chunk to
click the right stick and reveal the partner's location, then plan its route from the returned frame.

## On-screen markers and tasks

The prompt tells the model to treat the game's on-screen icons as its task list, in priority
order: yellow circles on objects (a ring around a white dot, or a `Y` when in range), then
tutorial prompts such as `X Dash`, then the distant white objective hexagon, and never the
partner's colored location dot. There is no walkthrough: the model decides what to do next
from what it sees.

The model works on one task at a time. `act` sets `task` when there is none or it changes, and
carries it over otherwise; changing it is refused (with nothing pressed) unless the call also
marks the previous task `done` or `blocked`. Each turn's
message repeats the current task. The model defines each task from the current frame, since play
can resume from any checkpoint. A chunk that stops
on a scene cut (respawn, checkpoint reload, cutscene) clears the task so it is defined again.

## Action timing

The model chooses `ms` (total step duration, a bare number) for every skill. Button skills also
accept a hold (`h250`): hold the button, then release it while continuing movement for the
remaining time. `double_jump` and `jump_dash` accept a gap (`g250`), the release time between the
two presses. Each press lasts the hold, so the total must cover both holds and the gap. All durations are positive integer
milliseconds; omitted timings preserve the original skill defaults. `interact`, `ability`, and
`skip_cutscene` hold for the whole step by default. `press` can tap any controller button.

Movement is camera-relative. Besides the 8 directions (`f b l r fl fr bl br`), any moving step
accepts a heading (`@-22`; degrees, 0 forward, 90 right, 180 back) for exact angles and a speed
(`s0.4`; 0.1-1, default 1) for how hard the stick is pushed; on `look` the same `s` sets the
camera turn rate. The prompt asks the model to walk ledges forward at speed 0.3-0.5 after turning the camera to face along them, because moving
sideways makes the camera swing and curves the path off the edge. Jumps stay at full speed.

A directional jump, double jump, jump-dash, or dash must keep steering until it lands: if the next
step is not another directional move (or a ground pound or grapple), its `ms` must cover the skill's
default airtime, or `act` rejects the chunk before pressing anything. Releasing the stick mid-air
(the chunk ending right after a short jump) made the character drop short.

`skip_cutscene` holds B with neutral sticks to skip a cutscene. Its default duration is 2,000 ms;
the model can choose a longer `ms` if needed and inspects the returned frame before moving again.

For example, this runs up to a gap, then jumps with a 150 ms press and keeps moving forward for
800 ms total:

```json
{"steps": "run f 600; jump f 800 h150"}
```

For a clear stretch of traversal, the model can request a single 7,000 ms run rather than many
short runs separated by inference. Runs longer than 3,000 ms automatically check low-resolution
snapshots locally for a stuck view or abrupt scene change. A check stops the chunk, skips later
steps, releases the controller, and reports why. These checks cannot detect arrival at a target
or every hazard, so the model should shorten runs near edges and destinations. A repeat block
(`5x(jump l; jump r)`) plays a block of steps several times in a row,
for rhythmic sequences such as hopping between two walls. `MAX_CHUNK_MS` in `run.py` sets the
overall chunk limit, counted after repeats are expanded.

There is no `wait` skill, `run` must have a direction, and `raw` must press something. Steps run
back to back like a player who never lets go of the controller, and the model already stands
still while it plans each call. In recorded runs, waits were mostly whole chunks spent just
looking (~3.5 s of planning for ~0.3 s of play).

Chunks are **pipelined**. A background pad thread (`PadThread` in `it_takes_me/sol/pad_thread.py`)
owns the controller. For a chunk at least twice the planning time (~7 s), `act` hands its result back
about one planning time before the chunk ends: the frame shows that moment and the result says which steps are still to
play. The model plans its next chunk while those steps play, and the next chunk queues and starts
the instant the current one ends, with no neutral gap. The lead time starts at 3.5 s and follows
the measured gap between one `act` returning and the next arriving (bounded to 1-6 s). Turns can
start mid-chunk too: the turn message says how much is left. If a chunk is stopped early by a
check, any chunk planned before the model saw that is not played (nothing is pressed); the model
gets the reason and a fresh frame and plans again. Shorter chunks return at the end: an early
frame from a 1-2 s chunk showed too little, and Sol spent every other call on a `wait` to look.
The model cannot ask to wait for the end (it chose that on every call, which turned pipelining
off); only `observe: "keyframes"`, for diagnosing failures, waits for the end of a long chunk.

When no chunk is queued, the pad releases at the end of a chunk unless the model sets
`keep_moving` on a chunk that ends with a directional `run`. Then that run continues until the
next chunk starts, a stuck view, a scene cut, or `CARRY_MAX_MS` (6 s); the next result says how long
it kept moving. Each chunk is logged as a `chunk` event (planned, played, idle and carried ms), and
`summarize.py` reports the share of time spent moving.

Every model call costs ~3.5 s of standing still, so `act` also discourages calls that play almost
nothing. A chunk of only camera `look` steps is refused unless it scans with
`observe: "keyframes"` or carries a line to say, and each result reports the share of time spent
moving ("stood still 3,300 ms planning, then played 850 ms"). Talking to the partner is the
optional `say` field of `act`, not a separate tool, so it rides along with a move instead of
costing its own call.

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
exact JPEGs sent to the model. The model's JPEG is made straight from the screen grab, and the
full-resolution PNG is written on a background thread: encoding a 4K PNG first cost ~0.4 s per
call that the model waited through. Navigation uses a 512 px observation by default. The
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
