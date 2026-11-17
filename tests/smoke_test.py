"""End-to-end smoke test for the zero-key multi-language synthetic pipeline.

Run from the repository root:

    .venv/bin/python tests/smoke_test.py
    .venv/bin/python tests/smoke_test.py --model tiny
    .venv/bin/python tests/smoke_test.py --languages en uk

The test generates a five-second 1280x720 testsrc/sine clip, runs faster-whisper
in each requested language, generates native metadata/subtitles, renders both
variants with the anti-duplicate pipeline, and verifies the 1080x1920 outputs.
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
    OUTPUT_ROOT,
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


def cleanup(languages: list[str]) -> None:
    (RAW_DIR / f"{SYNTH_ID}.mp4").unlink(missing_ok=True)
    for language in languages:
        (OUTPUT_ROOT / language / f"{SYNTH_ID}_{language}.mp4").unlink(missing_ok=True)
    for path in TEMP_DIR.glob(f"{SYNTH_ID}*"):
        path.unlink(missing_ok=True)


def verify_variant(language: str, result: dict[str, Any]) -> Path:
    outputs = result.get("outputs") or {}
    check(language in outputs, f"pipeline returned an {language.upper()} output")
    output_path = Path(outputs[language])
    check(output_path.exists(), f"{output_path.name} exists")
    check(
        output_path.stat().st_size > 10_000,
        f"{output_path.name} is non-trivial ({output_path.stat().st_size} bytes)",
    )

    probe = ffprobe(output_path)
    video = next(stream for stream in probe["streams"] if stream["codec_type"] == "video")
    audio = next(stream for stream in probe["streams"] if stream["codec_type"] == "audio")
    check(video["codec_name"] == "h264", f"{language.upper()} video codec is h264")
    check(video["pix_fmt"] == "yuv420p", f"{language.upper()} pixel format is yuv420p")
    check(video["width"] == 1080, f"{language.upper()} width is 1080")
    check(video["height"] == 1920, f"{language.upper()} height is 1920")
    check(audio["codec_name"] == "aac", f"{language.upper()} audio codec is aac")
    duration = float(probe["format"]["duration"])
    check(4.4 <= duration <= 5.3, f"{language.upper()} duration is ~5s ({duration:.3f}s)")

    variants = result.get("variants") or {}
    check(language in variants, f"{language.upper()} variant metadata exists")
    metadata = variants[language].get("metadata") or {}
    for field in ("hook", "title", "description", "cta", "hashtags"):
        check(bool(str(metadata.get(field) or "").strip()), f"{language.upper()} {field} is set")

    ass_path = Path(variants[language]["ass_path"])
    check(ass_path.exists(), f"{ass_path.name} exists")
    ass_text = ass_path.read_text(encoding="utf-8")
    check("PlayResX: 1080" in ass_text, f"{ass_path.name} PlayResX is 1080")
    check("PlayResY: 1920" in ass_text, f"{ass_path.name} PlayResY is 1920")
    check("[V4+ Styles]" in ass_text, f"{ass_path.name} contains ASS styles")
    check(",220,1" in ass_text, f"{ass_path.name} has safe MarginV")

    hook_file = TEMP_DIR / f"hook_{SYNTH_ID}_{language}.txt"
    check(hook_file.exists(), f"{hook_file.name} exists")
    check(bool(hook_file.read_text(encoding="utf-8").strip()), f"{hook_file.name} is non-empty")

    adjusted_ass = TEMP_DIR / f"{SYNTH_ID}_{language}_{language}_uniq.ass"
    check(adjusted_ass.exists(), f"{adjusted_ass.name} fingerprint timing copy exists")
    return output_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=os.getenv("SMOKE_WHISPER_MODEL", "tiny"),
        help="faster-whisper model to use for the smoke test (default: tiny)",
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=["en", "uk"],
        help="Language codes to render natively (default: en uk)",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Keep generated raw/output files after the test.",
    )
    args = parser.parse_args()
    languages = [str(item).strip().lower() for item in args.languages if str(item).strip()]
    if not languages:
        raise SystemExit("At least one language code is required.")
    if not args.keep:
        cleanup(languages)

    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        os.environ.pop(key, None)

    print("ShortStack synthetic smoke test")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Whisper model: {args.model}")
    print(f"Languages: {', '.join(code.upper() for code in languages)}")

    config = load_config()
    config["whisper_model"] = args.model
    results: dict[str, dict[str, Any]] = {}
    output_paths: dict[str, Path] = {}

    for language in languages:
        print(f"Running synthetic generation + native {language.upper()} render...")
        result = generate_synthetic_test_clip(
            config=config,
            progress=lambda message: print(f"  ..  {message}"),
            layout_mode="split",
            cam_box=(0.0, 0.0, 0.2, 0.25),
            language=language,
        )
        results[language] = result
        output_paths[language] = verify_variant(language, result)

    if len(output_paths) > 1:
        hashes = {md5(path) for path in output_paths.values()}
        check(len(hashes) == len(output_paths), "language outputs have distinct fingerprints")

    localization_file = TEMP_DIR / f"{SYNTH_ID}_localization.json"
    check(localization_file.exists(), "localization metadata was persisted")
    payload = json.loads(localization_file.read_text(encoding="utf-8"))
    check("variants" in payload, "localization payload contains variant metadata")

    print("SMOKE TEST PASSED")
    for language, path in output_paths.items():
        metadata = results[language]["variants"][language]["metadata"]
        print(f"  {language.upper()}: {path}")
        print(f"      Hook: {metadata['hook']}")
        print(f"      Title: {metadata['title']}")
    if not args.keep:
        cleanup(languages)
        print("Cleaned generated smoke-test artifacts.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, RuntimeError, FileNotFoundError, subprocess.CalledProcessError) as exc:
        print(f"SMOKE TEST FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
