# -*- coding: utf-8 -*-
"""
冒烟测试：在真实 ffmpeg 环境下验证「录制 -> 分片 -> 合并 -> 转封装」全链路。

用法::

    python smoke_test.py            # 执行全部测试，结束后清理临时文件
    python smoke_test.py --keep     # 保留临时文件，便于人工检查产物

测试内容
--------
1. ffmpeg 可用性；
2. 多分片 concat 无损合并的时长正确性；
3. MP4 转封装（``-c copy`` 不重编码）；
4. 录制器完整生命周期：目录结构、文件命名、事件序列；
5. 自动分卷 + 多分片合并：用小体积阈值强制分卷，验证分片合并后总时长正确。

该脚本不访问任何第三方平台，全部在本地（含本地 HTTP 服务模拟直播流）完成，
可安全反复执行。
"""

from __future__ import annotations

import functools
import http.server
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Tuple

# 允许直接以脚本方式运行
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_recorder.config.settings import AppConfig, RoomEntry
from live_recorder.platforms.base import StreamFormat, StreamInfo, StreamQuality
from live_recorder.recorder import (
    StreamRecorder,
    ffmpeg_available,
    merge_segments,
    remux_to_mp4,
    resolve_ffmpeg,
    verify_decodable,
)
from live_recorder.recorder.ffmpeg_utils import probe_duration, run_ffmpeg
from live_recorder.utils.logger import configure_logging, get_logger

logger = get_logger(__name__)

_PASS = "✔"
_FAIL = "✘"

# 模拟流素材时长（秒）
_STREAM_SECONDS = 12


# --------------------------------------------------------------------------
# 素材与工具
# --------------------------------------------------------------------------
def _make_test_video(ffmpeg: str, out_path: Path, seconds: int) -> bool:
    """用 ffmpeg 的测试源生成一段带音视频的 FLV 素材。

    强制每 1 秒一个关键帧，便于后续用 segment 复用器无损切片。
    """
    args = [
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", str(seconds),
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", "25", "-force_key_frames", "expr:gte(t,n_forced*1)",
        "-c:a", "aac", "-b:a", "64k",
        "-f", "flv",
        "-y", str(out_path),
    ]
    code, output = run_ffmpeg(args, ffmpeg_path=ffmpeg)
    if code != 0:
        logger.error("生成测试素材失败：%s", output[-300:])
    return code == 0 and out_path.exists() and out_path.stat().st_size > 0


class _ThrottledHandler(http.server.SimpleHTTPRequestHandler):
    """带限速的静态文件服务，用于模拟真实直播流的到达速率。

    默认不限速（``bytes_per_second = 0``），此时等价于普通静态服务。
    """

    bytes_per_second = 0

    def copyfile(self, source, outputfile):  # noqa: D102 - 覆盖父类实现
        if self.bytes_per_second <= 0:
            shutil.copyfileobj(source, outputfile)
            return

        chunk_size = 8192
        interval = chunk_size / float(self.bytes_per_second)
        while True:
            buffer = source.read(chunk_size)
            if not buffer:
                break
            outputfile.write(buffer)
            time.sleep(interval)

    def log_message(self, format, *args):  # noqa: A002 - 保持父类签名
        """屏蔽 HTTP 访问日志，避免干扰测试输出。"""
        return


class _QuietHttpServer(http.server.ThreadingHTTPServer):
    """静默的 HTTP 服务：录制被终止时客户端会主动断开连接，

    这类 ``ConnectionAbortedError`` 属于预期行为，不应打印堆栈干扰测试输出。
    """

    def handle_error(self, request, client_address) -> None:  # noqa: D102
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


def _start_http_server(
    directory: Path, bytes_per_second: int = 0
) -> Tuple[http.server.ThreadingHTTPServer, int]:
    """在随机端口启动一个只读 HTTP 服务，返回 (server, port)。

    ``bytes_per_second`` 大于 0 时启用限速，用于模拟直播流按实时速率到达。
    """
    handler = functools.partial(
        _ThrottledHandler, directory=str(directory)
    )
    _ThrottledHandler.bytes_per_second = bytes_per_second

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = _QuietHttpServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _record_session(
    ffmpeg: str,
    work_dir: Path,
    output_root: Path,
    tune: Callable[[AppConfig], None],
    throttle_bps: int = 0,
    wait_predicate: Callable[[StreamRecorder], bool] | None = None,
    wait_timeout: float = 40.0,
    tail_wait: float = 4.0,
    nickname: str = "自检主播",
    room_id: str = "smoke-test",
    mid_session_nickname: str = "",
) -> Tuple[StreamRecorder, List[Tuple[str, Dict]], int]:
    """通过本地 HTTP 服务模拟直播流，完整跑一遍 StreamRecorder 生命周期。

    参数
    ----
    tune:           用于按用例调整配置的回调（例如强制小体积分卷）
    throttle_bps:   模拟流的传输速率（字节/秒），0 表示不限速
    wait_predicate: 停止录制前的等待条件，返回 True 即提前停止等待
    wait_timeout:   等待条件的最长时间
    tail_wait:      未指定等待条件时，首批数据落盘后额外等待的时间
    nickname:       房间初始昵称（传空串可验证回退命名）
    room_id:        房间标识
    mid_session_nickname:
        非空时，在录制中途把 room.nickname 改成该值，
        用于验证「会话级命名冻结」（中途回填不应改变本场产出位置）

    返回 ``(recorder, events, 模拟流时长秒数)``。
    """
    serve_dir = work_dir / f"stream_{output_root.name}"
    serve_dir.mkdir(parents=True, exist_ok=True)
    stream_file = serve_dir / "live.flv"
    if not _make_test_video(ffmpeg, stream_file, _STREAM_SECONDS):
        raise RuntimeError("无法生成模拟流素材")

    server, port = _start_http_server(serve_dir, bytes_per_second=throttle_bps)
    stream_url = f"http://127.0.0.1:{port}/live.flv"

    config = AppConfig()
    config.save_path = str(output_root)
    config.ffmpeg_path = ffmpeg
    config.reconnect_interval = 5
    config.stall_timeout = 10
    config.min_free_disk_gb = 0.1
    config.max_file_size_gb = 4.0
    config.max_duration_minutes = 0
    config.convert_to_mp4 = False
    tune(config)

    room = RoomEntry(
        platform="douyin",
        raw_input=room_id,
        room_id=room_id,
        nickname=nickname,
    )

    def stream_provider() -> StreamInfo:
        return StreamInfo(
            url=stream_url,
            stream_format=StreamFormat.FLV,
            quality=StreamQuality.ORIGIN,
            quality_label="origin",
            headers={"User-Agent": "LiveRecorder-SmokeTest/1.0"},
        )

    events: List[Tuple[str, Dict]] = []
    recorder = StreamRecorder(
        room=room,
        stream_provider=stream_provider,
        config=config,
        on_event=lambda name, payload: events.append((name, dict(payload))),
    )

    try:
        recorder.start()
        # 等待首批数据落盘
        deadline = time.time() + 20
        while time.time() < deadline and recorder.stats().total_bytes == 0:
            time.sleep(0.3)

        if mid_session_nickname:
            # 模拟「录制中途平台回填了昵称」
            room.nickname = mid_session_nickname

        if wait_predicate is not None:
            deadline = time.time() + wait_timeout
            while time.time() < deadline and not wait_predicate(recorder):
                time.sleep(0.3)
        else:
            time.sleep(tail_wait)

        recorder.stop("smoke_test")
        recorder.join(timeout=120)
    finally:
        server.shutdown()
        server.server_close()

    return recorder, events, _STREAM_SECONDS


# --------------------------------------------------------------------------
# 测试用例
# --------------------------------------------------------------------------
def test_merge(ffmpeg: str, work_dir: Path) -> bool:
    """验证多分片 lossless 合并（segment 复用器切片）。"""
    print("\n[测试 2] 多分片 concat 无损合并……")
    source = work_dir / "source.flv"
    if not _make_test_video(ffmpeg, source, 9):
        print(f"      {_FAIL} 无法生成测试素材")
        return False

    pattern = work_dir / "seg_%03d.flv"
    code, output = run_ffmpeg(
        [
            "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-c", "copy",
            "-f", "segment",
            "-segment_time", "3",
            "-reset_timestamps", "1",
            "-y", str(pattern),
        ],
        ffmpeg_path=ffmpeg,
    )
    segments = sorted(work_dir.glob("seg_*.flv"))
    if code != 0 or len(segments) < 2:
        print(f"      {_FAIL} 切分失败（得到 {len(segments)} 个分片）：{output[-200:]}")
        return False
    print(f"      已生成 {len(segments)} 个测试分片")

    merged = work_dir / "merged.flv"
    result = merge_segments(segments, merged, ffmpeg_path=ffmpeg, container="flv")
    if result is None or not Path(result).exists():
        print(f"      {_FAIL} 合并未产出文件")
        return False

    duration = probe_duration(result, ffmpeg_path=ffmpeg)
    source_duration = probe_duration(source, ffmpeg_path=ffmpeg)
    ok = abs(duration - source_duration) <= 1.0
    print(
        f"      {_PASS if ok else _FAIL} 合并结果：{Path(result).name} "
        f"时长 {duration:.2f}s（源片 {source_duration:.2f}s）"
    )
    return ok


def test_remux_mp4(ffmpeg: str, work_dir: Path) -> bool:
    """验证 MP4 转封装（不重编码）。"""
    print("\n[测试 3] 转封装为 MP4（-c copy）……")
    source = work_dir / "source.flv"
    if not source.exists():
        print(f"      {_FAIL} 缺少测试素材")
        return False

    mp4 = remux_to_mp4(source, work_dir / "remux_test.mp4", ffmpeg_path=ffmpeg)
    if mp4 is None or not Path(mp4).exists():
        print(f"      {_FAIL} 转封装失败")
        return False

    duration = probe_duration(mp4, ffmpeg_path=ffmpeg)
    source_duration = probe_duration(source, ffmpeg_path=ffmpeg)
    ok = abs(duration - source_duration) <= 0.5
    print(
        f"      {_PASS if ok else _FAIL} 已生成 {Path(mp4).name}"
        f"（时长 {duration:.2f}s，源片 {source_duration:.2f}s）"
    )
    return ok


def test_verify_rejects_corrupt(ffmpeg: str, work_dir: Path) -> bool:
    """验证转封装完整性校验能检出码流损坏（防止「损坏 MP4 + 已删 FLV」复发）。"""
    print("\n[测试 3b] 转封装完整性校验能检出损坏……")
    source = work_dir / "source.flv"
    if not source.exists():
        print(f"      {_FAIL} 缺少测试素材")
        return False

    mp4_path = work_dir / "corrupt_check.mp4"
    mp4 = remux_to_mp4(source, mp4_path, ffmpeg_path=ffmpeg)
    if mp4 is None or not Path(mp4).exists():
        print(f"      {_FAIL} 转封装失败，无法进行损坏测试")
        return False

    # 人为破坏码流数据：打乱文件后半段的一小段字节，让 H.264 解码错位。
    data = bytearray(Path(mp4).read_bytes())
    start = len(data) // 2
    for i in range(start, min(start + 8192, len(data))):
        data[i] = (data[i] + 0x5A) & 0xFF
    Path(mp4).write_bytes(data)

    ok, err = verify_decodable(mp4, ffmpeg_path=ffmpeg)
    print(
        f"      {_PASS if not ok else _FAIL} 损坏文件被校验检出"
        f"（通过={ok}，摘要：{err[:80]!r}）"
    )
    return not ok


def test_recorder_lifecycle(ffmpeg: str, work_dir: Path) -> bool:
    """验证录制器完整生命周期：目录结构、命名规则、事件序列。"""
    print("\n[测试 4] 录制器完整生命周期（本地 HTTP 模拟直播流）……")

    output_root = work_dir / "recordings_lifecycle"

    def tune(config: AppConfig) -> None:
        config.convert_to_mp4 = True   # 顺带验证转封装接入

    recorder, events, _ = _record_session(ffmpeg, work_dir, output_root, tune)
    event_names = [name for name, _ in events]
    final = recorder.final_path

    checks: List[Tuple[str, bool, str]] = []

    # 目录结构：平台/主播昵称（两级，**日期不再单独建目录**，已并入文件名）
    expected_dir = output_root / "抖音" / "自检主播"
    dir_ok = expected_dir.is_dir()
    # 顺带确认没有遗留的日期子目录（如 2026年09月24日 / 2026-09-24）
    stray_date_dirs = [
        child.name for child in expected_dir.iterdir()
        if child.is_dir()
    ] if expected_dir.is_dir() else []
    checks.append(("目录结构（平台/主播昵称两级，无日期层）", dir_ok, str(expected_dir)))
    checks.append(("昵称目录下不残留日期子目录", not stray_date_dirs,
                   str(stray_date_dirs)))

    # 收尾产出
    file_ok = final is not None and Path(final).exists() and Path(final).stat().st_size > 0
    checks.append(("收尾产出文件", file_ok, str(final)))

    # 命名规则：平台-主播昵称-YYYY-MM-DD_HHMMSS-序号.(flv|mp4)
    if file_ok:
        name = Path(final).name
        name_ok = bool(re.fullmatch(
            r"抖音-自检主播-\d{4}-\d{2}-\d{2}_\d{6}-(merged|\d{3})(-\d+)?\.(flv|mp4)",
            name,
        ))
    else:
        name, name_ok = "-", False
    checks.append(("文件命名规则（平台-昵称-日期_时刻-序号）", name_ok, name))

    # 事件序列
    event_ok = "recorder_started" in event_names and "recorder_stopped" in event_names
    checks.append(("事件序列完整", event_ok, ",".join(sorted(set(event_names)))))

    # 录制确实产生了数据
    final_size = Path(final).stat().st_size if file_ok else 0
    checks.append(("产出文件非空", final_size > 10_000, f"{final_size / 1024:.1f} KB"))

    return _report(checks)


def test_rotation_and_merge(ffmpeg: str, work_dir: Path) -> bool:
    """验证自动分卷 + 多分片无损合并 + 原始分片清理。

    通过「把单分片体积上限设得极小」+「限速传输模拟实时直播流」，
    强制在录制过程中触发分卷，从而复现「一场直播产生多个分片」的场景。
    """
    print("\n[测试 5] 自动分卷 + 多分片合并……")

    output_root = work_dir / "recordings_rotation"

    def tune(config: AppConfig) -> None:
        config.max_file_size_gb = 0.0002   # 约 210KB，强制多次分卷
        config.convert_to_mp4 = False

    # 以约 45 KB/s 限速传输（源片约 460KB，全程约 10 秒），逼近真实直播码率
    recorder, events, stream_seconds = _record_session(
        ffmpeg,
        work_dir,
        output_root,
        tune,
        throttle_bps=45 * 1024,
        wait_predicate=lambda r: r.stats().segment_count >= 3,
        wait_timeout=45.0,
    )
    event_names = [name for name, _ in events]

    # 从收尾事件里取分片数量
    stopped_payload = next((p for n, p in events if n == "recorder_stopped"), {})
    segment_count = int(stopped_payload.get("segments") or 0)
    final = recorder.final_path

    checks: List[Tuple[str, bool, str]] = []
    checks.append(("触发自动分卷（分片数 >= 2）", segment_count >= 2, f"{segment_count} 个分片"))
    checks.append(
        ("产生多次分片完成事件", event_names.count("segment_finished") >= 2,
         f"segment_finished x{event_names.count('segment_finished')}")
    )

    file_ok = final is not None and Path(final).exists() and Path(final).stat().st_size > 0
    checks.append(("合并产出成品文件", file_ok, str(final)))

    if file_ok:
        out_dir = Path(final).parent
        duration = probe_duration(final, ffmpeg_path=ffmpeg)
        # 合并不丢内容：成品时长应接近模拟流总时长
        duration_ok = duration >= stream_seconds * 0.8
        checks.append(
            ("合并后时长完整（无内容丢失）", duration_ok,
             f"{duration:.2f}s / 源 {stream_seconds}s")
        )
        # 合并成功后原始分片应被清理
        # 分片名形如 抖音-自检主播-141530-001.flv（连字符分隔，序号 3 位）
        leftover = [
            p.name
            for p in out_dir.glob("*-[0-9][0-9][0-9].flv")
            if p.name != Path(final).name and not p.name.startswith(".")
        ]
        checks.append(
            ("原始分片已清理",
             not leftover,
             f"残留 {len(leftover)} 个" if leftover else "已全部清理")
        )
    return _report(checks)


def test_naming_rules(ffmpeg: str, work_dir: Path) -> bool:
    """验证命名契约：回退命名、日期时间进文件名、会话级冻结。

    覆盖四个容易回归的点：
    1. 昵称为空时**不得**把房间号伪装成昵称，而是用「未知主播」占位，
       目录追加房间号消歧；
    2. 目录只有**两级** ``平台/昵称``，日期不单独建目录；
    3. 文件名格式为 ``平台-昵称-YYYY-MM-DD_HHMMSS-序号.flv``，且不含房间号；
    4. 录制中途平台回填昵称，**不得**让本场分片散落到第二个目录。
    """
    print("\n[测试 6] 命名规则（回退 / 日期进文件名 / 会话冻结）……")

    output_root = work_dir / "recordings_naming"

    recorder, events, _ = _record_session(
        ffmpeg,
        work_dir,
        output_root,
        tune=lambda c: None,
        nickname="",                     # 昵称识别失败
        room_id="NingXinHui716",
        mid_session_nickname="中途回填昵称",   # 中途才解析出昵称
        tail_wait=5.0,
    )
    final = recorder.final_path
    files = sorted(
        p.relative_to(output_root).as_posix()
        for p in output_root.rglob("*")
        if p.is_file()
    )

    checks: List[Tuple[str, bool, str]] = []
    checks.append((
        "昵称缺失时回退为「未知主播-房间号」目录",
        any("未知主播-NingXinHui716" in f for f in files),
        files[0] if files else "无产出",
    ))
    checks.append((
        "房间号未被伪装成昵称（目录名不含纯房间号的昵称段）",
        not any("/NingXinHui716/" in f for f in files),
        "目录名合法" if files else "无产出",
    ))
    # 目录须只有两级：平台/昵称（日期已并入文件名，不再单独建目录）
    depth_ok = bool(files) and all(len(f.split("/")) == 3 for f in files)
    checks.append((
        "目录为两级 平台/昵称（无日期层）",
        depth_ok,
        files[0] if files else "无产出",
    ))
    # 文件名格式：平台-昵称-YYYY-MM-DD_HHMMSS-序号（可带 -2 去重后缀）
    # 昵称可能含横线，故用正则锚定「_HHMMSS-序号.容器」结尾，不按 "-" 硬切
    name_ok = False
    sample = Path(files[0]).name if files else ""
    if sample:
        name_ok = bool(re.fullmatch(
            r".+-\d{4}-\d{2}-\d{2}_\d{6}-(?:\d{3}|merged)(?:-\d+)?\.(flv|mp4)",
            sample,
        ))
    checks.append((
        "文件名格式 平台-昵称-日期_时刻-序号",
        name_ok,
        sample or "无产出",
    ))
    checks.append((
        "文件名不含房间号（房间号只在目录层消歧）",
        not any("NingXinHui716" in Path(f).name for f in files),
        sample or "无产出",
    ))
    checks.append((
        "会话冻结：中途回填的昵称未改变本场产出位置",
        not any("中途回填昵称" in f for f in files),
        "未受影响" if files else "无产出",
    ))
    # 所有产出必须落在同一个主播目录下（不因中途回填而分裂）
    nick_dirs = {f.split("/")[1] for f in files if len(f.split("/")) > 1}
    checks.append((
        "本场全部产出位于同一目录",
        len(nick_dirs) == 1,
        f"目录数 {len(nick_dirs)}: {sorted(nick_dirs)}",
    ))
    checks.append((
        "生成成品文件",
        final is not None and Path(final).exists() and Path(final).stat().st_size > 0,
        str(final),
    ))
    return _report(checks)


def _report(checks: List[Tuple[str, bool, str]]) -> bool:
    """输出检查项结果。"""
    all_ok = True
    for title, ok, detail in checks:
        print(f"      {_PASS if ok else _FAIL} {title}：{detail}")
        all_ok = all_ok and ok
    return all_ok


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def main() -> int:
    configure_logging()
    print("=" * 68)
    print("LiveRecorder 冒烟测试（本地执行，不访问任何第三方平台）")
    print("=" * 68)

    print("\n[测试 1] 检查 ffmpeg 可用性……")
    ffmpeg = resolve_ffmpeg("ffmpeg")
    if not ffmpeg or not ffmpeg_available("ffmpeg"):
        print(f"      {_FAIL} 未找到可用的 ffmpeg，请先安装并加入 PATH")
        return 1
    print(f"      {_PASS} ffmpeg：{ffmpeg}")

    work_dir = Path(tempfile.mkdtemp(prefix="live_recorder_smoke_"))
    print(f"      临时工作目录：{work_dir}")

    results: List[Tuple[str, bool]] = [("ffmpeg 可用性", True)]
    try:
        results.append(("多分片无损合并", test_merge(ffmpeg, work_dir)))
        results.append(("MP4 转封装", test_remux_mp4(ffmpeg, work_dir)))
        results.append(("完整性校验检出损坏", test_verify_rejects_corrupt(ffmpeg, work_dir)))
        results.append(("录制器生命周期", test_recorder_lifecycle(ffmpeg, work_dir)))
        results.append(("自动分卷与合并", test_rotation_and_merge(ffmpeg, work_dir)))
        results.append(("命名规则", test_naming_rules(ffmpeg, work_dir)))
    except Exception as exc:  # noqa: BLE001 - 冒烟测试要给出明确失败原因
        logger.exception("测试执行异常：%s", exc)
        results.append(("测试执行", False))
    finally:
        if "--keep" in sys.argv:
            print(f"\n临时文件已保留：{work_dir}")
        else:
            shutil.rmtree(work_dir, ignore_errors=True)

    print("\n" + "=" * 68)
    print("测试结果汇总")
    print("=" * 68)
    failed = 0
    for title, ok in results:
        print(f"  {_PASS if ok else _FAIL} {title}")
        if not ok:
            failed += 1

    if failed:
        print(f"\n有 {failed} 项未通过，请检查上面的输出。")
        return 1
    print("\n全部通过 ✔  录制链路工作正常。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
