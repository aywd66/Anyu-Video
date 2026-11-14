# -*- coding: utf-8 -*-
"""合并项目自检脚本:验证四个模块的路径解析与环境(不依赖网络)"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tests/ 的上级 = 项目根

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    try:
        print("[%s] %s %s" % ("OK " if ok else "FAIL", name, detail))
    except Exception:
        pass


# 1. clawfetch 模块存在且可导入
cf = os.path.join(HERE, "modules", "clawfetch", "clawfetch.py")
check("clawfetch 模块文件", os.path.exists(cf), cf)
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("clawfetch_selftest", cf)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("clawfetch 可导入", True, "version=%s" % getattr(mod, "VERSION", "?"))
except Exception as e:
    check("clawfetch 可导入", False, str(e))

# 2. 快手脚本路径解析(子进程隔离:脚本会重包 sys.stdout)
sys_py = sys.executable
ks_tools = os.path.join(HERE, "modules", "kuaishou", "tools")
probe = (
    "import sys; sys.path.insert(0, r'%s');\n"
    "import kuaishou_fast_dl, kuaishou_login;\n"
    "print('ROOT=' + kuaishou_fast_dl._find_root());\n"
    "print('OUTDIR=' + kuaishou_fast_dl.OUTDIR);\n"
    "print('PROFILE=' + kuaishou_fast_dl.PROFILE);\n"
    "print('COOKIE=' + kuaishou_login.COOKIE_OUT)\n" % ks_tools
)
r = subprocess.run([sys_py, "-c", probe], capture_output=True, cwd=ks_tools,
                   env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60,
                   encoding="utf-8", errors="replace")
r_stdout = r.stdout or ""
if r.returncode == 0 and r_stdout.strip():
    lines = dict(ln.split("=", 1) for ln in r_stdout.strip().splitlines() if "=" in ln)
    check("fast_dl 路径解析→项目根", os.path.normpath(lines.get("ROOT", "")) == os.path.normpath(HERE), lines.get("ROOT", ""))
    check("fast_dl OUTDIR→data/kuaishou", os.path.normpath(lines.get("OUTDIR", "")) == os.path.normpath(os.path.join(HERE, "data", "kuaishou")), lines.get("OUTDIR", ""))
    check("fast_dl PROFILE→runtime 登录态", os.path.normpath(lines.get("PROFILE", "")) == os.path.normpath(os.path.join(HERE, "runtime", "kuaishou-login-profile")), lines.get("PROFILE", ""))
    check("login COOKIE_OUT→data/kuaishou", os.path.normpath(os.path.dirname(lines.get("COOKIE", ""))) == os.path.normpath(os.path.join(HERE, "data", "kuaishou")), lines.get("COOKIE", ""))
else:
    check("快手脚本导入", False, (r.stderr or "")[-300:])

# 3. 抖音模块:配置解析 + 数据库只读 + 核心依赖
dy_py = os.path.join(HERE, "runtime", "douyin-venv", "Scripts", "python.exe")
dy_dir = os.path.join(HERE, "modules", "douyin")
code = ("import sys; sys.path.insert(0, r'%s'); "
        "from config import ConfigLoader; "
        "c = ConfigLoader(r'%s'); "
        "print(c.get('path')); print(c.get('database_path'))" % (dy_dir, os.path.join(dy_dir, "config.yml")))
r2 = subprocess.run([dy_py, "-c", code], capture_output=True, text=True, cwd=dy_dir)
if r2.returncode == 0:
    lines = r2.stdout.strip().splitlines()
    # `path` 是用户可自定义的下载目录（可能不在工具箱内），只校验已解析出绝对路径；
    # 早期断言硬编码 "downloads"，用户改到别处就会误报失败。
    check("抖音配置解析(path)",
          bool(lines) and os.path.isabs(lines[0].strip()),
          lines[0] if lines else "")
    check("抖音配置解析(database_path)", len(lines) > 1 and "data" in lines[1], lines[1] if len(lines) > 1 else "")
else:
    check("抖音配置解析", False, (r2.stderr or r2.stdout)[-300:])

# ⚠️ 数据库位置以 config.yml 的 `database_path` 为准（它是**相对 CWD** 的，
#    CLI 的 CWD 是 modules/douyin），不能硬编码 data/douyin —— 配一改就误报。
_db_raw = lines[1].strip() if r2.returncode == 0 and len(lines) > 1 else ""
db = _db_raw if os.path.isabs(_db_raw) else os.path.normpath(os.path.join(dy_dir, _db_raw))
if not (db and os.path.exists(db)):
    # 首次下载之前库还不存在，这是「还没数据」，不是装坏了 —— 别当失败吓人
    check("抖音数据库只读连接", True, "尚未生成，首次下载后自动建：%s" % (db or "?"))
else:
    db_uri = "file:%s?mode=ro" % db.replace("\\", "/")
    r3 = subprocess.run([dy_py, "-c",
                         "import sqlite3; c = sqlite3.connect(r'%s', uri=True); "
                         "rows = list(c.execute('SELECT name FROM sqlite_master LIMIT 5')); print(len(rows))" % db_uri],
                        capture_output=True, text=True)
    check("抖音数据库只读连接", r3.returncode == 0 and r3.stdout.strip() not in ("", "0"),
          (r3.stdout or r3.stderr)[:150])

# ⚠️ 库文件不存在 **不等于** 装好了 —— 上面那条在「库还没生成」时是放行的，
#    于是一个真故障被盖住了：`data\douyin\` 目录被清掉后，sqlite 打不开库
#    （它不会建目录），抖音下载每条都当场失败，自检却全绿。2026-09-25 踩过。
#    这里把「库能不能落地」单独盯住：目录在 → 必须可写；目录不在 → 上级必须可写
#    （storage/database.py 会自建）。
_db_parent = os.path.dirname(db) if db else ""
if _db_parent:
    if os.path.isdir(_db_parent):
        check("抖音数据库目录可写", os.access(_db_parent, os.W_OK), _db_parent)
    else:
        _up = os.path.dirname(_db_parent)
        _ok_up = bool(_up) and os.path.isdir(_up) and os.access(_up, os.W_OK)
        check("抖音数据库目录可自建", _ok_up,
              "目录 %s 不存在；上级 %s 可写=%s（下载时会自动建）" % (_db_parent, _up, _ok_up))
else:
    check("抖音数据库目录可写", False, "解析不出 database_path：%r" % _db_raw)

# 行为级守卫：光看「目录在不在」不够 —— 把 storage/database.py 里的自建逻辑
# 删掉，上面两条照样绿。这里真跑一遍：连一个父目录不存在的库，必须自建成功。
_bootstrap = (
    "import os, sys, asyncio, tempfile, shutil;"
    "sys.path.insert(0, r'%s');"
    "from storage.database import Database;"
    "d = tempfile.mkdtemp();"
    "p = os.path.join(d, 'x', 'y', 'dy.db');"
    "db = Database(db_path=p);"
    "asyncio.run(db.initialize());"
    "ok = os.path.exists(p);"
    "asyncio.run(db.close());"
    "shutil.rmtree(d, ignore_errors=True);"
    "print('MADE' if ok else 'MISSING')" % dy_dir)
r7 = subprocess.run([dy_py, "-c", _bootstrap], capture_output=True, text=True, cwd=dy_dir)
check("抖音数据库父目录自建", "MADE" in (r7.stdout or ""), (r7.stdout or r7.stderr)[-160:])

r4 = subprocess.run([dy_py, "-c",
                     "import aiohttp, httpx, aiofiles, aiosqlite, rich, yaml, dateutil, gmssl, imageio_ffmpeg; print('deps-ok')"],
                    capture_output=True, text=True)
check("抖音运行时核心依赖", r4.returncode == 0 and "deps-ok" in r4.stdout, (r4.stderr or "deps-ok")[-120:])

# 4. 快手运行时核心依赖 + yt-dlp 模块方式
ks_py = os.path.join(HERE, "runtime", "kuaishou-env", "Scripts", "python.exe")
r5 = subprocess.run([ks_py, "-c", "import yt_dlp, requests, playwright; print('ks-deps-ok')"],
                    capture_output=True, text=True)
check("快手运行时核心依赖", r5.returncode == 0 and "ks-deps-ok" in r5.stdout, (r5.stderr or "ks-deps-ok")[-120:])

r6 = subprocess.run([ks_py, "-m", "yt_dlp", "--version"], capture_output=True, text=True)
check("python -m yt_dlp 可用", r6.returncode == 0, (r6.stdout.strip() or r6.stderr[:100]))

# 5. 登录态 profile 存在
lp = os.path.join(HERE, "runtime", "kuaishou-login-profile")
check("快手登录态目录", os.path.isdir(lp) and len(os.listdir(lp)) > 0, "files=%d" % (len(os.listdir(lp)) if os.path.isdir(lp) else 0))

# 6. 数据文件(台账/清单)
# ⚠️ 台账**跟着产物目录走**（换目录 = 换一份空台账，这是刻意的），
#    所以这里必须去「当前生效的目录」找，写死 data/kuaishou 会误报。
_ks_out = os.path.join(HERE, "data", "kuaishou")
try:
    import json as _json2
    _od = os.path.join(HERE, "data", "output_dirs.json")
    if os.path.exists(_od):
        _ks_out = (_json2.loads(open(_od, encoding="utf-8").read()) or {}).get("kuaishou") or _ks_out
except Exception:
    pass
for f in ("download_log.json", "works_list.json"):
    p = os.path.join(_ks_out, f)
    if os.path.exists(p):
        check("快手数据文件 %s" % f, True, p)
    elif os.path.isdir(_ks_out):
        # 目录已就绪但还没跑完一次下载 —— 「还没数据」，不是装坏了
        check("快手数据文件 %s" % f, True, "尚未生成（目录 %s 已就绪）" % _ks_out)
    else:
        check("快手数据文件 %s" % f, False, "产物目录不存在：" + _ks_out)

# 7. 工具箱入口可编译
for f in ("toolbox.py", os.path.join("modules", "kuaishou", "tools", "快手一键下载器.pyw")):
    fp = os.path.join(HERE, f)
    r7 = subprocess.run([sys_py, "-m", "py_compile", fp], capture_output=True, text=True)
    check("编译 %s" % os.path.basename(f), r7.returncode == 0, (r7.stderr or "")[-120:])

# 8. 模块 4:直播录制 —— 运行时 / 模块代码 / 配置落位 / 解析层 / 共享浏览器缓存
lr_dir = os.path.join(HERE, "modules", "live_recorder")
lr_py = os.path.join(HERE, "runtime", "live-recorder-env", "Scripts", "python.exe")
check("直播录制运行时存在", os.path.exists(lr_py), lr_py)
check("直播录制模块入口", os.path.exists(os.path.join(lr_dir, "main.py")),
      os.path.join(lr_dir, "main.py"))

r8 = subprocess.run([lr_py, "-c",
                     "import PyQt6, httpx, requests, brotli, playwright; print('lr-deps-ok')"],
                    capture_output=True, text=True)
check("直播录制运行时核心依赖", r8.returncode == 0 and "lr-deps-ok" in r8.stdout,
      (r8.stderr or "lr-deps-ok")[-120:])

# playwright 期望的 chromium 必须已在系统共享缓存里 —— 否则「浏览器兜底取流」
# 与「扫码登录」两条路径会因缺浏览器内核而失败，而单元层面看不出问题。
r9 = subprocess.run(
    [lr_py, "-c",
     "import os\n"
     "from playwright.sync_api import sync_playwright\n"
     "with sync_playwright() as p:\n"
     "    path = p.chromium.executable_path\n"
     "    print(path)\n"
     "    print('EXISTS' if os.path.exists(path) else 'MISSING')\n"],
    capture_output=True, text=True)
check("playwright chromium 共享缓存可用",
      r9.returncode == 0 and "EXISTS" in r9.stdout,
      (r9.stdout or r9.stderr).strip().replace("\n", " ")[-140:])

# 配置必须落在模块自己的 config.json（而不是项目根或别处），否则用户的
# 监控清单与登录 Cookie 会在迁移中走失。
lr_parent = os.path.dirname(lr_dir)
r10 = subprocess.run(
    [lr_py, "-c",
     "import sys; sys.path.insert(0, r'%s')\n"
     "from live_recorder.config.settings import ConfigStore\n"
     "s = ConfigStore(); s.load()\n"
     "print('CFG=' + str(s.path))\n"
     "print('SAVE=' + str(s.config.save_path))\n"
     "print('ROOMS=' + str(len(s.rooms)))\n" % lr_parent],
    capture_output=True, text=True, cwd=lr_dir, encoding="utf-8", errors="replace",
    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
if r10.returncode == 0:
    lines = dict(ln.split("=", 1) for ln in r10.stdout.strip().splitlines() if "=" in ln)
    check("直播录制配置→模块内 config.json",
          os.path.normpath(lines.get("CFG", "")) == os.path.normpath(
              os.path.join(lr_dir, "config.json")),
          lines.get("CFG", ""))
    check("直播录制监控清单已保留",
          lines.get("ROOMS", "0").isdigit() and int(lines["ROOMS"]) > 0,
          "rooms=%s  save=%s" % (lines.get("ROOMS"), lines.get("SAVE")))
else:
    check("直播录制配置解析", False, (r10.stderr or r10.stdout or "")[-200:])

# 离线解析层:两平台的链接识别与快手房间号解析(不联网,不受风控影响)
r11 = subprocess.run(
    [lr_py, "-c",
     "import sys; sys.path.insert(0, r'%s')\n"
     "from live_recorder.platforms import get_parser\n"
     "from live_recorder.platforms.registry import detect_platform\n"
     "print('D=' + detect_platform('https://live.douyin.com/123456789'))\n"
     "print('K=' + detect_platform('https://v.kuaishou.com/abcDEF'))\n"
     "print('KR=' + get_parser('kuaishou').resolve_room("
     "'https://live.kuaishou.com/u/3xf8dh7tqmd9bps'))\n" % lr_parent],
    capture_output=True, text=True, cwd=lr_dir, encoding="utf-8", errors="replace",
    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
if r11.returncode == 0:
    lines = dict(ln.split("=", 1) for ln in r11.stdout.strip().splitlines() if "=" in ln)
    check("直播录制平台识别(抖音/快手)",
          lines.get("D") == "douyin" and lines.get("K") == "kuaishou",
          "douyin=%s  kuaishou=%s" % (lines.get("D"), lines.get("K")))
    check("直播录制快手房间号解析", lines.get("KR") == "3xf8dh7tqmd9bps", lines.get("KR", ""))
else:
    check("直播录制解析层", False, (r11.stderr or r11.stdout or "")[-200:])

# 9. 模块 4 的启动脚本
#    2026-11-07 起根目录入口统一进 启动工具箱.bat（直播录制器 = 菜单选项 3
#    -> launcher_win.py live），旧的 启动直播录制器.bat 已删除；模块内的
#    start.bat / login.bat 是模块级入口，保留。
for f in (os.path.join("modules", "live_recorder", "start.bat"),
          os.path.join("modules", "live_recorder", "login.bat")):
    check("存在 %s" % f.replace(os.sep, "/"), os.path.exists(os.path.join(HERE, f)), f)
_toolbox_bat = os.path.join(HERE, "启动工具箱.bat")
if os.path.exists(_toolbox_bat):
    _tb_raw = open(_toolbox_bat, "rb").read().decode("gbk", "replace")
    check("统一启动器含主界面入口（launcher_win.py toolbox，全模块统一管理）",
          'launcher_win.py toolbox' in _tb_raw
          or ('"%PYW%" "%APP%" toolbox' in _tb_raw))

# 10. 统一菜单里必须能查到模块 4 的四个入口
#     （按钮被误删/改名时这条例得住，避免"功能在但点不到"）
tb_path = os.path.join(HERE, "toolbox.py")
tb_src = open(tb_path, encoding="utf-8").read()
missing = [n for n in ("lr_gui", "lr_login", "lr_selftest", "lr_open_dir") if n not in tb_src]
check("工具箱菜单含模块 4 全部入口", not missing,
      ("缺少: %s" % missing) if missing else "lr_gui / lr_login / lr_selftest / lr_open_dir 均在")

# 11. 设计令牌单一来源(Web 工作台 / 桌面启动器共用同一份设计语言)
#     tokens.css 与 theme_tokens.py 都是 **生成物**,所以必须有一条门禁盯着它们
#     有没有被手改、或者改了源却忘了重新生成。否则两端会慢慢分叉,
#     而分叉的症状是"两边看着不太一样"——一个没人会当成 bug 去查的现象。
tok_src = os.path.join(HERE, "docs", "design-tokens.json")
gen = os.path.join(HERE, "tools", "gen_tokens.py")
check("设计令牌源存在", os.path.exists(tok_src), tok_src)
check("令牌生成器存在", os.path.exists(gen), gen)
for f in (os.path.join("modules", "douyin", "webui", "assets", "css", "tokens.css"),
          "theme_tokens.py"):
    check("令牌产物 %s" % f.replace(os.sep, "/"), os.path.exists(os.path.join(HERE, f)), f)
if os.path.exists(tok_src) and os.path.exists(gen):
    r12 = subprocess.run([sys_py, gen, "--check"], capture_output=True, text=True,
                         cwd=HERE, encoding="utf-8", errors="replace")
    check("令牌产物未漂移", r12.returncode == 0,
          (r12.stdout or r12.stderr).strip().replace("\n", " ")[-160:])

# 12. 产物目录可自定义(启动台「产物目录 · 更改」)
#     这条门禁盯着的是**接线**,不是 UI:前端能改、后端存了、但真正干活的下载脚本
#     不认 —— 是最容易发生的断线方式,而症状是「改了没生效」,没人会想到是这里断了。
bs_path = os.path.join(HERE, "modules", "douyin", "webui", "bridge_server.py")
bs_src = open(bs_path, encoding="utf-8").read() if os.path.exists(bs_path) else ""
need_routes = [n for n in ("/api/bridge/tool/set-dir", "/api/bridge/tool/pick-dir")
               if n not in bs_src]
check("桥接服务已注册产物目录接口", os.path.exists(bs_path) and not need_routes,
      ("缺少: %s" % need_routes) if need_routes else "set-dir / pick-dir 均已注册")

# 快手的两个下载脚本必须认 KS_OUTDIR(否则改了目录还是下到 data\kuaishou)
ks_ok = True
ks_detail = []
for f in ("kuaishou_headless.py", "kuaishou_fast_dl.py", "快手一键下载器.pyw"):
    p = os.path.join(ks_tools, f)
    src = open(p, encoding="utf-8", errors="replace").read() if os.path.exists(p) else ""
    hit = 'os.environ.get("KS_OUTDIR")' in src
    ks_ok = ks_ok and hit
    ks_detail.append("%s=%s" % (f, "OK" if hit else "未认 KS_OUTDIR"))
check("快手脚本认 KS_OUTDIR", ks_ok, " ".join(ks_detail))

# 登录用的 Edge profile 必须钉死在默认目录,不能跟着产物目录搬走
fd_src = open(os.path.join(ks_tools, "kuaishou_fast_dl.py"), encoding="utf-8",
              errors="replace").read()
check("快手登录 profile 不随产物目录搬家",
      "EDGE_PROFILE = os.path.join(_DEFAULT_OUT" in fd_src,
      "edge-manual-profile 固定在 data/kuaishou 下")

# 自定义目录的落地文件可 round-trip(写了要能读回来,且不能破坏已有值)
od_path = os.path.join(HERE, "data", "output_dirs.json")
try:
    import json as _json
    before = open(od_path, encoding="utf-8").read() if os.path.exists(od_path) else None
    d0 = _json.loads(before) if before else {}
    os.makedirs(os.path.dirname(od_path), exist_ok=True)
    tmp = od_path + ".tmp"
    open(tmp, "w", encoding="utf-8").write(_json.dumps(d0, ensure_ascii=False, indent=2))
    os.replace(tmp, od_path)
    d1 = _json.loads(open(od_path, encoding="utf-8").read())
    check("产物目录配置可 round-trip", d0 == d1,
          "%d 个自定义项" % len([k for k, v in d1.items() if v]))
    if before is None:                    # 本次新建的空文件不留残骸
        os.remove(od_path)
except Exception as e:
    check("产物目录配置可 round-trip", False, str(e))

# ---------------------------------------------------------------------------
# 任务中心进度：必须是**真解析出来的**,不许写死常数
# ---------------------------------------------------------------------------
bs_full = open(bs_path, encoding="utf-8", errors="replace").read()
check("任务接口带真实进度字段", "item[\"progress_info\"] = task_progress(t)" in bs_full,
      "/api/bridge/tasks 返回 progress_info")
check("进度解析函数存在", "def task_progress(task" in bs_full and "_RE_KS_DL" in bs_full,
      "bridge_server.task_progress")

# ---------------------------------------------------------------------------
# 直链任务的出网路径:不许把「外壳注入的代理」继承给 clawfetch 子进程
# ---------------------------------------------------------------------------
# 现场:手机端下发的那条 818.91MB 直链,桥接把 HTTP_PROXY/HTTPS_PROXY 原样传给了
# 子进程 —— "从这台机器直连那个 URL" 悄悄变成 "绕到 127.0.0.1:<某个别人的、
# 随时会消失的端口>",症状是一整片 502 和"无数据超时",且完全看不出走了哪条路。
# 这条门禁盯三件事:函数在、大小写两种写法都覆盖、直链分支**真的**调用了它。
# (只看"函数存在"是不够的 —— 定义了不调用照样白搭,这正是最典型的假绿。)
_PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
               "http_proxy", "https_proxy", "all_proxy", "no_proxy")
check("桥接服务有「摘掉代理环境变量」的工具函数",
      "def strip_proxy_env(env" in bs_full and "_PROXY_ENV_KEYS" in bs_full,
      "bridge_server.strip_proxy_env")
check("摘代理覆盖大小写两种写法(urllib 两种都认)",
      all(('"%s"' % k) in bs_full for k in _PROXY_KEYS),
      "%d 个键位" % len(_PROXY_KEYS))
_direct_i = bs_full.find('if task.get("kind") == "direct":')
check("直链任务分支真的调用了它(不是定义了不用)",
      _direct_i >= 0 and "strip_proxy_env(env)" in bs_full[_direct_i:_direct_i + 1200],
      "位于 subprocess_popen() 的 direct 分支内")
check("摘代理这件事对用户可见(任务日志里留一行)",
      "[提示] 已忽略继承的代理设置" in bs_full,
      "日志会写明摘掉了哪些键、以及要用代理时的办法")

api_js = os.path.join(HERE, "modules", "douyin", "webui", "assets", "js", "api.js")
lc_js = os.path.join(HERE, "modules", "douyin", "webui", "assets", "js", "pages", "launcher.js")
api_src = open(api_js, encoding="utf-8", errors="replace").read()
lc_src = open(lc_js, encoding="utf-8", errors="replace").read()
def _strip_js_comments(src):
    """去掉注释后再查关键字 —— 否则会误伤「注释里引用旧写法」的那几行。
    ⚠️ 行注释 `//` 和块注释 `/* */` 都要剥，只剥一种照样自伤。"""
    import re as _re
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
    return "\n".join(l.split("//")[0] for l in src.splitlines())


# ⚠️ 这两处曾写死 `status === 'running' ? 50 : 100` → 「进度条一直 50% 不动」
fake50 = [p for p, s in (("api.js", api_src), ("launcher.js", lc_src))
          if "? 50" in _strip_js_comments(s)]
check("前端不再写死 50% 进度", not fake50, "仍在写死的: " + ",".join(fake50))

comp_js = os.path.join(HERE, "modules", "douyin", "webui", "assets", "js", "components.js")
comp_src = open(comp_js, encoding="utf-8", errors="replace").read()
# ⚠️ 旧逻辑 `total > 0 ? 统计 : '等待开始'` 让**已完成**的任务也显示「等待开始」
check("任务行不再用「等待开始」兜底",
      "taskCounts(t)" in comp_src and "等待开始" not in _strip_js_comments(comp_src),
      "已改为按状态给可解释的文案")
check("总量未知时用不确定态进度条", "progressIndeterminate" in comp_src
      and "progress === null" in comp_src, "不拿常数冒充百分比")

pd_path = os.path.join(HERE, "modules", "douyin", "cli", "progress_display.py")
pd_src = open(pd_path, encoding="utf-8", errors="replace").read()
check("抖音 CLI 会在非终端下打进度行",
      "def _echo(self" in pd_src and "is_terminal" in pd_src and "[进度]" in pd_src,
      "否则任务日志中间一片空白")

# 真跑一次解析:构造一份内存日志,断言算出来的数对得上
_probe = r'''
import os, sys, tempfile
sys.path.insert(0, r"%s")
import bridge_server as B
d = tempfile.mkdtemp()
# 快手:55 个里下完 3 个
p1 = os.path.join(d, "k1.log")
open(p1, "w", encoding="utf-8").write("\n".join([
    "[2] 作者: X (ID: y) | 作品 55 个",
    "[plan] 待处理 55 | workers=4 sleep=0.25s",
    "  [dl 1/55] a1 OK | 720p", "  [dl 2/55] a2 OK | 720p", "  [dl 3/55] a3 FAIL | -",
]))
r1 = B.task_progress({"kind": "kuaishou", "status": "running", "log_path": p1,
                      "created_at": "2026-09-25T12:00:00+08:00"})
assert r1["total"] == 55 and r1["done"] == 2 and r1["failed"] == 1, r1
assert r1["progress"] == 5, r1            # 已处理 3/55 -> 5 percent
# 抖音:条目 2/5,其中失败 1 —— 失败数不能被算进成功
p2 = os.path.join(d, "b1.log")
open(p2, "w", encoding="utf-8").write("[进度] 条目 2/5 | 成功 1 失败 1 跳过 0")
r2 = B.task_progress({"kind": "douyin", "status": "running", "log_path": p2,
                      "created_at": "2026-09-25T12:00:00+08:00"})
assert r2["progress"] == 40 and r2["total"] == 5, r2
assert r2["done"] == 1 and r2["failed"] == 1, r2
# 总量未知(还在收集) -> 必须是 None,不能给假数
p3 = os.path.join(d, "k2.log")
open(p3, "w", encoding="utf-8").write("[b] 滚动 10  列表项=135 已收集=96")
r3 = B.task_progress({"kind": "kuaishou", "status": "running", "log_path": p3,
                      "created_at": "2026-09-25T12:00:00+08:00"})
assert r3["progress"] is None, r3
assert "收集作品清单" in r3["stage"], r3
print("probe-ok")
''' % os.path.join(HERE, "modules", "douyin", "webui")
_dy_py = os.path.join(HERE, "runtime", "douyin-venv", "Scripts", "python.exe")
r9 = subprocess.run([_dy_py, "-c", _probe], capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
check("进度解析算出真值", r9.returncode == 0 and "probe-ok" in (r9.stdout or ""),
      ((r9.stdout or "") + (r9.stderr or ""))[-160:])

# ── 产物目录配置不能被编码错解污染 ─────────────────────────────────────────
#
# ⚠️ 真实事故（2026-09-25）：Windows PowerShell 5.1 的 stdout 走**控制台代码页**
#    （本机 936/GBK），按 UTF-8 解会把 `E:\录屏` 读成 `E:\¼`；`os.makedirs` 照着
#     这个错名字在磁盘上**真建出一个叫 ¼ 的目录**，下载全落错地方且毫无报错。
#     修法是让 PowerShell 自己用 UTF-8 写临时文件、Python 读文件（见 bridge_server
#     的 h_tool_pick_dir）。这条门禁盯的是**乱码特征字符**，不是「能不能编码成 GBK」
#     —— 后者会误伤 `小辰妈吖🍒` 这类含 emoji 的正常路径。
_MOJIBAKE = re.compile(r"[\ufffd\u0080-\u00ff]")   # 替换符 + Latin-1 补充区
_bad_cfg = []
for _p in (os.path.join(HERE, "data", "output_dirs.json"),
           os.path.join(HERE, "modules", "douyin", "config.yml"),
           os.path.join(HERE, "modules", "live_recorder", "config.json")):
    if not os.path.exists(_p):
        continue
    try:
        _txt = open(_p, encoding="utf-8", errors="replace").read()
    except Exception:
        continue
    for _ln in _txt.splitlines():
        if _MOJIBAKE.search(_ln) and ("path" in _ln.lower() or ":\\" in _ln):
            _bad_cfg.append("%s | %s" % (os.path.basename(_p), _ln.strip()[:70]))
check("产物目录配置无乱码字符", not _bad_cfg, "; ".join(_bad_cfg[:3]))

# ── 快手「只收集清单」模式不能被单条下载截胡 ───────────────────────────────
# ⚠️ 真实事故（2026-09-25）：加了 `collect` 模式（大号先摸清有多少作品再决定下多少），
#    但 `run_download` 里那句「非 all 就走单条下载」没把它排除 →
#    说好只扫清单，结果**真的下了一个视频**（还因为两种 photoId 写法下了个重复文件）。
_ks_h = os.path.join(HERE, "modules", "kuaishou", "tools", "kuaishou_headless.py")
_ks_src = open(_ks_h, encoding="utf-8").read() if os.path.exists(_ks_h) else ""
check("快手 collect 模式走深滚不下视频",
      'mode.lower() not in ("all", "collect", "0")' in _ks_src and "if collect_only:" in _ks_src,
      "run_download 的单条下载分支必须显式排除 collect（2026-10-03 起还要排除 \"0\"=不限）")

# ── 启动脚本（.bat）格式门禁 ────────────────────────────────────────────────
# 2026-11-07 起：根目录唯一的 启动工具箱.bat（菜单式，合并了原来的
# 5 个 GUI 入口 + 手机下载（2026-11-08 删除）+ 防火墙 + WebUI 桥接，配套 .vbs 壳一并退役，
# 旧产物备份在 E:\lp\lp\douzy_launcher_backup_2026-11-07\）。
# 产物由 tools/gen_launchers.py 生成，别手改。
#
# ⚠️ 两条真实事故锁定的约束：
#   - 2026-09-25 .vbs 换行丢高位字节 → 整篇错位「双击没反应」；
#     .vbs 已退役，此类检查随之撤销。
#   - 2026-11-07 UTF-8 文件 + 行首 chcp 65001 → cmd 解析错位，中文行
#     被劈开当命令执行。所以本文件必须 GBK + CRLF + 无行首 chcp。
import glob as _glob

_launcher = os.path.join(HERE, "启动工具箱.bat")
_root_bat = sorted(_glob.glob(os.path.join(HERE, "*.bat")))
_root_vbs = sorted(_glob.glob(os.path.join(HERE, "*.vbs")))

check("根目录只有 启动工具箱.bat 一个启动脚本",
      _root_bat == [_launcher],
      "多余的: %s" % [os.path.basename(x) for x in _root_bat if x != _launcher])
check("根目录没有 .vbs（旧的无闪烁壳已随合并退役）",
      not _root_vbs,
      "多余的: %s" % [os.path.basename(x) for x in _root_vbs])

if os.path.exists(_launcher):
    _b = open(_launcher, "rb").read()
    check("启动工具箱.bat 无 BOM", not _b.startswith(b"\xef\xbb\xbf"), "cmd.exe 不认 BOM")
    _bare = _b.count(b"\n") - _b.count(b"\r\n")
    check("启动工具箱.bat 全 CRLF", _bare == 0, "裸 LF %d 个" % _bare)
    try:
        _t = _b.decode("gbk")
        check("启动工具箱.bat 能按 GBK 完整解码", True)
    except UnicodeDecodeError as _e:
        _t = ""
        check("启动工具箱.bat 能按 GBK 完整解码", False, str(_e))
    _chcp = [i for i, l in enumerate(_t.splitlines())
             if l.lstrip().lower().startswith("chcp")]
    if _chcp:
        _tail = "\n".join(_t.splitlines()[_chcp[0]:])
        check("chcp 之后到文件尾全为 ASCII（代码页切换后解析安全）",
              _tail.isascii())
    else:
        check("chcp 之后到文件尾全为 ASCII（代码页切换后解析安全）", True)
    _need = ["launcher_win.py", "bridge_server.py", ":mode_gui", ":mode_bridge"]
    _miss = [_k for _k in _need if _k not in _t]
    check("启动工具箱.bat 关键片段齐全", not _miss,
          ("缺少: %s" % _miss) if _miss else "%d 条均在" % len(_need))
    # 产物 = 生成器输出（重放模板全部自检），不一致说明有人手改过产物
    import subprocess as _sub
    _r = _sub.run([sys.executable, os.path.join(HERE, "tools", "gen_launchers.py"),
                   "--check"], capture_output=True, timeout=60)
    _out = (_r.stdout + _r.stderr).decode("utf-8", "replace").strip()
    check("启动工具箱.bat 与 gen_launchers.py 输出一致", _r.returncode == 0,
          _out[:300])
else:
    check("存在 启动工具箱.bat", False, _launcher)

# 生成器本身只允许产出这一个文件：防止旧「一工具一脚本」的清单回流
_gen_src = open(os.path.join(HERE, "tools", "gen_launchers.py"),
                "rb").read().decode("utf-8", "replace")
check("gen_launchers.py 不再生成 .vbs / 多入口清单",
      "VBS_TPL" not in _gen_src and "TOOLS = [" not in _gen_src
      and "SERVER_TOOLS" not in _gen_src,
      "生成器里仍有旧多入口痕迹，运行它会复活已删除的启动器")


# ── 快手：限流码绝不能被当成「作品永久失效」────────────────────────────
# 2026-09-25 真事：同一批 20 个里 18 个返回 status_208005（接口风控，作品好好的），
# 旧代码把它塞进 skip_ids → 这 18 部以后**永远不再重试**且毫无提示 = 静默丢片。
_fdl_path = os.path.join(HERE, "modules", "kuaishou", "tools", "kuaishou_fast_dl.py")
_fdl = ""
if os.path.exists(_fdl_path):
    with open(_fdl_path, "r", encoding="utf-8") as f:
        _fdl = f.read()


def _strip_py_comments(src):
    """剥掉 `#` 行注释后再检查 —— 否则会误伤注释里引用旧写法的那些行。
    ⚠️ 三引号 docstring 也要剥，同样是为了不被自己的说明文字绊倒。"""
    import re as _re2
    src = _re2.sub(r'("""|\'\'\').*?\1', "", src, flags=_re2.S)
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


_fdl_code = _strip_py_comments(_fdl)
_bad_skip = [l.strip() for l in _fdl_code.splitlines()
             if "skip_ids.add" in l and "208005" in l]
check("快手限流码 208005 不再进永久跳过名单",
      bool(_fdl_code) and not _bad_skip and "_permanent_fail" in _fdl_code,
      ("仍在跳过: %s" % _bad_skip[:2]) if _bad_skip else "只认 status_1040")
check("快手有撞风控后的止损/提示逻辑",
      "blocked" in _fdl_code and "208005" in _fdl_code,
      "连续撞墙会提前收手而不是按 5s/个 硬撑")

# ⚠️⚠️ 2026-10-03 真事：刹车判据只认**纯 208005**，而实测风控有「两种长相」——
#     `208005×9 | None×2 | 208005×1 | None×6 | 208005×8`，最长纯连续只有 9 < 阈值 12
#     → 刹车永不触发 → 守卫冷却从未落盘 → 每轮重新撞墙（用户报「快手下载失败」，
#     而日志里一条冷却记录都没有）。下面三条把这个坑钉死。
check("★ 风控两种状态都算（status_None 与 208005 同属风控）",
      "RISK_STATUS" in _fdl_code and "None" in _fdl_code
      and "_is_risk_status" in _fdl_code,
      "只认 208005 的话，混合序列永远达不到刹车阈值")

# 反向断言：不能退回「st == '208005' 才计数、其余清零」的老写法。
_bad_streak = [l.strip() for l in _fdl_code.splitlines()
               if 'st == "208005"' in l]
check("反向：刹车计数不再只认纯 208005（否则 status_None 会清零）",
      bool(_fdl_code) and not _bad_streak,
      ("仍有裸比较: %s" % _bad_streak[:2]) if _bad_streak
      else "统一走 _is_risk_status()")

# 配套：出现过风控信号的一轮不允许自我解封（否则冷却刚写盘就被抹平）。
check("★ 撞过风控的一轮不会清掉自己的冷却（防自我解封）",
      "n_risk == 0" in _fdl_code,
      "只判 len(jobs)>0 会把刚记下的冷却提前清掉")

# 回归测试必须在位（用真实台账序列锁死这个 bug）
_ks_bt = os.path.join(HERE, "modules", "kuaishou", "tests", "test_ks_block_streak.py")
check("风控混合序列的回归测试在位",
      os.path.exists(_ks_bt),
      "缺 test_ks_block_streak.py（真实台账序列的回归）")

# 断点续跑：大号唯一能跑完的姿势，入口必须存在且被桥接认。
_kh_path = os.path.join(HERE, "modules", "kuaishou", "tools", "kuaishou_headless.py")
_kh, _bs = "", ""
if os.path.exists(_kh_path):
    with open(_kh_path, "r", encoding="utf-8") as f:
        _kh = f.read()
_bsp = os.path.join(HERE, "modules", "douyin", "webui", "bridge_server.py")
if os.path.exists(_bsp):
    with open(_bsp, "r", encoding="utf-8") as f:
        _bs = _strip_py_comments(f.read())
check("快手断点续跑模式存在",
      "_resume_pending" in _kh and '== "resume"' in _kh and "_resume_pending()" in _kh,
      "headless 在解析前就把 mode=resume 分走")

# ── 关注管理「一键更新」的取件窗口：不能写死 ───────────────────────────
# ⚠️ 2026-10-03 真事：`creators.js` 里 `const UPDATE_LIMIT = 20` 写死。
#   作品列表按时间**倒序**返回，「取最新 N 个」只覆盖最新那一段。某位作者一次新增
#   超过 N 个作品时，排在第 N+1 位以后的：这一轮扫不到；下一轮**还是**只取最新 N 个
#   → 那几个**永远在窗口外** → 永久扫不到。增量去重只省掉「已下过的」，
#   **省不掉「没被扫到的」** —— 这是两种不同的漏。症状：静默丢片，且零提示。
_cr_path = os.path.join(HERE, "modules", "douyin", "webui", "assets", "js", "pages", "creators.js")
_cf_path = os.path.join(HERE, "modules", "douyin", "webui", "assets", "js", "config.js")
_cr = _cf = ""
if os.path.exists(_cr_path):
    with open(_cr_path, "r", encoding="utf-8") as f:
        _cr = f.read()
if os.path.exists(_cf_path):
    with open(_cf_path, "r", encoding="utf-8") as f:
        _cf = f.read()

# 反向断言：不得再有「写死的取件上限」常量。
_hard_limit = [l.strip() for l in _cr.splitlines()
               if l.strip().startswith("const UPDATE_LIMIT")]
check("★ 一键更新的取件上限不再写死（写死会静默丢片）",
      bool(_cr) and not _hard_limit and "creators_update_limit" in _cr,
      ("仍有写死常量: %s" % _hard_limit[:1]) if _hard_limit
      else "改为读设置 creators_update_limit")

# 设置项必须真的存在（前端读它、后端也靠它），且默认值不能太小。
check("取件上限有设置项且默认 >= 30",
      "creators_update_limit" in _cf and "DEFAULT_SETTINGS" in _cf
      and "creators_update_limit: 30" in _cf,
      "缺 DEFAULT_SETTINGS.creators_update_limit 或默认 < 30")
check("取件上限支持 0 = 不限（默认值给 0 才叫「一个都不漏」）",
      "0 = 不限" in _cf or "0 = 不限" in _cr,
      "没有说明 0 的语义")

# 后端侧：mode="0" 必须真的表示「不限」。
# ⚠️ 旧写法 `max(int(mode), 1)` 会把 "0" 抬成 1 → 用户要「不限」实际只下 1 个。
# ⚠️ 扫的是**剥掉注释后**的代码：上面那段解释里就写着 `max(int(mode), 1)` 这个字面量，
#    直接扫全文会被自己的说明文字绊倒（反向断言的经典误伤）。
_kh_code = _strip_py_comments(_kh)
_bad_zero = [l.strip() for l in _kh_code.splitlines()
             if "max(int(" in l and "mode" in l]
check("★ 后端 mode=\"0\" 表示不限（不会被抬成 1 个）",
      bool(_kh) and not _bad_zero and 'str(mode).strip() == "0"' in _kh_code,
      ("仍把 0 抬成 1: %s" % _bad_zero[:1]) if _bad_zero
      else "如实传 0，且走深滚全量")

# DEFAULT_SETTINGS 必须真的被定义（store.js / settings.js 都读它）。
# ⚠️ 它曾被 3 处引用却**从未定义** → 读 undefined[key] 抛 TypeError，
#   表现是「在设置页把数字框清空再失焦，整页 JS 崩掉」。
check("★ DEFAULT_SETTINGS 已定义（设置页曾因此崩）",
      "DEFAULT_SETTINGS:" in _cf,
      "store.js 的 resetSettings 与设置页数字回退都读它，缺定义会 TypeError")

# ── 一键更新的画质：抖音默认走「原画」，且尊重用户的显式选择 ───────────────
# 2026-10-03：用户要求「抖音一键更新画质设成原画」。抖音的码率阶梯里最高的
# **转码档**常常还不是作者上传的原片（后台压过一轮），所以「更新」这个场景
# 默认探原片更符合直觉。代价是每条多一次探测请求（超时上限 10s）。
check("★ 抖音一键更新默认走 original（原画）",
      "function updateQuality()" in _cr and "return 'original'" in _cr,
      "updateQuality() 缺原画默认")
check("一键更新的两条路径都把 quality 传给后端",
      _cr.count("quality: Q") == 2,
      "应恰好 2 处（一键 updateAll + 单个 updateOne），实际 %d 处" % _cr.count("quality: Q"))
check("快手侧不传 quality（那边按 manifest 最优码率，没有档位概念）",
      "kuaishouDownload(c.url, String(LIMIT))" in _cr,
      "别给快手也塞 quality")
# 用户在设置页显式改过画质时不能被静默覆盖。
check("★ 设置页显式选过画质时，更新尊重用户选择（不静默改回原画）",
      "s.video_quality" in _cr and "g !== 'highest'" in _cr,
      "缺「全局画质被改过就尊重它」的分支 = 会覆盖用户设置")
# 「留空 = 原画」必须真能留空：select 没有空选项时浏览器会显示第一项、
# 用户以为留空、其实值已被改成最高清。
check("「留空=原画」这个选项能真的留空（blankLabel）",
      "blankLabel" in _cf and "blankLabel" in open(
          os.path.join(HERE, "modules", "douyin", "webui", "assets", "js",
                       "pages", "settings.js"), encoding="utf-8").read(),
      "没有 blankLabel 的话浏览器会把 select 显示成第一项，值静默改变")
check("后端把 options.quality 落到 video_quality（链路不断）",
      'cfg["video_quality"] = options["quality"]' in _bs,
      "bridge_server 没收下 quality，前端传了也没用")
check("后端支持 original 档位（不是只认 highest/lowest）",
      '"original"' in open(os.path.join(HERE, "modules", "douyin", "core",
                                        "downloader_base.py"), encoding="utf-8").read(),
      "downloader_base 不认 original，原画会被当成 highest")

# ── 设置页在 bridge 模式下必须「能改」（2026-10-03 修「只能看不能改」）──────
# ⚠️ 真实故障：设置页在 bridge 模式（= 实际运行时）把整个表单换成了**只读表格**。
#    根因是只有 `GET /api/bridge/config`、**没有任何写入通道** → 页面上一个输入框都没有。
#    后果：上一轮刚加的「一键更新每人取最新 / 专用画质」在真实环境里根本没法改，
#    只能手工编辑 YAML。表单能渲染 ≠ 能改 —— 必须同时有 schema、bind、和写端点。
_st_js = os.path.join(HERE, "modules", "douyin", "webui", "assets", "js", "pages", "settings.js")
_st = open(_st_js, encoding="utf-8").read() if os.path.exists(_st_js) else ""
check("★ 设置页有写入端点（否则 bridge 模式只能看不能改）",
      "h_config_save" in _bs and 'add_post("/api/bridge/config"' in _bs,
      "缺 POST /api/bridge/config —— 这就是「只能看不能改」的根因")
check("★ bridge 模式下仍然渲染可编辑表单并绑定事件",
      "SETTINGS_SCHEMA.map(groupCard)" in _st
      and _st.count("bind(view)") >= 2,
      "bridge 分支必须保留 groupCard 表单并调 bind()")
check("bridge 模式下用真实 config.yml 回填表单值",
      "hydrate(cfgObj)" in _st,
      "不回填的话用户看到的是默认值，不是工程里的真实值")
check("保存失败时回滚界面（不能界面显示已保存、实际没落盘）",
      "saveOne" in _st and "回滚界面" in _st,
      "失败要恢复控件原值 + 报错，别静默")
check("写入走白名单（SETTABLE_KEYS），不是前端传什么写什么",
      "SETTABLE_KEYS" in _bs and "不在可修改白名单内" in _bs,
      "无白名单的写入口 = config.yml 可被任意覆盖")
check("敏感/有副作用的键不在白名单里",
      not any(('"%s"' % k) in _bs.split("SETTABLE_KEYS = {")[1].split("}")[0]
              for k in ("path", "cookies")),
      "path 有专门迁移流程、cookies 敏感，都不该由网页改（带引号键名匹配，"
      "避免注释里的普通词误命中）；database_path/proxy 2026-11-08 起按用户"
      "要求进白名单，但 database_path 不参与「恢复默认」（环境相关键）")
check("写 config.yml 前先备份（破坏性动作必须留退路）",
      "config.yml.bak" in _bs or '.bak"' in _bs,
      "缺备份 = 写坏了没法回滚")
check("写配置是逐键最小 diff（不整份 safe_dump 重排）",
      "_set_yaml_scalar" in _bs and "_write_config(cfg)" in _bs,
      "整份 yaml.safe_dump 会把 76 行配置的键序/引号风格全改掉")
# ⚠️ 元断言：确认上面那条不是在匹配一段死代码/注释
check("元：_set_yaml_scalar 真被调用（写入路径没绕开它）",
      "_set_yaml_scalar(k, v)" in _bs,
      "定义了却没调用 = 白写")
check("部分键失败时不写任何东西（原子性）",
      "未写入任何内容" in _bs,
      "半途失败会留下写了一半的配置")
check("★ 设置页有真浏览器回归测试（跨作用域错误只有浏览器能查出来）",
      os.path.exists(os.path.join(HERE, "modules", "douyin", "webui", "tests",
                                  "test_settings_editable_browser.py")),
      "缺 test_settings_editable_browser.py —— 「cfg is not defined」这类错"
      "node --check 查不出来，只能靠真浏览器")
# `path` 有专门的迁移流程，故意不进写白名单；那就别在表单里假装能改。
check("白名单外的字段在表单里标为只读（别让用户改了必失败）",
      "readOnly: true" in _cf and "fd.readOnly" in _st,
      "path 不在写白名单里，表单还让它可编辑 = 用户改了必被拒")

# ── 重启桥接服务时，绝不能连带干掉直播录制 ─────────────────────────────
# ⚠️ 2026-10-03 实测：录制守护（modules/live_recorder/daemon.py，监听 8519）与
#    桥接（8518）是**两棵完全独立的进程树**（实测 pid 与祖先链均无交集，
#    录制守护已连续运行 142 小时）。所以重启桥接本不该影响录制。
#    但只要 taskkill 加了 `/T`（树终止），就会把整棵树连锅端 —— 而用户
#    以为只是重启了个界面。**这条断言就是为了钉住「不加 /T」。**
_rb_path = os.path.join(HERE, "ops", "restart-bridge.py")
_rb = open(_rb_path, encoding="utf-8").read() if os.path.exists(_rb_path) else ""
_rb_code = _strip_py_comments(_rb)
_tk = [l.strip() for l in _rb_code.splitlines() if "taskkill" in l and "/PID" in l]
check("★ 重启桥接不会连带杀死直播录制（taskkill 禁用 /T 树终止）",
      bool(_rb) and bool(_tk) and not any('"/T"' in l or "'/T'" in l for l in _tk),
      "taskkill 加了 /T = 连整棵进程树一起杀，会把录制守护一起干掉：%s" % _tk[:1])
check("重启前后会核对录制守护的状态（出事要说得清）",
      "recorder_snapshot" in _rb_code and "recorder_guard" in _rb_code
      and "未受影响" in _rb,
      "缺录制核对 = 真出问题时只能默默录没了")
check("★ 拉起桥接走 launcher_win.ensure_bridge（自己 Popen 会无声消失）",
      "ensure_bridge" in _rb_code,
      "本脚本自己 Popen 在部分宿主环境下会被连带清理，"
      "表现为「端口起来过、几秒后进程没了、日志无报错」")




# ⚠️ 第三处白名单：main(argv) 里也得放行 resume，否则会被回落成 all ——
#    表现是「续跑」变成「重新深滚收集 + 全下」，白跑十几分钟（2026-09-25 真踩）。
# 白名单行的特征：元组里挨着写着 `"all", "1"` —— 单条下载的分支判断不在这个集合里
_whitelist_lines = [l.strip() for l in _kh.splitlines()
                    if '("all", "1"' in l.replace("'", '"')]
check("mode 白名单三处都认 resume",
      bool(_whitelist_lines) and all("resume" in l for l in _whitelist_lines),
      "%d 处白名单" % len(_whitelist_lines))

# ── 快手：风控守卫必须存在、必须被两个入口用上 ─────────────────────────────
# 2026-09-25 实证：连调 55 个详情接口 0 失败，但先深滚收集 1790 个之后
# 第 12 个详情就开始全灭（status_208005），而且封了 4 小时 26 分钟还没解。
# 真正触发风控的不是「接口配额」而是「规模」——守卫就是拦这个的。
_gp = os.path.join(HERE, "modules", "kuaishou", "tools", "ks_guard.py")
_g, _fdl = "", ""
if os.path.exists(_gp):
    with open(_gp, "r", encoding="utf-8") as f:
        _g = f.read()
_fdlp = os.path.join(HERE, "modules", "kuaishou", "tools", "kuaishou_fast_dl.py")
if os.path.exists(_fdlp):
    with open(_fdlp, "r", encoding="utf-8") as f:
        _fdl = f.read()
check("快手风控守卫存在", bool(_g) and "def blocked_info" in _g and "def allow_detail" in _g)
check("守卫被 fast_dl 与 headless 同时用上",
      _fdl.count("ks_guard.") >= 4 and _kh.count("ks_guard.") >= 3,
      "fast_dl=%d headless=%d" % (_fdl.count("ks_guard."), _kh.count("ks_guard.")))
# 反向：冷却首轮不许再被调回「十几分钟」——那是已经被实测否掉的乐观假设
_cooldown_ok = bool(re.search(r"BASE_COOLDOWN_MIN\s*=\s*(\d+)", _g or "")) and \
    int(re.search(r"BASE_COOLDOWN_MIN\s*=\s*(\d+)", _g).group(1)) >= 60
check("守卫首轮冷却 ≥ 60 分钟（12 分钟那套实测无效）", _cooldown_ok,
      (re.search(r"BASE_COOLDOWN_MIN\s*=\s*\d+", _g).group(0) if _g and
       re.search(r"BASE_COOLDOWN_MIN\s*=\s*\d+", _g) else "读不到"))
# 反向：旧的「原地等 12 分钟 × 8 轮」必须彻底消失，不能只是加开关绕过
check("反向：headless 里不再有原地等待的重试循环",
      "time.sleep(cooldown_min" not in _kh and "waited < max_wait_rounds" not in _kh)

# ── 批量独占：回答「能不能多开几个窗口并行跑」的那道闸 ─────────────────────
# 预算是「读-判断-写」，两个进程会各领一份 400 → 实际打 800，守卫被自己的
# 第二个窗口绕过去。而且两路并行打接口正是 09-25 撞出 208005 的那个形状。
check("守卫有批量独占租约", "def acquire_run" in _g and "def run_holder" in _g)
# 判活只用心跳时间戳。**不用 pid 探活**：Windows 上 os.kill(pid, 0) 不是探活，
# 它会真的 TerminateProcess 掉对方。（精确的反向断言在 test_ks_guard.py 里，
# 那边用 tokenize 把注释剔掉再扫，这里只确认机制在。）
check("租约判活靠心跳时间戳 + 过期自动作废",
      "RUN_STALE_SEC" in _g and "beat" in _g, "心跳 + stale")
check("fast_dl 与 headless 的批量入口都领租约",
      "ks_guard.acquire_run(" in _fdl and "acquire_run(" in _kh)
check("headless 把租约 token 传给子进程（否则 resume 会自己拦自己）",
      "KS_RUN_TOKEN" in _kh and "current_token()" in _kh)
check("租约留了显式出路（KS_ALLOW_MULTI=1）", "KS_ALLOW_MULTI" in _g)
check("粒度判别工具在位（「多窗口有没有用」的可执行答案）",
      os.path.exists(os.path.join(HERE, "modules", "kuaishou", "tools",
                                  "ks_granularity_probe.py")))
check("独立自检文件覆盖租约", os.path.exists(os.path.join(HERE, "tests", "test_ks_guard.py")))

# ── 风控桶：回答「多个 ck 有没有用」的那一层 ───────────────────────────────
# 桶按 **did（设备）** 算；按 userId（账号）分桶等于拆掉刹车 —— 同一台设备上
# 两个号会各拿一份 400，而平台很可能按设备判。所以分桶口径是**反向断言**。
check("守卫有「桶」这一层（身份 → 冷却/额度/租约都按桶记）",
      "def bucket_of" in _g and "def set_identity" in _g and "def switch_bucket" in _g)
check("★ 分桶取 did 优先（按 userId 分桶 = 拆掉刹车）",
      'p.get("did") or p.get("userId")' in _g)
check("租约按桶区分（全局一把锁会堵死「两台设备并行」）",
      "run.%s.lock" in _g)
check("身份注册表（profile → 桶）：启动只查 JSON，不开浏览器",
      "idents.json" in _g and "def identity_for_profile" in _g)
check("旧格式状态会被**认领**而不是丢弃（升级不能弄丢没到期的冷却）",
      "def _adopt_legacy" in _g)
check("两个入口都在查冷却之前确定身份",
      _fdl.index("set_identity_for_profile(") < _fdl.index("ks_guard.blocked_info()")
      and _kh.index("set_identity_for_profile(") < _kh.index("ks_guard.budget_line()"))
check("登录态可换：KS_PROFILE 在各入口都生效（多 ck 唯一的实现方式）",
      all("KS_PROFILE" in open(os.path.join(HERE, "modules", "kuaishou", "tools", f),
                               encoding="utf-8").read()
          for f in ("kuaishou_fast_dl.py", "kuaishou_headless.py", "kuaishou_full_dl.py",
                    "kuaishou_login.py", "快手一键下载器.pyw")))
check("身份诊断工具在位（让用户能自己看见有几个桶）",
      os.path.exists(os.path.join(HERE, "modules", "kuaishou", "tools", "ks_ident.py")))

# ⚠️ 必须按「快手下载接口」切片再看白名单：bridge_server 里还有哔哩哔哩的白名单
#    （B 站本来就没有 resume 模式），用「最后一段」会把 B 站那段当成快手来判，
#    于是明明放行了也报失败（2026-10-23 的假红灯）。
_ks_handler = _bs.split("async def h_kuaishou_download")[-1].split("async def h_bilibili_download")[0]
check("桥接服务放行 resume 模式",
      '"resume"' in _ks_handler and "resume" in _ks_handler.split("mode.lower() not in")[-1][:80],
      "mode 白名单含 resume")

print()
failed = [x for x in results if not x[1]]
print("== 自检完成: %d 项, 失败 %d ==" % (len(results), len(failed)))
sys.exit(1 if failed else 0)
