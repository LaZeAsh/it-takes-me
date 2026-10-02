"""Luna + Jev player: a separate technique from the Sol/Codex player in `it_takes_me.inference`.

Luna (a vision model) only describes each frame as a structured scene. Jev (TypeSafe's decision
model, via OpenRouter's Decisions API) chooses the next controller input from the same skills Sol
uses (`run`, `jump`, ...), one typed question per skill field. Code turns Jev's answers into a step
and runs it through the shared action runner in `it_takes_me.game.chunks`.
"""
