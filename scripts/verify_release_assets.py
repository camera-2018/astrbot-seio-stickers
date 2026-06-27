#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from fractions import Fraction
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "dist" / "release"
TELEGRAM_MAX_BYTES = 256 * 1024
CUSTOM_GIF_MAX_BYTES = 500_000
CUSTOM_GIF_SIZE = 240


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def read_manifest(zip_file: zipfile.ZipFile) -> dict:
    try:
        with zip_file.open("manifest.json") as file:
            return json.loads(file.read().decode("utf-8"))
    except KeyError as exc:
        raise ValueError("manifest.json is missing") from exc


def ffprobe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,r_frame_rate,duration:format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def validate_zip(path: Path, deep: bool) -> None:
    log(f"checking {display_path(path)}")
    with zipfile.ZipFile(path) as zip_file:
        zip_file.testzip()
        manifest = read_manifest(zip_file)
        stickers = manifest.get("stickers")
        if not isinstance(stickers, list):
            raise ValueError(f"{path.name}: stickers must be a list")

        file_names = {
            info.filename
            for info in zip_file.infolist()
            if not info.is_dir() and info.filename.startswith("stickers/")
        }
        manifest_paths = {item["path"] for item in stickers}
        if file_names != manifest_paths:
            raise ValueError(f"{path.name}: manifest paths do not match ZIP contents")

        ids = [int(item["id"]) for item in stickers]
        if ids != list(range(1, len(ids) + 1)):
            raise ValueError(f"{path.name}: ids are not contiguous")

        if "telegram-webm" in path.name:
            log(f"{path.name}: checking Telegram size limits")
            for item in stickers:
                if item["size"] > TELEGRAM_MAX_BYTES:
                    raise ValueError(f"{path.name}: {item['path']} is over 256 KB")
            if deep:
                log(f"{path.name}: deep-checking Telegram WebM files")
                with tempfile.TemporaryDirectory(prefix="verify-telegram-webm-") as temp_dir:
                    temp_root = Path(temp_dir)
                    for index, item in enumerate(stickers, start=1):
                        output_path = temp_root / Path(item["path"]).name
                        output_path.write_bytes(zip_file.read(item["path"]))
                        metadata = ffprobe(output_path)
                        stream = metadata["streams"][0]
                        width = int(stream["width"])
                        height = int(stream["height"])
                        if stream["codec_name"] != "vp9":
                            raise ValueError(f"{item['path']}: codec is not VP9")
                        if width != 512 and height != 512:
                            raise ValueError(f"{item['path']}: one side must be 512 px")
                        if width > 512 or height > 512:
                            raise ValueError(f"{item['path']}: dimensions exceed 512 px")
                        frame_rate = Fraction(stream["r_frame_rate"])
                        if frame_rate > 30:
                            raise ValueError(f"{item['path']}: frame rate is over 30 FPS")
                        duration = float(stream.get("duration") or metadata["format"]["duration"])
                        if duration > 3.05:
                            raise ValueError(f"{item['path']}: duration is over 3 seconds")
                        if index == 1 or index == len(stickers) or index % 25 == 0:
                            log(f"{path.name}: deep checked {index}/{len(stickers)}")

        if "gif-240-500k" in path.name:
            log(f"{path.name}: checking 240x240 GIF size limits")
            for item in stickers:
                if item["size"] > CUSTOM_GIF_MAX_BYTES:
                    raise ValueError(f"{path.name}: {item['path']} is over 500 KB")
            if deep:
                log(f"{path.name}: deep-checking GIF dimensions")
                with tempfile.TemporaryDirectory(prefix="verify-gif-240-500k-") as temp_dir:
                    temp_root = Path(temp_dir)
                    for index, item in enumerate(stickers, start=1):
                        output_path = temp_root / Path(item["path"]).name
                        output_path.write_bytes(zip_file.read(item["path"]))
                        metadata = ffprobe(output_path)
                        stream = metadata["streams"][0]
                        width = int(stream["width"])
                        height = int(stream["height"])
                        if width != CUSTOM_GIF_SIZE or height != CUSTOM_GIF_SIZE:
                            raise ValueError(f"{item['path']}: dimensions are not 240x240")
                        if index == 1 or index == len(stickers) or index % 25 == 0:
                            log(f"{path.name}: deep checked {index}/{len(stickers)}")


def display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify generated release ZIP files.")
    parser.add_argument("output_dir", nargs="?", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="use ffprobe for Telegram WebM technical checks",
    )
    args = parser.parse_args()

    zip_paths = sorted(args.output_dir.glob("*.zip"))
    if not zip_paths:
        print(f"no ZIP files found in {args.output_dir}", file=sys.stderr)
        return 1

    for path in zip_paths:
        validate_zip(path, args.deep)
        log(f"ok {display_path(path)}")
    log("release asset verification finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
