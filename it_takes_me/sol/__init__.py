"""Sol player: GPT-6.1 Sol (or Astra) plays through the Codex app-server or the Responses API.

The model acts in chunks through one `act` dynamic tool on a persistent thread. It shares the game
layer (`it_takes_me.game`), frame cropping (`vision.py`), and recording (`recording.py`) with the
Luna + Jev player in `it_takes_me.jev`.
"""
