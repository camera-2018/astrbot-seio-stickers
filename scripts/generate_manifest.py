#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "manifest.json"
STICKERS_DIR = ROOT / "stickers"
STICKER_NAME_RE = re.compile(r"^(?P<id>\d{3})_(?P<name>.+)\.gif$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest() -> dict:
    if not STICKERS_DIR.is_dir():
        raise FileNotFoundError(f"missing stickers directory: {STICKERS_DIR}")

    stickers = []
    seen_ids = set()

    files = sorted(
        STICKERS_DIR.glob("*.gif"),
        key=lambda path: int(path.name.split("_", 1)[0])
        if path.name.split("_", 1)[0].isdigit()
        else 999999,
    )

    for path in files:
        match = STICKER_NAME_RE.fullmatch(path.name)
        if not match:
            raise ValueError(f"invalid sticker filename: {path.name}")

        sticker_id = int(match.group("id"))
        sticker_id_text = match.group("id")
        if sticker_id in seen_ids:
            raise ValueError(f"duplicate sticker id: {sticker_id_text}")
        seen_ids.add(sticker_id)

        stickers.append(
            {
                "id": sticker_id_text,
                "name": match.group("name"),
                "path": path.relative_to(ROOT).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )

    expected_ids = list(range(1, len(stickers) + 1))
    actual_ids = [int(item["id"]) for item in stickers]
    if actual_ids != expected_ids:
        raise ValueError("sticker ids must be contiguous starting at 001")

    return {
        "name": "astrbot-seio-stickers",
        "display_name": "AstrBot seio娘表情包",
        "version": "2026.06",
        "description": "AstrBot seio娘 GIF 表情资源。",
        "stickers": stickers,
    }


def render_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate manifest.json from stickers.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="check whether manifest.json is up to date without writing it",
    )
    args = parser.parse_args()

    rendered = render_json(build_manifest())

    if args.check:
        current = MANIFEST_PATH.read_text(encoding="utf-8") if MANIFEST_PATH.exists() else ""
        if current != rendered:
            print("manifest.json is out of date", file=sys.stderr)
            return 1
        print("manifest.json is up to date")
        return 0

    MANIFEST_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {MANIFEST_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
