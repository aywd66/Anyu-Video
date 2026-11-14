# -*- coding: utf-8 -*-
"""
真实链路自检（端到端）

与 ``smoke_test.py`` 的分工
---------------------------
``smoke_test.py``  purely 本地（本地 HTTP 服务模拟直播流），验证「录制 -> 分片 ->
合并 -> 转封装」这条链路本身；本脚本则**访问真实平台**，验证「平台解析层 +
监控所需的取流逻辑」在当前接口下是否仍然工作。

平台接口会不定期变更，怀疑「录不上」时，先跑本脚本定位是平台侧问题还是程序问题。

用法::

    python e2e_check.py                  # 完整自检（含真实录制约 20 秒）
    python e2e_check.py --quick          # 跳过录制与分卷，只验证解析/状态/取流
    python e2e_check.py --keep           # 保留录制产物，便于人工播放检查
    python e2e_check.py --only login     # 只验证「扫码登录能否弹出二维码」
    python e2e_check.py --only ui        # 只验证界面里的扫码登录入口

注意事项
--------
1. 本脚本会真实请求平台接口并短暂拉流录制，请勿高频反复执行（有风控风险）；
2. 抖音：从官方 feed 接口挑选一个在播房间，并逐个用程序自身的开播检测逻辑复核，
   避免推荐列表滞后的干扰；
3. 快手：按房间查询的接口已对未登录访客关闭，本脚本改为验证**推荐流索引**
   （当前唯一可用的批量取流入口）能否端到端录出文件。
   若你需要监控任意指定主播，请在界面「设置 → 账号 → 扫码登录快手」里登录一次，
   或直接双击 ``login.bat``。
4. C 段用无头浏览器验证「登录入口 -> 二维码」这条路径，不需要你动手扫码。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_recorder.config.settings import AppConfig, RoomEntry
from live_recorder.platforms import get_parser
from live_recorder.platforms.base import (
    LiveStatus,
    PlatformError,
    RoomUnavailableError,
    StreamQuality,
)
from live_recorder.platforms.registry import resolve_stream
from live_recorder.recorder import StreamRecorder
from live_recorder.recorder.ffmpeg_utils import probe_duration, run_ffmpeg
from live_recorder.recorder.merger import remux_to_mp4
from live_recorder.utils.logger import configure_logging

# 快手分享链 -> 期望解析出的主播 ID。
# 历史缺陷：曾把重定向地址里的 efid（**分享者** ID）当作主播 ID，
# 导致「解析出的房间号永远是分享者自己的」，取流必然失败。
KS_SHARE_CASES = {
    "https://v.kuaishou.com/7IUnfWDV": "cl3503011999",
    "https://v.kuaishou.com/KyerR3vx": "NingXinHui716",
}
# 抖音官方推荐流接口：可公开获取在播房间及其分享链接，用于挑选测试样本
DY_FEED_API = "https://live.douyin.com/webcast/feed/"
DY_FEED_PARAMS = {
    "aid": "6383", "app_name": "douyin_web", "live_id": "1",
    "device_platform": "web", "language": "zh-CN", "browser_language": "zh-CN",
    "browser_platform": "Win32", "browser_name": "Chrome",
    "browser_version": "126.0.0.0", "os_name": "Windows", "os_version": "10",
    "cookie_enabled": "true", "count": "30", "channel_id": "1",
}

RESULTS: list[tuple[str, bool, str]] = []
TEMP_DIRS: list[Path] = []
KEEP_ARTIFACTS = False


def check(label: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((label, ok, detail))
    print(f"  {'✔' if ok else '✘'} {label}" + (f"：{detail}" if detail else ""))


def decode_duration(path: Path) -> float:
    """完整解码一遍来测量时长。

    比读容器元数据更可靠：直播流的 FLV 常常不写 ``duration`` 字段，
    而「能从头解码到尾」同时也证明了文件本身可正常播放。
    """
    if not path or not path.exists():
        return 0.0
    _code, output = run_ffmpeg(
        ["-v", "info", "-i", str(path), "-f", "null", "-"], timeout=180
    )
    stamps = re.findall(r"time=(\d+):(\d+):(\d+\.\d+)", output or "")
    if not stamps:
        return 0.0
    hours, minutes, seconds = stamps[-1]
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def best_duration(path) -> float:
    """优先用容器元数据，拿不到就退化为解码测量。``path`` 可以为空。"""
    if not path:
        return 0.0
    return probe_duration(path) or decode_duration(path)


def log_tail(lines: int = 12) -> str:
    """取最近若干条日志，用于失败时定位原因。"""
    from live_recorder.utils.logger import LOG_BUFFER

    records = LOG_BUFFER.snapshot()[-lines:]
    if not records:
        return "       （无日志）"
    return "\n".join(f"       {rec.format_line()}" for rec in records)


def record_for(seconds: float, *, stream, room_entry, segment_gb=None):
    """用真实录制器录一段，返回 ``(工作目录, 录制器, 事件列表, 中途快照)``。"""
    work = Path(tempfile.mkdtemp(prefix="lr_e2e_"))
    TEMP_DIRS.append(work)
    rec_config = AppConfig()
    rec_config.save_path = str(work)
    rec_config.convert_to_mp4 = False
    rec_config.min_free_disk_gb = 0.0
    rec_config.stall_timeout = 60
    if segment_gb is not None:
        # 测试专用：正常下限是 0.5GB，这里调小以便快速触发分卷
        rec_config.max_file_size_gb = segment_gb

    events: list[str] = []
    recorder = StreamRecorder(
        room=room_entry,
        stream_provider=lambda: stream,
        config=rec_config,
        on_event=lambda evt, payload: events.append(evt),
    )
    recorder.start()
    print(f"     录制 {seconds:.0f} 秒……")
    time.sleep(seconds)
    snapshot = recorder.stats()
    recorder.stop(reason="自检结束")
    recorder.join(timeout=120)
    return work, recorder, events, snapshot


def collect_live_douyin_rooms(dy) -> tuple[list[tuple[str, str]], list[str]]:
    """从抖音官方 feed 接口收集一批**已核实在播**的房间。

    返回 ``([(分享链接, web_rid), ...], [已下播房间, ...])``。

    必要性
    ------
    1. feed 是推荐流，条目存在滞后（刚下播的房间仍标 ``status=2``），
       必须逐个用程序自身的 ``check_live`` 复核，否则会拿到已下播的样本；
    2. 直播场次长短不一，从复核到正式使用之间房间仍可能下播，
       因此返回**一批**候选供调用方依次重试，而不是只挑一个。
    """
    candidates: list[tuple[str, str]] = []
    with_origin: list[tuple[str, str]] = []
    stale: list[str] = []
    seen: set[str] = set()

    for page_no in range(3):
        feed = dy.request(
            "GET", DY_FEED_API, params=DY_FEED_PARAMS,
            referer="https://live.douyin.com/", retries=2, throttle_range=(1.0, 2.0),
        )
        rooms = [((it or {}).get("data") or {}) for it in (feed.get("data") or [])]
        print(f"  第 {page_no + 1} 页候选 {len(rooms)} 个")
        for room in rooms:
            share = room.get("share_url")
            if not share or room.get("status") != 2:
                continue
            rid = dy.resolve_room(share)
            if rid in seen:
                continue
            seen.add(rid)
            info = dy.check_live(rid)
            if info.status is not LiveStatus.LIVE:
                print(f"  跳过（feed 滞后，实测 {info.status.value}）：{room.get('id_str')}")
                stale.append(rid)
                continue
            # 带原画档（FULL_HD1）的优先
            flv_map = (room.get("stream_url") or {}).get("flv_pull_url") or {}
            bucket = with_origin if "FULL_HD1" in flv_map else candidates
            bucket.append((share, rid))
            print(f"  在播候选：{room.get('id_str')} web_rid={rid} "
                  f"昵称={info.nickname!r} 原画档={'有' if 'FULL_HD1' in flv_map else '无'}")
        if len(with_origin) >= 2:
            break

    return with_origin + candidates, stale


# ==========================================================================
def run_douyin(config, record_seconds: float) -> None:
    print("\n" + "=" * 74)
    print("A. 抖音：分享链接 -> web_rid -> 开播状态 -> 原画流 -> 真实录制")
    print("=" * 74)

    dy = get_parser("douyin", config)
    room_entry = None
    stream = None
    try:
        # ---- A1 收集在播候选，并复核到某个仍确实在播的房间 ----
        pool, _stale = collect_live_douyin_rooms(dy)
        check("从官方接口取得「已核实在播」的房间候选", bool(pool), f"{len(pool)} 个")

        chosen = None
        for share, rid in pool[:6]:
            info = dy.check_live(rid)          # 复核：候选可能在这几秒内已下播
            if info.status is LiveStatus.LIVE:
                chosen = (share, rid, info)
                break
            print(f"  候选 {rid} 复核为 {info.status.value}，换下一个")

        check("在播房间复核通过（拿到可用的分享链接）", bool(chosen),
              (chosen[0][:70] if chosen else "无可用候选"))
        if not chosen:
            raise RuntimeError("未取得可用在播房间，跳过后续抖音验证")
        share_url, web_rid, info = chosen

        # ---- A2 分享链接 -> web_rid（19 位 room_id 必须自动换算）----
        web_rid = dy.resolve_room(share_url)
        check("分享链接解析为 web_rid（19 位 room_id 已换算）",
              len(web_rid) < 17, f"web_rid={web_rid}")

        # ---- A3 开播状态 ----
        print(f"     状态={info.status.value} 昵称={info.nickname!r} "
              f"标题={info.title!r} 在线={info.online_count}")
        check("开播状态检测为「直播中」", info.status is LiveStatus.LIVE, info.status.value)
        check("解析出主播昵称", bool(info.nickname), info.nickname)

        # ---- A4 取流 ----
        stream = resolve_stream(dy, web_rid, config)
        print(f"     档位={stream.quality_label} 格式={stream.stream_format.value} "
              f"降级={stream.is_fallback}")
        check("取得可拉流地址（原画 FLV 优先）",
              str(stream.url).startswith("http"),
              f"{stream.quality_label}/{stream.stream_format.value}")

        room_entry = RoomEntry(
            platform="douyin", raw_input=share_url,
            room_id=web_rid, nickname=info.nickname,
        )

        # ---- A8 下播房间必须直接判定「不可用」，阻断降级与浏览器兜底 ----
        # 确定性校验：伪造「接口仍返回 room 节点、但 status=4 且流地址为空」的响应。
        # 这正是「抖音下播页自动播放推荐直播」会录制到**别的房间**的那条路径。
        original = dy._fetch_room_payload
        try:
            dy._fetch_room_payload = lambda rid, **kw: {
                "data": {"data": [{"id_str": "synthetic", "status": 4,
                                   "stream_url": {"flv_pull_url": {}}}]}
            }
            try:
                dy.fetch_stream("synthetic_offline", StreamQuality.ORIGIN)
                check("下播房间取流直接判定不可用", False, "竟然返回了流地址")
            except RoomUnavailableError as exc:
                check("下播房间取流直接判定不可用（阻断浏览器兜底）", True, str(exc)[:80])
            except PlatformError as exc:
                check("下播房间取流直接判定不可用（阻断浏览器兜底）", False,
                      f"抛出普通 PlatformError 会触发兜底：{str(exc)[:80]}")
        finally:
            dy._fetch_room_payload = original

        if record_seconds <= 0:
            print("     --quick：跳过真实录制与分卷验证")
            return

        # ---- A5 真实录制 -> 有效文件 ----
        work, recorder, record_events, snapshot = record_for(
            record_seconds, stream=stream, room_entry=room_entry
        )
        print(f"     录制中状态={snapshot.state.value} 已写={snapshot.total_mb:.2f}MB "
              f"分片={snapshot.segment_count}")
        final = recorder.final_path
        size = final.stat().st_size if final and final.exists() else 0
        meta_duration = probe_duration(final) if final else 0.0
        duration = best_duration(final)
        print(f"     成品={final.name if final else None} 体积={size / 1024 / 1024:.2f}MB "
              f"元数据时长={meta_duration:.1f}s 解码时长={duration:.1f}s")
        if final is None:
            # 录不出文件时把事件序列打出来，便于区分「主播下播」与「程序缺陷」
            print(f"     录制事件={record_events}")
            print(f"     最近日志：\n{log_tail()}")
        check("录制产出有效 FLV 文件（含真实音视频数据）",
              size > 200_000, f"{size / 1024 / 1024:.2f}MB")
        check("录像文件可完整解码且时长充足", duration >= record_seconds * 0.5,
              f"{duration:.1f}s")

        # ---- A6 目录与命名规则 ----
        if final and final.exists():
            rel = final.relative_to(work)
            print(f"     相对路径={rel}")
            # 两级目录：平台/昵称（日期已并入文件名，不再单独建目录层）
            check("输出目录为「平台/主播昵称」两级", len(rel.parts) == 3,
                  str(Path(*rel.parts[:-1])))
            # 文件名为 平台-昵称-YYYY-MM-DD_HHMMSS-序号.flv
            # 昵称可能含横线，用正则锚定「_HHMMSS-序号」结尾而非按 "-" 硬切
            pattern = r"^抖音-.+-\d{4}-\d{2}-\d{2}_\d{6}-(\d{3}|merged)(-\d+)?\.flv$"
            check("文件名含日期时间（平台-昵称-日期_时刻-序号.flv）",
                  bool(re.match(pattern, final.name)), final.name)
            check("日期已并入文件名（YYYY-MM-DD_HHMMSS）",
                  bool(re.search(r"\d{4}-\d{2}-\d{2}_\d{6}", final.name)), final.name)

        # ---- A7 无损转封装 MP4 ----
        if final and final.exists():
            mp4 = remux_to_mp4(final)
            ok = bool(mp4) and Path(mp4).exists() and Path(mp4).stat().st_size > 200_000
            check("成品可无损转封装为 MP4（-c copy）", ok, Path(mp4).name if mp4 else "未生成")

        # ---- A9 分卷 + 无损合并 ----
        work2, recorder2, seg_events, _ = record_for(
            min(record_seconds, 15.0), stream=stream, room_entry=room_entry,
            segment_gb=0.001,
        )
        finished = seg_events.count("segment_finished")
        merged = recorder2.final_path
        merged_duration = best_duration(merged)
        print(f"     分片事件={finished} 个；合并文件={merged.name if merged else None} "
              f"解码时长={merged_duration:.1f}s")
        check("达到分片上限后自动分卷（产生多个分片）", finished >= 2, f"{finished} 个")
        check("分片无损合并为单文件且时长完整",
              bool(merged) and merged.exists() and merged_duration > 0,
              f"{merged_duration:.1f}s")
    except Exception as exc:  # noqa: BLE001 - 自检需完整报错而非中断
        traceback.print_exc()
        check("抖音端到端流程未抛异常", False, str(exc)[:140])


# ==========================================================================
def run_kuaishou(config, record_seconds: float) -> None:
    print("\n" + "=" * 74)
    print("B. 快手：分享链解析 / 推荐流索引 / 真实取流与录制")
    print("=" * 74)

    ks = get_parser("kuaishou", config)
    try:
        # ---- B1 分享链必须解析出「主播」而不是「分享者」----
        for raw, expect in KS_SHARE_CASES.items():
            got = ks.resolve_room(raw)
            check(f"分享链解析为主播 ID（…/{raw.rsplit('/', 1)[-1]}）",
                  got == expect, f"{got}（期望 {expect}）")

        # ---- B2 推荐流索引：快手当前唯一可用的批量取流入口 ----
        index = ks._load_reco_index()  # noqa: SLF001 - 自检脚本需要直接观察索引
        check("推荐流索引可用（可批量取得在播房间的真实流地址）",
              bool(index), f"{len(index)} 个在播房间")
        if not index:
            print("     推荐流为空，跳过后续快手验证")
            return

        pid, entry = next(iter(index.items()))
        nickname = entry.get("nickname") or pid
        print(f"     选中房间：{pid} {nickname!r}")

        # ---- B3 状态判定与取流 ----
        info = ks.check_live(pid)
        check("推荐流命中的房间判定为直播中",
              info.status is LiveStatus.LIVE, f"{info.status.value} / {info.note}")

        stream = resolve_stream(ks, pid, config)
        print(f"     档位={stream.quality_label} 格式={stream.stream_format.value}")
        check("取得 FLV 流地址", ".flv" in stream.url.lower(), stream.quality_label)

        # ---- B4 真实录制 ----
        if record_seconds <= 0:
            return
        room_entry = RoomEntry(
            platform="kuaishou", raw_input=pid, room_id=pid, nickname=nickname
        )
        work, recorder, _events, _snapshot = record_for(
            record_seconds, stream=stream, room_entry=room_entry
        )
        final = recorder.final_path
        produced = [p for p in work.rglob("*.flv") if p.stat().st_size > 0]
        size = max((p.stat().st_size for p in produced), default=0)
        duration = best_duration(final) if final and final.exists() else 0.0
        print(f"     产物：{[p.name for p in produced]}")
        print(f"     体积={size / 1024 / 1024:.2f}MB 时长={duration:.1f}s")
        check("快手录制产出有效 FLV 文件", size > 200_000, f"{size / 1024 / 1024:.2f}MB")
        check("快手产物可完整解码",
              duration >= min(10.0, record_seconds * 0.5), f"{duration:.1f}s")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        check("快手端到端流程未抛异常", False, str(exc)[:140])


def run_login_flow() -> None:
    """验证「扫码登录」真的能弹出二维码。

    历史缺陷：``login.bat`` 只打开了 ``live.kuaishou.com`` 首页就干等，
    而快手首页**不会自动弹二维码**（必须先点右上角「登录」），
    用户因此反馈「没看见快手扫码获取 cookies」。
    """
    print("\n" + "=" * 74)
    print("C. 扫码登录：登录入口与二维码")
    print("=" * 74)

    from live_recorder.platforms.qr_login import SPECS, QrLoginSession

    # ---- C0 两处入口共用同一套流程 ----
    try:
        from live_recorder.platforms.login_helper import PLATFORMS as CLI_PLATFORMS
        from live_recorder.ui.login_dialog import LoginDialog, LoginWorker  # noqa: F401

        check("命令行与界面共用同一套登录流程",
              set(CLI_PLATFORMS) == set(SPECS), "login_helper / qr_login 平台一致")
    except Exception as exc:  # noqa: BLE001
        check("命令行与界面共用同一套登录流程", False, str(exc)[:120])

    session = None
    try:
        session = QrLoginSession("kuaishou", headless=True)
        code = session.open()
        is_png = bool(code) and bytes(code[:8]) == b"\x89PNG\r\n\x1a\n"
        check("打开登录页后能自动点出二维码弹窗并取到图片",
              is_png, f"{len(code) if code else 0} 字节" if code else "未取到")

        fresh = session.current_qr()
        check("二维码可重复读取（供界面轮询刷新）",
              bool(fresh) and bytes(fresh[:8]) == b"\x89PNG\r\n\x1a\n")

        check("未扫码时不误判为已登录", not session.is_logged_in(),
              f"{session.cookie_count()} 个 Cookie")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        check("扫码登录流程未抛异常", False, f"{type(exc).__name__}: {exc}"[:140])
    finally:
        if session is not None:
            try:
                session.close()
                session.close()  # 重复关闭必须安全
                check("登录会话可安全关闭且可重复关闭", True)
            except Exception as exc:  # noqa: BLE001
                check("登录会话可安全关闭且可重复关闭", False, str(exc)[:120])


def run_ui_check() -> None:
    """验证界面里确实存在「扫码登录」入口（用户无需去找 login.bat）。"""
    print("\n" + "=" * 74)
    print("D. 界面：设置 → 账号 是否提供扫码登录入口")
    print("=" * 74)
    try:
        from PyQt6.QtWidgets import QApplication, QPushButton

        from live_recorder.ui.settings_dialog import SettingsDialog

        app = QApplication.instance() or QApplication([])
        dialog = SettingsDialog(AppConfig())
        texts = [b.text() for b in dialog.findChildren(QPushButton)]
        check("「设置 → 账号」提供扫码登录按钮",
              any("扫码登录" in t for t in texts),
              " / ".join(t for t in texts if "扫码登录" in t))
        check("同时保留手动粘贴 Cookie 的输入框",
              hasattr(dialog, "_ks_cookie_edit") and hasattr(dialog, "_dy_cookie_edit"))
        check("账号页会提示未登录的影响范围",
              "尚未登录快手" in dialog._ks_status.text(),  # noqa: SLF001
              dialog._ks_status.text()[:60])  # noqa: SLF001

        # 回填逻辑：粘贴 Cookie 后取状态应变为「已保存」
        dialog._ks_cookie_edit.setPlainText("did=web_test; passToken=demo")  # noqa: SLF001
        dialog._refresh_cookie_status()  # noqa: SLF001
        check("填入 Cookie 后状态提示同步更新",
              "已保存快手 Cookie" in dialog._ks_status.text(),  # noqa: SLF001
              dialog._ks_status.text()[:60])  # noqa: SLF001
        dialog.close()

        # 主工具栏也要有入口，否则用户很难发现「扫码登录」这个功能
        import tempfile as _tempfile

        from PyQt6.QtGui import QAction as _QAction

        from live_recorder.config.settings import ConfigStore
        from live_recorder.ui.main_window import MainWindow

        with _tempfile.TemporaryDirectory(prefix="lr_ui_") as tmp:
            window = MainWindow(ConfigStore(Path(tmp) / "config.json"))
            titles = [a.text() for a in window.findChildren(_QAction)]
            check("主工具栏提供「扫码登录」入口",
                  any("扫码登录" in t for t in titles),
                  " / ".join(t for t in titles if "扫码登录" in t))
            window.close()
        _ = app
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        check("界面自检未抛异常", False, f"{type(exc).__name__}: {exc}"[:140])


def main() -> int:
    global KEEP_ARTIFACTS
    parser = argparse.ArgumentParser(description="真实链路端到端自检")
    parser.add_argument("--quick", action="store_true",
                        help="跳过真实录制与分卷验证，只验证解析/状态/取流")
    parser.add_argument("--keep", action="store_true", help="保留录制产物以便人工检查")
    parser.add_argument("--record-seconds", type=float, default=20.0,
                        help="每段录制时长（秒），默认 20")
    parser.add_argument("--only", choices=("douyin", "kuaishou", "login", "ui"),
                        help="只跑其中一段（login = 扫码登录取码，ui = 界面入口）")
    args = parser.parse_args()
    KEEP_ARTIFACTS = args.keep

    configure_logging(level=30)
    config = AppConfig()
    config.playwright_enabled = True

    record_seconds = 0.0 if args.quick else args.record_seconds
    only = args.only
    if only in (None, "douyin"):
        run_douyin(config, record_seconds)
    if only in (None, "kuaishou"):
        run_kuaishou(config, record_seconds)
    if only in (None, "login"):
        run_login_flow()
    if only in (None, "ui"):
        run_ui_check()

    print("\n" + "=" * 74)
    print("自检汇总")
    print("=" * 74)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    for label, ok, detail in RESULTS:
        print(f"  {'✔' if ok else '✘'} {label}" + (f"  [{detail}]" if detail else ""))
    print(f"\n  {passed}/{len(RESULTS)} 项通过")

    for path in TEMP_DIRS:
        if KEEP_ARTIFACTS:
            print(f"  保留产物目录：{path}")
        else:
            shutil.rmtree(path, ignore_errors=True)

    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
