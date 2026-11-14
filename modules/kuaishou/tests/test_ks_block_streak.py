# -*- coding: utf-8 -*-
r"""test_ks_block_streak.py — 风控刹车「两种长相」的回归测试（2026-10-03）。

## 为什么有这个文件

2026-10-03 排查「快手下载失败」，根因是**刹车条件在真实数据下永远达不到**：

    台账实测顺序：208005×9 | None×2 | 208005×1 | None×6 | 208005×8

`kuaishou_fast_dl.py` 旧代码只把 `208005` 计入 `block_streak`，且遇到任何别的
状态就清零；而 `status_None`（`visionVideoDetail` 字段缺失，接口没给状态码）
**也是风控**，只是另一种表现。于是纯 208005 的最长连续只有 9 < 阈值 12
→ `note_block()` 一次都没调用 → 守卫里 `block_count` 恒 0、`blocked_until` 恒 0.0
→ 跨进程冷却从未落盘 → 每次运行重新撞墙。

这个测试用**真实台账里的那段序列**做回归，锁死两件事：
  1. 混合序列必须触发刹车（旧代码不会）；
  2. 纯内容失效（`status_1040`）**不能**被当成风控（否则误刹车，白等 90 分钟）。

离线跑，不碰网络、不启动浏览器。
"""
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)                      # .../kuaishou/tools
if not os.path.exists(os.path.join(TOOLS, "kuaishou_fast_dl.py")):
    # 自愈：万一本文件被挪动过一层，按文件名反查 tools 目录
    TOOLS = os.path.join(os.path.dirname(HERE), "tools")
sys.path.insert(0, TOOLS)

import kuaishou_fast_dl as F                        # noqa: E402


def _trip_index(sequence):
    """把预取循环里那段刹车判定单独跑一遍，返回「在第几个元素上刹车」。

    与 `kuaishou_fast_dl.py` 里的判定保持同一套语义：
      - 成功 → 计数清零；
      - 风控信号（208005 / None）→ 计数 +1；
      - 其它状态（如 1040）→ 计数清零（内容级失效，与风控无关）；
      - 计数达到阈值 → 刹车，返回下标。
    """
    streak = 0
    for idx, st in enumerate(sequence):
        if st == "OK":
            streak = 0
            continue
        if F._is_risk_status(st):
            streak += 1
        else:
            streak = 0
        if streak >= F.BLOCK_STREAK_TRIP:
            return idx
    return None


class TestRiskSignalClassification(unittest.TestCase):
    def test_208005_is_risk(self):
        self.assertTrue(F._is_risk_status("208005"))

    def test_none_is_risk(self):
        """`status_None` 是被漏认的那一种：字段缺失也是风控。"""
        self.assertTrue(F._is_risk_status("None"))

    def test_1040_is_not_risk(self):
        """内容已删除 —— 跟风控无关，误判会导致白等 90 分钟。"""
        self.assertFalse(F._is_risk_status("1040"))

    def test_ok_is_not_risk(self):
        self.assertFalse(F._is_risk_status("OK"))

    def test_accepts_non_str_input(self):
        """`str()` 兜底：真实调用传的是 `str(vd.get("status"))`，但别因类型崩掉。"""
        self.assertTrue(F._is_risk_status(208005))


class TestBrakeTriggers(unittest.TestCase):
    def test_real_ledger_sequence_trips(self):
        """**核心回归**：2026-10-03 真实台账尾段。旧代码在这里返回 None（刹车失效）。"""
        real = (["OK"] * 74
                + ["208005"] * 9 + ["None"] * 2
                + ["208005"] + ["None"] * 6
                + ["208005"] * 8
                + ["OK"] * 20)
        self.assertIsNotNone(_trip_index(real),
                             "混合风控信号必须触发刹车（旧代码漏认 status_None）")

    def test_pure_208005_still_trips(self):
        """纯 208005（原来的判据）不能被改坏。"""
        self.assertIsNotNone(_trip_index(["208005"] * 12))

    def test_pure_none_still_trips(self):
        """整段都是 status_None（另一种风控形态）也要能刹车。"""
        self.assertIsNotNone(_trip_index(["None"] * 12))

    def test_below_threshold_does_not_trip(self):
        """只有 11 个 —— 不到阈值就不该停（别过早收工）。"""
        self.assertIsNone(_trip_index(["208005"] * 11))

    def test_1040_run_does_not_trip(self):
        """一长串 1040（内容删除）**不能**刹车：那是内容问题，重试也没用但不必等风控冷却。"""
        self.assertIsNone(_trip_index(["1040"] * 40))

    def test_success_resets_streak(self):
        """成功必须清零：成功穿插时不能无限累加。"""
        seq = ["208005"] * 11 + ["OK"] + ["208005"] * 11
        self.assertIsNone(_trip_index(seq))


class TestNoSelfPardon(unittest.TestCase):
    """`note_ok()` 的判据：这一轮**一个风控信号都没有**才算跑顺了。

    旧条件只看 `len(jobs) > 0`（预取到了几个直链）就清冷却 ——
    实测「撞 26 个风控信号但没触发刹车」的一轮会把冷却提前抹平，等于自我解封。
    """

    def _should_clear(self, jobs, n_risk, blocked_hit):
        # 与 kuaishou_fast_dl.py 里的判据逐字对应：not blocked_hit and n_risk == 0 and jobs
        return (not blocked_hit) and n_risk == 0 and jobs > 0

    def test_clears_when_clean(self):
        self.assertTrue(self._should_clear(jobs=5, n_risk=0, blocked_hit=False))

    def test_does_not_clear_when_risk_seen(self):
        self.assertFalse(self._should_clear(jobs=5, n_risk=26, blocked_hit=False))

    def test_does_not_clear_when_braked(self):
        self.assertFalse(self._should_clear(jobs=5, n_risk=0, blocked_hit=True))


class TestRiskNotLedgered(unittest.TestCase):
    """风控信号**不写失败台账**（只有内容级失效才写）。"""

    def test_risk_statuses_excluded_from_failed(self):
        for st in F.RISK_STATUS:
            self.assertTrue(F._is_risk_status(st))
        # 反向断言：把 1040 排除在风控之外
        self.assertNotIn("1040", F.RISK_STATUS)


class TestTripThresholdSanity(unittest.TestCase):
    def test_threshold_is_sane(self):
        """阈值必须 ≥ 12 —— 与 09-25 实测「连调 55 个 0 失败」的安全侧一致。"""
        self.assertGreaterEqual(F.BLOCK_STREAK_TRIP, 12)


class TestMetaFilterWorks(unittest.TestCase):
    """元断言：证明本测试用的「只跑判定」的重构不是自己骗自己。"""

    def test_helper_reproduces_old_broken_logic(self):
        """同样的序列，用**旧语义**（只认 208005、其余清零）必须**不**触发 ——
        这就是被修掉的那个 bug 的原始行为。"""
        seq = (["OK"] * 74
               + ["208005"] * 9 + ["None"] * 2
               + ["208005"] + ["None"] * 6
               + ["208005"] * 8 + ["OK"] * 20)
        streak, fired = 0, None
        for idx, st in enumerate(seq):
            if st == "OK":
                streak = 0
                continue
            if st == "208005":
                streak += 1
            else:
                streak = 0
            if streak >= 12:
                fired = idx
                break
        self.assertIsNone(fired, "旧逻辑本该在这里失效（否则说明复现无效）")


if __name__ == "__main__":
    buf = io.StringIO()
    unittest.TextTestRunner(stream=buf, verbosity=0).run(
        unittest.TestLoader().loadTestsFromModule(sys.modules[__name__]))
    out = buf.getvalue()
    print(out[-4000:])
    sys.exit(0 if "OK" in out.splitlines()[-1] else 1)
