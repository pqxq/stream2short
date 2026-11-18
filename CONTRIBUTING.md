# Contributing to Stream2Short

Contributions are welcome across clip discovery, subtitle styling, FFmpeg filtergraphs, and streamer crop presets.

## Local Development

```bash
git clone https://github.com/pqxq/stream2short.git
cd stream2short
./start.sh
```

Before opening a pull request, run syntax checks, linting, and the zero-API-key smoke test:

```bash
.venv/bin/python -m py_compile app.py src/*.py tests/smoke_test.py
.venv/bin/ruff check .
.venv/bin/python tests/smoke_test.py --model tiny
```

## Adding Streamer Presets

To contribute normalized webcam crop coordinates for common layouts, add an entry to `presets.json` using values in the `0.0` to `1.0` range and open a pull request.
