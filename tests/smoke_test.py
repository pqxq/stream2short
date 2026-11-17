"""End-to-end smoke test for the zero-key synthetic clip pipeline.

Run from the repository root:

    .venv/bin/python tests/smoke_test.py
    .venv/bin/python tests/smoke_test.py --model small

The test generates a five-second 1280x720 testsrc/sine clip, runs faster-whisper,
translation fallbacks, subtitle generation and both FFmpeg renders, then asserts
that the EN and RU outputs are valid, distinct 1080x1920 H.264/AAC files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import (
    OUTPUT_EN_DIR,
    OUTPUT_RU_DIR,
    RAW_DIR,
    TEMP_DIR,
    generate_synthetic_test_clip,
)
from src.transcriber import load_config

SYNTH_ID = "synth_test"


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  OK  {message}")


def md5(path: Path) -> str:
    digest = hashlib.md5()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def cleanup() -> None:
    for path in (
        RAW_DIR / f"{SYNTH_ID}.mp4",
        OUTPUT_EN_DIR / f"{SYNTH_ID}_en.mp4",
        OUTPUT_RU_DIR / f"{SYNTH_ID}_ru.mp4",
        TEMP_DIR / f"{SYNTH_ID}_en.ass",
        TEMP_DIR / f"{SYNTH_ID}_ru.ass",
        TEMP_DIR / f"{SYNTH_ID}_en_ru.ass",
        TEMP_DIR / f"{SYNTH_ID}_ru_ru.ass",
        TEMP_DIR / f"{SYNTH_ID}_translation.json",
        TEMP_DIR / f"{SYNTH_ID}_audio.wav",
        TEMP_DIR / f"hook_{SYNTH_ID}_en.txt",
        TEMP_DIR / f"hook_{SYNTH_ID}_ru.txt",
        TEMP_DIR / "synth_test_preview_frame.png",
    ):
        path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("SMOKE_WHISPER_MODEL", "tiny"),
        help="faster-whisper model to use for the smoke test (default: tiny)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep generated raw/output files after the test.",
    )
    args = parser.parse_args()

    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        os.environ.pop(key, None)

    print("GTA6 Clip Factory synthetic smoke test")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Whisper model: {args.model}")
    cleanup()

    config = load_config()
    config["whisper_model"] = args.model

    print("Running synthetic generation + transcription + EN/RU render...")
    result = generate_synthetic_test_clip(
        config=config,
        progress=lambda message: print(f"  ..  {message}"),
        layout_mode="split",
        cam_box=(0.0, 0.0, 0.2, 0.25),
    )

    en_path = Path(result["en"])
    ru_path = Path(result["ru"])
    print("Verifying output files...")
    for path in (en_path, ru_path):
        check(path.exists(), f"{path.name} exists")
        check(path.stat().st_size > 10_000, f"{path.name} is non-trivial ({path.stat().st_size} bytes)")

    en_probe = ffprobe(en_path)
    ru_probe = ffprobe(ru_path)
    for label, probe in (("EN", en_probe), ("RU", ru_probe)):
        video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
        audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
        check(video["codec_name"] == "h264", f"{label} video codec is h264")
        check(video["pix_fmt"] == "yuv420p", f"{label} pixel format is yuv420p")
        check(video["width"] == 1080, f"{label} width is 1080")
        check(video["height"] == 1920, f"{label} height is 1920")
        check(audio["codec_name"] == "aac", f"{label} audio codec is aac")

    en_duration = float(en_probe["format"]["duration"])
    ru_duration = float(ru_probe["format"]["duration"])
    check(4.7 <= en_duration <= 5.3, f"EN duration is ~5s ({en_duration:.3f}s)")
    check(4.5 <= ru_duration < en_duration, f"RU duration is shorter at 1.02x ({ru_duration:.3f}s)")
    check(md5(en_path) != md5(ru_path), "EN and RU files have different fingerprints")

    ass_en = TEMP_DIR / f"{SYNTH_ID}_en.ass"
    ass_ru = TEMP_DIR / f"{SYNTH_ID}_ru.ass"
    for ass_path in (ass_en, ass_ru):
        text = ass_path.read_text(encoding="utf-8")
        check(ass_path.exists(), f"{ass_path.name} exists")
        check("PlayResX: 1080" in text, f"{ass_path.name} PlayResX is 1080")
        check("PlayResY: 1920" in text, f"{ass_path.name} PlayResY is 1920")
        check("[V4+ Styles]" in text, f"{ass_path.name} contains ASS styles")
        check("DejaVu Sans" in text, f"{ass_path.name} uses DejaVu Sans")
        check("MarginV=220" in text or ",220,1" in text, f"{ass_path.name} has safe MarginV")

    for hook_path in (TEMP_DIR / f"hook_{SYNTH_ID}_en.txt", TEMP_DIR / f"hook_{SYNTH_ID}_ru.txt"):
        check(hook_path.exists(), f"{hook_path.name} exists")
        check(bool(hook_path.read_text(encoding="utf-8").strip()), f"{hook_path.name} is non-empty")

    print("SMOKE TEST PASSED")
    print(f"  EN: {en_path} ({en_duration:.3f}s)")
    print(f"  RU: {ru_path} ({ru_duration:.3f}s)")
    print(f"  Hooks: {result['hooks']['en_hook']} / {result['hooks']['ru_hook']}")
    print(f"  Hook source: {result.get('hook_source')}")
    if not args.keep:
        cleanup()
        print("Cleaned generated smoke-test artifacts.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
