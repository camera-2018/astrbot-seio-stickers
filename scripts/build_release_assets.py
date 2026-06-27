#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STICKERS_DIR = ROOT / "stickers"
DEFAULT_OUTPUT_DIR = ROOT / "dist" / "release"
DEFAULT_WORK_DIR = ROOT / "dist" / "work"
STICKER_RE = re.compile(r"^(?P<id>\d{3})_(?P<name>.+)\.gif$")
PROJECT_NAME = "astrbot-seio-stickers"
TELEGRAM_MAX_BYTES = 256 * 1024
CUSTOM_GIF_MAX_BYTES = 500_000
CUSTOM_GIF_SIZE = 240


@dataclass(frozen=True)
class Target:
    key: str
    extension: str
    zip_name: str
    description: str


TARGETS = {
    "gif": Target(
        key="gif",
        extension=".gif",
        zip_name=f"{PROJECT_NAME}-gif.zip",
        description="Original GIF files.",
    ),
    "gif-optimized": Target(
        key="gif-optimized",
        extension=".gif",
        zip_name=f"{PROJECT_NAME}-gif-optimized.zip",
        description="Compressed GIF files.",
    ),
    "gif-240-500k": Target(
        key="gif-240-500k",
        extension=".gif",
        zip_name=f"{PROJECT_NAME}-gif-240-500k.zip",
        description="240x240 GIF files capped at 500 KB each.",
    ),
    "webm": Target(
        key="webm",
        extension=".webm",
        zip_name=f"{PROJECT_NAME}-webm.zip",
        description="VP9 WebM files for web usage.",
    ),
    "webp": Target(
        key="webp",
        extension=".webp",
        zip_name=f"{PROJECT_NAME}-webp.zip",
        description="Animated WebP files.",
    ),
    "telegram-webm": Target(
        key="telegram-webm",
        extension=".webm",
        zip_name=f"{PROJECT_NAME}-telegram-webm.zip",
        description="Telegram-sized VP9 WebM files.",
    ),
}


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] {message}", flush=True)


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        cmd = " ".join(command)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        raise RuntimeError(f"command failed: {cmd}")


def tool_path(name: str) -> str | None:
    return shutil.which(name)


def ffmpeg_supports_webp() -> bool:
    ffmpeg = tool_path("ffmpeg")
    if not ffmpeg:
        return False
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return "libwebp_anim" in result.stdout or " libwebp " in result.stdout


def require_tools(targets: set[str]) -> None:
    missing = []
    needs_ffmpeg = {"gif-optimized", "gif-240-500k", "webm", "telegram-webm"} & targets
    if needs_ffmpeg and not tool_path("ffmpeg"):
        missing.append("ffmpeg")
    if "webp" in targets and not tool_path("gif2webp") and not ffmpeg_supports_webp():
        missing.append("gif2webp or FFmpeg with libwebp_anim")

    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"missing required tool(s): {joined}")

    log("tool check passed")


def sticker_inputs(limit: int | None) -> list[Path]:
    paths = sorted(
        STICKERS_DIR.glob("*.gif"),
        key=lambda path: int(path.name.split("_", 1)[0]),
    )
    if limit is not None:
        paths = paths[:limit]
    if not paths:
        raise RuntimeError(f"no GIF files found in {STICKERS_DIR}")
    return paths


def sticker_info(path: Path) -> tuple[str, str]:
    match = STICKER_RE.fullmatch(path.name)
    if not match:
        raise ValueError(f"invalid sticker filename: {path.name}")
    return match.group("id"), match.group("name")


def output_name(input_path: Path, extension: str) -> str:
    sticker_id, name = sticker_info(input_path)
    return f"{sticker_id}_{name}{extension}"


def ffmpeg_gif_optimized(input_path: Path, output_path: Path) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-loop",
            "0",
            "-filter_complex",
            "fps=15,split[s0][s1];[s0]palettegen=max_colors=160[p];"
            "[s1][p]paletteuse=dither=bayer:bayer_scale=5",
            str(output_path),
        ]
    )


def convert_gif_optimized(input_path: Path, output_path: Path) -> None:
    gifsicle = tool_path("gifsicle")
    if gifsicle:
        run(
            [
                gifsicle,
                "-O3",
                "--lossy=35",
                "--colors",
                "192",
                str(input_path),
                "-o",
                str(output_path),
            ]
        )
        return

    ffmpeg_gif_optimized(input_path, output_path)


def custom_gif_filter(frame_rate: int | None, colors: int) -> str:
    filters = []
    if frame_rate is not None:
        filters.append(f"fps={frame_rate}")
    filters.extend(
        [
            f"scale={CUSTOM_GIF_SIZE}:{CUSTOM_GIF_SIZE}:"
            "force_original_aspect_ratio=decrease:flags=lanczos",
            f"pad={CUSTOM_GIF_SIZE}:{CUSTOM_GIF_SIZE}:(ow-iw)/2:(oh-ih)/2:color=0x00000000",
            "split[s0][s1]",
        ]
    )
    return (
        ",".join(filters)
        + f";[s0]palettegen=max_colors={colors}:reserve_transparent=on[p];"
        + "[s1][p]paletteuse=dither=bayer:bayer_scale=5:alpha_threshold=128"
    )


def ffmpeg_custom_gif(
    input_path: Path,
    output_path: Path,
    frame_rate: int | None,
    colors: int,
) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-loop",
            "0",
            "-filter_complex",
            custom_gif_filter(frame_rate, colors),
            str(output_path),
        ]
    )


def gifsicle_optimize(input_path: Path, output_path: Path, lossy: int | None = None) -> None:
    gifsicle = tool_path("gifsicle")
    if not gifsicle:
        shutil.copy2(input_path, output_path)
        return

    command = [gifsicle, "-O3"]
    if lossy is not None:
        command.append(f"--lossy={lossy}")
    command.extend([str(input_path), "-o", str(output_path)])
    run(command)


def convert_custom_gif(input_path: Path, output_path: Path) -> None:
    candidates: list[tuple[int | None, int]] = [
        (None, 256),
        (None, 192),
        (None, 128),
        (None, 96),
        (None, 64),
        (None, 48),
        (None, 32),
        (None, 24),
        (None, 16),
        (30, 128),
        (30, 96),
        (30, 64),
        (30, 48),
        (30, 32),
        (24, 96),
        (24, 64),
        (24, 48),
        (24, 32),
        (20, 64),
        (20, 48),
        (20, 32),
        (15, 48),
        (15, 32),
        (15, 24),
        (15, 16),
        (12, 32),
        (12, 24),
        (12, 16),
        (10, 24),
        (10, 16),
        (8, 24),
        (8, 16),
        (6, 16),
    ]

    best_path: Path | None = None
    best_size: int | None = None

    with tempfile.TemporaryDirectory(prefix="gif-240-500k-", dir=output_path.parent) as temp_dir:
        temp_root = Path(temp_dir)
        for frame_rate, colors in candidates:
            candidate_path = temp_root / f"{output_path.stem}.{frame_rate or 'keep'}fps.{colors}c.gif"
            ffmpeg_custom_gif(input_path, candidate_path, frame_rate, colors)

            optimized_path = temp_root / f"{candidate_path.stem}.opt.gif"
            gifsicle_optimize(candidate_path, optimized_path)
            size = optimized_path.stat().st_size

            if best_size is None or size < best_size:
                best_path = optimized_path
                best_size = size
            if size <= CUSTOM_GIF_MAX_BYTES:
                shutil.copy2(optimized_path, output_path)
                return

        assert best_path is not None
        shutil.copy2(best_path, output_path)

    if output_path.stat().st_size > CUSTOM_GIF_MAX_BYTES:
        raise RuntimeError(
            f"{output_path.name} is {output_path.stat().st_size} bytes, "
            f"over custom GIF limit {CUSTOM_GIF_MAX_BYTES}"
        )


def convert_webm(input_path: Path, output_path: Path) -> None:
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-an",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "32",
            "-deadline",
            "good",
            "-cpu-used",
            "4",
            "-row-mt",
            "1",
            "-pix_fmt",
            "yuva420p",
            "-auto-alt-ref",
            "0",
            str(output_path),
        ]
    )


def convert_webp(input_path: Path, output_path: Path) -> None:
    gif2webp = tool_path("gif2webp")
    if gif2webp:
        run(
            [
                gif2webp,
                "-lossy",
                "-q",
                "75",
                "-m",
                "6",
                "-mt",
                str(input_path),
                "-o",
                str(output_path),
            ]
        )
        return

    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-loop",
            "0",
            "-c:v",
            "libwebp_anim",
            "-lossless",
            "0",
            "-q:v",
            "75",
            "-compression_level",
            "6",
            str(output_path),
        ]
    )


def telegram_filter(content_box: int) -> str:
    return (
        "trim=duration=3,setpts=PTS-STARTPTS,fps=30,"
        f"scale={content_box}:{content_box}:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=0x00000000,"
        "format=yuva420p"
    )


def convert_telegram_webm(input_path: Path, output_path: Path, strict: bool) -> None:
    candidates: list[tuple[int, int]] = []
    for content_box in (512, 480, 448, 416, 384, 352):
        for crf in (36, 40, 44, 48, 52, 56, 60, 63):
            candidates.append((content_box, crf))

    best_path: Path | None = None
    best_size: int | None = None

    with tempfile.TemporaryDirectory(prefix="telegram-webm-", dir=output_path.parent) as temp_dir:
        temp_root = Path(temp_dir)
        for index, (content_box, crf) in enumerate(candidates, start=1):
            candidate_path = temp_root / f"{output_path.stem}.{index}.webm"
            run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(input_path),
                    "-an",
                    "-filter:v",
                    telegram_filter(content_box),
                    "-c:v",
                    "libvpx-vp9",
                    "-b:v",
                    "0",
                    "-crf",
                    str(crf),
                    "-deadline",
                    "good",
                    "-cpu-used",
                    "4",
                    "-row-mt",
                    "1",
                    "-auto-alt-ref",
                    "0",
                    "-t",
                    "3",
                    str(candidate_path),
                ]
            )
            size = candidate_path.stat().st_size
            if best_size is None or size < best_size:
                best_path = candidate_path
                best_size = size
            if size <= TELEGRAM_MAX_BYTES:
                shutil.copy2(candidate_path, output_path)
                return

        assert best_path is not None
        shutil.copy2(best_path, output_path)

    if strict and output_path.stat().st_size > TELEGRAM_MAX_BYTES:
        raise RuntimeError(
            f"{output_path.name} is {output_path.stat().st_size} bytes, "
            f"over Telegram limit {TELEGRAM_MAX_BYTES}"
        )


def convert_one(
    target: Target,
    input_path: Path,
    output_dir: Path,
    telegram_strict: bool,
) -> dict:
    stickers_dir = output_dir / "stickers"
    stickers_dir.mkdir(parents=True, exist_ok=True)
    output_path = stickers_dir / output_name(input_path, target.extension)

    if target.key == "gif":
        shutil.copy2(input_path, output_path)
    elif target.key == "gif-optimized":
        convert_gif_optimized(input_path, output_path)
    elif target.key == "gif-240-500k":
        convert_custom_gif(input_path, output_path)
    elif target.key == "webm":
        convert_webm(input_path, output_path)
    elif target.key == "webp":
        convert_webp(input_path, output_path)
    elif target.key == "telegram-webm":
        convert_telegram_webm(input_path, output_path, telegram_strict)
    else:
        raise ValueError(f"unknown target: {target.key}")

    sticker_id, name = sticker_info(input_path)
    return {
        "id": sticker_id,
        "name": name,
        "path": output_path.relative_to(output_dir).as_posix(),
        "size": output_path.stat().st_size,
        "sha256": sha256_file(output_path),
    }


def write_manifest(target: Target, output_dir: Path, stickers: list[dict]) -> None:
    manifest = {
        "name": PROJECT_NAME,
        "display_name": "AstrBot seio娘表情包",
        "version": "2026.06",
        "format": target.key,
        "description": target.description,
        "stickers": stickers,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    log(f"{target.key}: wrote manifest.json ({len(stickers)} items)")


def zip_dir(input_dir: Path, zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    log(f"zipping {input_dir.name} -> {display_path(zip_path)}")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as zip_file:
        for path in sorted(input_dir.rglob("*")):
            if path.is_file():
                zip_file.write(path, path.relative_to(input_dir).as_posix())
    log(f"zip ready {display_path(zip_path)} ({human_size(zip_path.stat().st_size)})")


def display_path(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def build_target(
    target: Target,
    inputs: list[Path],
    output_dir: Path,
    work_dir: Path,
    jobs: int,
    telegram_strict: bool,
) -> dict:
    started = time.perf_counter()
    target_dir = work_dir / target.key
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)

    log(f"{target.key}: converting {len(inputs)} files with {jobs} job(s)")
    stickers: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(convert_one, target, input_path, target_dir, telegram_strict)
            for input_path in inputs
        ]
        total = len(futures)
        for future in concurrent.futures.as_completed(futures):
            item = future.result()
            stickers.append(item)
            done = len(stickers)
            if done == 1 or done == total or done % 10 == 0:
                log(
                    f"{target.key}: converted {done}/{total} "
                    f"(last {item['id']}, {human_size(item['size'])})"
                )

    stickers.sort(key=lambda item: int(item["id"]))
    write_manifest(target, target_dir, stickers)

    zip_path = output_dir / target.zip_name
    zip_dir(target_dir, zip_path)
    elapsed = time.perf_counter() - started
    log(f"{target.key}: done in {elapsed:.1f}s")

    return {
        "target": target.key,
        "zip": display_path(zip_path),
        "files": len(stickers),
        "size": zip_path.stat().st_size,
        "sha256": sha256_file(zip_path),
    }


def parse_targets(raw_targets: list[str]) -> list[Target]:
    keys: list[str]
    if "all" in raw_targets:
        keys = list(TARGETS)
    else:
        keys = raw_targets

    unknown = sorted(set(keys) - set(TARGETS))
    if unknown:
        raise ValueError(f"unknown target(s): {', '.join(unknown)}")
    return [TARGETS[key] for key in keys]


def main() -> int:
    parser = argparse.ArgumentParser(description="Build release ZIP files.")
    parser.add_argument(
        "--target",
        action="append",
        choices=["all", *TARGETS.keys()],
        default=None,
        help="target to build; may be repeated; defaults to all",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--jobs", type=int, default=max(1, min(4, os.cpu_count() or 2)))
    parser.add_argument("--limit", type=int, default=None, help="convert only the first N files")
    parser.add_argument("--clean", action="store_true", help="remove output/work dirs first")
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="keep generated folders used to create ZIP files",
    )
    parser.add_argument(
        "--no-telegram-strict",
        action="store_true",
        help="do not fail if a Telegram WebM remains over 256 KB",
    )
    args = parser.parse_args()

    targets = parse_targets(args.target or ["all"])
    target_keys = {target.key for target in targets}
    log(f"selected targets: {', '.join(target.key for target in targets)}")
    require_tools(target_keys)

    output_dir = args.output_dir.resolve()
    work_dir = args.work_dir.resolve()
    if args.clean:
        log("cleaning output and work directories")
        shutil.rmtree(output_dir, ignore_errors=True)
        shutil.rmtree(work_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    log(f"output dir: {display_path(output_dir)}")
    log(f"work dir: {display_path(work_dir)}")

    inputs = sticker_inputs(args.limit)
    log(f"loaded {len(inputs)} GIF file(s)")
    results = []
    for target in targets:
        log(f"building {target.key}")
        results.append(
            build_target(
                target=target,
                inputs=inputs,
                output_dir=output_dir,
                work_dir=work_dir,
                jobs=args.jobs,
                telegram_strict=not args.no_telegram_strict,
            )
        )

    summary = {
        "name": PROJECT_NAME,
        "files": len(inputs),
        "assets": results,
    }
    summary_path = output_dir / "release-assets.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {display_path(summary_path)}")

    if not args.keep_work:
        log("removing work directory")
        shutil.rmtree(work_dir, ignore_errors=True)
    log("release asset build finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
