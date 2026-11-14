# -*- coding: utf-8 -*-
"""qr_svg 与参考实现（PyPI `qrcode`）逐模块比对

这个测试**不参与线上运行**，也不进工具箱自检（tests\\selftest.py）—— 它需要一个
装着 `qrcode` 的外部解释器当对照物，而工具箱的硬约束是自带运行时、零额外依赖。
二维码是「手机下载」页面的关键一环，错了就是扫码扫不出来，所以宁可留一份对照脚本，
让后续改动能一键验证，而不是靠肉眼看着「像二维码」就上线。

跑法（用任意装了 qrcode 的解释器）：
    <python> tests\\test_qr_svg.py
    # 本机现成可用的一个（受管运行时里装过 qrcode）：
    #   C:\\Users\\Mechrevo\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe
    # 注意：工具箱自带的 runtime\\douyin-venv 里**没有** qrcode，
    # 用它会走到下面那条「跳过」分支 —— 看到「跳过」不等于通过，别误判。
未安装 qrcode 时直接跳过并以 0 退出，避免在别人的环境里变成红色噪音。

比对口径：同一份字节内容、同一纠错等级下，
  1. 自动选出的掩码号必须一致；
  2. 整个模块矩阵（含格式信息、版本信息、暗模块）必须逐格一致。
"""
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "modules", "douyin", "webui"))

try:
    import qrcode
    from qrcode.util import MODE_8BIT_BYTE, QRData
except Exception as e:                                   # pragma: no cover
    print("跳过：本机没有 qrcode 参考实现（%s）" % e)
    sys.exit(0)

import qr_svg                                            # noqa: E402

LEVELS = [("L", qrcode.constants.ERROR_CORRECT_L),
          ("M", qrcode.constants.ERROR_CORRECT_M)]


def oracle(payload, level, mask=None):
    """参考实现给出的模块矩阵（True=深色）。"""
    q = qrcode.QRCode(version=None, error_correction=level, box_size=1,
                      border=0, mask_pattern=mask)
    q.add_data(QRData(payload, mode=MODE_8BIT_BYTE))
    q.make(fit=True)
    return q.modules


def oracle_auto_mask(payload, level):
    """参考实现自动选择的掩码号：拿 8 个固定掩码逐个对，对上哪个就是哪个。"""
    auto = oracle(payload, level)
    for m in range(8):
        if oracle(payload, level, mask=m) == auto:
            return m
    raise AssertionError("参考实现自动掩码结果不属于 0–7 任一固定掩码")


def compare(payload, ec_name, level, check_mask=True):
    mine, my_mask = qr_svg.matrix(payload, ec_name)
    ref = oracle(payload, level)
    if mine != ref:
        n = len(ref)
        diff = [(r, c) for r in range(n) for c in range(n) if mine[r][c] != ref[r][c]]
        raise AssertionError(
            "内容长度 %d / EC %s：矩阵不一致，%d 格不同，前几处 %s"
            % (len(payload), ec_name, len(diff), diff[:5]))
    if check_mask:
        ref_mask = oracle_auto_mask(payload, level)
        if my_mask != ref_mask:
            raise AssertionError(
                "内容长度 %d / EC %s：掩码选择不同（本实现 %d，参考 %d）"
                % (len(payload), ec_name, my_mask, ref_mask))
    return my_mask


def main():
    cases = 0
    seen_versions = set()
    rnd = random.Random(20260925)

    for ec_name, level in LEVELS:
        # 1) 每个版本都用「刚好塞满该版本」的长度打一遍，确保分块表逐行正确
        for v in range(1, qr_svg._MAX_VERSION + 1):
            cap = qr_svg._data_codewords(v, ec_name)
            cnt_bits = 8 if v <= 9 else 16
            max_len = (cap * 8 - 4 - cnt_bits) // 8
            for n_bytes in {max_len, max(1, max_len - 1), max(1, max_len // 2)}:
                payload = bytes(rnd.randrange(256) for _ in range(n_bytes))
                compare(payload, ec_name, level)
                cases += 1
            # 确认长度确实落在预期版本上（防呆：别把边界算错还自我感觉良好）
            v_got = qr_svg._pick_version(max_len, ec_name)
            seen_versions.add(v_got)
            assert v_got == v, "长度 %d 应落在 v%d，实际 v%d" % (max_len, v, v_got)

        # 2) 随机长度扫一遍
        for _ in range(120):
            n_bytes = rnd.randrange(1, 200)
            payload = bytes(rnd.randrange(256) for _ in range(n_bytes))
            compare(payload, ec_name, level)
            cases += 1

    # 3) 真实场景：中文标题 + 局域网地址
    for txt in ("http://192.168.1.7:8520",
                "http://10.0.0.23:8520/?t=手机下载",
                "https://v.douyin.com/ABCDEFG/",
                "X" * 1, "Y" * 213):
        for ec_name, level in LEVELS:
            compare(txt.encode("utf-8"), ec_name, level)
            cases += 1

    # 4) SVG 产物自检：尺寸对得上，且 path 段数 = 全部深色连续段数
    svg = qr_svg.qr_svg("http://192.168.1.7:8520", scale=8, border=2)
    m, _ = qr_svg.matrix("http://192.168.1.7:8520")
    n = len(m)
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in svg
    assert 'width="%d"' % ((n + 4) * 8) in svg
    runs = 0
    for row in m:
        prev = False
        for v in row:
            if v and not prev:
                runs += 1
            prev = v
    strokes = svg.count("v8h-")
    assert strokes == runs, "SVG 段数 %d ≠ 深色连续段数 %d" % (strokes, runs)

    # 5) 缩放与留白参数真正生效
    big = qr_svg.qr_svg("http://192.168.1.7:8520", scale=16, border=4)
    assert 'width="%d"' % ((n + 8) * 16) in big
    assert big.count("v16h-") == runs

    print("通过：%d 组比对全部一致（覆盖版本 %s，纠错等级 L/M）"
          % (cases, "、".join("v%d" % v for v in sorted(seen_versions))))


if __name__ == "__main__":
    main()
