# -*- coding: utf-8 -*-
"""离线验证：批量租约（run lease）能不能正确传给 fast_dl 子进程。

跑法（不需要网络、不碰任何真实数据 —— 全程用临时 KS_GUARD_HOME）：
    python modules/kuaishou/tests/test_guard_run_token.py

## 这个测试是为哪条线上问题写的

2026-09-29 用户报「快手下载不了」：任务日志里明明收集到 400 个作品、额度也扣了，
**一个文件都没下**，任务状态还是 `success`。日志最后写着：

    [guard] 桶 xxx 已经有一个快手批量任务在跑（PID 11448 的「batch」，已跑 2 分钟）

而 11448 就是**它自己的父进程**。根因：`kuaishou_headless.py` 的 `all`/限量主线用
`subprocess.run(cmd)` 拉起 fast_dl，**没把 `KS_RUN_TOKEN` 传下去**；子进程于是去
`acquire_run("fast_dl")`，看到盘上那把锁，以为是别人，直接刹车。
（`_resume_pending()` 那条路是传了的，所以只有 `resume` 正常 —— 这解释了为什么
"续跑能用、全量不能用"。）

所以本文件分三段：
  A. **证明**：租约不传 token 时，子进程会把父进程报成竞争对手（报出父进程自己的 pid）。
  B. **防线**：`_spawn_fast_dl()` 必须把 token 放进子进程 env，且 env 是完整继承的。
  C. **回归栅栏**：模块里所有「拉起 kuaishou_fast_dl.py」的调用都必须带 `env=`，
     否则这个 bug 会以同样的形状回来。（yt-dlp / powershell 那几处本来就不该带，
     所以只盯 fast_dl 这一族 —— 断言范围要精确，不然它自己会先变成噪音。）
"""
import ast
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(os.path.dirname(HERE), "tools")     # modules/kuaishou/tools
HEADLESS = os.path.join(TOOLS, "kuaishou_headless.py")
sys.path.insert(0, TOOLS)

import ks_guard                                          # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   " + label + (("  " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  [FAIL] " + label + (("  " + str(extra)) if extra else ""))


CHILD_SCRIPT = (
    "import sys, json; sys.path.insert(0, r'%s');"
    "import ks_guard as G;"
    "ok, h = G.acquire_run('fast_dl');"
    "print(json.dumps({'ok': ok, 'pid': (h or {}).get('pid'), 'mode': (h or {}).get('mode')}))"
    % TOOLS
)


def _child_env(guard_home, token=None):
    """干净的子进程环境：只带 KS_GUARD_HOME 和（可选的）KS_RUN_TOKEN。"""
    env = dict(os.environ)
    for k in ("KS_RUN_TOKEN", "KS_GUARD_OFF", "KS_ALLOW_MULTI", "KS_GUARD_HOME"):
        env.pop(k, None)
    env["KS_GUARD_HOME"] = guard_home
    env["PYTHONUTF8"] = "1"
    if token:
        env["KS_RUN_TOKEN"] = token
    return env


def _run_child(guard_home, token=None):
    p = subprocess.run([sys.executable, "-c", CHILD_SCRIPT],
                       env=_child_env(guard_home, token),
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0 or not (p.stdout or "").strip():
        raise RuntimeError("子进程失败 rc=%s out=%r err=%r"
                           % (p.returncode, p.stdout, (p.stderr or "")[-400:]))
    return json.loads(p.stdout.strip().splitlines()[-1])


def _fast_dl_spawn_lines(src):
    """找出「拉起 kuaishou_fast_dl.py」的 subprocess 调用，返回 (行号, 是否带 env) 列表。

    命令是先在上一行赋给变量再传进来的（`cmd = [...fast_dl...]` → `subprocess.run(cmd)`），
    所以 AST 层面要把变量解一层：从赋值语句里收集字符串字面量，看是否含 fast_dl 文件名。
    """
    tree = ast.parse(src)
    var_lits = {}          # 变量名 -> 该赋值表达式里出现的字符串字面量
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            lits = {s.value for s in ast.walk(node.value)
                    if isinstance(s, ast.Constant) and isinstance(s.value, str)}
            var_lits.setdefault(node.targets[0].id, set()).update(lits)

    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("run", "Popen")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"):
            continue
        lits, names = set(), set()
        for arg in list(node.args):
            for s in ast.walk(arg):
                if isinstance(s, ast.Constant) and isinstance(s.value, str):
                    lits.add(s.value)
                if isinstance(s, ast.Name):
                    names.add(s.id)
        for n in names:                                   # 解一层变量
            lits |= var_lits.get(n, set())
        if any("kuaishou_fast_dl.py" in s for s in lits):
            has_env = any(k.arg == "env" for k in node.keywords)
            out.append((node.lineno, has_env))
    return out


def main():
    print("== A. 租约跨进程语义（这就是 09-29 的故障形态）==")
    with tempfile.TemporaryDirectory(prefix="ks_guard_tok_") as home:
        ks_guard._bucket = ks_guard.BUCKET_DEFAULT
        ok, holder = ks_guard.acquire_run("batch", explicit=home)
        check("父进程领到租约", ok and holder is None, holder)
        tok = ks_guard.current_token()
        check("父进程拿到 token", bool(tok), tok)
        lock = ks_guard.run_path(home)
        check("锁文件已落盘", os.path.exists(lock), lock)

        # 子进程 ①：不传 token（= 修复前 all 主线的行为）
        r1 = _run_child(home)
        check("不传 token 时子进程**被拦**（复现故障）", r1["ok"] is False, r1)
        check("它报的「竞争对手」就是自己的父进程 pid",
              r1["pid"] == os.getpid(),
              "子进程看到 pid=%s，父进程 pid=%s" % (r1["pid"], os.getpid()))
        check("报出来的模式是父进程的 batch（不是 fast_dl）",
              r1["mode"] == "batch", r1["mode"])

        # 子进程 ②：传了 token（= 修复后的行为）
        r2 = _run_child(home, token=tok)
        check("传了 token 时子进程直接放行", r2["ok"] is True, r2)

        ks_guard.release_run(home)
        check("释放后锁文件消失", not os.path.exists(lock))

    print()
    print("== B. 修复的防线：spawn 必须带上租约 token ==")
    import kuaishou_headless as KH

    if not hasattr(KH, "_spawn_fast_dl"):
        check("kuaishou_headless 提供了统一的 spawn 助手", False,
              "没有 _spawn_fast_dl —— 说明修复没打上（或被打回）")
    else:
        check("kuaishou_headless 提供了统一的 spawn 助手", True)

        with tempfile.TemporaryDirectory(prefix="ks_guard_tok2_") as home:
            ks_guard._bucket = ks_guard.BUCKET_DEFAULT
            ks_guard.acquire_run("batch", explicit=home)
            captured = {}

            def _fake_run(cmd, *a, **kw):
                captured["cmd"] = cmd
                captured["env"] = kw.get("env")

                class R:
                    returncode = 0
                return R()

            real_run = KH.subprocess.run
            KH.subprocess.run = _fake_run
            try:
                KH._spawn_fast_dl(["python", "kuaishou_fast_dl.py"])
            finally:
                KH.subprocess.run = real_run

            env = captured.get("env") or {}
            check("spawn 时带上了 env", bool(env))
            check("env 里有 KS_RUN_TOKEN", bool(env.get("KS_RUN_TOKEN")))
            check("token 与父进程手里的一致",
                  env.get("KS_RUN_TOKEN") == ks_guard.current_token(),
                  env.get("KS_RUN_TOKEN"))
            check("env 是完整继承的（不是只有 token）",
                  "PATH" in env or "SystemRoot" in env, sorted(env)[:4])
            ks_guard.release_run(home)

    print()
    print("== C. 回归栅栏：拉起 fast_dl 的调用必须带 env= ==")
    src = open(HEADLESS, encoding="utf-8").read()
    spawns = _fast_dl_spawn_lines(src)
    check("找得到拉起 fast_dl 的调用点", len(spawns) > 0, spawns)
    no_env = [ln for ln, has_env in spawns if not has_env]
    check("每个都带 env=（否则子进程又会把自己的父进程当对手）",
          not no_env, ("缺 env= 的行：%s" % no_env) if no_env else
          ("共 %d 处全部合规" % len(spawns)))

    # 光盯住助手自身不够：要确认**调用点**也走了助手。
    # 已知两条主线：`_resume_pending()`（resume）和 all/限量主线，各应有一处。
    call_sites = sum(1 for ln in src.splitlines() if "_spawn_fast_dl(" in ln)
    check("调用点都改用助手（>=2 处：resume + all 主线）",
          call_sites >= 2, "源码里 _spawn_fast_dl( 出现 %d 次（含 1 次定义）" % call_sites)

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
