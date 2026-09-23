# -*- coding: utf-8 -*-
"""合并项目自检脚本:验证四个模块的路径解析与环境(不依赖网络)"""
import os
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
    check("抖音配置解析(path)", bool(lines) and "downloads" in lines[0], lines[0] if lines else "")
    check("抖音配置解析(database_path)", len(lines) > 1 and "data" in lines[1], lines[1] if len(lines) > 1 else "")
else:
    check("抖音配置解析", False, (r2.stderr or r2.stdout)[-300:])

db = os.path.join(HERE, "data", "douyin", "dy_downloader.db")
db_uri = "file:%s?mode=ro" % db.replace("\\", "/")
r3 = subprocess.run([dy_py, "-c",
                     "import sqlite3; c = sqlite3.connect(r'%s', uri=True); "
                     "rows = list(c.execute('SELECT name FROM sqlite_master LIMIT 5')); print(len(rows))" % db_uri],
                    capture_output=True, text=True)
check("抖音数据库只读连接", r3.returncode == 0 and r3.stdout.strip() not in ("", "0"), (r3.stdout or r3.stderr)[:150])

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
for f in ("download_log.json", "works_list.json"):
    p = os.path.join(HERE, "data", "kuaishou", f)
    check("快手数据文件 %s" % f, os.path.exists(p), p)

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
for f in ("启动直播录制器.bat",
          os.path.join("modules", "live_recorder", "start.bat"),
          os.path.join("modules", "live_recorder", "login.bat")):
    check("存在 %s" % f.replace(os.sep, "/"), os.path.exists(os.path.join(HERE, f)), f)

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

print()
failed = [x for x in results if not x[1]]
print("== 自检完成: %d 项, 失败 %d ==" % (len(results), len(failed)))
sys.exit(1 if failed else 0)
