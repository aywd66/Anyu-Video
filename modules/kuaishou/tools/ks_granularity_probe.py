# -*- coding: utf-8 -*-
"""ks_granularity_probe.py — 判定快手风控的**粒度**：账号级，还是单页面级？

## 这个脚本回答什么问题

「多开几个窗口并行跑，会不会更快？」

答案完全取决于风控的阈值绑在哪：

- **绑在账号上**（同一份 cookie）→ 多窗口**毫无收益，纯有害**：总吞吐不变，
  但请求速率翻 n 倍，只会更早撞墙、把冷却拉长。
- **绑在单个页面上下文上** → 多窗口**真的能 n 倍**，因为每个页面各有一份额度。

## 为什么现在还不知道

手头最硬的那条反证 —— 「撞墙 5h24m 后跑单条成功」—— 同时满足两件事：
**换了新页面** 和 **冷却已过**。两个变量混在一起，所以它没法区分粒度。
（这一条已如实记在 `docs/验证台账.md` O8「未验证的假设」里。）

## 怎么区分（关键设计）

两种模式，**总请求数和账号级速率完全一致**，只有「铺在几个页面上」不同：

| 模式 | 布置 | 若阈值是账号级 | 若阈值是页面级 |
|---|---|---|---|
| `serial` | 1 个页面串行打 N 个 | 约在第 Q 个开始全灭 | 约在第 Q 个开始全灭 |
| `rr` | P 个页面**轮转**打，共 N 个 | 约在第 Q 个开始全灭（**同一个 Q**） | 每个页面各撑到 Q → 总共约 **P×Q** |

轮转（round-robin）顺序是 A,B,C,A,B,C… 所以**账号看到的请求速率和串行完全一样**。
这就把「粒度」从「速率」里隔离出来了 —— 这是本实验唯一有价值的地方，
换成一口气把 A 打完再打 B，就会把两个变量又搅在一起。

判读：先跑 `serial` 拿到基线 Q，再跑 `rr` 看总数落在 Q 还是 P×Q。

## 代价（说清楚）

- 会**真实消耗**当日详情额度（默认 45 次）。
- 大概率会**再次触发冷却**（这正是实验的目的 —— 它就是要把阈值撞出来）。
- 所以尽量在**刚出冷却、会话是冷的**时候跑。脚本会检查「第一条就必须成功」，
  否则直接判定本次无效（会话本来就在封里，测了也不算）。

## 跑法

    # 1) 基线：单页面串行 45 个
    python ks_granularity_probe.py --mode serial --total 45

    # 2) 等冷却过去之后，轮转：3 个页面 × 每个 15 个
    python ks_granularity_probe.py --mode rr --pages 3 --per-page 15

结果追加进 `data/kuaishou/probe_granularity.jsonl`，脚本会自动跟历史结果对比并给判读。
**这不是产品功能**，是一次性的取证工具，不从任何菜单入口调用。
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import ks_guard
except Exception:
    ks_guard = None

import kuaishou_fast_dl as k   # 复用 ensure_edge / inject_cookies / DETAIL_Q

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

OUTDIR = os.environ.get("KS_OUTDIR") or k.OUTDIR
# 单页面串行的历史基线：09-25 16:53 那次「先深滚 1790 个、之后第 12 个详情起全灭」。
# 只作参考，真正的基线要用 --mode serial 现测（不同时间点的阈值可能已经变了）。
SERIAL_BASELINE_HINT = 12


def find_list():
    """找一个现成的作品清单当 PID 来源（探测不需要重新收集，省额度）。

    递归找是因为历史遗留：早期版本的清单落在了 `OUTDIR/edge-manual-profile/`
    里（实测那儿有 755 / 477 / 267 条几份），只扫顶层会一份都找不到。
    """
    cands = []
    if os.path.isdir(OUTDIR):
        for root, dirs, files in os.walk(OUTDIR):
            if root[len(OUTDIR):].count(os.sep) >= 3:
                dirs[:] = []
                continue
            for n in files:
                if n.startswith("works_list") and n.endswith(".json"):
                    cands.append(os.path.join(root, n))
    # 优先 pending（几千条的大号清单），其次按体积
    cands.sort(key=lambda p: ("pending" not in p, -os.path.getsize(p)))
    for p in cands:
        try:
            got = json.load(open(p, encoding="utf-8-sig"))
        except Exception:
            continue
        ids = [str(w.get("id")) for w in got if isinstance(w, dict) and w.get("id")]
        if len(ids) >= 20:
            return p, ids
    return None, []


def load_cookies():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        c0 = p.chromium.launch_persistent_context(k.PROFILE, channel="msedge", headless=True,
                                                  user_agent=k.UA, locale="zh-CN",
                                                  viewport={"width": 1280, "height": 900})
        ck = c0.cookies()
        c0.close()
    return ck


def ask(pg, pid):
    """调一次详情接口。返回 (status, 是否 http 层报错)。"""
    try:
        r = pg.evaluate(
            """async (args) => {
                const resp = await fetch('/graphql', {method: 'POST',
                    headers: {'Content-Type': 'application/json'}, credentials: 'include',
                    body: JSON.stringify({operationName: 'visionVideoDetail',
                        variables: {photoId: args.pid, page: 'main', webPageArea: ''},
                        query: args.q})});
                return await resp.text();
            }""", {"pid": pid, "q": k.DETAIL_Q})
        vd = ((json.loads(r).get("data") or {}).get("visionVideoDetail") or {})
        return str(vd.get("status")), None
    except Exception as e:
        return "", str(e)[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["serial", "rr"], default="rr")
    ap.add_argument("--total", type=int, default=45, help="serial 模式的总请求数")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--per-page", type=int, default=15)
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--list", default="")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不发一个请求")
    args = ap.parse_args()

    total = args.total if args.mode == "serial" else args.pages * args.per_page
    pages = 1 if args.mode == "serial" else max(2, args.pages)

    print("== 风控粒度判别 ==")
    print("模式: %s | 页面数: %d | 总请求: %d | 间隔: %.2fs" % (args.mode, pages, total, args.sleep))

    # ── 守卫：冷却中就别测了，测了也不作数 ──
    if ks_guard:
        print(ks_guard.budget_line())
        if ks_guard.blocked_info()[0]:
            print(ks_guard.cooling_message())
            print("[probe] 会话还在冷却里 —— 现在测出来的只会是「已封状态」。收工。")
            return 0
        _ok, _holder = ks_guard.acquire_run("probe")
        if not _ok:
            print(ks_guard.conflict_message(_holder))
            return 0
        _allow = ks_guard.allow_detail(total)
        if _allow < total:
            print("[probe] 今日详情额度只剩 %d，本次只测 %d 个 —— 数量不足会削弱判读力。"
                  % (_allow, _allow))
            total = _allow
            if args.mode == "rr":
                args.per_page = max(1, total // pages)
                total = args.per_page * pages
        if total < 6:
            print(ks_guard.budget_exhausted_message())
            return 0

    lp = args.list
    ids = []
    if lp:
        ids = [str(w.get("id")) for w in json.load(open(lp, encoding="utf-8-sig"))
               if isinstance(w, dict) and w.get("id")]
    else:
        lp, ids = find_list()
    if len(ids) < total:
        print("[probe] PID 不够（需要 %d，拿到 %d）—— 先用 collect 模式扫一份清单。" % (total, len(ids)))
        return 3
    print("[probe] PID 来源: %s (%d 个可用)" % (lp or "(显式传入)", len(ids)))
    ids = ids[:total]

    # ── 排班表：serial = 一条道；rr = 轮转 ──
    if args.mode == "serial":
        schedule = [(0, pid) for pid in ids]
    else:
        per = total // pages
        schedule = []
        for kth in range(per):
            for pi in range(pages):
                schedule.append((pi, ids[pi * per + kth]))
        print("[probe] 轮转顺序: 页面 %s 依次各打 1 个，共 %d 轮"
              % ("/".join(str(i) for i in range(pages)), per))

    if args.dry_run:
        print("[probe] dry-run：计划 %d 个请求" % len(schedule))
        for i, (pi, pid) in enumerate(schedule[:12]):
            print("   %2d  page=%d  %s" % (i + 1, pi, pid))
        return 0

    k.ensure_edge()
    from playwright.sync_api import sync_playwright

    cookies = load_cookies()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(k.CDP)
        ctx = browser.contexts[0]
        k.inject_cookies(ctx, cookies)

        pgs = []
        for i in range(pages):                      # 每个页面都是**全新的**
            pg = ctx.new_page()
            pg.goto("https://www.kuaishou.com/short-video/" + ids[i * max(1, len(ids) // pages)],
                    wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(3500)
            pgs.append(pg)
        print("[probe] %d 个全新页面已就绪" % len(pgs))
        time.sleep(1.0)

        first_ok = [None] * pages     # 每页第一个成功的序号
        first_block = [None] * pages  # 每页第一个 208005 的序号
        ok_total = 0
        calls = 0
        timeline = []
        aborted = ""

        for i, (pi, pid) in enumerate(schedule):
            st, err = ask(pgs[pi], pid)
            calls += 1
            if st == "1":
                ok_total += 1
                if first_ok[pi] is None:
                    first_ok[pi] = i + 1
            elif st == "208005" and first_block[pi] is None:
                first_block[pi] = i + 1
            timeline.append({"n": i + 1, "page": pi, "pid": pid, "status": st or ("ERR:" + str(err))})
            print("  [%2d/%d] page=%d %s  status=%s" % (i + 1, len(schedule), pi, pid, st or "ERR"))
            if i == 0 and st != "1":
                aborted = ("第一条就失败（status=%s）→ 会话本来就是热的/被封的，"
                           "这次测量无效。等冷却结束后重跑。" % (st or err))
                break
            if all(b is not None for b in first_block):
                aborted = "所有页面都已 208005，没有继续打的必要"
                break
            time.sleep(args.sleep)

        if ks_guard:
            ks_guard.commit_detail(calls)

        res = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "mode": args.mode, "pages": pages, "sleep": args.sleep,
            "planned": len(schedule), "calls": calls,
            "ok_total": ok_total, "first_ok": first_ok, "first_block": first_block,
            "aborted": aborted, "timeline": timeline,
        }
        hist = os.path.join(OUTDIR, "probe_granularity.jsonl")
        try:
            os.makedirs(OUTDIR, exist_ok=True)
            with open(hist, "a", encoding="utf-8") as f:
                f.write(json.dumps(res, ensure_ascii=False) + "\n")
        except Exception as e:
            print("[probe] 结果写盘失败: %s" % e)

        print()
        print("== 结果 ==")
        print("总请求 %d，成功 %d，每次成功请求前的累计成功数 = %d" % (calls, ok_total, ok_total))
        print("各页第一个 208005 出现在第 %s 个（None = 全程没撞）" % first_block)
        if aborted:
            print("⚠️ 提前终止：%s" % aborted)
        print("本次判读：%s" % verdict(res))
        print("明细已追加: %s" % hist)
        if ks_guard:
            ks_guard.release_run()
    return 0


def verdict(res):
    """把一次测量翻译成人话。**宁可说「不够判」也不要过度解读。**"""
    if res["aborted"].startswith("第一条就失败"):
        return "无效 —— 会话不冷，重跑"
    okn = res["ok_total"]
    if res["mode"] == "serial":
        q = okn
        return ("基线 Q ≈ %d（单页面串行能撑住这么多次）。"
                "接下来跑 `--mode rr`：若总数仍 ≈ %d → 账号级；"
                "若总数 ≈ %d×页面数 → 页面级。" % (q, q, q))
    died = sum(1 for b in res["first_block"] if b is not None)
    if died < 2:
        return ("只有 %d 个页面撞墙 —— 在 %d 次这个量级上还没撞到天花板，"
                "本次**说明不了**粒度（但顺带说明阈值 > %d）。要更狠地测：加大 --per-page。"
                % (died, res["calls"], okn))
    baseline = _last_serial_q() or SERIAL_BASELINE_HINT
    ratio = okn / float(baseline) if baseline else 0
    if ratio <= 1.6:
        return ("总数 %d ≈ 单页面基线 %d 的同一量级 → **账号级**。"
                "多开窗口不会提高总量，只会把请求速率乘以页面数、更早撞墙。"
                "→ 结论：不要多开，安心按当日额度拆到多天。" % (okn, baseline))
    if ratio >= 2.0:
        return ("总数 %d 明显高于单页面基线 %d（×%.1f）→ 倾向**页面级**。"
                "多窗口**可能**真的有收益 —— 但只测了一次，"
                "建议换个时间再跑一次同样参数复现，再按页面级设计并发。"
                % (okn, baseline, ratio))
    return ("总数 %d 落在基线 %d 的灰区（×%.2f）—— **不够判**。"
            "加大 --per-page 或换个时间复现。" % (okn, baseline, ratio))


def _last_serial_q():
    try:
        p = os.path.join(OUTDIR, "probe_granularity.jsonl")
        last = None
        for line in open(p, encoding="utf-8"):
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("mode") == "serial" and not str(d.get("aborted") or "").startswith("第一条"):
                last = d
        return last.get("ok_total") if last else None
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
