# -*- coding: utf-8 -*-
"""合并项目自检脚本:验证三个模块的路径解析与环境(不依赖网络)"""
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

print()
failed = [x for x in results if not x[1]]
print("== 自检完成: %d 项, 失败 %d ==" % (len(results), len(failed)))
sys.exit(1 if failed else 0)
