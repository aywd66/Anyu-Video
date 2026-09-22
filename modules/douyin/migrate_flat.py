#!/usr/bin/env python3
"""Migrate nested download layout to flat: Downloaded/<author>/<file>.mp4

Old: Downloaded/<author>/<mode>/<leaf>/<leaf>.mp4
New: Downloaded/<author>/<leaf>.mp4   (folderstyle=false, group_by_mode=false)

Dedup index matches aweme_id inside FILE NAMES (rglob), so moving is safe.
"""
import shutil
from pathlib import Path

BASE = Path("Downloaded")
SKIP_DIRS = {"hot_board"}


def count_media(p: Path) -> int:
    exts = {".mp4", ".jpg", ".jpeg", ".png", ".webp", ".mp3", ".m4a"}
    return sum(1 for f in p.rglob("*") if f.is_file() and f.suffix.lower() in exts)


def main() -> None:
    before = count_media(BASE)
    moved = collisions = 0
    empty_removed = 0

    authors = [d for d in BASE.iterdir() if d.is_dir() and d.name not in SKIP_DIRS]
    for author in authors:
        # move every file from nested sub-dirs up to the author dir
        for sub in [d for d in author.rglob("*") if d.is_dir()]:
            for f in list(sub.iterdir()):
                if not f.is_file():
                    continue
                dest = author / f.name
                if dest.exists():
                    if dest.stat().st_size == f.stat().st_size:
                        f.unlink()  # identical duplicate, drop the nested copy
                        collisions += 1
                    else:
                        print(f"[COLLISION-SIZE] {f} -> {dest.name}")
                else:
                    shutil.move(str(f), str(dest))
                    moved += 1

        # remove now-empty dirs (deepest first)
        for d in sorted((d for d in author.rglob("*") if d.is_dir()),
                        key=lambda p: len(p.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
                empty_removed += 1
        for d in [d for d in author.iterdir() if d.is_dir() and d.name in {"post", "like", "mix", "music", "collect", "collectmix"}]:
            if not any(d.iterdir()):
                d.rmdir()
                empty_removed += 1

    after = count_media(BASE)
    print(f"media files before={before} after={after} moved={moved} dedup_dropped={collisions} empty_dirs_removed={empty_removed}")
    if before != after:
        print("[WARN] media file count changed - inspect above")
    else:
        print("MIGRATION OK")


if __name__ == "__main__":
    main()
