# -*- coding: utf-8 -*-
"""命名契约验证：覆盖正常 / 回退 / 冲突三类场景。

命名格式（唯一权威见 recorder/storage.py 模块 docstring）::

    目录：<root>/<平台>/<昵称>/
    分片：<平台>-<昵称>-<YYYY-MM-DD_HHMMSS>-<序号>.flv
    成品：<平台>-<昵称>-<YYYY-MM-DD_HHMMSS>-merged.flv

日期**不再**单独建目录层，而是并入文件名的时间分量（用户要求：
「命名直接加上时间，不要给时间单独创建文件夹」）。

运行（需把模块的**上一级**目录放进 sys.path，因为目录名即包名）：
    python tools/_check_naming.py
"""
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from live_recorder.recorder.storage import (  # noqa: E402
    MAX_DEDUP_TRIES,
    UNKNOWN_NICKNAME,
    build_merged_path,
    build_output_dir_from_naming,
    build_segment_path,
    make_session_naming,
    reserve_path,
    sanitize_name,
)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"{'OK ' if ok else 'FAIL'} | {label}\n       got  = {got!r}\n       want = {want!r}")
    if not ok:
        FAILED.append(label)


WHEN = datetime(2026, 8, 28, 14, 15, 30)
STAMP = "2026-08-28_141530"

print("=== 1. sanitize_name ===")
check("普通昵称原样保留", sanitize_name("阿姨芥末"), "阿姨芥末")
check("非法字符替换", sanitize_name('a/b:c*d?e"f<g>h|i'), "a_b_c_d_e_f_g_h_i")
check("连续空白折叠", sanitize_name("盐甜  跃跃子"), "盐甜_跃跃子")
check("中间连字符保留", sanitize_name("阿姨-芥末"), "阿姨-芥末")
check("首尾连字符剥除（避免与分隔符混淆）", sanitize_name("-阿姨-"), "阿姨")
check("首尾点下划线连字符清理", sanitize_name(" ._-主播-_. "), "主播")
check("空串回退", sanitize_name(""), UNKNOWN_NICKNAME)
check("纯空白回退", sanitize_name("   "), UNKNOWN_NICKNAME)
check("纯非法字符回退", sanitize_name("///"), UNKNOWN_NICKNAME)
check("超长截断", len(sanitize_name("x" * 200)) <= 60, True)

print("\n=== 2. 正常命名（日期时间进文件名）===")
naming = make_session_naming("douyin", "阿姨芥末", "622593204340", WHEN)
check("平台中文名", naming.platform_label, "抖音")
check("昵称", naming.nickname, "阿姨芥末")
check("目录昵称片段", naming.dir_nickname, "阿姨芥末")
check("日期分量（带横线）", naming.date_token, "2026-08-28")
check("时刻分量", naming.time_token, "141530")
check("日期+时刻整体分量（下划线相连）", naming.datetime_token, STAMP)
check("分片文件名", naming.segment_name(1), f"抖音-阿姨芥末-{STAMP}-001.flv")
check("分片 12 补零", naming.segment_name(12), f"抖音-阿姨芥末-{STAMP}-012.flv")
check("成品文件名", naming.merged_name(), f"抖音-阿姨芥末-{STAMP}-merged.flv")
check("ts 容器", naming.segment_name(1, "ts"), f"抖音-阿姨芥末-{STAMP}-001.ts")
check("文件名不含房间号", "622593204340" not in naming.segment_name(1), True)

print("\n=== 3. 快手昵称 ===")
ks = make_session_naming("kuaishou", "超级玛丽🥰", "cl3503011999", WHEN)
check("快手平台名", ks.platform_label, "快手")
check("快手文件名", ks.segment_name(1), f"快手-超级玛丽🥰-{STAMP}-001.flv")

print("\n=== 4. 回退命名（昵称识别失败）===")
fb = make_session_naming("kuaishou", "", "NingXinHui716", WHEN)
check("昵称回退占位", fb.nickname, UNKNOWN_NICKNAME)
check("目录片段带房间号消歧", fb.dir_nickname, "未知主播-NingXinHui716")
check("文件名用占位昵称（不带房间号）",
      fb.segment_name(1), f"快手-未知主播-{STAMP}-001.flv")

fb2 = make_session_naming("kuaishou", "", "forever1421yl", WHEN)
check("不同未知房间目录不冲突", fb.dir_nickname != fb2.dir_nickname, True)

print("\n=== 5. 昵称=房间号的纠偏（历史调用方习惯）===")
legacy = make_session_naming("kuaishou", "cl3503011999", "cl3503011999", WHEN)
check("检测为未识别", legacy.nickname, UNKNOWN_NICKNAME)
check("目录带房间号", legacy.dir_nickname, "未知主播-cl3503011999")
check("文件名不出现房间号",
      legacy.segment_name(1), f"快手-未知主播-{STAMP}-001.flv")

print("\n=== 6. 未知平台 ===")
unk = make_session_naming("", "某人", "12345", WHEN)
check("平台回退", unk.platform_label, "未知平台")
check("未知平台文件名", unk.segment_name(1), f"未知平台-某人-{STAMP}-001.flv")

print("\n=== 7. 文件级冲突规避 ===")
tmp = Path(tempfile.mkdtemp(prefix="lr-naming-"))
try:
    s1 = f"抖音-某人-{STAMP}-001"
    target = tmp / f"{s1}.flv"
    check("首次不冲突原样返回", reserve_path(target), target)
    target.write_bytes(b"x")
    check("已存在则 -2", reserve_path(target).name, f"{s1}-2.flv")
    (tmp / f"{s1}-2.flv").write_bytes(b"x")
    check("再冲突则 -3", reserve_path(target).name, f"{s1}-3.flv")
    for i in range(2, MAX_DEDUP_TRIES + 2):
        (tmp / f"{s1}-{i}.flv").write_bytes(b"x")
    fallback = reserve_path(target)
    check("兜底仍不覆盖且名字更长",
          fallback.exists() is False and fallback.name != target.name, True)

    print("\n=== 8. 端到端：目录 + 分片（回退场景）===")
    root = tmp / "root"
    d = build_output_dir_from_naming(root, fb)
    check("目录只有两级（平台/昵称，无日期层）",
          d.relative_to(root).as_posix(), "快手/未知主播-NingXinHui716")
    seg = build_segment_path(d, "kuaishou", "", WHEN, 1, "flv", room_id="NingXinHui716")
    check("落地分片名", seg.name, f"快手-未知主播-{STAMP}-001.flv")
    seg.write_bytes(b"data")
    again = build_segment_path(d, "kuaishou", "", WHEN, 1, "flv", room_id="NingXinHui716")
    check("重名自动避让", again.name, f"快手-未知主播-{STAMP}-001-2.flv")
    m = build_merged_path(d, "kuaishou", "", WHEN, "flv", room_id="NingXinHui716")
    check("成品名", m.name, f"快手-未知主播-{STAMP}-merged.flv")

    print("\n=== 9. 同主播多次录制一致性 ===")
    later = make_session_naming("douyin", "阿姨芥末", "622593204340",
                                datetime(2026, 8, 28, 20, 0, 0))
    check("同主播不同场次：昵称/房间号一致",
          (later.nickname, later.room_id) == (naming.nickname, naming.room_id), True)
    check("同一天不同场次：时刻不同 → 文件名不冲突",
          later.segment_name(1) != naming.segment_name(1), True)
    check("同一天不同场次：落在同一个文件夹（不再按日分目录）",
          build_output_dir_from_naming(root, later)
          == build_output_dir_from_naming(root, naming), True)

    print("\n=== 10. 跨天 ===")
    other_day = make_session_naming("douyin", "阿姨芥末", "622593204340",
                                    datetime(2026, 12, 1, 9, 5, 0))
    check("跨天：日期分量变化", other_day.date_token, "2026-12-01")
    check("跨天：月日补零", other_day.segment_name(1),
          "抖音-阿姨芥末-2026-12-01_090500-001.flv")
    check("跨天：仍落在同一个主播文件夹",
          build_output_dir_from_naming(root, other_day)
          == build_output_dir_from_naming(root, naming), True)

    print("\n=== 11. 日期时间分量可被反向解析 ===")
    # 文件名结构：<平台>-<昵称>-<YYYY-MM-DD>_<HHMMSS>-<序号>
    #
    # ⚠️ 日期里带横线，所以**不能**按 "-" 切分来定位时间戳：
    # `split("-")` 会把 2026-09-24 拆成三段，`parts[-2]` 只会拿到 "24_205245"。
    # 正确做法是**锚定 `_HHMMSS` 这个固定形状**（下划线 + 6 位数字）：
    # 用正则找出它的位置，左边是日期、右边是序号，中间留白即昵称。
    import re
    pat = re.compile(r"_(?P<time>\d{6})-(?P<seq>\d{3}|merged)(?:-\d+)?$")
    fname = "快手-J姜月亮_🌙-2026-09-24_205245-001.mp4"
    stem = fname.rsplit(".", 1)[0]
    m2 = pat.search(stem)
    check("能锚定 _HHMMSS-序号 结尾", m2 is not None, True)
    if m2:
        check("解析出时刻", m2.group("time"), "205245")
        check("解析出序号", m2.group("seq"), "001")
        head = stem[: m2.start()]              # 快手-J姜月亮_🌙-2026-09-24
        date_part = head[-10:]                 # 固定 10 位的 YYYY-MM-DD
        label = head[:-11]                     # 去掉日期与它前面的分隔符
        check("解析出日期", date_part, "2026-09-24")
        check("解析出平台", label.split("-", 1)[0], "快手")
        check("解析出昵称", label.split("-", 1)[1], "J姜月亮_🌙")

    # 无昵称回退场景（含 unknown 占位与房间号）也要能解析
    fname2 = "快手-未知主播-cl3503011999-2026-09-24_205245-merged.mp4"
    stem2 = fname2.rsplit(".", 1)[0]
    m3 = pat.search(stem2)
    check("回退命名也能锚定", m3 is not None, True)
    if m3:
        check("回退命名序号位是 merged", m3.group("seq"), "merged")
        head2 = stem2[: m3.start()]
        check("回退命名日期", head2[-10:], "2026-09-24")
        check("回退命名昵称（含房间号）", head2[:-11].split("-", 1)[1],
              "未知主播-cl3503011999")

    # 二次去重后缀（-2 / -3）也要能被吃掉
    fname3 = "快手-某人-2026-09-24_205245-001-2.mp4"
    m4 = pat.search(fname3.rsplit(".", 1)[0])
    check("去重后缀 -2 可被识别", m4 is not None and m4.group("seq") == "001", True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 60)
if FAILED:
    print(f"FAILED {len(FAILED)}: {FAILED}")
    sys.exit(1)
print("ALL PASSED")
