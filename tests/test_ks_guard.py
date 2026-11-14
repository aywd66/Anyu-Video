# -*- coding: utf-8 -*-
"""快手风控守卫自检（离线，**不碰网络**）

为什么单独测这个：守卫的全部价值就是「该刹车的时候真的刹住」。它一旦失灵，
表现是**下载照常跑**——看起来一切正常，直到号又被风控封掉。所以这里除了
逐条断言，还专门起一个真子进程验证「冷却中时入口当场拒绝、且退出码是 0」。

覆盖四类：

  1. **当日预算**：额度截断、核销、用完归零、跨天重置、环境变量覆盖与非法值回落。
  2. **冷却**：首次 90 分钟、指数退避、封顶 24 小时、到期自动失效、成功后清除。
  3. **容错**：状态文件损坏、落点不可写、`KS_GUARD_OFF=1` 全放行 ——
     守卫绝对不能自己变成新的故障点。
  4. **接线**：两个入口真的调了守卫；旧的「等 12 分钟 × 8 轮」已彻底移除
     （这一条是**反向断言**：只要那段代码回来就报 FAIL）。

跑法（标准库即可，任何 python 3.8+ 都行）：
    runtime\\kuaishou-env\\Scripts\\python.exe tests\\test_ks_guard.py
"""
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "modules", "kuaishou", "tools")
sys.path.insert(0, TOOLS)

PASSES = []
FAILS = []


def check(name, ok, detail=""):
    (PASSES if ok else FAILS).append(name)
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name,
                        ("  — " + str(detail)) if detail else ""))


import ks_guard as g  # noqa: E402  （必须先加 sys.path）


def fresh():
    return tempfile.mkdtemp(prefix="ksguard_")


def read_src(name):
    p = os.path.join(TOOLS, name)
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def code_only(text):
    """只留**标识符/语法**，把注释和字符串（含 docstring）全部丢掉。

    ⚠️ 反向断言必须扫这个而不是全文 —— 否则「在注释里写明为什么不能用
    `os.kill(pid, 0)`」会被自己的注释判成违规。

    ⚠️⚠️ 别用「逐行看有没有三引号」这种手写状态机：**单行 docstring**
    （`\"\"\"一句话。\"\"\"`）是开完就关的，相位不该翻转，而朴素写法会把它当
    成「开了没关」→ 从那一行起相位整体反转 → 后面真正的注释行反而被当代码扫进来。
    真踩过：`gsave/load/cooling_message` 里几条单行 docstring 一连，两条反向
    断言就一起挂了，而看起来像是"代码里真的用了 os.kill"。
    交给 `tokenize` 不会有这种错。
    """
    try:
        toks = tokenize.generate_tokens(io.StringIO(text).readline)
        return "\n".join(t.string for t in toks
                         if t.type not in (tokenize.COMMENT, tokenize.STRING))
    except Exception:
        return text


# ── 1. 当日预算 ────────────────────────────────────────────────────────────
def test_budget():
    d = fresh()
    check("初始：详情额度等于默认上限",
          g.allow_detail(10 ** 6, d) == g.DEFAULT_MAX_DETAIL_PER_DAY,
          g.allow_detail(10 ** 6, d))
    check("初始：收集剩余等于默认上限",
          g.collect_left(d) == g.DEFAULT_MAX_COLLECT_PER_DAY, g.collect_left(d))

    check("申请量小于额度 → 原样放行", g.allow_detail(30, d) == 30)
    check("申请量大于额度 → 截断到额度",
          g.allow_detail(10 ** 6, d) == g.DEFAULT_MAX_DETAIL_PER_DAY)

    g.commit_detail(g.DEFAULT_MAX_DETAIL_PER_DAY, d)
    check("核销满额后 → 允许 0 个", g.allow_detail(50, d) == 0, g.allow_detail(50, d))
    check("额度用完时不抛异常，只是返回 0（调用方负责解释）", True)

    d2 = fresh()
    g.commit_collect(120, d2)
    check("收集核销后剩余减少",
          g.collect_left(d2) == g.DEFAULT_MAX_COLLECT_PER_DAY - 120,
          g.collect_left(d2))
    check("收集量超出剩余 → 截断",
          g.allow_collect(10 ** 6, d2) == g.DEFAULT_MAX_COLLECT_PER_DAY - 120)

    # 跨天：预算归零，但**冷却不减**（风控窗口跟自然日无关）
    d3 = fresh()
    st = g.load(d3)
    st.update({"day": "2000-01-01", "collect_used": 999, "detail_used": 999,
               "blocked_until": time.time() + 3600, "block_count": 2})
    g.save(st, d3)
    st2 = g.load(d3)
    check("跨自然日：预算归零", st2["collect_used"] == 0 and st2["detail_used"] == 0,
          (st2["collect_used"], st2["detail_used"]))
    check("跨自然日：冷却保持不变（这是刻意的）",
          g.blocked_info(d3)[0] is True and st2["block_count"] == 2)

    # 环境变量
    os.environ["KS_MAX_DETAIL_PER_DAY"] = "7"
    try:
        check("环境变量可收紧额度", g.allow_detail(100, fresh()) == 7)
        os.environ["KS_MAX_DETAIL_PER_DAY"] = "abc"
        check("环境变量非法 → 回落默认值，而不是 0 或崩溃",
              g.allow_detail(10 ** 6, fresh()) == g.DEFAULT_MAX_DETAIL_PER_DAY)
        os.environ["KS_MAX_DETAIL_PER_DAY"] = "-5"
        check("环境变量负数 → 回落默认值",
              g.allow_detail(10 ** 6, fresh()) == g.DEFAULT_MAX_DETAIL_PER_DAY)
    finally:
        os.environ.pop("KS_MAX_DETAIL_PER_DAY", None)


# ── 2. 冷却 ────────────────────────────────────────────────────────────────
def test_cooling():
    d = fresh()
    check("初始：不在冷却", g.blocked_info(d)[0] is False)
    check("初始：冷却不产生提示语", g.cooling_message(d) == "")

    m1 = g.note_block(d, "test")
    check("首次撞墙 = %d 分钟（旧行为 12 分钟，实测无效）" % g.BASE_COOLDOWN_MIN,
          m1 == g.BASE_COOLDOWN_MIN, m1)
    on, left, until = g.blocked_info(d)
    check("撞墙后进入冷却，且给出截止时间", on and left > 0 and bool(until), until)
    check("冷却提示语包含「几时再来」", bool(until) and until in g.cooling_message(d))

    m2 = g.note_block(d, "test")
    check("再次撞墙 → 退避翻倍（%d 分钟）" % (g.BASE_COOLDOWN_MIN * 2),
          m2 == g.BASE_COOLDOWN_MIN * 2, m2)

    # 连续撞墙要封顶，不能指数爆炸成几年
    for _ in range(12):
        last = g.note_block(d, "soak")
    check("连续撞墙 → 封顶 24 小时", last == g.COOLDOWN_CAP_MIN, last)

    # 到期自动失效
    st = g.load(d)
    st["blocked_until"] = time.time() - 5
    g.save(st, d)
    check("冷却到期 → 自动放行（不需要人工清）", g.blocked_info(d)[0] is False)

    g.note_block(d)
    g.note_ok(d)
    check("这一轮跑顺了 → 冷却与退避计数一起清零",
          g.blocked_info(d)[0] is False and g.load(d)["block_count"] == 0)

    g.note_block(d)
    g.clear(d)
    check("人工解封可用（换网络/重登后想立刻重试）", g.blocked_info(d)[0] is False)

    check("冷却上限是 24 小时", g.COOLDOWN_CAP_MIN == 24 * 60)


# ── 3. 容错：守卫不能自己变成故障点 ────────────────────────────────────────
def test_robustness():
    d = fresh()
    with open(g.state_path(d), "w", encoding="utf-8") as f:
        f.write("{ 这不是合法 JSON —— 模拟写入中断")
    try:
        st = g.load(d)
        check("状态文件损坏 → 回落成空白状态，不抛异常", st["detail_used"] == 0)
    except Exception as e:
        check("状态文件损坏 → 回落成空白状态，不抛异常", False, repr(e))

    # 落点是个「文件」而不是目录 → save 必须静默失败，不能把下载带崩
    d2 = fresh()
    blocker = os.path.join(d2, "not_a_dir")
    with open(blocker, "w", encoding="utf-8") as f:
        f.write("x")
    try:
        ok = g.save(g._blank(), blocker)
        check("落点不可写 → save 返回 False 而不是抛异常", ok is False, ok)
    except Exception as e:
        check("落点不可写 → save 返回 False 而不是抛异常", False, repr(e))
    try:
        g.load(blocker)
        check("落点不可读 → load 回落空白而不是抛异常", True)
    except Exception as e:
        check("落点不可读 → load 回落空白而不是抛异常", False, repr(e))

    os.environ["KS_GUARD_OFF"] = "1"
    try:
        d3 = fresh()
        g.note_block(d3)
        check("KS_GUARD_OFF=1 → 冷却不再拦人", g.blocked_info(d3)[0] is False)
        check("KS_GUARD_OFF=1 → 预算不再限量", g.allow_detail(10 ** 6, d3) == 10 ** 6)
        check("KS_GUARD_OFF=1 → 状态行明说已关闭",
              "关闭" in g.budget_line(d3), g.budget_line(d3))
    finally:
        os.environ.pop("KS_GUARD_OFF", None)

    check("状态文件名字固定（免得换工作目录后「凭空解封」）",
          os.path.basename(g.state_path()) == "risk_state.json")
    check("状态落在固定锚点里（与产物目录无关）",
          g.state_path("/tmp") == os.path.join("/tmp", "risk_state.json"))

    # 当天踩过的真实旁路：状态跟着 KS_OUTDIR 走 → 换个产物目录就把冷却是绕过了。
    os.environ["KS_OUTDIR"] = "X:/somewhere/else"
    try:
        check("风控状态**不**跟随 KS_OUTDIR（风控是账号属性，不是产物目录属性）",
              "KS_OUTDIR" not in (g.out_dir() or ""), g.out_dir())
        check("换个产物目录绕不过冷却（把这个旁路钉死）",
              g.out_dir() == os.path.join(g._find_root(), "data", "kuaishou"), g.out_dir())
    finally:
        os.environ.pop("KS_OUTDIR", None)


# ── 4. 接线 ────────────────────────────────────────────────────────────────
def test_wiring():
    fdl = read_src("kuaishou_fast_dl.py")
    kh = read_src("kuaishou_headless.py")

    check("fast_dl 引入了守卫（且失败不致命）",
          "import ks_guard" in fdl and "ks_guard = None" in fdl)
    check("fast_dl 有刹车（撞墙即写冷却）", "ks_guard.note_block(" in fdl)
    check("fast_dl 有放行（跑顺了清冷却）", "ks_guard.note_ok(" in fdl)
    check("fast_dl 入口查冷却", "blocked_info(" in fdl)
    check("fast_dl 收集阶段按额度限量", "dom_collect(pg, limit=_climit)" in fdl)
    check("fast_dl 详情阶段按额度截断", "allow_detail(" in fdl)
    check("fast_dl 按实际请求数核销（不是计划数）", "commit_detail(n_calls" in fdl)

    check("headless 引入了守卫", "import ks_guard" in kh and "ks_guard = None" in kh)
    check("headless 入口查冷却", "blocked_info(" in kh)
    check("headless 撞墙后写冷却并收工", "ks_guard.note_block(" in kh)
    check("headless 区分「额度用完」与「撞墙」两种 0 成功",
          "allow_detail(1) <= 0" in kh)
    # 2026-09-25 实测：冷却期内单条模式**成功过**（同一个 fetch('/graphql')）
    # → 冷却只该拦批量，拦单条是添堵。这条断言把这个区分钉住。
    check("冷却只拦批量、放行单条（实测单条在冷却期可用）",
          "max_items != 1" in kh and "单条不受影响" in kh)
    check("单条下载也核销当日额度（否则总额被低估）",
          "commit_detail(1)" in kh)

    # 反向断言：旧的原地等待必须是**彻底删掉**，不是加开关绕过
    code = "\n".join(l for l in kh.splitlines() if not l.strip().startswith("#"))
    check("反向：不再有 time.sleep(cooldown_min * 60) 那段 96 分钟白等",
          "time.sleep(cooldown_min" not in code)
    check("反向：不再有 max_wait_rounds 驱动的重试 while",
          "waited < max_wait_rounds" not in code)

    check("清理调试 Edge 的注册早于守卫刹车（刹车路径也要收拾）",
          fdl.index("atexit.register(cleanup_debug_edge)") < fdl.index("blocked_info()"),
          "注册位置")
    check("接线里不再把产物目录传给守卫（否则冷却会被换目录绕过）",
          "ks_guard.blocked_info(outdir)" not in fdl
          and "ks_guard.collect_left(outdir)" not in fdl)

    # ── 批量独占：两个入口都必须领，且子进程必须能借用父进程的 ──────────────
    check("fast_dl 领批量租约（否则第二个窗口把额度各领一份）",
          "ks_guard.acquire_run(" in fdl)
    check("fast_dl 被拒时给的是「冲突」而不是「冷却」那种文案",
          "ks_guard.conflict_message(" in fdl)
    check("headless 批量分支领租约", 'acquire_run("batch")' in kh)
    check("headless resume 也领租约（resume 同样会打接口）",
          'acquire_run("resume")' in kh)
    check("headless 把租约 token 传给它拉起的 fast_dl 子进程（否则会自己拦自己）",
          "KS_RUN_TOKEN" in kh and "ks_guard.current_token()" in kh)
    check("单条模式不领租约（单条在冷却期实测可用，拦它是添堵）",
          kh.index('acquire_run("batch")') > kh.index("if max_items != 1:"))

    # 判别工具：把「多窗口有没有用」这个悬案的可执行答案留在仓库里
    probe = read_src("ks_granularity_probe.py")
    check("粒度判别工具在位，且说明了必须轮转（把粒度从速率里隔出来）",
          "--mode" in probe and "轮转" in probe and "rr" in probe)
    check("判别工具会先查冷却（不冷的会话测了不算数）",
          "blocked_info(" in probe)
    check("判别工具会核销当日额度（不能白耗预算还不记账）",
          "commit_detail(" in probe)

    # ── 风控桶：身份必须在**查冷却之前**确定，否则算到别的桶头上 ──────────────
    gsrc = read_src("ks_guard.py")
    check("两个入口都在启动时确定身份（查注册表，不启动浏览器）",
          "set_identity_for_profile(PROFILE)" in fdl
          and "set_identity_for_profile(PROFILE)" in kh)
    check("★ 确定身份的位置**早于**第一次读冷却（顺序错了整套就白做）",
          _before(fdl, "set_identity_for_profile(", "ks_guard.blocked_info()")
          and _before(kh, "set_identity_for_profile(", "ks_guard.budget_line()"))
    check("fast_dl 读到 cookie 后登记身份，并在桶变了时改口径",
          "remember_identity(" in fdl and "switch_bucket(" in fdl)
    check("headless 未登记时读一次 cookie 补登记（一次性成本）",
          "read_profile_cookies(" in kh and "remember_identity(" in kh
          and "switch_bucket(" not in kh)
    check("登录脚本也登记身份（「身份刚确定」的那一刻最该记）",
          "remember_identity(" in read_src("kuaishou_login.py"))
    check("登录态可换：KS_PROFILE 在 5 个入口都生效（多 ck 唯一的实现方式）",
          all("KS_PROFILE" in read_src(f) for f in
              ("kuaishou_fast_dl.py", "kuaishou_headless.py", "kuaishou_full_dl.py",
               "kuaishou_login.py", "快手一键下载器.pyw")))
    # 反向断言：分桶必须 did 优先。这一条盯的是「有人把口径改成按账号分」——
    # 那等于拆掉刹车（同一台设备两个号各拿 400）。扫原文而不是 code_only，
    # 因为这个表达式本身含字符串字面量，而 code_only 会把字符串抹掉。
    check("反向：分桶取 did 优先（按 userId 分桶 = 拆掉刹车）",
          'p.get("did") or p.get("userId")' in gsrc)
    check("守卫文档写明了「按什么分桶」以及为什么（不然下一个人会改回去）",
          "按什么分桶" in gsrc and "猜错的代价不对称" in gsrc)
    check("诊断工具在位（让用户能看见自己有几个桶）",
          "bucket" in read_src("ks_ident.py").lower()
          and "结论" in read_src("ks_ident.py"))


def _before(src, a, b):
    """`a` 在 `b` 之前出现吗？（两处都必须存在，否则算失败 —— 防的是「改没了」。）"""
    ia, ib = src.find(a), src.find(b)
    return ia >= 0 and ib >= 0 and ia < ib


# ── 5. 批量独占租约（「多开几个窗口并行跑」的刹车）────────────────────────
def test_lease():
    """「多开窗口」这件事必须有人管，否则守卫被自己的第二个窗口绕过去。

    机制上的要点全在这里：预算是「读-判断-写」，两个进程会各领一份 400。
    租约必须 ①真的拦得住第二个进程 ②过期自动作废（进程被 kill 不留死锁）
    ③子进程能借用父进程的（否则 resume 一启动就把自己拦死）。
    """
    d = fresh()
    for k in ("KS_ALLOW_MULTI", "KS_GUARD_OFF", "KS_RUN_TOKEN"):
        os.environ.pop(k, None)
    g._held_token = g._held_dir = None
    g._inherited = False

    ok1, _ = g.acquire_run("A", d)
    check("第一个批量进程拿到租约", ok1 is True)
    check("租约落盘（跨进程才看得见）", os.path.exists(g.run_path(d)))

    tok = g._held_token
    g._held_token = None                       # 模拟「另一个进程」：清掉自己的持有标记
    ok2, holder = g.acquire_run("B", d)
    check("第二个批量进程被拒 —— 这才是「多开窗口不会更快」的机制", ok2 is False, holder)
    check("拒绝时能说清是谁在跑",
          isinstance(holder, dict) and holder.get("pid") and holder.get("mode") == "A", holder)
    _msg = g.conflict_message(holder)
    check("拒绝信息同时给出「为什么」和「出路」",
          "各领一份" in _msg and "KS_ALLOW_MULTI=1" in _msg)

    # 过期租约必须自动作废 —— 否则进程被 taskkill /F（atexit 不执行）之后用户永久卡死
    g._held_token = tok
    fp = g.run_path(d)
    st = json.load(open(fp, encoding="utf-8"))
    st["beat"] = time.time() - (g.RUN_STALE_SEC + 60)
    json.dump(st, open(fp, "w", encoding="utf-8"))
    g._held_token = None
    check("租约过期 → run_holder 报「没人」", g.run_holder(d) is None)
    ok3, _ = g.acquire_run("C", d)
    check("过期之后新进程能接手（不会死锁）", ok3 is True)
    check("接手时换了新 token（不复用旧的）", g._held_token != tok)

    before = json.load(open(fp, encoding="utf-8"))["beat"]
    time.sleep(0.05)
    g._beat(d)
    after = json.load(open(fp, encoding="utf-8"))["beat"]
    check("心跳会真的刷新 beat", after > before, "%.3f → %.3f" % (before, after))
    check("心跳线程是 daemon（不会吊住进程退出）",
          g._beat_thread is not None and g._beat_thread.daemon is True)
    check("心跳线程记住了落点（否则会刷到默认锚点、真租约悄悄过期）",
          g._held_dir == d, g._held_dir)

    g.release_run(d)
    check("释放后租约文件被删", not os.path.exists(fp))
    check("释放后再申请能拿到", g.acquire_run("D", d)[0] is True)
    g.release_run(d)

    # 借用：resume 的子进程会带 KS_RUN_TOKEN
    g._held_token = g._held_dir = None
    g._inherited = False
    os.environ["KS_RUN_TOKEN"] = "parent-token-xyz"
    try:
        ok4, _ = g.acquire_run("child", d)
        check("子进程带 KS_RUN_TOKEN → 直接借用，不跟父进程抢锁", ok4 is True)
        check("借用者不写租约文件（那份归上层）", not os.path.exists(g.run_path(d)))
        check("借用者不自称持有者（release 不会误删上层那份）", g.current_token() is None)
        g.release_run(d)
        check("借用者的 release 是空操作", not os.path.exists(g.run_path(d)))
    finally:
        os.environ.pop("KS_RUN_TOKEN", None)

    # 出路与旁路
    g._held_token = g._held_dir = None
    g._inherited = False
    g.acquire_run("owner", d)
    g._held_token = None
    os.environ["KS_ALLOW_MULTI"] = "1"
    try:
        check("KS_ALLOW_MULTI=1 → 显式放行（拦得住但留了门）", g.acquire_run("E", d)[0] is True)
    finally:
        os.environ.pop("KS_ALLOW_MULTI", None)
    os.environ["KS_GUARD_OFF"] = "1"
    try:
        check("KS_GUARD_OFF=1 → 租约也不拦", g.acquire_run("F", d)[0] is True)
    finally:
        os.environ.pop("KS_GUARD_OFF", None)
    g._held_token = g._held_dir = None
    os.remove(fp) if os.path.exists(fp) else None

    # 损坏的租约文件不能把人卡死
    d2 = fresh()
    with open(g.run_path(d2), "w", encoding="utf-8") as f:
        f.write("{ 坏了")
    g._held_token = None
    check("租约文件损坏 → 当作无持有者，放行", g.acquire_run("G", d2)[0] is True)
    g.release_run(d2)

    # 注册表坏掉也不能让启动崩 —— 身份查不到只是「落 default 桶」
    d6 = tempfile.mkdtemp(prefix="ksguard_")
    with open(g.ident_reg_path(d6), "w", encoding="utf-8") as f:
        f.write("{ 坏了")
    check("身份注册表损坏 → 查不到身份（落 default），不抛异常",
          g.identity_for_profile("x", d6) == ""
          and g.set_identity_for_profile("x", d6) == g.BUCKET_DEFAULT)
    # 损坏的注册表**可以**被登记动作直接覆盖修复（不是「静默失败」）——
    # 这是一次性的小文件，重写比留着坏数据更有用；关键是别抛异常。
    _fixed = g.remember_identity("x", [{"name": "did", "value": "d"}], d6)
    check("注册表损坏时登记不崩，并把坏文件修复成合法 JSON",
          bool(_fixed) and g.identity_for_profile("x", d6) == _fixed)

    # 反向断言：探活**绝不能**用 os.kill —— Windows 上 os.kill(pid, 0) 不是探活，
    # 它会真的把目标进程 TerminateProcess 掉。
    # ⚠️ 扫 `code_only` 而不是全文：注释里正写着「为什么不能用 os.kill」。
    gcode = code_only(read_src("ks_guard.py"))
    check("反向：可执行代码里没有 os.kill（Windows 上它会杀掉对方进程）",
          "os.kill(" not in gcode and "os.kill" not in gcode)
    check("反向：租约与状态都不跟 KS_OUTDIR 走（只有注释里才提它）",
          "KS_OUTDIR" not in gcode)
    check("反向：租赁判活只看心跳时间戳",
          "RUN_STALE_SEC" in gcode and "GetExitCodeProcess" not in gcode)
    check("code_only 真的滤掉了注释（否则上面两条是假绿灯）",
          "os.kill" in read_src("ks_guard.py") and "os.kill" not in gcode)


# ── 7. 风控桶：身份分桶（「多个 ck 有没有用」的代码答案）──────────────────
def test_bucket():
    """桶按 `did`（设备）算，**不按** `userId`（账号）。

    这一组是本次改动的核心：按账号分桶会把刹车拆掉（同一台设备两个号各拿 400），
    所以下面既有正向断言（换设备 → 换桶），也有反向断言（换账号 → 不换桶）。
    """
    try:
        # ① 从 cookie 里取身份
        cks = [{"name": "did", "value": "web_aaa"}, {"name": "userId", "value": "111"},
               {"name": "other", "value": "x"}, {"name": "did", "value": "web_aaa"}]
        check("身份字段从 cookie 里取得到（did + userId）",
              g.ident_parts(cks) == {"did": "web_aaa", "userId": "111"}, g.ident_parts(cks))
        check("同名 cookie 出现多次只取一次（快手的 did 在两个域上都种）",
              g.ident_parts([{"name": "did", "value": "a"},
                             {"name": "did", "value": "b"}]) == {"did": "a"})
        check("认不出的输入 → 空 dict，不抛异常",
              g.ident_parts(None) == {} and g.ident_parts(["不是dict"]) == {}
              and g.ident_parts([{"name": "did"}]) == {})

        # ② 桶 = did（★ 用户问题的答案就在这三条里）
        b1 = g.bucket_of({"did": "web_aaa", "userId": "111"})
        b2 = g.bucket_of({"did": "web_aaa", "userId": "222"})
        b3 = g.bucket_of({"did": "web_bbb", "userId": "111"})
        check("★ 同一台设备换账号 → **同一个桶**（多登号不给额外额度）",
              b1 == b2 and bool(b1), "%s vs %s" % (b1, b2))
        check("★ 换台设备（新 profile）→ **不同的桶**（这才是多 ck 唯一的收益来源）",
              b1 != b3 and bool(b3), "%s vs %s" % (b1, b3))
        check("反向：桶**不**随 userId 变（按账号分桶 = 拆掉刹车）", b1 == b2)
        check("没有 did 时退回 userId（聊胜于无，而不是全挤进 default）",
              bool(g.bucket_of({"userId": "111"}))
              and g.bucket_of({"userId": "111"}) != g.bucket_of({"userId": "222"}))
        check("两个身份字段都没有 → 空串（调用方落回 default 桶）",
              g.bucket_of({}) == "" and g.bucket_of(None) == "")

        # ③ 桶之间互不牵连 —— 这是「多开一路」成立的前提
        d = fresh()
        A, B = "aaaa1111", "bbbb2222"
        g.set_identity(A, d)
        g.note_block(d, "A 撞墙")
        g.set_identity(B, d)
        check("★ A 桶冷却中**不影响** B 桶（否则换份登录态先被自己的冷却拦住）",
              g.blocked_info(d)[0] is False)
        g.set_identity(A, d)
        check("A 桶自己的冷却还在", g.blocked_info(d)[0] is True)
        g.set_identity(B, d)
        g.commit_detail(50, d)
        check("B 桶用掉的额度不记在 A 桶头上",
              int(g.load(d).get("detail_used") or 0) == 50)
        g.set_identity(A, d)
        check("A 桶的额度没被动过", int(g.load(d).get("detail_used") or 0) == 0)
        g.set_identity(B, d)
        check("B 桶仍有完整额度",
              g.allow_detail(10 ** 6, d) == g.DEFAULT_MAX_DETAIL_PER_DAY - 50)

        # ④ 旧格式（v1 扁平）的认领：升级不能把没到期的冷却丢掉
        d2 = fresh()
        with open(g.state_path(d2), "w", encoding="utf-8") as f:
            json.dump({"day": g._today(), "collect_used": 3, "detail_used": 4,
                       "blocked_until": time.time() + 3600, "blocked_at": time.time(),
                       "block_count": 1, "last_reason": "旧状态"}, f)
        g.set_identity("", d2)
        check("旧格式仍读得到（身份未登记时按 default 读）", g.blocked_info(d2)[0] is True)
        g.set_identity("cccc3333", d2)
        on, _left, _u = g.blocked_info(d2)
        check("★ 认领：旧状态整份归给第一个出现的真桶（冷却不丢、账不错）",
              on is True and int(g.load(d2).get("detail_used") or 0) == 4)
        blob = json.load(open(g.state_path(d2), encoding="utf-8"))
        check("认领是一次性的：legacy 槽清空，不会被第二个桶再认领一遍",
              blob.get("legacy") is None and "cccc3333" in (blob.get("buckets") or {}))
        g.set_identity("dddd4444", d2)
        check("★ 之后出现的桶**拿不到**那份冷却（否则「换个号」就等于绕过刹车）",
              g.blocked_info(d2)[0] is False)
        check("后来的桶拿到的是全新额度",
              g.allow_detail(10 ** 6, d2) == g.DEFAULT_MAX_DETAIL_PER_DAY)

        # ⑤ save 只动自己那个桶
        g.set_identity("eeee5555", d2)
        g.commit_collect(7, d2)
        blob = json.load(open(g.state_path(d2), encoding="utf-8"))
        check("写自己的桶不会把别的桶抹掉（多路并行时这是必须的）",
              "cccc3333" in blob["buckets"] and "eeee5555" in blob["buckets"])
        d5 = fresh()
        g.set_identity("", d5)
        g.commit_collect(9, d5)
        blob5 = json.load(open(g.state_path(d5), encoding="utf-8"))
        check("default 桶写进「未认领槽」，不再另存一份 default（否则同源两份）",
              "default" not in (blob5.get("buckets") or {})
              and int((blob5.get("legacy") or {}).get("collect_used") or 0) == 9)

        # ⑥ 注册表：profile → 桶（不启动浏览器）
        d3 = fresh()
        prof = os.path.join(d3, "runtime", "kuaishou-login-profile")
        check("没登记过 → 查不到，落 default 桶（与没有这套机制时一致）",
              g.identity_for_profile(prof, d3) == ""
              and g.set_identity_for_profile(prof, d3) == g.BUCKET_DEFAULT)
        bk = g.remember_identity(prof, [{"name": "did", "value": "web_zzz"},
                                        {"name": "userId", "value": "999"}], d3)
        check("登记后查得到桶（下次启动纯查 JSON，不开浏览器）",
              bool(bk) and g.identity_for_profile(prof, d3) == bk, bk)
        check("路径大小写/多余分隔符不影响查表（normcase + abspath）",
              g.identity_for_profile(os.path.join(d3, "runtime", ".", "Kuaishou-Login-Profile"),
                                     d3) == bk)
        check("不同 profile 各自独立（互不冒充）",
              g.identity_for_profile(os.path.join(d3, "runtime", "ks-b"), d3) == "")
        check("没有身份字段的 profile 不会被登记成桶",
              g.remember_identity(prof, [{"name": "kfoo", "value": "1"}], d3) == "")
        check("register 失败也不覆盖已有的正确记录",
              g.identity_for_profile(prof, d3) == bk)
        snap = dict((b, (st, leg)) for b, st, leg in g.buckets_snapshot(d2))
        check("诊断快照能列出各桶", "cccc3333" in snap and "eeee5555" in snap,
              sorted(snap.keys()))

        # ⑦ 租约按桶：全局一把锁会把「两台设备并行」这条路堵死
        d4 = fresh()
        A2, B2 = "1111aaaa", "2222bbbb"
        with open(g.run_path(d4, bucket=B2), "w", encoding="utf-8") as f:
            json.dump({"token": "other", "pid": 424242, "mode": "batch",
                       "started_at": time.time() - 60, "beat": time.time()}, f)
        g._held_token = None
        g.set_identity(A2, d4)
        check("★ B 桶有任务在跑时，A 桶照样能开",
              g.acquire_run("test", d4)[0] is True)
        g.release_run(d4)
        g._held_token = None
        g.set_identity(B2, d4)
        ok, holder = g.acquire_run("test", d4)
        check("★ 同一个桶里已有任务在跑 → 第二个当场被拦",
              ok is False and isinstance(holder, dict), holder)
        check("租约文件名按桶区分（不是全项目一把锁）",
              os.path.basename(g.run_path(d4, bucket=B2)) == "run.%s.lock" % B2)
        check("预算行会带出「我是谁」（多路并行时日志必须能分清身份）",
              B2 in g.budget_line(d4))
        try:
            os.remove(g.run_path(d4, bucket=B2))
        except Exception:
            pass
    finally:
        g._held_token = g._held_dir = None
        g.set_identity("")            # 别把桶留给后面的用例


# ── 6. 端到端：冷却中时入口必须当场拒绝（真子进程，不碰网络）──────────────
def test_entry_gate():
    g.set_identity("")                # 端到端只验 default 桶这条路径
    d = fresh()
    g.note_block(d, "pretend")
    env = dict(os.environ)
    # 落点是**固定锚点**（刻意不跟 KS_OUTDIR 走），所以端到端测试用专门的诊断变量
    # 把落点引到临时目录，避免碰真实状态。
    env["KS_GUARD_HOME"] = d
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "KS_OUTDIR", "KS_GUARD_OFF",
              "KS_RUN_TOKEN", "KS_ALLOW_MULTI"):
        env.pop(k, None)          # 本机代理会把 127.0.0.1 也吞掉，这里虽不联网也一并绕开

    script = os.path.join(TOOLS, "kuaishou_fast_dl.py")
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-u", script, d, "--list",
                            os.path.join(d, "no_such_list.json")],
                           capture_output=True, text=True, timeout=60, env=env)
        out = (r.stdout or "") + (r.stderr or "")
        dt = time.time() - t0
    except subprocess.TimeoutExpired:
        check("端到端：冷却中时入口当场收工（不联网、不等待）", False, "超时 60s")
        return

    check("端到端：冷却中时入口当场收工（不联网、不等待）",
          dt < 30, "耗时 %.1fs" % dt)
    cool_line = [l for l in out.splitlines() if "冷却" in l]
    check("端到端：日志里说清了「多久之后再来」",
          bool(cool_line) and "分钟" in cool_line[0],
          cool_line[0].strip()[:110] if cool_line else "（没有冷却提示行）")
    check("端到端：退出码 0 —— 主动刹车不是失败",
          r.returncode == 0, "退出码 %d" % r.returncode)
    check("端到端：没有走到浏览器/网络那一步（无 playwright 相关输出）",
          "playwright" not in out.lower() and "Traceback" not in out,
          "输出共 %d 行" % len(out.splitlines()))

    # ── 第二个窗口：批量入口必须当场拦住，而且拦在 playwright 之前 ──
    # 这条直接对应用户的问题「能不能多开几个窗口并行跑」。
    d2 = fresh()
    with open(g.run_path(d2), "w", encoding="utf-8") as f:
        json.dump({"token": "someone-else", "pid": 999999, "mode": "batch",
                   "started_at": time.time() - 60, "beat": time.time()}, f)
    env2 = dict(env)
    env2["KS_GUARD_HOME"] = d2
    t0 = time.time()
    try:
        r2 = subprocess.run([sys.executable, "-u", script, d2, "--list",
                             os.path.join(d2, "no_such_list.json")],
                            capture_output=True, text=True, timeout=60, env=env2)
        out2 = (r2.stdout or "") + (r2.stderr or "")
        dt2 = time.time() - t0
    except subprocess.TimeoutExpired:
        check("端到端：第二个窗口当场被拦（不等待）", False, "超时 60s")
        return
    conf = [l for l in out2.splitlines() if "已经有一个快手批量任务在跑" in l]
    check("端到端：已有批量在跑时，第二个窗口当场被拦",
          bool(conf), conf[0].strip()[:100] if conf else "（没有冲突提示行）")
    check("端到端：被拦时退出码 0（主动刹车不是失败）",
          r2.returncode == 0, "退出码 %d" % r2.returncode)
    check("端到端：拦在 playwright 之前（零浏览器、耗时 %.1fs）" % dt2,
          "playwright" not in out2.lower() and dt2 < 30)
    check("端到端：拒绝信息明确给出出路（KS_ALLOW_MULTI=1）",
          "KS_ALLOW_MULTI=1" in out2)
    check("端到端：第二个窗口不碰别人的租约文件",
          os.path.exists(g.run_path(d2)))
    try:
        os.remove(g.run_path(d2))
    except Exception:
        pass


def main():
    print("== 快手风控守卫自检（离线）==")
    print()
    test_budget()
    print()
    test_cooling()
    print()
    test_robustness()
    print()
    test_wiring()
    print()
    test_lease()
    print()
    test_bucket()
    print()
    test_entry_gate()
    print()
    print("通过 %d 项，失败 %d 项" % (len(PASSES), len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
