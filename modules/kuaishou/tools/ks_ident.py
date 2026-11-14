# -*- coding: utf-8 -*-
r"""ks_ident.py — 看清「我是谁、有几个风控桶、每个桶还剩多少额度」。

## 这个脚本回答什么问题

「再搞几个 ck 有没有用？能不能多开几路？」

答案取决于**桶**：快手的风控额度看起来是绑在**设备**上的，而设备就是 cookie 里的
`did`（36 位）。账号是另一个 cookie `userId`（10 位）—— 两个各自独立：

- 同一个浏览器 profile 里登出再登另一个号 → `userId` 变、**`did` 不变**
  → **同一个桶**，多登号不增加额度。
- 换一份 profile（`--user-data-dir` 不同）或换台机器 → 新的 `did`
  → **不同的桶**，各有一份额度。

这个脚本干的事就是：把每份登录态的 `did`/`userId` 读出来、算成桶、
和守卫盘上的额度对起来，让你**直接看见**上面那句话在你的机器上是哪种情况。

## 跑法

    python ks_ident.py                          # 只读报告（不启动浏览器）
    python ks_ident.py --register               # 顺便读 cookie 把没登记的补登记
    python ks_ident.py --profile E:\录屏\ks-b   # 临时加一份 profile

    # 查别的落点（诊断用，正常不用）
    set KS_GUARD_HOME=D:\tmp\g && python ks_ident.py

## 现在都还没登记过？

注册表是这次才加的，所以第一份登录态要**补登记**一次：

    python ks_ident.py --register

之后启动就纯查 `idents.json`，不再需要开浏览器（冷却期要 0.5 秒内收工）。

## 注意

- `--register` 会短暂启动一次**无头**浏览器去解密 cookie（Windows 上 cookie 值
  是 DPAPI 加密的，纯 Python 读不出明文）。它**不访问快手**，只读本地 cookie 库。
- `data\kuaishou\edge-manual-profile`（CDP 调试用的那份 Edge）**不在扫描范围**：
  它的 cookie 是从登录 profile **注入**过去的，所以它自己的 did 根本不参与风控；
  把它列进来只会把报告搅乱。
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import ks_guard
except Exception:
    ks_guard = None

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

# 和 kuaishou_headless / 一键下载器 同一套「登录成功」判据。
# ⚠️ 判据里**不能**放 `did`：`did` 是匿名访问就会种下的设备 cookie，
#    只看它会把「没登录的 profile」也报成有登录。
LOGIN_KEYS = ("passToken", "kuaishou.server.web_st", "kuaishou.server.web_day7_st", "userId")


def _root():
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here)),
              os.path.dirname(os.path.dirname(os.path.dirname(here)))):
        if os.path.isdir(os.path.join(d, "data", "kuaishou")):
            return d
    return os.path.dirname(here)


ROOT = _root()


def candidates(extra=()):
    """要检查的登录态目录：默认那份 + KS_PROFILES + runtime 下像登录态的 + 命令行指定。"""
    seen, out = set(), []

    def add(p, why):
        if not p:
            return
        k = os.path.normcase(os.path.abspath(p))
        if k in seen:
            return
        seen.add(k)
        out.append((os.path.abspath(p), why))

    add(os.path.join(ROOT, "runtime", "kuaishou-login-profile"), "默认登录态")
    for p in (os.environ.get("KS_PROFILES") or "").replace(",", ";").split(";"):
        add(p.strip(), "KS_PROFILES")
    rt = os.path.join(ROOT, "runtime")
    if os.path.isdir(rt):
        for n in sorted(os.listdir(rt)):
            if n == "kuaishou-login-profile" or not os.path.isdir(os.path.join(rt, n)):
                continue
            if "profile" in n.lower() or n.lower().startswith("ks-"):
                add(os.path.join(rt, n), "runtime 下发现")
    for p in extra:
        add(p, "命令行指定")
    return out


def has_login(cookies):
    names = {c.get("name") for c in (cookies or []) if isinstance(c, dict)}
    return bool(names & set(LOGIN_KEYS))


def _load_reg():
    try:
        with open(ks_guard.ident_reg_path(), encoding="utf-8") as f:
            got = json.load(f)
        return got if isinstance(got, dict) else {}
    except Exception:
        return {}


def fmt_used(st):
    ccap = ks_guard._cap("KS_MAX_COLLECT_PER_DAY", ks_guard.DEFAULT_MAX_COLLECT_PER_DAY)
    dcap = ks_guard._cap("KS_MAX_DETAIL_PER_DAY", ks_guard.DEFAULT_MAX_DETAIL_PER_DAY)
    return "收集 %d/%d、详情 %d/%d" % (int(st.get("collect_used") or 0), ccap,
                                      int(st.get("detail_used") or 0), dcap)


def main():
    ap = argparse.ArgumentParser(description="快手身份 / 风控桶 报告")
    ap.add_argument("--register", action="store_true",
                    help="读 cookie 把没登记的登录态补登记（会短暂启动无头浏览器，不访问快手）")
    ap.add_argument("--profile", action="append", default=[],
                    help="额外检查的 profile 目录，可重复")
    args = ap.parse_args()

    if ks_guard is None:
        print("✘ 找不到 ks_guard.py —— 这个脚本要和它放在同一个目录。")
        return 2

    print("快手身份 / 风控桶 报告")
    print("=" * 68)
    print("状态落点：%s" % ks_guard.out_dir())
    print("分桶依据：cookie `did`（设备，36 位）。`userId`（账号）只作展示 ——")
    print("          同一台设备换账号 did 不变，所以**不**多给额度。")
    print()

    reg = _load_reg()
    print("【登录态】")
    rows = []
    for p, why in candidates(args.profile):
        exists = os.path.isdir(p)
        rec = reg.get(os.path.normcase(os.path.abspath(p)))
        note = ""
        if args.register and exists:
            cks = ks_guard.read_profile_cookies(p)
            if cks and not has_login(cks):
                note = "  → 读了 cookie，但里面没有登录项（这份 profile 没登录）"
            elif cks:
                b = ks_guard.remember_identity(p, cks)
                if b:
                    parts = ks_guard.ident_parts(cks)
                    rec = {"bucket": b, "did": parts.get("did", ""),
                           "userId": parts.get("userId", "")}
                    note = "  → 已登记"
                else:
                    note = "  → 读到了 cookie 但没有 did/userId，登记不了"
            else:
                note = "  → 读不到 cookie（浏览器没起来 / 目录不是 Chromium profile？）"

        if not exists:
            mark = "✘目录不存在"
        elif rec and rec.get("userId"):
            mark = "✅已登记"
        elif rec:
            mark = "◐只差账号"
        else:
            mark = "❔未登记"
        rows.append({"path": p, "why": why, "rec": rec or {}})

        r = rec or {}
        print("  %-10s %s   (%s)" % (mark, p, why))
        if exists:
            print("             did=%s…  userId=%s%s"
                  % ((r.get("did") or "?")[:10], r.get("userId") or "?",
                     "  桶 %s" % r["bucket"] if r.get("bucket") else ""))
            if not r.get("bucket"):
                print("             ⚠️ 没有桶 → 会按 default 桶计额度（与其它未登记的登录态合并）")
        if note:
            print(note)

    # ── 桶 → 额度 ──
    print()
    print("【桶 → 今日额度】")
    snaps = dict((b, (st, leg)) for b, st, leg in ks_guard.buckets_snapshot())
    by_bucket = {}
    for r in rows:
        bk = (r["rec"] or {}).get("bucket")
        if bk:
            by_bucket.setdefault(bk, []).append(r["path"])
    known = set(by_bucket) | set(snaps)
    if not known:
        print("  （还没有任何桶 —— 跑一次登录，或 python ks_ident.py --register）")
    for bk in sorted(known):
        disp = bk if bk != ks_guard.BUCKET_DEFAULT else "default（未认领槽）"
        st, leg = snaps.get(bk, (ks_guard._blank(), False))
        cool_on, left, until = ks_guard.blocked_info(bucket=bk)
        tag = ("冷却中（截止 %s，剩 %d 分钟）" % (until, int(left // 60) + 1)
               if cool_on else "正常")
        if leg:
            tag += " ←未认领的旧状态"
        print("  %s   %s" % (disp, tag))
        print("               %s%s" % (fmt_used(st),
                                     "" if not st.get("block_count")
                                     else "   已连续撞墙 %d 次" % st["block_count"]))
        for p in by_bucket.get(bk, []):
            print("               ← %s" % p)

    # ── 结论 ──
    print()
    print("【结论】")
    # ⚠️ 结论只看「有登录态映射过去」的桶 —— `default` 是身份未知时的合计槽，
    #    不是一路额度，计进桶数会把「只有一份登录态」误报成「有两路」。
    n = len(by_bucket)
    if n == 0:
        print("  还没有任何登录态登记过身份 —— 先补登记一次：")
        print("      %s ks_ident.py --register" % _py())
    elif n == 1:
        print("  目前只有 1 个桶 → **多个 ck 现在没有收益**。")
        print("  同一台设备上再登几个号：`userId` 变了、`did` 没变 → 还是这一个桶，")
        print("  公用一份额度；而且两个号一起打只是把请求形态变得更激进。")
        print()
        print("  想真正多一路，需要拿到**新的 did** = 新的 profile 目录：")
        print("      set KS_PROFILE=E:\\录屏\\ks-b")
        print("      %s kuaishou_login.py            （扫码登第二个号）" % _py())
        print("      %s ks_ident.py --register        （登记后即可用）" % _py())
    else:
        print("  有 %d 个不同的桶 → **每个桶各有一份额度**（默认每天各 400），" % n)
        print("  可以并行跑，互不抢额度、也互不抢租约。跑法：")
        print("      窗口 A：正常启动（默认 profile）")
        print("      窗口 B：set KS_PROFILE=E:\\录屏\\ks-b   然后跑同一个入口")
    print()
    print("  ⚠️ 一个本机测不出来的变量：如果平台还有 cookie 之外的指纹（canvas/字体/")
    print("     出口 IP），这些桶在服务端**可能仍被合并**。真伪只能靠撞墙情况判断 ——")
    print("     所以别一上来就把每路都开满，先让第二路跑小批量观察。")
    print()
    print("  放宽单桶额度（自担风险）：KS_MAX_COLLECT_PER_DAY / KS_MAX_DETAIL_PER_DAY")
    return 0


def _py():
    return os.path.basename(sys.executable)


if __name__ == "__main__":
    sys.exit(main())
