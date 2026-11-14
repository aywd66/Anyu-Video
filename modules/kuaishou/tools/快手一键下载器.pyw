# -*- coding: utf-8 -*-
"""
快手视频一键下载器（图形化启动器）
双击快捷方式 → 弹窗粘贴链接 → 自动下载到“郁闷装深沉”文件夹
- 登录态检测：无登录自动弹扫码窗口
- 下载完成弹窗提示；文件夹自动打开
"""
import sys, io, os, re, time, json, subprocess, threading, queue
# pythonw 下 sys.stdout 可能为 None,安全重定向(修复双击无反应:pythonw 下报 AttributeError 闪退)
try:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

import tkinter as tk
from tkinter import scrolledtext, messagebox

# ---- 合并项目路径解析 ----
def _find_root():
    _here = os.path.dirname(os.path.abspath(__file__))
    for _d in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
               os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
        if os.path.isdir(os.path.join(_d, "data", "kuaishou")):
            return _d
    return os.path.dirname(_here)

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
# 同 kuaishou_headless：产物根目录可被 KS_OUTDIR 覆盖（启动台改目录后 GUI 也跟走）
OUTDIR = os.environ.get("KS_OUTDIR") or os.path.join(_find_root(), "data", "kuaishou")
PROFILE = (os.environ.get("KS_PROFILE")
           or os.path.join(_find_root(), "runtime", "kuaishou-login-profile"))
LOG_PATH = os.path.join(OUTDIR, "download_log.json")
LOGIN_KEYS = ("passToken", "kuaishou.server.web_st", "kuaishou.server.web_day7_st", "userId")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DETAIL_Q = ("query visionVideoDetail($photoId: String, $page: String, $webPageArea: String) { "
            "visionVideoDetail(photoId: $photoId, page: $page, webPageArea: $webPageArea) { status type "
            "photo { photoUrl photoH265Url caption duration "
            "manifest { adaptationSet { representation { id qualityType width height avgBitrate url } } } } } }")

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|#\s]+', "_", name).strip("_")[:60] or "kuaishou_video"

def append_log(entry):
    try:
        log = json.load(open(LOG_PATH, encoding="utf-8")) if os.path.exists(LOG_PATH) else []
    except Exception:
        log = []
    log.append(entry)
    json.dump(log, open(LOG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

def get_works(short_url):
    """移动 UA 访问短链,从 SSR 提取作品列表 + 作者信息(动态作者,不再硬编码)"""
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1", "Accept-Language": "zh-CN,zh;q=0.9"})
    r = s.get(short_url, timeout=30, allow_redirects=True)
    h = r.text
    caps = {}
    for m in re.finditer(r'"caption":"([^"]{2,80})"', h):
        seg = h[max(0, m.start() - 1500):m.start() + 1500]
        pids = re.findall(r'feed_photo\|([0-9]{15,25})\|', seg)
        if pids and pids[0] not in caps:
            caps[pids[0]] = m.group(1)
    ids = re.findall(r'feed_photo\|([0-9]{15,25})\|[0-9]+\|', h)
    works, seen = [], set()
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            works.append({"photoId": pid, "caption": caps.get(pid, "")})
    # 动态作者提取:优先路径 ID(fw/user/xxx),其次 userEid
    author_id = None
    m = re.search(r'/fw/user/([0-9A-Za-z]+)', r.url)
    if m:
        author_id = m.group(1)
    else:
        eids = re.findall(r'"userEid":"([0-9A-Za-z]+)"', h)
        if eids:
            author_id = eids[0]
    names = re.findall(r'"userName":"([^"]{1,40})"', h)
    author_name = names[0] if names else ""
    return works, author_id, author_name

def login_ok_in_profile():
    """检查 login-profile 是否有登录 Cookie(无窗口:只读 Cookie 存储,不启动浏览器渲染)"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE, channel="msedge", headless=True,
                user_agent=UA, locale="zh-CN",
                args=["--window-position=-32000,-32000"],  # 双保险:窗口移出屏幕外
                viewport={"width": 1, "height": 1})
            names = {c["name"] for c in ctx.cookies()}
            ctx.close()
            return any(k in names for k in LOGIN_KEYS)
    except Exception:
        return False

class App:
    def __init__(self, root):
        self.root = root
        root.title("快手视频一键下载器")
        root.geometry("760x520")
        tk.Label(root, text="粘贴快手分享链接（v.kuaishou.com/... 或长链接）:", anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        self.entry = tk.Entry(root, font=("Microsoft YaHei", 11))
        self.entry.pack(fill="x", padx=10)
        btnf = tk.Frame(root); btnf.pack(fill="x", padx=10, pady=6)
        self.btn = tk.Button(btnf, text="开始下载", font=("Microsoft YaHei", 11, "bold"), command=self.start)
        self.btn.pack(side="left")
        self.mode = tk.StringVar(value="all")
        tk.Radiobutton(btnf, text="全部作品", variable=self.mode, value="all").pack(side="left", padx=(12, 4))
        tk.Radiobutton(btnf, text="仅最新 1 个", variable=self.mode, value="1").pack(side="left")
        tk.Label(root, text="日志:", anchor="w").pack(fill="x", padx=10)
        self.log = scrolledtext.ScrolledText(root, height=20, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.q = queue.Queue()
        self.poll()

    def say(self, msg):
        self.q.put(msg)

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self.log.insert("end", msg + "\n")
                self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(200, self.poll)

    def start(self):
        url = self.entry.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先粘贴链接")
            return
        mode = self.mode.get()
        self.btn.config(state="disabled")
        t = threading.Thread(target=self.run_download, args=(url, mode), daemon=True)
        t.start()

    def run_download(self, url, mode):
        try:
            self.say("[1] 解析链接 ...")
            works, author_id, author_name = get_works(url)
            if not works:
                self.say("[!] 未能从链接提取作品（单作品链接或已删除）")
                self.q.put(("__done__", False))
                return
            author_disp = author_name or (author_id or "未知作者")
            self.say("[2] 作者: %s (ID: %s) | 作品 %d 个" % (author_disp, author_id or "?", len(works)))
            author_dir = os.path.join(OUTDIR, sanitize(author_disp))
            os.makedirs(author_dir, exist_ok=True)
            self.say("[2b] 保存到: " + author_dir)
            targets = works if mode == "all" else works[:1]

            self.say("[3] 检查登录态 ...")
            if not login_ok_in_profile():
                self.say("[3] 无登录态,弹出扫码窗口(请在浏览器中扫码) ...")
                r = subprocess.run([sys.executable, "-u", os.path.join(_TOOLS_DIR, "kuaishou_login.py"), "600"],
                                   capture_output=True, text=True)
                out = (r.stdout or "") + (r.stderr or "")
                if "DONE" not in out:
                    self.say("[!] 登录未完成,终止")
                    self.q.put(("__done__", False))
                    return
                self.say("[3] 登录成功")
            # 登录态检查最多做一次;若登录后仍解析失败不反复弹窗

            if mode != "all":
                ok = self.download_single(targets[0], author_dir)
                self.q.put(("__done__", ok, author_dir))
                return
            # all 模式:优先按作者主页滚动收集全量作品(分享页 SSR 只内置最新一批,比如 15 个)
            profile_url = "https://www.kuaishou.com/profile/" + author_id if author_id else ""
            list_path = os.path.join(OUTDIR, "works_list_pending.json")
            if profile_url:
                self.say("[4] 滚动收集作者主页全量作品(分享页只内置部分,全量需滚动) ...")
                cmd = [sys.executable, "-u", os.path.join(_TOOLS_DIR, "kuaishou_fast_dl.py"), author_dir,
                       "--workers", "4", "--profile", profile_url]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                for line in proc.stdout:
                    self.say(line.rstrip())
                proc.wait()
                ok = proc.returncode == 0
                self.say("[完成] 退出码 %d" % proc.returncode)
                self.q.put(("__done__", ok, author_dir))
                return
            # 无作者 ID 时回落:用分享页内置清单
            json.dump([{"id": w["photoId"], "caption": w.get("caption", ""), "duration": 0} for w in targets],
                      open(list_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            self.say("[4] 无作者主页 ID,用分享页内置清单(%d 个)下载 ..." % len(targets))
            cmd = [sys.executable, "-u", os.path.join(_TOOLS_DIR, "kuaishou_fast_dl.py"), author_dir,
                   "--workers", "4", "--list", list_path]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                self.say(line.rstrip())
            proc.wait()
            ok = proc.returncode == 0
            self.say("[完成] 退出码 %d" % proc.returncode)
            self.q.put(("__done__", ok, author_dir))
        except Exception as e:
            self.say("[异常] %s" % e)
            self.q.put(("__done__", False))

    def download_single(self, work, author_dir):
        """单作品模式:CDP 详情接口 + yt-dlp"""
        try:
            ensure_edge_ready = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, r'%s'); import kuaishou_fast_dl as k; k.ensure_edge()" % _TOOLS_DIR], capture_output=True, text=True)
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                ctx = browser.contexts[0]
                # 注入登录 Cookie
                with sync_playwright() as p2:
                    c0 = p2.chromium.launch_persistent_context(PROFILE, channel="msedge", headless=True, user_agent=UA, locale="zh-CN", viewport={"width": 1280, "height": 800})
                    cks = c0.cookies(); c0.close()
                inj = []
                for c in cks:
                    if "kuaishou" not in (c.get("domain") or ""): continue
                    cc = {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path", "/")}
                    if c.get("expires", -1) > 0: cc["expires"] = c["expires"]
                    if c.get("httpOnly"): cc["httpOnly"] = True
                    if c.get("secure"): cc["secure"] = True
                    inj.append(cc)
                ctx.add_cookies(inj)
                pid = work["photoId"]
                pg = ctx.new_page()
                pg.goto("https://www.kuaishou.com/short-video/" + pid, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(4000)
                r = pg.evaluate(
                    """async (args) => {
                        const resp = await fetch('/graphql', {method: 'POST',
                            headers: {'Content-Type': 'application/json'}, credentials: 'include',
                            body: JSON.stringify({operationName: 'visionVideoDetail',
                                variables: {photoId: args.pid, page: 'main', webPageArea: ''},
                                query: args.q})});
                        return await resp.text();
                    }""", {"pid": pid, "q": DETAIL_Q})
                d = json.loads(r)
                vd = ((d.get("data") or {}).get("visionVideoDetail") or {})
                ph = vd.get("photo") or {}
                if vd.get("status") != 1 or not ph:
                    self.say("[!] 作品不可用 status=%s" % vd.get("status"))
                    pg.close()
                    return False
                cands = []
                for aset in ((ph.get("manifest") or {}).get("adaptationSet") or []):
                    for rep in (aset.get("representation") or []):
                        u = rep.get("url") or ""
                        if u:
                            cands.append(((rep.get("avgBitrate") or 0) + (0.5 if ".mp4" in u else 0), u,
                                          "%s %sx%s %skbps" % (rep.get("qualityType"), rep.get("width"), rep.get("height"), rep.get("avgBitrate"))))
                if not cands and ph.get("photoUrl"):
                    cands = [(0, ph["photoUrl"], "photoUrl")]
                if not cands:
                    pg.close(); return False
                cands.sort(key=lambda x: -x[0])
                _, vurl, note = cands[0]
                title = sanitize(ph.get("caption") or pid)
                dest = author_dir
                os.makedirs(dest, exist_ok=True)
                outtmpl = os.path.join(dest, title + "_[" + pid + "].%(ext)s")
                self.say("[5] %s | %s" % (pid, note))
                ytdlp = [sys.executable, "-m", "yt_dlp"]
                ret = subprocess.run(ytdlp + ["--no-playlist", "--no-overwrites", "-o", outtmpl, vurl])
                pg.close()
                append_log({"photoId": pid, "caption": ph.get("caption") or "", "ok": ret.returncode == 0,
                            "quality": note, "duration_ms": ph.get("duration"),
                            "file": outtmpl.replace("%(ext)s", "mp4") if ret.returncode == 0 else None})
                self.say("[完成] %s" % ("下载成功" if ret.returncode == 0 else "下载失败(码 %d)" % ret.returncode))
                try:
                    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    os.path.join(_TOOLS_DIR, "kill_debug_edge.ps1")], capture_output=True, timeout=60)
                except Exception:
                    pass
                return ret.returncode == 0
        except Exception as e:
            self.say("[异常] %s" % e)
            return False

def main():
    root = tk.Tk()
    app = App(root)
    # 命令行传链接（WebUI 桥接服务唤起时用）：自动填入输入框并自动开始，
    # 用户无需手动粘贴。无参数双击时行为与原来完全一致。
    if len(sys.argv) > 1 and sys.argv[1].strip():
        app.entry.insert(0, sys.argv[1].strip())
        root.after(400, app.start)
    def poll_done():
        try:
            while True:
                item = app.q.get_nowait()
                if isinstance(item, tuple) and item[0] == "__done__":
                    ok = item[1]
                    open_dir = item[2] if len(item) > 2 and item[2] else os.path.join(OUTDIR, "郁闷装深沉")
                    app.btn.config(state="normal")
                    if ok:
                        messagebox.showinfo("完成", "下载完成!文件保存在:\n" + open_dir)
                        os.startfile(open_dir)
                    else:
                        messagebox.showwarning("结束", "处理结束(有失败项,详见日志)")
                else:
                    app.q.put(item)
                    break
        except queue.Empty:
            pass
        root.after(300, poll_done)
    root.after(300, poll_done)
    root.mainloop()

if __name__ == "__main__":
    main()
