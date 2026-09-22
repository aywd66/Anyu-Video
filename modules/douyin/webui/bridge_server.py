# -*- coding: utf-8 -*-
"""
Douzy WebUI 桥接服务（零新增依赖：仅用工程 venv 自带的 aiohttp + pyyaml + sqlite3）

职责：
  1. 静态托管 webui/ 下的网页界面
  2. 只读查询 dy_downloader.db（作品 / 作者 / 下载历史 / 统计），绝不写库
  3. 发起真实下载：调用工程自身的 run.py CLI（临时配置文件仅放在 webui/.tasks/ 内）
  4. 读取 config.yml 供设置页展示（Cookie 等敏感字段一律脱敏）

启动（在工程根目录）：
    venv\\Scripts\\python.exe webui\\bridge_server.py
然后浏览器打开 http://127.0.0.1:8518

安全边界：
  - 只监听 127.0.0.1
  - 数据库以 mode=ro 打开（只读，绝不动之前的下载数据）
  - 不修改工程任何现有文件；所有新增文件都在 webui/ 内
"""
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime

import yaml
from aiohttp import web

# ---------- 路径 ----------
HERE = os.path.dirname(os.path.abspath(__file__))          # .../dyxz/webui
ROOT = os.path.dirname(HERE)                                # .../dyxz（工程根）
DB_PATH = os.path.join(ROOT, "dy_downloader.db")
CONFIG_PATH = os.path.join(ROOT, "config.yml")
RUN_PY = os.path.join(ROOT, "run.py")
TASK_DIR = os.path.join(HERE, ".tasks")
os.makedirs(TASK_DIR, exist_ok=True)

PORT = 8518
SENSITIVE_KEYS = {"msToken", "ttwid", "odin_tt", "passport_csrf_token", "sid_guard",
                  "cookie", "cookies", "api_key", "token", "bot_token"}

# ---------- 运行中任务表 ----------
TASKS = {}    # id -> {id,url,status,pid,proc,exit_code,log_path,config_path}
PENDING = []  # 排队中的 task_id（并发上限 2，完成后自动接续）
MAX_CONCURRENT = 2


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def db_connect():
    """只读连接；每次请求新建，保证读到 CLI 刚写入的最新数据。"""
    if not os.path.exists(DB_PATH):
        return None
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return con


def rows_to_dicts(cur):
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------- 数据查询（全部只读） ----------
def q_authors():
    con = db_connect()
    if not con:
        return []
    try:
        cur = con.execute(
            "SELECT author_name, MAX(author_id) author_id, MAX(author_sec_uid) sec_uid,"
            " COUNT(*) works, MIN(create_time) first_t, MAX(create_time) last_t"
            " FROM aweme WHERE author_name IS NOT NULL AND author_name != ''"
            " GROUP BY author_name ORDER BY works DESC")
        return rows_to_dicts(cur)
    finally:
        con.close()


def q_aweme(author=None, aweme_type=None, q=None, limit=60, offset=0):
    con = db_connect()
    if not con:
        return {"total": 0, "items": []}
    try:
        where, args = [], []
        if author:
            where.append("author_name = ?"); args.append(author)
        if aweme_type and aweme_type != "all":
            where.append("aweme_type = ?"); args.append(aweme_type)
        if q:
            where.append("(title LIKE ? OR file_path LIKE ?)"); args += [f"%{q}%", f"%{q}%"]
        wsql = (" WHERE " + " AND ".join(where)) if where else ""
        total = con.execute(f"SELECT COUNT(*) FROM aweme{wsql}", args).fetchone()[0]
        cur = con.execute(
            "SELECT aweme_id, aweme_type, title, author_name, author_id, author_sec_uid,"
            " create_time, download_time, file_path, cover_urls"
            f" FROM aweme{wsql} ORDER BY download_time DESC LIMIT ? OFFSET ?",
            args + [max(1, min(int(limit), 200)), max(0, int(offset))])
        items = rows_to_dicts(cur)
        for it in items:
            it["cover"] = extract_cover(it.pop("cover_urls", None))
        return {"total": total, "items": items}
    finally:
        con.close()


def extract_cover(raw):
    """cover_urls 是 JSON 数组字符串，取第一张；失败返回 None。"""
    if not raw:
        return None
    try:
        arr = json.loads(raw)
        if isinstance(arr, list) and arr:
            return arr[0]
    except Exception:
        pass
    return None


def q_history(limit=30):
    con = db_connect()
    if not con:
        return []
    try:
        cur = con.execute(
            "SELECT id, url, url_type, download_time, total_count, success_count"
            " FROM download_history ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 100)),))
        return rows_to_dicts(cur)
    finally:
        con.close()


def q_stats():
    con = db_connect()
    if not con:
        return {"available": False}
    try:
        total = con.execute("SELECT COUNT(*) FROM aweme").fetchone()[0]
        authors = con.execute(
            "SELECT COUNT(DISTINCT author_name) FROM aweme").fetchone()[0]
        by_type = {r[0]: r[1] for r in con.execute(
            "SELECT COALESCE(aweme_type,'unknown'), COUNT(*) FROM aweme GROUP BY 1")}
        hist = con.execute("SELECT COUNT(*) FROM download_history").fetchone()[0]
        last_dl = con.execute("SELECT MAX(download_time) FROM aweme").fetchone()[0]
        return {"available": True, "works": total, "authors": authors,
                "byType": by_type, "history": hist,
                "lastDownload": iso_ts(last_dl)}
    finally:
        con.close()


def iso_ts(ts):
    try:
        ts = int(ts)
        if ts <= 0:
            return None
        return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
    except Exception:
        return None


def read_config_sanitized():
    """读 config.yml 供展示；敏感字段值一律替换为 ***。"""
    if not os.path.exists(CONFIG_PATH):
        return {"available": False, "reason": "未找到 config.yml"}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        return {"available": False, "reason": f"解析失败: {e}"}

    def mask(obj):
        if isinstance(obj, dict):
            return {k: ("***已隐藏***" if any(s in k.lower() for s in SENSITIVE_KEYS) and v
                        else mask(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [mask(x) for x in obj]
        if isinstance(obj, str) and any(s in obj.lower() for s in ("ms_token", "your_ms_token")):
            return "***已隐藏***"
        return obj

    return {"available": True, "config": mask(data)}


# ---------- 真实下载（调用工程 CLI） ----------
def build_temp_config(task_id, options):
    """base config.yml + 本次覆盖项 → webui/.tasks/<id>.config.yml"""
    if not os.path.exists(CONFIG_PATH):
        return None, "未找到 config.yml"
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    options = options or {}
    if options.get("modes"):
        cfg["mode"] = list(options["modes"])
    num = options.get("number")
    if isinstance(num, int) and num >= 0:
        number = cfg.get("number") if isinstance(cfg.get("number"), dict) else {}
        for m in (options.get("modes") or ["post"]):
            number[m] = num
        cfg["number"] = number
    if options.get("quality"):
        cfg["video_quality"] = options["quality"]
    if isinstance(options.get("increase"), bool):
        inc = cfg.get("increase") if isinstance(cfg.get("increase"), dict) else {}
        for m in ("post", "like", "mix", "music"):
            inc[m] = bool(options["increase"])
        cfg["increase"] = inc
    path = os.path.join(TASK_DIR, f"{task_id}.config.yml")
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return path, None


def start_download(url, options, loop):
    task_id = "b" + uuid.uuid4().hex[:10]
    cfg_path, err = build_temp_config(task_id, options)
    if err:
        return None, err
    log_path = os.path.join(TASK_DIR, f"{task_id}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    log_f.write(f"# 任务 {task_id}\n# URL: {url}\n# 创建: {now_iso()}\n\n")
    log_f.close()
    task = {"id": task_id, "url": url, "status": "pending", "created_at": now_iso(),
            "pid": None, "exit_code": None, "log_path": log_path,
            "config_path": cfg_path, "finished_at": None}
    TASKS[task_id] = task
    PENDING.append(task_id)
    loop.create_task(pump_queue())
    return task, None


async def pump_queue():
    """把排队任务按并发上限启动；在任务提交与结束时都会调用。"""
    while True:
        running = [t for t in TASKS.values() if t["status"] == "running"]
        if len(running) >= MAX_CONCURRENT or not PENDING:
            return
        tid = PENDING.pop(0)
        task = TASKS.get(tid)
        if not task or task["status"] != "pending":
            continue
        log_f = open(task["log_path"], "a", encoding="utf-8", errors="replace")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess_popen(task["config_path"], task["url"], log_f, env)
        except Exception as e:
            log_f.write(f"\n[启动失败] {e}\n")
            log_f.close()
            task["status"] = "failed"
            task["exit_code"] = -1
            task["finished_at"] = now_iso()
            continue
        log_f.close()
        task["status"] = "running"
        task["pid"] = proc.pid
        asyncio.get_running_loop().create_task(watch_task(tid, proc))


def subprocess_popen(cfg_path, url, log_f, env):
    import subprocess
    return subprocess.Popen(
        [sys.executable, RUN_PY, "-c", cfg_path, "-u", url],
        cwd=ROOT, stdout=log_f, stderr=subprocess.STDOUT,
        env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)


async def watch_task(task_id, proc):
    task = TASKS.get(task_id)
    try:
        code = await asyncio.get_running_loop().run_in_executor(None, proc.wait)
    except Exception:
        code = -1
    if not task:
        return
    task["exit_code"] = code
    task["status"] = "success" if code == 0 else "failed"
    task["finished_at"] = now_iso()
    # 清理临时配置（保留日志供查看）
    try:
        if task.get("config_path") and os.path.exists(task["config_path"]):
            os.remove(task["config_path"])
    except Exception:
        pass
    # 队列接续
    try:
        await pump_queue()
    except Exception:
        pass


def tail_log(path, n=60):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except Exception:
        return ""


# ---------- HTTP 处理器 ----------
def j(data, status=200):
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


async def h_health(req):
    st = q_stats()
    return j({"status": "ok", "mode": "bridge", "projectRoot": ROOT, "db": st,
              "time": now_iso()})


async def h_authors(req):
    authors = q_authors()
    for a in authors:
        a["first_time"] = iso_ts(a.pop("first_t", None))
        a["last_time"] = iso_ts(a.pop("last_t", None))
    return j({"authors": authors})


async def h_aweme(req):
    q = req.query
    return j(q_aweme(author=q.get("author"), aweme_type=q.get("type"),
                     q=q.get("q"), limit=q.get("limit", 60), offset=q.get("offset", 0)))


async def h_history(req):
    return j({"history": q_history(req.query.get("limit", 30))})


async def h_stats(req):
    return j(q_stats())


async def h_config(req):
    return j(read_config_sanitized())


async def h_download(req):
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    loop = asyncio.get_running_loop()
    task, err = start_download(url, body.get("options"), loop)
    if err:
        return j({"error": err}, 409)
    return j({"task": task})


async def h_tasks(req):
    out = []
    for t in sorted(TASKS.values(), key=lambda x: x["created_at"], reverse=True):
        item = dict(t)
        item.pop("log_path", None)
        item.pop("config_path", None)
        out.append(item)
    return j({"tasks": out, "history": q_history(20)})


async def h_task_detail(req):
    tid = req.match_info["id"]
    t = TASKS.get(tid)
    if not t:
        return j({"error": "task not found"}, 404)
    item = dict(t)
    item["log_tail"] = tail_log(t["log_path"])
    return j(item)


async def h_cancel(req):
    tid = req.match_info["id"]
    t = TASKS.get(tid)
    if not t:
        return j({"error": "task not found"}, 404)
    if t["status"] != "running":
        return j({"error": "任务已结束"}, 409)
    try:
        import subprocess
        subprocess.run(["taskkill", "/PID", str(t["pid"]), "/T", "/F"],
                       capture_output=True, timeout=15)
        return j({"ok": True})
    except Exception as e:
        return j({"error": f"终止失败: {e}"}, 500)


# ---------- 静态托管 ----------
async def h_index(req):
    return web.FileResponse(os.path.join(HERE, "index.html"))


def make_app():
    app = web.Application(client_max_size=2 * 1024 * 1024)
    app.router.add_get("/", h_index)
    app.router.add_get("/index.html", h_index)
    app.router.add_static("/assets/", os.path.join(HERE, "assets"))
    app.router.add_get("/docs/", lambda req: web.FileResponse(
        os.path.join(HERE, "docs", "assets", "arch-diagram.png")))
    app.router.add_get("/api/bridge/health", h_health)
    app.router.add_get("/api/bridge/authors", h_authors)
    app.router.add_get("/api/bridge/aweme", h_aweme)
    app.router.add_get("/api/bridge/history", h_history)
    app.router.add_get("/api/bridge/stats", h_stats)
    app.router.add_get("/api/bridge/config", h_config)
    app.router.add_post("/api/bridge/download", h_download)
    app.router.add_get("/api/bridge/tasks", h_tasks)
    app.router.add_get("/api/bridge/task/{id}", h_task_detail)
    app.router.add_post("/api/bridge/cancel/{id}", h_cancel)
    return app


def main():
    if sys.version_info < (3, 8):
        print("需要 Python 3.8+")
        return 1
    from aiohttp import web as _w
    print("=" * 56)
    print("  Douzy WebUI 桥接服务")
    print("  界面:   http://127.0.0.1:%d" % PORT)
    print("  工程根: %s" % ROOT)
    print("  数据库: %s（只读）" % ("OK" if os.path.exists(DB_PATH) else "未找到"))
    print("  按 Ctrl+C 停止")
    print("=" * 56)
    _w.run_app(make_app(), host="127.0.0.1", port=PORT, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
