# -*- coding: utf-8 -*-
"""
分片无损合并与转封装

场景
----
一场直播可能因为「拉流中断重连」或「单文件达到 4GB 自动分卷」而产生多个分片。
下播后需要把它们合并成一个完整文件。

三级策略（逐级降级，全部为无损操作）
------------------------------------
1. **直接 concat**：``-f concat -c copy``，速度最快；
2. **归一化后 concat**：若合并结果的时长与各分片时长总和的偏差过大
   （通常是某一分片的起始时间戳不归零导致 concat 时叠加了偏移），
   则先用 ``-fflags +genpts -avoid_negative_ts make_zero`` 把每个分片的
   时间戳归零，再重新 concat；
3. **二进制拼接**：TS 容器下直接字节级拼接，天然无损且不受时间戳影响。

合并完成后可选地转封装为 MP4（同样 ``-c copy``，不重编码）。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ..utils.logger import get_logger
from .ffmpeg_utils import parse_ffmpeg_error, probe_duration, run_ffmpeg
from .storage import ensure_dir

logger = get_logger(__name__)

# 合并结果与分片时长总和的相对偏差容忍度：超过该比例视为时间戳异常
_DURATION_TOLERANCE = 0.25


def merge_segments(
    segments: Sequence[str | Path],
    output_path: str | Path,
    ffmpeg_path: str = "ffmpeg",
    container: str = "flv",
) -> Optional[Path]:
    """把多个分片无损合并为一个文件。

    参数
    ----
    segments:    分片路径列表（按时间先后顺序）
    output_path: 合并后目标文件路径
    ffmpeg_path: ffmpeg 可执行文件路径
    container:   容器类型，"flv" 或 "ts"

    返回
    ----
    最终成品文件路径；合并失败返回 None。

    - 无有效分片      -> None
    - 仅 1 个有效分片 -> 直接返回该分片（不重命名，保持分片命名规则）
    - 合并成功        -> 返回 ``output_path``
    """
    valid = [Path(p) for p in segments if Path(p).exists() and Path(p).stat().st_size > 0]
    if not valid:
        logger.warning("没有可合并的分片（文件不存在或为空）")
        return None

    output = Path(output_path)
    ensure_dir(output.parent)

    # 只有一个分片：它本身就是完整录制，无需合并也无需改名
    if len(valid) == 1:
        logger.info("本场直播仅有 1 个分片，无需合并：%s", valid[0].name)
        return valid[0]

    logger.info("开始合并 %d 个分片 -> %s", len(valid), output.name)

    # ---- 第 1 级：直接 concat ----
    if _merge_with_concat_demuxer(valid, output, ffmpeg_path, container, tag="直接"):
        if _duration_is_sane(valid, output, ffmpeg_path):
            return output
        logger.warning(
            "合并结果时长与各分片时长总和偏差过大，疑似分片时间戳未归零，"
            "改用逐段归一化后重新合并"
        )
        _safe_unlink(output)

    # ---- 第 2 级：逐段归一化时间戳后 concat ----
    normalized = _normalize_segments(valid, output.parent, ffmpeg_path, container)
    if normalized:
        ok = _merge_with_concat_demuxer(
            normalized, output, ffmpeg_path, container, tag="归一化后"
        )
        if ok and output.exists() and output.stat().st_size > 0:
            _cleanup_temp(normalized)
            logger.info(
                "归一化合并完成：%s（%.1f MB）",
                output.name,
                output.stat().st_size / 1024 ** 2,
            )
            return output
        _cleanup_temp(normalized)

    # ---- 第 3 级：TS 容器二进制拼接（绝对无损） ----
    if container.lower() == "ts":
        logger.warning("ffmpeg 合并未成功，改用二进制拼接方式（TS 容器安全无损）")
        if _merge_by_binary_concat(valid, output):
            return output

    logger.error("分片合并不成功，原始分片已保留：%s", [p.name for p in valid])
    return None


# --------------------------------------------------------------------------
# 具体实现
# --------------------------------------------------------------------------
def _merge_with_concat_demuxer(
    segments: List[Path],
    output: Path,
    ffmpeg_path: str,
    container: str,
    tag: str = "",
) -> bool:
    """使用 ffmpeg concat 解复用器合并（推荐方式）。"""
    list_file = output.parent / f".concat_{output.stem}_{abs(hash(tag)) & 0xFFFF}.txt"
    try:
        with list_file.open("w", encoding="utf-8") as fp:
            for segment in segments:
                # concat 清单中的路径需转义单引号
                safe_path = str(segment.resolve()).replace("'", "'\\''")
                fp.write(f"file '{safe_path}'\n")

        fmt_flag = "mpegts" if container.lower() == "ts" else "flv"
        code, output_text = run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel", "warning",
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                # 重新生成 PTS，并对负时间戳做归零处理，提升兼容性
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-f", fmt_flag,
                "-y",
                str(output),
            ],
            ffmpeg_path=ffmpeg_path,
        )
        if code == 0 and output.exists() and output.stat().st_size > 0:
            logger.info(
                "%s合并完成：%s（%.1f MB）",
                f"{tag}" if tag else "",
                output.name,
                output.stat().st_size / 1024 ** 2,
            )
            return True
        logger.warning("%s合并未成功：%s", f"{tag}" if tag else "", parse_ffmpeg_error(output_text))
        return False
    except OSError as exc:
        logger.warning("concat 清单写入失败：%s", exc)
        return False
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except OSError:
            pass


def _normalize_segments(
    segments: List[Path],
    work_dir: Path,
    ffmpeg_path: str,
    container: str,
) -> List[Path]:
    """把每个分片的时间戳归零，输出到临时文件，返回新的分片路径列表。

    用于修复「分片起始时间戳不为零」导致的合并时长翻倍问题。
    任一分片归一化失败时，整体放弃（返回空列表），避免产出错误成品。
    """
    fmt_flag = "mpegts" if container.lower() == "ts" else "flv"
    normalized: List[Path] = []

    for index, segment in enumerate(segments, start=1):
        temp_path = work_dir / f".norm_{index:03d}_{segment.stem}.{container}"
        code, output_text = run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel", "error",
                "-i", str(segment),
                "-c", "copy",
                "-fflags", "+genpts",
                "-avoid_negative_ts", "make_zero",
                "-f", fmt_flag,
                "-y",
                str(temp_path),
            ],
            ffmpeg_path=ffmpeg_path,
        )
        if code != 0 or not temp_path.exists() or temp_path.stat().st_size == 0:
            logger.warning(
                "分片时间戳归一化失败（%s）：%s",
                segment.name,
                parse_ffmpeg_error(output_text),
            )
            _cleanup_temp(normalized)
            _safe_unlink(temp_path)
            return []
        normalized.append(temp_path)

    logger.info("已完成 %d 个分片的时间戳归一化", len(normalized))
    return normalized


def _duration_is_sane(segments: List[Path], merged: Path, ffmpeg_path: str) -> bool:
    """校验合并结果时长是否与分片时长总和接近。

    偏差过大说明分片时间戳存在叠加偏移（最典型的是时长翻倍），
    需要走归一化路径重新合并。
    """
    merged_duration = probe_duration(merged, ffmpeg_path=ffmpeg_path)
    if merged_duration <= 0:
        # 探测不到时长（例如 ffprobe 缺失）时不做判断，信任 ffmpeg 的结果
        return True

    total = 0.0
    for segment in segments:
        total += probe_duration(segment, ffmpeg_path=ffmpeg_path)
    if total <= 0:
        return True

    deviation = abs(merged_duration - total) / total
    if deviation > _DURATION_TOLERANCE:
        logger.debug(
            "时长校验：合并 %.2fs vs 分片总和 %.2fs（偏差 %.0f%%）",
            merged_duration,
            total,
            deviation * 100,
        )
        return False
    return True


def _merge_by_binary_concat(segments: List[Path], output: Path) -> bool:
    """TS 分片的二进制直接拼接（最快的无损合并方式）。"""
    try:
        with output.open("wb") as out_fp:
            for segment in segments:
                with segment.open("rb") as in_fp:
                    shutil.copyfileobj(in_fp, out_fp, length=1024 * 1024)
        if output.stat().st_size > 0:
            logger.info(
                "二进制拼接完成：%s（%.1f MB）",
                output.name,
                output.stat().st_size / 1024 ** 2,
            )
            return True
        return False
    except OSError as exc:
        logger.error("二进制拼接失败：%s", exc)
        return False


def _cleanup_temp(paths: List[Path]) -> None:
    """清理中间产生的临时文件。"""
    for path in paths:
        _safe_unlink(path)


def _safe_unlink(path: Path) -> None:
    """安全删除文件（失败仅记录调试日志）。"""
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.debug("删除临时文件失败 %s：%s", path, exc)


# --------------------------------------------------------------------------
# 转封装
# --------------------------------------------------------------------------

# 解码校验时判定「数据损坏」的关键词。B 帧重排产生的 dts 单调性警告是无害的，
# 不在此列；只有真正的码流损坏（Invalid NAL / corrupt / 解码失败）才算数。
_DECODE_ERROR_KEYWORDS = (
    "invalid nal",
    "error while decoding",
    "corrupt",
    "missing picture",
    "invalid data found",
    "missing pps",
    "no frame",
)


def verify_decodable(
    path: str | Path,
    ffmpeg_path: str = "ffmpeg",
    timeout: Optional[float] = None,
) -> Tuple[bool, str]:
    """对已转封装的文件做**完整解码**校验，确认码流无损坏。

    背景：某些直播平台（如快手）的私有 metadata 会在 ``-c copy`` 转封装成 MP4 时
    被 ffmpeg 错误写进视频数据区，导致样本索引与实际 H.264 数据错位。这种损坏
    转封装命令本身返回 0、不报任何错，文件头也正常，只有**实际解码**才会在
    错位点报 ``Invalid NAL unit size`` 等错误。

    因此转封装成功后必须做一次完整解码，确认能从头播到尾；有损坏则判定失败。

    返回 ``(是否通过, 错误摘要)``。错误摘要在通过时为空串。
    """
    code, output = run_ffmpeg(
        [
            "-hide_banner",
            "-v", "error",
            # 必须单线程解码：多线程（帧级并行）遇到码流错位时会漏报错误，
            # 把损坏文件误判为完好（实测同一文件多线程 0 错误、单线程数百条）。
            "-threads", "1",
            "-i", str(path),
            "-f", "null",
            "-",
        ],
        ffmpeg_path=ffmpeg_path,
        timeout=timeout,
    )

    # ffmpeg 异常（超时 / 执行失败）：无法确认文件完好，一律判为校验不通过，
    # 交给调用方保留原文件，避免「校验没跑成却放行」的假绿灯。
    if code < 0:
        return False, f"解码校验异常：{output or 'ffmpeg 无法执行'}"

    # 排除 B 帧重排导致的 dts 单调性警告（正常现象，不是损坏）
    cleaned = "\n".join(
        line
        for line in output.splitlines()
        if "non monotonically increasing dts" not in line
        and "non-monotonous dts" not in line
    )
    lowered = cleaned.lower()
    if any(keyword in lowered for keyword in _DECODE_ERROR_KEYWORDS):
        return False, parse_ffmpeg_error(cleaned)
    return True, ""


def remux_to_mp4(
    source: str | Path,
    target: Optional[str | Path] = None,
    ffmpeg_path: str = "ffmpeg",
) -> Optional[Path]:
    """把录制文件转封装为 MP4（``-c copy``，不重编码，速度极快）。

    返回目标文件路径；失败返回 None（原文件不受影响）。
    """
    src = Path(source)
    if not src.exists():
        logger.warning("转封装源文件不存在：%s", src)
        return None

    dst = Path(target) if target else src.with_suffix(".mp4")
    if dst == src:
        dst = src.with_name(src.stem + "_mp4.mp4")
    ensure_dir(dst.parent)

    logger.info("开始转封装为 MP4（不重编码）：%s", dst.name)
    code, output_text = run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel", "warning",
            "-i", str(src),
            "-c", "copy",
            # faststart 让 moov 前置，便于播放器秒开
            "-movflags", "+faststart",
            "-y",
            str(dst),
        ],
        ffmpeg_path=ffmpeg_path,
    )

    if code == 0 and dst.exists() and dst.stat().st_size > 0:
        # 转封装命令返回 0 只说明「写出了一个 MP4」，不代表码流没有错位 ——
        # 快手的私有 metadata 会在 -c copy 转封装时被塞进视频数据区，
        # 只有完整解码才能发现（见 verify_decodable 的说明）。
        ok, verify_err = verify_decodable(dst, ffmpeg_path=ffmpeg_path)
        if not ok:
            logger.warning(
                "转封装产物解码校验失败，删除损坏 MP4 并保留原文件 %s：%s",
                src.name,
                verify_err,
            )
            _safe_unlink(dst)
            return None
        logger.info("转封装完成并通过解码校验：%s", dst.name)
        return dst

    logger.warning("转封装 MP4 失败：%s", parse_ffmpeg_error(output_text))
    return None
