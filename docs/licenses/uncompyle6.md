# uncompyle6 runtime tool

- Pinned version: `3.9.3` (see `pyproject.toml` and `uv.lock`).
- Upstream: <https://github.com/rocky/python-uncompyle6>
- License: GNU GPL v3.
- Integration: invoked as a separate, resource-limited subprocess; game bytecode is never
  imported or executed.

The Python package and its license metadata are included by `uv sync` in the container image.
Redistributors of the image must preserve the package's GPL notices and provide the corresponding
source in accordance with that license. `game-downloader` records the exact tool/version in every
GameSnapshot that contains `.pyc → .py` representations.
