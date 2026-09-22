#!/usr/bin/env python3
"""Launcher: double-click friendly input loop for douyin-downloader.

- Prompts for a link (or full share text), extracts every http(s) URL in it,
  then runs run.py once per batch with -u flags (config `link:` list stays empty).
- Drag & drop / command line args are also treated as input text.
- Loops so several links can be downloaded in one session; empty input exits.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

ROOT = Path(__file__).resolve().parent
URL_RE = re.compile(r"https?://[^\s\"'，。、）)】>]+", re.IGNORECASE)


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text or "")


def run(urls: list[str]) -> int:
    cmd = [
        str(ROOT / "venv" / "Scripts" / "python.exe"),
        str(ROOT / "run.py"),
        "-c", str(ROOT / "config.yml"),
    ]
    for url in urls:
        cmd += ["-u", url]
    return subprocess.call(cmd, cwd=str(ROOT))


def main() -> int:
    if len(sys.argv) > 1:
        # 拖拽/命令行参数：从整段文本里提取链接
        urls = extract_urls(" ".join(sys.argv[1:]))
        if not urls:
            print("未在参数中识别到 http(s) 链接。")
            return 1
        return run(urls)

    while True:
        try:
            raw = input(
                "\n请粘贴抖音链接或分享口令后回车开始下载"
                "（可一次粘多个，直接回车退出）："
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not raw:
            return 0
        urls = extract_urls(raw)
        if not urls:
            print("[!] 未识别到链接：请粘贴包含 http(s):// 的分享文本")
            continue
        run(urls)


if __name__ == "__main__":
    sys.exit(main())
