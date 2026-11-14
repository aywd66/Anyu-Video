"""批量下载路径的吞吐基准（走**真实**的 UserDownloader._download_mode_items）。

为什么要有这个：并发（`thread`）调优最容易被"跨时间点的吞吐数字"骗到 ——
源站在限速态时同一份代码能慢 3 倍。只有**同一批作品、前后紧挨着跑**的 A/B
才算对照，所以这里把"同一批作品跑一次"做成一条命令，方便前后紧挨着跑两遍。

输入取**本地历史库**里的 aweme metadata（`database_path` 指向的那个库），
因此**不打详情接口** —— 取证期间把 `/aweme/v1/web/aweme/detail/` 和
`/aweme/v1/web/aweme/post/` 打进风控窗口过（Empty 200 / 403 risk_control），
之后就只能靠库里存的 payload 驱动验证了。

用法（并发 A/B，注意两次要**紧挨着**跑）：

    python tools/bench_batch_path.py --kind gallery -n 24 --thread 5  -o %TEMP%\\dy_a
    python tools/bench_batch_path.py --kind gallery -n 24 --thread 16 -o %TEMP%\\dy_b

    python tools/bench_batch_path.py --kind video   -n 16 --thread 5  -o %TEMP%\\dy_a
    python tools/bench_batch_path.py --kind video   -n 16 --thread 16 -o %TEMP%\\dy_b

输出末尾的 `breaker_escalation` / `cooldown_remaining` 用来确认熔断**没有**被
零星失败误触发（健康批次的期望值是 `escalation=0`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(MODULE_ROOT))

import utils.logger as ulog  # noqa: E402

logging.basicConfig(level=logging.CRITICAL)


def _resolve_db_path(config_path: Path) -> Path:
    """按工程约定解析 `database_path`：相对路径以模块根为基准，不跟 CWD 走。"""
    import yaml

    raw = "dy_downloader.db"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            data = yaml.safe_load(handle) or {}
        raw = str(data.get("database_path") or raw)
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else (MODULE_ROOT / candidate).resolve()


def load_items(db_path: Path, kind: str, limit: int) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "select metadata from aweme where aweme_type = ? "
            "order by create_time desc limit ?",
            (kind, limit),
        ).fetchall()
    finally:
        con.close()
    items = []
    for (meta,) in rows:
        try:
            payload = json.loads(meta)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("aweme_id"):
            items.append(payload)
    return items


async def run(args) -> int:
    from auth import CookieManager
    from config import ConfigLoader
    from control import QueueManager, RateLimiter, RetryHandler
    from core.api_client import DouyinAPIClient
    from core.downloader_factory import DownloaderFactory
    from storage import FileManager

    config_path = Path(args.config) if args.config else MODULE_ROOT / "config.yml"
    db_path = _resolve_db_path(config_path)
    items = load_items(db_path, args.kind, args.number)
    if not items:
        print(f"库里没有可用的 {args.kind} payload：{db_path}")
        print("（先跑一次正常下载把 metadata 写进库，或换 --kind）")
        return 2

    outdir = Path(args.out).resolve()
    cfg = ConfigLoader(str(config_path))
    cfg.update(video_quality=args.quality, thread=args.thread, failure_retry_passes=args.passes)
    cm = CookieManager(str(MODULE_ROOT / ".cookies.json"))
    cookies = cm.get_cookies()
    if not cookies:
        print(f"没有可用 cookie：{MODULE_ROOT / '.cookies.json'}")
        return 2

    print(
        f"db={db_path}\nkind={args.kind} items={len(items)} thread={args.thread} "
        f"quality={args.quality} passes={args.passes}\nout={outdir}",
        flush=True,
    )
    fm = FileManager(str(outdir))
    started = time.monotonic()
    async with DouyinAPIClient(cookies, proxy=cfg.get("proxy")) as api:
        downloader = DownloaderFactory.create(
            "user",
            cfg,
            api,
            fm,
            cm,
            None,
            RateLimiter(max_per_second=float(cfg.get("rate_limit", 2) or 2)),
            RetryHandler(int(cfg.get("retry_times", 3) or 3)),
            QueueManager(max_workers=args.thread),
        )
        result = await downloader._download_mode_items("post", items, "bench")
    wall = time.monotonic() - started

    total_bytes = sum(f.stat().st_size for f in outdir.rglob("*") if f.is_file())
    empty = sum(1 for f in outdir.rglob("*") if f.is_file() and f.stat().st_size <= 0)
    leftovers = sum(1 for f in outdir.rglob("*.tmp"))
    print("\n==== SUMMARY ====")
    print(
        f"wall={wall:.1f}s items={result.total} success={result.success} "
        f"failed={result.failed} skipped={result.skipped}"
    )
    print(
        f"bytes={total_bytes / 1e6:.1f}MB "
        f"throughput={total_bytes / max(wall, 0.001) / 1e6:.2f}MB/s "
        f"items/sec={result.total / max(wall, 0.001):.2f}"
    )
    print(f"empty_files={empty} leftover_tmp={leftovers}")
    print(
        f"breaker_escalation={fm.breaker._escalation} "
        f"cooldown_remaining={fm.breaker.cooldown_remaining:.1f}s"
    )
    return 0 if result.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default="", help="config.yml 路径（默认模块根）")
    parser.add_argument("--kind", default="gallery", choices=["gallery", "video"])
    parser.add_argument("-n", "--number", type=int, default=24, help="取多少条")
    parser.add_argument("--thread", type=int, default=16)
    parser.add_argument("--quality", default="highest")
    parser.add_argument("--passes", type=int, default=2, help="failure_retry_passes")
    parser.add_argument("-o", "--out", required=True, help="产物目录（建议用临时目录）")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    ulog.set_console_log_level(logging.WARNING if args.verbose else logging.ERROR)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
