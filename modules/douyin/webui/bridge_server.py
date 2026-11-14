# -*- coding: utf-8 -*-
"""
Douzy WebUI 桥接服务（零新增依赖：仅用工程 venv 自带的 aiohttp + pyyaml + sqlite3）

职责：
  1. 静态托管 webui/ 下的网页界面
  2. 查询两个平台的本地库（作品 / 作者 / 下载历史 / 统计）；默认只读，
     例外见下方安全边界
       · 抖音 → SQLite dy_downloader.db 的 aweme 表
       · 快手 → JSON 台账 download_log.json（可能与抖音不同盘，见 ks_ledger_roots）
  3. 发起真实下载：抖音走工程自身的 run.py CLI（临时配置文件仅放在 webui/.tasks/ 内），
     快手走 modules/kuaishou/tools/kuaishou_headless.py
  4. 读取 config.yml 供设置页展示（Cookie 等敏感字段一律脱敏）

启动（在工程根目录）：
    venv\\Scripts\\python.exe webui\\bridge_server.py
然后浏览器打开 http://127.0.0.1:8518

安全边界：
  - 只监听 127.0.0.1
  - 查询一律只读（抖音 mode=ro；快手台账只解析不写）
  - 全部写操作只有五处，都必须在网页上明确点击才触发：
      · delete_author()        — 抖音：按 author_name 精确删 aweme 表记录
      · ks_delete_author()     — 快手：按落盘目录名从台账移记录
      · save 作者主页映射       — 快手：记下用户手动填的主页链接
      · h_dedup_delete()       — 重复视频：把确认过的冗余文件移入 Windows 回收站
      · set_author_follow()    — 加入/取消关注：只写 .data/follow_state.json
    前两处写前都会先备份（.tasks/db-backups/ 与 .tasks/ks-backups/），
    磁盘上的视频/图片文件一律不动，其它表/其它台账行也不碰。
    第四处**只动磁盘文件**（不动数据库/台账），且只进回收站、可恢复；
    删除路径必须落在抖音/快手已知根下，越界一律拒绝。
    第五处只改「该作者算不算关注」，既不删记录也不动磁盘，
    取消关注后作品照样留在「作品档案」里。
  - 不修改工程任何现有文件；新增文件只在 webui/.tasks/、webui/.data/ 与
    data/kuaishou/（快手的主页映射与额外台账根声明）内
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import quote

import yaml
from aiohttp import web

# ---------- 路径 ----------
HERE = os.path.dirname(os.path.abspath(__file__))          # .../modules/douyin/webui
ROOT = os.path.dirname(HERE)                               # .../modules/douyin（抖音模块根）
CONFIG_PATH = os.path.join(ROOT, "config.yml")
RUN_PY = os.path.join(ROOT, "run.py")
TASK_DIR = os.path.join(HERE, ".tasks")
os.makedirs(TASK_DIR, exist_ok=True)


def _resolve_db_path():
    """作品库路径 = config.yml 里的 `database_path`；相对路径以 ROOT 为基准解析。

    ⚠️ 不要写死 `ROOT/dy_downloader.db`。工程把数据目录迁到了 `data/` 下，
    `config.yml` 里写的是 `../../data/douyin/dy_downloader.db`；写死会指向一个
    **不存在的文件**，`db_connect()` 于是返回 None，于是
    「作品档案（任务记录）」「关注管理」两页一律显示"暂无记录" ——
    看起来像空壳功能，其实是读错了地方。（2026-09-29 的真实故障）

    基准用 ROOT 而不是 CWD：桥接服务平时由 launcher 以 `cwd=modules/douyin` 起，
    但手动/其它方式启动时 CWD 未必是它；ROOT 与 CLI 的口径一致，且不依赖 CWD。
    """
    rel = "dy_downloader.db"
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
                cfg = yaml.safe_load(f) or {}
            rel = str(cfg.get("database_path") or rel).strip() or rel
    except Exception:
        pass        # 配置读不出来就退回默认名，别因为解析失败连读都不读
    return rel if os.path.isabs(rel) else os.path.normpath(os.path.join(ROOT, rel))


DB_PATH = _resolve_db_path()

# 账号中心（我的作品/喜欢/收藏/关注）。放在这里而不是文件顶部：它要用上面的
# DB_PATH 判断「哪些作品已经下到本地」，而 DB_PATH 又是从 config.yml 解析出来的。
from account_service import (  # noqa: E402
    KS_TABS as KS_ACCOUNT_TABS,
    TABS as ACCOUNT_TABS,
    AccountService,
    KsAccountService,
)
import qr_svg  # noqa: E402  （同目录的零依赖二维码渲染，扫码登录用）

ACCOUNT = AccountService(db_path=DB_PATH)
KS_ACCOUNT = KsAccountService(db_path=DB_PATH)


def _account_of(platform: str):
    """抖音/快手是两套完全独立的引擎，能力也不同（快手没有喜欢/收藏）。"""
    return KS_ACCOUNT if str(platform or "").lower() == "kuaishou" else ACCOUNT


def _tabs_of(platform: str):
    return KS_ACCOUNT_TABS if str(platform or "").lower() == "kuaishou" else ACCOUNT_TABS

PORT = 8518
SENSITIVE_KEYS = {"msToken", "ttwid", "odin_tt", "passport_csrf_token", "sid_guard",
                  "cookie", "cookies", "api_key", "token", "bot_token"}

# 起**控制台程序**（powershell.exe / python.exe）时必须带上这个标志。
# 桥接服务自己是 pythonw 拉起来的（没有控制台），Windows 在这种情况下会给
# 子进程**新建一个控制台窗口** —— 用户看到的就是"一切换页面就黑窗一闪"。
# 2026-09-28 实测：不带标志时打 6 次前端接口抓到 5 个可见控制台窗口（其中
# 快手登录态那次因脚本要跑 6 秒，是一整块黑窗）；带上标志后 0 个。
# 本文件里凡起控制台程序的地方一律用它，别再各写各的 getattr。
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

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


# ---------- 「关注」：显式清单 + 从作品来源派生 ----------
# ⚠️ 为什么不是一张独立的关注表，也不是「下过就算关注」：
#
# 关注列表一直是**派生**出来的（aweme 按 author_name 聚合 / 快手台账按落盘目录
# 聚合），好处是任何入口（CLI、WebUI、手机下载、独立图形窗口）写进来的作品都
# 自动出现在这里，不用同步、不会分叉。坏处是它把两件不同的事混成了一件：
#
#   ① 用户关注了一位作者，于是把他主页下下来（批量）；
#   ② 用户只是拿一条单作品链接存个视频，作者是谁都未必看过。
#
# ② 也会给作者建目录、写作品记录，于是作者凭空出现在「关注」里，还进了
# 「一键更新」的目标集合 —— 下次批量更新会去爬一个用户根本没关注的人。
# 2026-10-30 的实测数据里就躺着这么一位（作品数 1，来自一条 /video/ 链接）。
#
# 所以判据改成「作者名下有没有**主页来源**的作品」：作品入库时记下 origin
# （见 modules/douyin/storage/database.py 与 kuaishou_headless.py），
# origin 为 user / 空串（历史数据、来源未知）的才算关注来源。
# 单作品链接下到的作品照样进「作品档案」，只是不把作者算成关注。
#
# 两件事靠这份 JSON 覆盖（用户明确操作优先于一切派生结果）：
#   added   —— 明确「加入关注」（哪怕该作者只有单链接作品）
#   removed —— 明确「取消关注」（档案保留，不再计入统计与一键更新）
# 派生是只读的，这份文件是唯一的可写状态，所以它落在 .data/ 下、不碰数据库。
FOLLOW_PATH = os.path.join(HERE, ".data", "follow_state.json")
FOLLOW_LOCK = threading.Lock()
FOLLOW_VERSION = 1
# 一次性回填的版本标记：老库里的作品没有 origin，靠下载历史把「只下过一条
# 单作品链接」的作者认出来。跑过一次就不再跑（结果已写进 follow_state.json）。
FOLLOW_LEGACY_FLAG = "legacy_single_v1"


def _follow_blank():
    return {"version": FOLLOW_VERSION, "updated": None,
            "reconciled": [], "entries": {}}


def _follow_load():
    """读关注覆盖表。文件损坏/不存在一律当空表 —— 派生规则本身就能工作。"""
    d = _follow_blank()
    try:
        with open(FOLLOW_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return d
    if not isinstance(raw, dict):
        return d
    if isinstance(raw.get("entries"), dict):
        d["entries"] = raw["entries"]
    if isinstance(raw.get("reconciled"), list):
        d["reconciled"] = raw["reconciled"]
    d["updated"] = raw.get("updated")
    return d


def _follow_save(d):
    os.makedirs(os.path.dirname(FOLLOW_PATH), exist_ok=True)
    d["version"] = FOLLOW_VERSION
    d["updated"] = now_iso()
    tmp = FOLLOW_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FOLLOW_PATH)     # 原子替换：读到半截文件的下场是整表丢失


def _follow_key(platform, author):
    return "%s|%s" % ("kuaishou" if str(platform).lower() == "kuaishou" else "douyin",
                      author or "")


def _follow_entry(platform, author, d=None):
    d = d if d is not None else _follow_load()
    return d["entries"].get(_follow_key(platform, author))


def _follow_set(platform, author, state, why="manual"):
    """写入一条显式覆盖。state: 'added' | 'removed'。返回落盘后的条目。"""
    author = (author or "").strip()
    if not author:
        return None
    with FOLLOW_LOCK:
        d = _follow_load()
        key = _follow_key(platform, author)
        d["entries"][key] = {
            "state": state, "why": why, "author": author,
            "platform": "kuaishou" if str(platform).lower() == "kuaishou" else "douyin",
            "at": now_iso(),
        }
        _follow_save(d)
        return d["entries"].get(key)


def _follow_forget(platform, author):
    """作者记录被删除时顺带清掉覆盖项，别留一条指向空作者的孤儿条目。"""
    with FOLLOW_LOCK:
        d = _follow_load()
        if d["entries"].pop(_follow_key(platform, author), None) is not None:
            _follow_save(d)


# ---------- 数据查询（全部只读） ----------
# origin 列是后加的（见 storage/database.py 的迁移）。桥接服务可能先于
# 新版 CLI 启动，读到老库时这列还不存在 —— 那种情况下退回「全部按关注处理」，
# 等价于升级前的行为，绝不让关注列表因为一列缺失直接报错或变空。
_ORIGIN_COL = {"ok": False}     # 命中一次就永久缓存；没命中则每次重查（列可能后补）


def _aweme_has_origin(con):
    if _ORIGIN_COL["ok"]:
        return True
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(aweme)")}
    except sqlite3.Error:
        return False
    _ORIGIN_COL["ok"] = "origin" in cols
    return _ORIGIN_COL["ok"]


def _douyin_authors_split():
    """抖音作者 → (关注中, 未关注)。未关注那侧的每项带 why。

    判据只有一条：作者名下有没有 origin ∈ ('', 'user') 的作品。
      · 'user' —— 主页批量（含「一键更新」「只更新这一位」）
      · ''     —— 该字段上线前的历史数据，来源不可考，按关注处理
      · 其余   —— 单作品 / 图文 / 合集 / 音乐 / 直播回放等，不算关注来源
    """
    con = db_connect()
    if not con:
        return [], []
    try:
        has_origin = _aweme_has_origin(con)
        agg = ("SUM(CASE WHEN COALESCE(origin, '') IN ('', 'user') THEN 1 ELSE 0 END)"
               if has_origin else "COUNT(*)")
        cur = con.execute(
            "SELECT author_name, MAX(author_id) author_id, MAX(author_sec_uid) sec_uid,"
            " COUNT(*) works, MIN(create_time) first_t, MAX(create_time) last_t,"
            " %s profile_works FROM aweme"
            " WHERE author_name IS NOT NULL AND author_name != ''"
            " GROUP BY author_name ORDER BY works DESC" % agg)
        rows = rows_to_dicts(cur)
    finally:
        con.close()

    with FOLLOW_LOCK:
        state = _follow_load()
    followed, others = [], []
    for r in rows:
        derived = (r.pop("profile_works", 0) or 0) > 0
        entry = state["entries"].get(_follow_key("douyin", r["author_name"]))
        if entry is None:
            ok = derived
            why = "" if derived else "single"
        else:
            ok = entry.get("state") == "added"
            why = entry.get("why") or ("manual" if not ok else "")
        if ok:
            followed.append(r)
        else:
            r["why"] = why or "single"
            others.append(r)
    return followed, others


def q_authors():
    """「关注」列表 = 派生结果 + 显式覆盖，**不含**只在单链接里出现过的作者。"""
    return _douyin_authors_split()[0]


def q_unfollowed_authors():
    """未计入关注、但库里确实有作品的作者（前端「未加入关注」清单）。"""
    return _douyin_authors_split()[1]


def _ensure_aweme_origin_column():
    """给还没迁移过 origin 的老库补上这一列（幂等）。返回是否可用。

    正常情况下这一列由 CLI 在 initialize() 里迁移。但用户完全可能「先开 WebUI、
    还没下过任何东西」，那时关注列表读不到来源，回填也只能干等 —— 所以这里补一次。
    用的是与 CLI **完全相同**的一句 DDL（`ALTER TABLE ... ADD COLUMN`，带常量
    默认值 = 纯元数据操作，1GB 的库也是毫秒级），重复执行无副作用。
    失败（库被锁/只读文件）不算错误：读侧本来就有「来源未知=按关注」的兜底。
    """
    con = db_connect_rw()
    if not con:
        return False
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(aweme)")}
        if not cols:
            return False            # aweme 表还不存在（空库）：等 CLI 建表
        if "origin" in cols:
            return True
        con.execute("ALTER TABLE aweme ADD COLUMN origin TEXT NOT NULL DEFAULT ''")
        con.commit()
        print("[关注] 已为老库补上 aweme.origin 列（作品来源，用于区分主页/单链接）",
              flush=True)
        return True
    except sqlite3.Error as e:
        print("[关注] 无法补 origin 列（%s），本次按「来源未知=关注」处理" % e, flush=True)
        return False
    finally:
        con.close()


def _legacy_single_candidates():
    """老库里「只下过一条单作品链接」的作者（一次性回填用，之后不再调用）。

    origin 是 2026-10-30 才加的，之前入库的作品一律是 ''（来源未知），按规则
    会被当成「关注」—— 于是升级前就已经混进关注列表的那几位会**留在**列表里，
    用户会以为这次改动没生效。所以拿 `download_history` 反推一次：

    证据链要同时满足，宁可漏也不认错（认错会把真关注的作者踢出列表）：
      ① 该作者在库里只有 1 条作品，且它的 origin 还是 ''；
      ② 那条作品的 download_time 与某条下载历史相差 ≤60 秒；
      ③ 这些历史里**有**一次 total_count == 1 的非主页下载（就是那条链接）；
      ④ 这些历史里**没有** url_type == 'user'（同窗口里有主页批量就不能断定）。

    时间窗取 60 秒而不是更宽：作品行是「文件下完」时写的，下载历史是「这一轮
    跑完」时写的，同一轮单链接下载两者只差几秒（实测同一秒）。窗口放宽只会
    把无关的并发任务卷进来，增加误判。
    """
    con = db_connect()
    if not con:
        return []
    try:
        if not _aweme_has_origin(con):
            return []          # 老库还没迁移，连 origin 都没有，谈不上判断
        rows = con.execute(
            "SELECT author_name, MAX(download_time) last_t FROM aweme"
            " WHERE author_name IS NOT NULL AND author_name != ''"
            "   AND COALESCE(origin, '') = ''"
            " GROUP BY author_name HAVING COUNT(*) = 1").fetchall()
        out = []
        for name, ts in rows:
            if not ts:
                continue
            hist = con.execute(
                "SELECT url_type, total_count FROM download_history"
                " WHERE ABS(download_time - ?) <= 60", (int(ts),)).fetchall()
            if not hist:
                continue
            if any((t or "") == "user" for t, _c in hist):
                continue
            if not any((c or 0) == 1 for _t, c in hist):
                continue
            out.append(name)
        return out
    except sqlite3.Error:
        return []
    finally:
        con.close()


def follow_legacy_reconcile():
    """一次性把「只有单链接作品」的历史作者标成未关注（可撤销）。

    只写 follow_state.json（state=removed, why=legacy-single），**不改数据库**：
    用户随时能在「未加入关注」清单里一键加回来，删掉这个文件也能全部还原。
    列还没补上时**不写标记**，下次启动还能再试（否则一次失败就永久错过）。
    """
    with FOLLOW_LOCK:
        d = _follow_load()
        if FOLLOW_LEGACY_FLAG in d.get("reconciled", []):
            return 0
    if not _ensure_aweme_origin_column():
        return 0
    names = _legacy_single_candidates()
    marked = 0
    with FOLLOW_LOCK:
        d = _follow_load()
        for name in names:
            key = _follow_key("douyin", name)
            if key in d["entries"]:
                continue        # 用户已经表过态，不覆盖
            d["entries"][key] = {"state": "removed", "why": "legacy-single",
                                 "author": name, "platform": "douyin",
                                 "at": now_iso()}
            marked += 1
        d.setdefault("reconciled", []).append(FOLLOW_LEGACY_FLAG)
        _follow_save(d)
    if marked:
        print("[关注] 历史数据回填：%d 位作者只下过单链接作品，已移出关注列表"
              "（可在「未加入关注」清单里加回）：%s" % (marked, "、".join(names)), flush=True)
    return marked


def set_author_follow(platform, author, follow):
    """「加入关注 / 取消关注」的唯一入口。返回 (payload, http_status)。

    不删任何作品记录，只改关注状态 —— 与「删除作者信息」（删 aweme / 台账记录）
    是两件不同的事，界面上也分成两个菜单项，别混。
    """
    author = (author or "").strip()
    if not author:
        return {"error": "缺少作者名"}, 400
    if platform == "kuaishou":
        known = {a["nickname"] for a in ks_authors_all()}
    else:
        con = db_connect()
        if not con:
            return {"error": "数据库不存在：%s" % DB_PATH}, 404
        try:
            known = {r[0] for r in con.execute(
                "SELECT DISTINCT author_name FROM aweme WHERE author_name = ?", (author,))}
        finally:
            con.close()
    if author not in known:
        return {"error": "库里没有作者「%s」的作品记录" % author}, 404
    entry = _follow_set(platform, author, "added" if follow else "removed",
                        "manual" if follow else "manual")
    return {"ok": True, "author": author, "followed": bool(follow),
            "platform": platform, "entry": entry}, 200




# ==================== 唯一写库入口：删除作者信息 ====================
# 本服务整体是只读的（见文件头安全边界）。「关注管理」里作者是从 aweme 表按
# author_name 聚合出来的，库里没有独立的关注表——所以让一位作者从列表里消失，
# 只能删掉他在 aweme 里的作品记录。这是用户明确要求的能力（"删工程库记录、留本地
# 文件"），为把风险压到最低，这里做三件事：
#   1. 删前用 sqlite3 在线备份 API 整库备份（WAL 下安全，无需停服）；
#   2. 只按 author_name 精确匹配删 aweme，其它表一行不碰、磁盘文件一个不动；
#   3. 备份只保留最近 KEEP_BACKUPS 份，避免无限占磁盘。
BACKUP_DIR = os.path.join(TASK_DIR, "db-backups")
KEEP_BACKUPS = 2


def db_connect_rw():
    """可写连接 —— 只有 delete_author() 会用它，别在别处调用。

    与只读的 db_connect() 分开写，是为了让「谁在写库」在代码里一眼可查：
    全局搜 db_connect_rw 应当只命中删除作者这一条链路。
    """
    if not os.path.exists(DB_PATH):
        return None
    con = sqlite3.connect(DB_PATH, timeout=15)
    con.text_factory = lambda b: b.decode("utf-8", errors="replace")
    return con


def _backup_db():
    """整库在线备份到 .tasks/db-backups/，返回备份文件路径（失败返回 None）。

    用 sqlite3 的 backup API 而不是 shutil.copy：WAL 模式下直接拷主库文件
    会漏掉尚未 checkpoint 的 -wal 内容，备份出来的可能不是最新状态。
    """
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dst_path = os.path.join(
            BACKUP_DIR, "dy_downloader_%s.db" % time.strftime("%Y%m%d-%H%M%S"))
        src = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        dst = sqlite3.connect(dst_path)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
            src.close()
        # 只留最近 KEEP_BACKUPS 份（按文件名即时间戳排序）
        olds = sorted(f for f in os.listdir(BACKUP_DIR)
                      if f.startswith("dy_downloader_") and f.endswith(".db"))
        for f in olds[:-KEEP_BACKUPS]:
            try:
                os.remove(os.path.join(BACKUP_DIR, f))
            except OSError:
                pass
        return dst_path
    except Exception:
        return None


def delete_author(author):
    """删除某位作者在作品库里的全部记录，返回 (payload, http_status)。

    ⚠️ 这是本服务唯一的破坏性操作。调用方（h_author_delete）只在用户点下
    「确认删除」后才走到这里。磁盘上的视频 / 图片文件一律不动。
    """
    author = (author or "").strip()
    if not author:
        return {"error": "缺少作者名"}, 400

    con = db_connect_rw()
    if not con:
        return {"error": f"数据库不存在：{DB_PATH}"}, 404
    try:
        # 与 q_authors() 完全同一个谓词 —— 界面上看到的分组 = 会被删掉的范围，
        # 不给"看到的和删掉的不一致"留空间。
        where, args = "author_name = ?", [author]
        total = con.execute(f"SELECT COUNT(*) FROM aweme WHERE {where}", args).fetchone()[0]
        if not total:
            return {"error": f"数据库里没有「{author}」的作品记录"}, 404
        # 同一昵称可能对应多个 author_id（重名账号）。真出现时如实回传，
        # 让界面能提醒用户，而不是悄悄按昵称全删。
        ids = [r[0] for r in con.execute(
            f"SELECT DISTINCT author_id FROM aweme WHERE {where}", args)]

        backup = _backup_db()
        cur = con.execute(f"DELETE FROM aweme WHERE {where}", args)
        deleted = cur.rowcount
        con.commit()
        left = con.execute("SELECT COUNT(*) FROM aweme").fetchone()[0]
        # 记录都删了，关注覆盖项也一并清掉：留着只是一条指向空作者的孤儿条目，
        # 将来同名作者出现时还会被它莫名其妙地"继承"关注状态。
        _follow_forget("douyin", author)
        return {
            "ok": True,
            "author": author,
            "deleted": deleted,
            "authorIds": [i for i in ids if i],
            "worksLeft": left,
            "backup": backup,
            "filesKept": True,
        }, 200
    except sqlite3.OperationalError as e:
        # 最常见的是库被别的进程长时间写锁住（正在下载）
        return {"error": f"数据库写入失败：{e}（若正在下载，等任务跑完再试）"}, 503
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


# ---------- 本地媒体索引（作品档案「点封面直接播放」） ----------
# 为什么建索引，而不是拿 aweme.file_path 拼路径：
#   库里 file_path 存的是**目录**，而且历史上存在**三种基准** ——
#   `Downloaded\作者\post\...`（相对旧的 dyxz 目录）、`..\..\downloads\...`、
#   `E:\...`（绝对）。不管按哪个基准拼，都有一大批对不上（实测 1702 条里
#   只有一部分能拼中），因为用户中途换过下载目录、新旧目录并存。
#   反过来「拿 aweme_id 去文件名里找」一次全树扫描就能对上 1692/1702（99.4%），
#   且与目录结构、基准、换盘全都无关 —— 文件名里的 `<日期>_<标题>_<aweme_id>.mp4`
#   是工程写盘时就定死的。
#
# 索引是**进程内缓存**（TTL 5 分钟）：作品档案首屏就要问 60 条，逐条扫盘不可行；
# 全树扫一次本机实测 0.03s（3720 个文件），建好之后查询是 dict 命中，微秒级。
MEDIA_VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".flv", ".m4v", ".ts",
                   ".avi", ".wmv", ".3gp"}
MEDIA_IMAGE_EXT = {".webp", ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".avif"}
MEDIA_MIME = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
              ".mkv": "video/x-matroska", ".flv": "video/x-flv", ".m4v": "video/mp4",
              ".ts": "video/mp2t", ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
              ".3gp": "video/3gpp", ".webp": "image/webp", ".jpg": "image/jpeg",
              ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
              ".bmp": "image/bmp", ".avif": "image/avif"}
# 这些目录里不可能有作品文件，但会有成千上万个无关文件（venv 里一个
# site-packages 就够拖慢扫描），必须剪掉。
MEDIA_SKIP_DIRS = {"venv", ".venv", ".git", "node_modules", "__pycache__",
                   "site-packages", "runtime", "dist-info", "$recycle.bin",
                   "system volume information"}
MEDIA_MAX_DEPTH = 6
MEDIA_INDEX_TTL = 300        # 秒；新下载的文件走 miss 触发重建，不必等 TTL
MEDIA_MISS_RESCAN_GAP = 15   # 秒；索引未命中时最多这么频繁重建一次，防止被刷

_MEDIA = {"map": {}, "at": 0.0, "files": 0, "roots": [], "miss_at": 0.0}
_MEDIA_LOCK = threading.Lock()
# 后台重建进行中标志：TTL 到期后的重建放后台线程做，事件循环不卡；
# 只有「首次建索引 / 显式 force」才同步建（见 _media_index）。
_MEDIA_BUILDING = {"on": False}


def _media_index(force=False):
    now = time.time()
    with _MEDIA_LOCK:
        fresh = _MEDIA["at"] and (now - _MEDIA["at"]) < MEDIA_INDEX_TTL
        empty = not _MEDIA["at"]
        if not force and fresh:
            return _MEDIA
        if force or empty:
            # 首次建索引（缓存为空）或显式 force：没有旧数据可退，同步建。
            sync = True
        elif _MEDIA_BUILDING["on"]:
            return _MEDIA
        else:
            _MEDIA_BUILDING["on"] = True
            sync = False
    if sync:
        # 注意：不能在持有 _MEDIA_LOCK 时调用 —— _media_rebuild 自己也要拿这把锁
        # （threading.Lock 不可重入），会死锁。
        return _media_rebuild()
    # TTL 过期：后台线程重建，立刻返回旧索引。os.walk 全树 7.5 万个文件要 ~0.5s，
    # 若同步跑在事件循环上，整个 bridge（含任务页/关注页）都会跟着卡半秒。
    # 旧索引最多只比新索引少「刚下载的几条」，可接受。
    threading.Thread(target=_media_rebuild_async, daemon=True).start()
    return _MEDIA


def _media_rebuild_async():
    try:
        _media_rebuild()
    finally:
        with _MEDIA_LOCK:
            _MEDIA_BUILDING["on"] = False


def _media_rebuild():
    try:
        idx, nfiles = _media_scan()
    except Exception:
        return _MEDIA      # 扫描失败保留上一次的索引，绝不因此把页面搞崩
    roots = _media_roots()
    with _MEDIA_LOCK:
        _MEDIA.update(map=idx, at=time.time(), files=nfiles, roots=roots)
    print("[media] 索引 %d 个作品 / 扫描 %d 个媒体文件 / 根 %s"
          % (len(idx), nfiles, roots), flush=True)
    return _MEDIA


def _media_roots():
    """候选下载根：config.yml 的 `path`（抖音的权威产物目录）+ 工具箱 downloads/。

    第二个是历史基准：早期记录里 file_path 写的是 `..\\..\\downloads\\douyin\\...`，
    那批文件就落在工具箱根下的 downloads/ 里（本机实测确实存在）。
    """
    roots = []
    for p in (_douyin_cfg_dir(), os.path.join(TOOLBOX_ROOT, "downloads")):
        try:
            if p and os.path.isdir(p):
                rp = os.path.realpath(p)
                if rp not in roots:
                    roots.append(rp)
        except Exception:
            continue
    return roots


def _media_scan():
    """os.walk 候选根，按「文件名里的 19 位 aweme_id」建索引。

    返回 (map, 扫过的媒体文件数)。map[aweme_id] = {"video": 路径|None, "images": [...]}。
    """
    idx, nfiles = {}, 0
    for root in _media_roots():
        base = root.rstrip("\\/").count(os.sep)
        for dp, dns, fns in os.walk(root):
            if dp.count(os.sep) - base >= MEDIA_MAX_DEPTH:
                dns[:] = []                      # 到深度上限就停止下钻
            else:
                dns[:] = [d for d in dns
                          if d.lower() not in MEDIA_SKIP_DIRS
                          and not d.startswith((".", "$"))]
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if ext in MEDIA_VIDEO_EXT:
                    kind = "video"
                elif ext in MEDIA_IMAGE_EXT:
                    kind = "image"
                else:
                    continue
                nfiles += 1
                # 只认 15~20 位的纯数字串：文件里还有 `1080`、`_01`、`2026` 这些短数字，
                # 不限定长度会把它们当成 id（`..._1080_0.mp4` 的 `0` 就是典型误伤）。
                ids = re.findall(r"(?<!\d)\d{15,20}(?!\d)", fn)
                if not ids:
                    continue
                ent = idx.setdefault(ids[-1], {"video": None, "images": []})
                full = os.path.join(dp, fn)
                if kind == "video":
                    if ent["video"] is None:      # 同一作品多清晰度时留第一个
                        ent["video"] = full
                else:
                    ent["images"].append(full)
    for ent in idx.values():
        ent["images"].sort()                      # `_01/_02/_03` 文件名排序即正确顺序
        ent["has_media"] = bool(ent["video"] or ent["images"])
    return idx, nfiles


def _media_lookup(aweme_id):
    """查单个作品的本地文件；索引没有时触发一次（节流过的）重建。"""
    aid = re.sub(r"\D", "", str(aweme_id or ""))
    if not aid:
        return None
    ent = _media_index()["map"].get(aid)
    if ent:
        return ent
    # 刚下载完的作品还不在索引里 —— 重建一次就能看见，不必干等 TTL 到期。
    # 但要节流：一直点「文件不存在」的作品不该把服务拖进扫盘循环。
    if time.time() - _MEDIA["miss_at"] > MEDIA_MISS_RESCAN_GAP:
        _MEDIA["miss_at"] = time.time()
        return _media_index(force=True)["map"].get(aid)
    return None


def attach_media(items):
    """给 /api/bridge/aweme 的每条记录补「本地有没有文件、是视频还是图集」。

    前端据此决定封面上的播放按钮显不显示、点下去是播放还是如实说"文件不在本地"。
    """
    idx = _media_index()["map"]
    for it in items or []:
        ent = idx.get(str(it.get("aweme_id") or ""))
        it["local"] = bool(ent and ent.get("has_media"))
        it["local_kind"] = ("video" if (ent and ent.get("video"))
                            else ("image" if (ent and ent.get("images")) else None))
        it["local_images"] = len(ent.get("images") or []) if ent else 0


async def h_media(req):
    """本地作品文件流（给 <video>/<img> 用）。

    ⚠️ 只接受 aweme_id，**不接受任何路径**：路径一律由服务端索引给出，
    否则这个接口就成了「读本机任意文件」的漏洞。
    ⚠️ 必须支持 Range（206）：<video> 拖进度条要靠它。FileResponse 自己会处理
    Range，但 aiohttp 不发 `Accept-Ranges` 头，浏览器可能据此认为不能拖动 —— 显式补上。
    """
    aid = re.sub(r"\D", "", req.query.get("id") or "")
    if not aid:
        return j({"error": "缺少 id"}, status=400)
    ent = _media_lookup(aid)
    if not ent:
        return j({"error": "本地未找到该作品的文件（可能已删除或移动）", "id": aid}, status=404)
    kind = (req.query.get("kind") or "").strip().lower()
    path = None
    if kind == "image" or not ent.get("video"):
        imgs = ent.get("images") or []
        if imgs:
            try:
                i = int(req.query.get("i", 0))
            except Exception:
                i = 0
            path = imgs[max(0, min(i, len(imgs) - 1))]
    if path is None:
        path = ent.get("video")
    if not path or not os.path.isfile(path):
        return j({"error": "文件不存在或已被移动", "id": aid}, status=404)
    filename = os.path.basename(path)
    return web.FileResponse(path, headers={
        "Content-Type": MEDIA_MIME.get(os.path.splitext(path)[1].lower(),
                                       "application/octet-stream"),
        "Accept-Ranges": "bytes",
        "Content-Disposition": "inline; filename*=UTF-8''%s" % quote(filename),
        "Cache-Control": "no-store",
    })


async def h_media_reveal(req):
    """在资源管理器里定位该作品的文件（选中，而不是只打开目录）。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    aid = re.sub(r"\D", "", str((body or {}).get("id") or ""))
    ent = _media_lookup(aid) if aid else None
    path = (ent or {}).get("video") or ((ent or {}).get("images") or [None])[0]
    if not path or not os.path.isfile(path):
        return j({"error": "本地未找到该作品的文件", "id": aid}, status=404)
    try:
        if os.name == "nt":
            _explorer_open_front(path, select=True)
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
        return j({"ok": True, "path": path})
    except Exception as e:
        return j({"error": "打开失败: %s" % e}, status=500)


# 直链框允许一次粘多个链接：分隔符可能是换行/空格/中英文逗号，
# 所以只认「http(s):// 开头 + 非空白非引号」这一段，其余字符一律当分隔符。
_URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def dedup_urls(urls):
    """保序去重：同一批里的重复链接只下一次。

    用户从聊天记录里连粘两次同一条链接太常见了；不去重就会下两遍，
    白占一份磁盘和一份带宽（多链接批量时还会把源站多压一路）。
    """
    seen, out = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def split_urls(text):
    """从一段文本里抽出所有 http(s) 链接，保序去重。

    与前端 U.extractUrls 同口径，但**必须**在后端也做一遍：
    接口是公开的，不能假设调用方一定是那个网页。
    末尾的标点要削掉 —— 从聊天/文档里粘来的链接常带着「，。」或右括号。
    """
    out = []
    for m in _URL_RE.findall(text or ""):
        out.append(m.rstrip("，。、；：）】》」』,.;:)"))
    return dedup_urls(out)


def q_history(limit=30):
    con = db_connect()
    if not con:
        return []
    try:
        cur = con.execute(
            "SELECT id, url, url_type, download_time, total_count, success_count"
            " FROM download_history ORDER BY id DESC LIMIT ?", (max(1, min(int(limit), 100)),))
        rows = rows_to_dicts(cur)
        # 2026-11-13：为每条历史补「输出目录」。aweme.file_path 与批次的
        # download_time 一一对应（同一批次的落盘记录共享同一个时间戳），
        # 取该批次所有落盘路径的所在目录去重。file_path 多数情况本身就是
        # 目录（作者/日期子目录）；是文件时取其父目录。
        for r in rows:
            r["output_dir"] = None
            r["output_dirs"] = []
            r["output_name"] = None
            r["output_names"] = []
            t = r.get("download_time")
            if not t:
                continue
            try:
                fps = [row[0] for row in con.execute(
                    "SELECT DISTINCT file_path FROM aweme"
                    " WHERE download_time = ? AND file_path IS NOT NULL AND file_path != ''"
                    " LIMIT 20", (t,))]
            except Exception:
                continue
            dirs, names = [], []
            for fp in fps:
                d = fp if os.path.isdir(fp) else os.path.dirname(fp)
                if d and d not in dirs:
                    dirs.append(d)
                # 名称 = 文件夹名或文件名（basename）；文件时也归一成落盘位置的名字
                nm = os.path.basename(fp.rstrip("\\/")) or (os.path.basename(d) if d else "")
                if nm and nm not in names:
                    names.append(nm)
            r["output_dirs"] = dirs[:5]
            r["output_names"] = names[:5]
            if names:
                r["output_name"] = (names[0] + " 等 %d 个" % len(names)) if len(names) > 1 else names[0]
            if len(dirs) == 1:
                r["output_dir"] = dirs[0]
            elif dirs:
                # 多目录：取公共祖先（主页批量按作者分目录时，这就是批次根）。
                # 公共祖先若是盘根（E:\ 这种），点开毫无信息量，回落第一个目录。
                try:
                    common = os.path.commonpath(dirs)
                except Exception:
                    common = ""
                if common and os.path.isdir(common) and len(common) > 3:
                    r["output_dir"] = common
                else:
                    r["output_dir"] = dirs[0]
    finally:
        con.close()
    return rows


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


# ---------- 环境体检（顶栏常驻状态灯的数据源） ----------
def q_env():
    """本地环境检查：只查本机事实，不发网络请求（快，适合前端轮询）。

    为什么这个接口必须由桥接服务提供：ffmpeg 装没装、磁盘还剩多少，
    浏览器沙箱外的事前端永远无法知道。V2 设计的「环境状态常驻」
    要成为真实功能而不是装饰，数据只能来自这里。
    """
    def disk_info():
        # 保存目录可能还不存在（首次运行），此时向上找最近的存在的祖先来取盘符，
        # 绝不顺手创建目录 —— 体检接口不该有副作用。
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                save = (yaml.safe_load(f) or {}).get("path") or "downloads"
        except Exception:
            save = "downloads"
        p = save if os.path.isabs(save) else os.path.normpath(os.path.join(ROOT, save))
        probe = p
        while probe and not os.path.exists(probe):
            parent = os.path.dirname(probe)
            probe = None if parent == probe else parent
        try:
            du = shutil.disk_usage(probe or ROOT)
            free_gb = du.free / 1024 ** 3
            # 阈值口径：5GB 起一个直播间长录就悬了；1GB 以下连合并分片都危险
            level = "ok" if free_gb >= 5 else ("warn" if free_gb >= 1 else "danger")
            return {"level": level, "free_gb": round(free_gb, 1),
                    "total_gb": round(du.total / 1024 ** 3, 1), "path": p,
                    "exists": os.path.exists(p)}
        except Exception as e:
            return {"level": "warn", "free_gb": None, "path": p,
                    "exists": os.path.exists(p), "error": str(e)}

    # ffmpeg：先查 PATH，回落 imageio-ffmpeg（下载引擎实际用的就是它）
    ff, ff_src = shutil.which("ffmpeg"), "PATH"
    if not ff:
        try:
            import imageio_ffmpeg
            ff, ff_src = imageio_ffmpeg.get_ffmpeg_exe(), "imageio-ffmpeg"
        except Exception:
            ff, ff_src = None, ""

    # Cookie：auto_cookie 是「自动获取登录态」开关，只报状态不报值（脱敏）
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            auto_cookie = bool((yaml.safe_load(f) or {}).get("auto_cookie"))
        cookie = {"level": "ok" if auto_cookie else "warn",
                  "note": "自动获取已开启" if auto_cookie
                  else "未开启，仅能下载公开内容"}
    except Exception as e:
        cookie = {"level": "warn", "note": "读取失败: %s" % e}

    return {
        "ffmpeg": {"ok": bool(ff), "source": ff_src, "path": ff or ""},
        "runtime": {"ok": sys.version_info >= (3, 8),
                    "version": "%d.%d.%d" % sys.version_info[:3]},
        "cookie": cookie,
        "disk": disk_info(),
    }


async def h_env(req):
    return j(q_env())


# ---------- 账号中心：当前登录账号的作品 / 喜欢 / 收藏 / 关注 ----------
# 数据由 account_service 用「登录态页面通道」拉取（纯 HTTP 会被抖音 Argus 403），
# 这里只做 HTTP 壳：读缓存、触发同步、扫码登录。刷新是手动的 —— 喜欢 660 条要翻
# 33 页，自动轮询等于拿风控额度换新鲜度。
async def h_account_status(req):
    return j(_account_of(req.query.get("platform")).status())


async def h_account_list(req):
    platform = req.query.get("platform")
    tab = (req.query.get("tab") or "post").strip()
    if tab not in _tabs_of(platform):
        return j({"error": f"未知 tab：{tab}"}, 400)
    try:
        limit = int(req.query.get("limit") or 0)
    except ValueError:
        limit = 0
    return j(_account_of(platform).list_cached(tab, limit))


async def h_account_sync(req):
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    platform = body.get("platform") or req.query.get("platform")
    svc = _account_of(platform)
    tabs = body.get("tabs")
    if isinstance(tabs, str):
        tabs = [tabs]
    # 登录窗口正开着时不要排队同步：登录流程结束后浏览器就关了，会话随之消失，
    # 排进去的那个同步必然失败，还会把「同步失败」糊到刚登录成功的界面上。
    if (svc.status().get("login") or {}).get("state") == "waiting":
        return j({"started": False, "sync": svc.status()["sync"],
                  "message": "登录进行中，扫码成功后会自动同步一次，不用再点"})
    # 同步可能跑几分钟，不能把 HTTP 请求挂在那里：起后台任务，前端轮询 status。
    if not svc.status()["sync"].get("running"):
        asyncio.get_running_loop().create_task(svc.sync(tabs))
    return j({"started": True, "sync": svc.status()["sync"]})


async def h_account_login(req):
    try:
        body = await req.json()
    except Exception:
        body = {}
    platform = (body or {}).get("platform") if isinstance(body, dict) else None
    platform = platform or req.query.get("platform")
    return j(await _account_of(platform).start_login())


async def h_account_login_status(req):
    return j(_account_of(req.query.get("platform")).login_status())


# ---------- 存储位置（读取 / 校验 / 写入 config.yml + 带进度取消的迁移） ----------
MIGRATION = {}          # 迁移任务状态（单例：同时只允许一个迁移在跑）
MIG_LOCK = threading.Lock()
DATA_DIR = os.path.join(HERE, ".data")
FAV_PATH = os.path.join(DATA_DIR, "favorites.json")


def _read_config():
    """读 config.yml。utf-8-sig 兼容文件头 BOM（本工程的 config.yml 带 BOM）。"""
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        return yaml.safe_load(f) or {}


def _write_config(cfg):
    """写回 config.yml。sort_keys=False 保持原有键序；allow_unicode 中文不转义。
    ⚠️ yaml.safe_dump 会丢注释 —— 本工程 config.yml 无注释，可放心；若日后加了
    注释，这里必须换成保留注释的写入方式。"""
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, CONFIG_PATH)


def _resolve_save_path(raw):
    """config 的 path 是相对工程根的；返回绝对路径。"""
    p = raw if os.path.isabs(raw) else os.path.normpath(os.path.join(ROOT, raw))
    return os.path.normpath(p)


def _nearest_existing(p):
    """向上找最近的存在的祖先（绝不顺手创建目录 —— 检查不能有副作用）。"""
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        p = None if parent == p else parent
    return p


def _dir_usage(base, max_files=20000):
    """目录占用：顶层子目录各占多少。文件数封顶防卡死；超限标记 partial。"""
    if not base or not os.path.isdir(base):
        return {"total": 0, "groups": [], "partial": False}
    agg, loose, n, total = {}, 0, 0, 0
    try:
        for root, dirs, files in os.walk(base):
            rel = os.path.relpath(root, base)
            top = rel.split(os.sep)[0] if rel != "." else "."
            for fn in files:
                try:
                    sz = os.path.getsize(os.path.join(root, fn))
                except OSError:
                    continue
                agg[top] = agg.get(top, 0) + sz
                total += sz
                n += 1
                if n >= max_files:
                    return {"total": total, "groups": _top_groups(agg),
                            "partial": True}
    except OSError:
        pass
    return {"total": total, "groups": _top_groups(agg), "partial": False}


def _top_groups(agg, keep=6):
    items = sorted(agg.items(), key=lambda kv: -kv[1])
    head = [{"name": ("" if k == "." else k), "bytes": v} for k, v in items[:keep]]
    rest = sum(v for _, v in items[keep:])
    if rest:
        head.append({"name": "其他", "bytes": rest})
    return head


def q_storage():
    """存储现状（设置页「存储位置」面板的数据源）。"""
    try:
        raw = (_read_config().get("path") or "downloads").strip()
    except Exception as e:
        return {"available": False, "error": str(e)}
    abs_p = _resolve_save_path(raw)
    usage = _dir_usage(abs_p)
    probe = _nearest_existing(abs_p)
    running = [t for t in TASKS.values() if t["status"] in ("running", "pending")]
    free = None
    try:
        st = shutil.disk_usage(probe or ROOT)
        free = round(st.free / 1024 ** 3, 1)
    except OSError:
        pass
    return {"available": True, "raw": raw, "abs": abs_p,
            "exists": os.path.exists(abs_p), "free_gb": free,
            "usage": usage, "running_tasks": len(running)}


def check_path(candidate):
    """候选路径体检：只检查，绝不创建/修改任何东西。"""
    raw = (candidate or "").strip()
    if not raw:
        return {"ok": False, "error": "路径不能为空"}
    if os.name == "nt" and any(c in raw for c in '<>"|?*'):
        return {"ok": False, "error": "路径含 Windows 不允许的字符"}
    abs_p = _resolve_save_path(raw)
    drive_new = os.path.splitdrive(abs_p)[0].lower()
    exists = os.path.isdir(abs_p)
    # 可写性：在最近存在的祖先里建临时文件再删（目录不存在时无法直接测）
    probe = _nearest_existing(abs_p)
    writable, werr = False, ""
    if probe:
        try:
            tf = os.path.join(probe, ".douzy-write-test")
            with open(tf, "w") as f:
                f.write("x")
            os.remove(tf)
            writable = True
        except OSError as e:
            werr = str(e)
    else:
        werr = "路径与所有上级目录均不存在"
    try:
        st = shutil.disk_usage(probe or ROOT)
        free_gb = round(st.free / 1024 ** 3, 1)
    except OSError:
        free_gb = None
    # same_disk 的语义是「迁移要不要真拷数据」：候选路径 vs **当前生效路径**，
    # 不是候选路径 vs 它自己的祖先（那样永远是同盘，等于没说）。
    try:
        cur_raw = (_read_config().get("path") or "downloads").strip()
        cur_drive = os.path.splitdrive(_resolve_save_path(cur_raw))[0].lower()
    except Exception:
        cur_drive = os.path.splitdrive(ROOT)[0].lower()
    same_disk = (drive_new == cur_drive)
    if not writable:
        return {"ok": False, "abs": abs_p, "exists": exists, "writable": False,
                "error": werr or "目录不可写"}
    return {"ok": True, "abs": abs_p, "exists": exists, "writable": True,
            "free_gb": free_gb, "same_disk": same_disk,
            "note": "" if exists else "目录尚不存在，保存时会自动创建"}


def _scan_dir(base):
    """迁移前快照：{相对路径: 大小}。"""
    out = {}
    for root, _dirs, files in os.walk(base):
        rel = os.path.relpath(root, base)
        for fn in files:
            p = os.path.join(root, fn)
            try:
                out[os.path.normpath(os.path.join(rel, fn))] = os.path.getsize(p)
            except OSError:
                pass
    return out


def _migrate_worker(old, new, verify, mig):
    """迁移线程：逐项 move，支持取消；完成后按快照校验大小。"""
    import shutil as _sh
    try:
        os.makedirs(new, exist_ok=True)
        snap = _scan_dir(old)
        mig["total"] = len(snap)
        mig["bytes_total"] = sum(snap.values())
        done = 0
        for rel, sz in sorted(snap.items()):
            if mig["canceled"]:
                mig["status"] = "canceled"
                mig["error"] = "已取消（已完成 %d/%d 项；已搬文件不回退）" % (done, mig["total"])
                return
            src, dst = os.path.join(old, rel), os.path.join(new, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            mig["current"] = rel
            _sh.move(src, dst)
            done += 1
            mig["done"], mig["bytes_done"] = done, mig["bytes_done"] + sz
        if verify:
            mig["phase"] = "verify"
            bad = [rel for rel, sz in snap.items()
                   if not os.path.exists(os.path.join(new, rel))
                   or os.path.getsize(os.path.join(new, rel)) != sz]
            mig["verify_bad"] = len(bad)
            if bad:
                mig["status"] = "failed"
                mig["error"] = "校验发现 %d 个文件大小不一致，例如 %s" % (len(bad), bad[0])
                return
        # 旧目录搬空后清掉空壳（避免留下一个空目录让人误以为还在用）
        try:
            if not os.listdir(old):
                os.rmdir(old)
        except OSError:
            pass
        mig["status"] = "done"
        mig["finished_at"] = now_iso()
    except Exception as e:
        mig["status"] = "failed"
        mig["error"] = str(e)
        mig["finished_at"] = now_iso()


def start_migration(old, new, verify):
    mig = {"status": "running", "phase": "move", "old": old, "new": new,
           "total": 0, "done": 0, "bytes_total": 0, "bytes_done": 0,
           "current": "", "canceled": False, "verify": bool(verify),
           "verify_bad": 0, "error": "", "started_at": now_iso()}
    with MIG_LOCK:
        if MIGRATION.get("status") == "running":
            return None, "已有迁移在进行中"
        MIGRATION.clear()
        MIGRATION.update(mig)
    t = threading.Thread(target=_migrate_worker, args=(old, new, verify, MIGRATION),
                         daemon=True)
    t.start()
    return MIGRATION, None


async def h_storage_get(req):
    return j(q_storage())


async def h_storage_check(req):
    body = await req.json()
    return j(check_path(body.get("path", "")))


async def h_storage_apply(req):
    body = await req.json()
    chk = check_path(body.get("path", ""))
    if not chk.get("ok"):
        return j({"error": chk.get("error", "路径不可用")}, status=400)
    running = [t for t in TASKS.values() if t["status"] in ("running", "pending")]
    if running:
        return j({"error": "有 %d 个下载任务在进行中，等它们结束后再改存储位置"
                  % len(running)}, status=409)
    with MIG_LOCK:
        if MIGRATION.get("status") == "running":
            return j({"error": "已有迁移在进行中"}, status=409)
    new_abs = chk["abs"]
    try:
        cfg = _read_config()
        old_raw = cfg.get("path") or "downloads"
        cfg["path"] = body["path"].strip().replace("\\", "/")
        _write_config(cfg)
    except Exception as e:
        return j({"error": "写入 config.yml 失败: %s" % e}, status=500)
    old_abs = _resolve_save_path(old_raw)
    same = os.path.normpath(old_abs) == os.path.normpath(new_abs)
    if body.get("migrate") and not same and os.path.isdir(old_abs):
        _mig, err = start_migration(old_abs, new_abs, body.get("verify"))
        if err:
            return j({"error": err, "written": True}, status=409)
        return j({"written": True, "migrating": True})
    return j({"written": True, "migrating": False, "same_path": same})


async def h_storage_migrate(req):
    return j(MIGRATION)


async def h_storage_cancel(req):
    with MIG_LOCK:
        if MIGRATION.get("status") == "running":
            MIGRATION["canceled"] = True
            return j({"ok": True})
    return j({"ok": False, "error": "没有进行中的迁移"}, status=409)


# ---------- 收藏夹（用户主动标记"还想要的"，持久化到 .data/favorites.json） ----------
# 为什么不用 localStorage：那是每个浏览器一份，换浏览器/清缓存就丢。
# 桥接服务是常驻本机的，由它落一份 JSON 才算真正的持久化。
FAV_LOCK = threading.Lock()


def _fav_load():
    if not os.path.exists(FAV_PATH):
        return {"folders": [{"id": "unfiled", "name": "未分类", "system": True}],
                "items": []}
    try:
        with open(FAV_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"folders": [{"id": "unfiled", "name": "未分类", "system": True}],
                "items": []}
    d.setdefault("folders", [])
    d.setdefault("items", [])
    if not any(x.get("id") == "unfiled" for x in d["folders"]):
        d["folders"].insert(0, {"id": "unfiled", "name": "未分类", "system": True})
    return d


def _fav_save(d):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = FAV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FAV_PATH)


def _fav_counts(d):
    c = {f["id"]: 0 for f in d["folders"]}
    for it in d["items"]:
        c[it.get("folder", "unfiled")] = c.get(it.get("folder", "unfiled"), 0) + 1
    return c


async def h_fav_folders_get(req):
    with FAV_LOCK:
        d = _fav_load()
        c = _fav_counts(d)
    folders = [dict(f, count=c.get(f["id"], 0)) for f in d["folders"]]
    return j({"folders": folders, "total": len(d["items"])})


async def h_fav_folders_post(req):
    body = await req.json()
    act, name = body.get("action"), (body.get("name") or "").strip()
    with FAV_LOCK:
        d = _fav_load()
        if act == "create":
            if not name:
                return j({"error": "名称不能为空"}, status=400)
            if any(f["name"] == name for f in d["folders"]):
                return j({"error": "已有同名收藏夹"}, status=400)
            fid = "f" + uuid.uuid4().hex[:8]
            d["folders"].append({"id": fid, "name": name, "system": False})
            _fav_save(d)
            return j({"ok": True, "id": fid})
        if act == "rename":
            f = next((x for x in d["folders"] if x["id"] == body.get("id")), None)
            if not f:
                return j({"error": "收藏夹不存在"}, status=404)
            if f.get("system"):
                return j({"error": "系统收藏夹不能改名"}, status=400)
            if not name:
                return j({"error": "名称不能为空"}, status=400)
            f["name"] = name
            _fav_save(d)
            return j({"ok": True})
        if act == "delete":
            f = next((x for x in d["folders"] if x["id"] == body.get("id")), None)
            if not f:
                return j({"error": "收藏夹不存在"}, status=404)
            if f.get("system"):
                return j({"error": "「未分类」是兜底收藏夹，不能删除"}, status=400)
            d["folders"] = [x for x in d["folders"] if x["id"] != f["id"]]
            moved = 0
            for it in d["items"]:
                if it.get("folder") == f["id"]:
                    it["folder"] = "unfiled"   # 删夹不删收藏：条目回收到未分类
                    moved += 1
            _fav_save(d)
            return j({"ok": True, "moved": moved})
    return j({"error": "未知操作"}, status=400)


async def h_fav_items_get(req):
    folder = req.query.get("folder", "all")
    with FAV_LOCK:
        d = _fav_load()
    items = d["items"]
    if folder == "unfiled" or (folder != "all" and folder):
        items = [x for x in items if x.get("folder", "unfiled") == folder]
    items = sorted(items, key=lambda x: x.get("added_at", ""), reverse=True)
    return j({"items": items})


async def h_fav_items_post(req):
    body = await req.json()
    act = body.get("action")
    with FAV_LOCK:
        d = _fav_load()
        if act == "add":
            url = (body.get("url") or "").strip()
            if not url:
                return j({"error": "请粘贴作品链接"}, status=400)
            m = re.search(r"(?:video|note)/(\d+)|[?&]aweme_id=(\d+)", url)
            key = (m.group(1) or m.group(2)) if m else url
            if any(x.get("key") == key for x in d["items"]):
                hit = next(x for x in d["items"] if x.get("key") == key)
                fname = next((f["name"] for f in d["folders"]
                              if f["id"] == hit.get("folder", "unfiled")), "未分类")
                return j({"error": "这条已在收藏里（%s）" % fname}, status=409)
            folder = body.get("folder") or "unfiled"
            if not any(f["id"] == folder for f in d["folders"]):
                folder = "unfiled"
            it = {"id": "i" + uuid.uuid4().hex[:8], "key": key, "url": url,
                  "title": (body.get("title") or url).strip()[:120],
                  "folder": folder, "done": False,
                  "added_at": now_iso()}
            d["items"].append(it)
            _fav_save(d)
            return j({"ok": True, "item": it})
        it = next((x for x in d["items"] if x["id"] == body.get("id")), None)
        if not it:
            return j({"error": "收藏项不存在"}, status=404)
        if act == "move":
            f = body.get("folder") or "unfiled"
            if not any(x["id"] == f for x in d["folders"]):
                return j({"error": "目标收藏夹不存在"}, status=400)
            it["folder"] = f
        elif act == "rename":
            t = (body.get("title") or "").strip()
            if t:
                it["title"] = t[:120]
        elif act == "toggle-done":
            it["done"] = not it.get("done")
        elif act == "remove":
            d["items"] = [x for x in d["items"] if x["id"] != it["id"]]
        else:
            return j({"error": "未知操作"}, status=400)
        _fav_save(d)
        return j({"ok": True})


# ---------- 直播录制（模块 4：桌面 LiveRecorder 的状态总览 + 唤起入口） ----------
# 直播录制是一个独立的 PyQt6 桌面程序（modules/live_recorder），WebUI 不实现录制，
# 只提供「状态总览 + 一键唤起 + 打开录像目录」的诚实入口，不假装能在网页里录。
TOOLBOX_ROOT = os.path.dirname(os.path.dirname(ROOT))   # 工具箱根目录（douyin 模块的上一级上一级）
LR_DIR = os.path.join(TOOLBOX_ROOT, "modules", "live_recorder")
LR_RUNTIME = os.path.join(TOOLBOX_ROOT, "runtime", "live-recorder-env")
LR_CONFIG = os.path.join(LR_DIR, "config.json")
LR_MAIN = os.path.join(LR_DIR, "main.py")

# 快手模块（模块 2：桌面 tkinter 一键下载器）
KS_TOOLS = os.path.join(TOOLBOX_ROOT, "modules", "kuaishou", "tools")
KS_PYW = os.path.join(KS_TOOLS, "快手一键下载器.pyw")
KS_HEADLESS = os.path.join(KS_TOOLS, "kuaishou_headless.py")
KS_RUNTIME = os.path.join(TOOLBOX_ROOT, "runtime", "kuaishou-env")

# 哔哩哔哩模块（复用 kuaishou-env 解释器：yt-dlp 已装在该环境）
BILI_DIR = os.path.join(TOOLBOX_ROOT, "modules", "bilibili")
BILI_HEADLESS = os.path.join(BILI_DIR, "bilibili_headless.py")
TY_DIR = os.path.join(TOOLBOX_ROOT, "modules", "ty")
TY_HEADLESS = os.path.join(TY_DIR, "ty_headless.py")

# yt-dlp 万能站点下载器（复用 kuaishou-env 解释器：yt-dlp 已装在该环境）
YTDLP_DIR = os.path.join(TOOLBOX_ROOT, "modules", "ytdlp")
YTDLP_HEADLESS = os.path.join(YTDLP_DIR, "ytdlp_headless.py")
YTDLP_DATA_DIR = os.path.join(TOOLBOX_ROOT, "data", "ytdlp")
YTDLP_CONF = os.path.join(YTDLP_DATA_DIR, "yt-dlp.conf")        # 高级配置（yt-dlp 原生 --config-location）
YTDLP_ARCHIVE = os.path.join(YTDLP_DATA_DIR, "archive.txt")     # 下载存档（去重）

# Stripchat 直播录制（模块 6：纯标准库后台录制器，无独立运行时需求）
SC_DIR = os.path.join(TOOLBOX_ROOT, "modules", "stripchat")
SC_RECORDER = os.path.join(SC_DIR, "stripchat_recorder.py")
# 运行期工件（日志 / 停止文件）放 data 目录，不混进用户录像目录
SC_STATE_DIR = os.path.join(TOOLBOX_ROOT, "data", "stripchat")
SC_LOG = os.path.join(SC_STATE_DIR, "recorder.log")
SC_STOP = os.path.join(SC_STATE_DIR, "stop")
# 录制器自己写的会话登记：桥接重启后凭它「认领」在跑的录制进程，
# 否则监护丢失 —— 网页会误显示「未在录制」，而进程还在后台录。
SC_SESSION = os.path.join(SC_STATE_DIR, "session.json")
SC_PIDFILE = os.path.join(SC_STATE_DIR, "recorder.pid")
# 418 自愈：浏览器 Cookie 抓取器（playwright + Edge，跑在 kuaishou-env）
SC_GRABBER = os.path.join(SC_DIR, "grab_cookies.py")
SC_COOKIES = os.path.join(SC_STATE_DIR, "cookies.json")

# clawfetch 直链下载器（模块 3：纯标准库 CLI）
CLAW_DIR = os.path.join(TOOLBOX_ROOT, "modules", "clawfetch")
CLAW_PY = os.path.join(CLAW_DIR, "clawfetch.py")
DIRECT_DIR = os.path.join(TOOLBOX_ROOT, "downloads", "direct")

# 会「劫持」子进程出网行为的代理环境变量（大小写两种写法 urllib 都认）。
_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                   "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def strip_proxy_env(env):
    """把代理类环境变量从子进程环境里摘掉，返回新 dict（不改原对象）。

    为什么必须摘：这些变量常由「启动桥接的那个外壳」注入，而不是用户本意。
    本项目已经吃过一次这个亏 —— 见 lr_daemon_call() 里那句注释：注入的
    HTTP_PROXY 会让 127.0.0.1 的请求也走代理并 502，当时只对守护进程做了规避，
    没管子进程。子进程（clawfetch / playwright）会一路继承下去，
    于是「用户点一下下载」变成一个连到别人的代理端口上的请求。

    摘掉 env **不等于**禁用代理：urllib 在 Windows 上读不到环境变量时会继续读
    系统代理（注册表 ProxyEnable/ProxyServer）。所以自己开了 Clash 之类
    「系统代理」的用户不受影响，被摘掉的只是外壳临时注入的那一份。
    """
    out = dict(env)
    for k in _PROXY_ENV_KEYS:
        out.pop(k, None)
    return out


def lr_python(which="pythonw"):
    """返回 live-recorder-env 的解释器绝对路径；不存在返回 None。"""
    p = os.path.join(LR_RUNTIME, "Scripts", which + ".exe")
    return p if os.path.exists(p) else None


def ks_python(which="pythonw"):
    """返回 kuaishou-env 的解释器绝对路径；不存在返回 None。"""
    p = os.path.join(KS_RUNTIME, "Scripts", which + ".exe")
    return p if os.path.exists(p) else None


_KS_LOGIN_CACHE = {"ok": None, "ts": 0.0}
_KS_LOGIN_LOCK = threading.Lock()


def ks_login_ok(force=False):
    """快手登录态（带 30s 缓存）。跑 kuaishou_headless.py --check-login，
    需启动 playwright headless msedge 读 Cookie，约 2~4 秒，故缓存。
    任何异常一律按「未登录」处理（最坏是让用户多扫一次码，而非误判后下载失败）。"""
    now = time.time()
    with _KS_LOGIN_LOCK:
        if not force and _KS_LOGIN_CACHE["ok"] is not None and now - _KS_LOGIN_CACHE["ts"] < 30:
            return _KS_LOGIN_CACHE["ok"]
    exe = ks_python("python") or ks_python("pythonw")
    if not exe or not os.path.exists(KS_HEADLESS):
        return False
    try:
        import subprocess
        out = subprocess.run(
            [exe, KS_HEADLESS, "--check-login"],
            capture_output=True, timeout=60,
            # 不加这个标志，这 6 秒里用户会看到一个黑色的控制台窗口挂在屏幕上
            creationflags=NO_WINDOW,
        )
        ok = b"LOGGED_IN" in (out.stdout or b"")
    except Exception:
        ok = False
    with _KS_LOGIN_LOCK:
        _KS_LOGIN_CACHE["ok"] = ok
        _KS_LOGIN_CACHE["ts"] = time.time()
    return ok


# ====================== 快手本地作品库（模块 2 的只读侧） ======================
# 快手与抖音是两套**完全独立**的引擎，台账格式也完全不同，所以这里另起一套数据层，
# 不与抖音的 q_aweme / _MEDIA 混用：
#   抖音 → SQLite 的 aweme 表，ID 是 19 位纯数字 `aweme_id`
#   快手 → JSON 数组 download_log.json，ID 是 `photoId`，而且**有两种形态**：
#          base36（3xt6wwyepq6vytq）与纯数字（5209820537914262695）在真实台账里并存。
# 混用会撞索引，也会把抖音侧「19 位数字」这个判据搞乱。
#
# 台账来源可能不止一处 —— 工具箱换过产物目录，老库不会自己搬过来，所以几处都读：
#   ① data\kuaishou        工具箱「快手下载」的**默认**落点
#   ② data\output_dirs.json 的 `kuaishou`（启动台可改，经 KS_OUTDIR 下发给下载器）
#      —— 2026-09-29 实测踩到：落点被改成 E:\录屏\kssp\kuaishou，而索引只读 ① 和 ③，
#         于是「明明下成功了，作品档案/关注管理里却看不到」，新下的 2 个文件当场"消失"。
#   ③ E:\录屏\spxz         命令行版（kuaishou_dl.py）的历史主库，1100+ 条
# 第 ③ 类通过 data\kuaishou\ledger_roots.json 声明（数组），换机器/换盘不用改代码。
KS_PRIMARY_ROOT = os.path.join(TOOLBOX_ROOT, "data", "kuaishou")
KS_EXTRA_ROOTS = os.path.join(KS_PRIMARY_ROOT, "ledger_roots.json")
KS_LEGACY_ROOTS = [r"E:\录屏\spxz"]      # 内置候选（存在才读）
# ⚠️ 第 ② 类也必须是**模块级常量**，否则测试没法隔离它：合成台账的测试环境里会混进
#    真实落点的台账（作者列表冒出真实作者、B 段期望值直接算错）。这条 2026-09-29 踩过。
#    None = 运行时从 output_dirs.json 取；"" = 本环境没有工具落点（测试用）。
KS_TOOL_OUTDIR = None
# 作者主页：自动推断不出来的作者靠它补，也允许覆盖自动结果。
# 格式 {"作者昵称": "https://www.kuaishou.com/profile/xxx"} 或直接给作者 ID。
KS_PROFILE_MAP = os.path.join(KS_PRIMARY_ROOT, "author_profiles.json")
KS_BACKUP_DIR = os.path.join(TASK_DIR, "ks-backups")
KS_KEEP_BACKUPS = 5
KS_INDEX_TTL = 120        # 秒；台账改动主要靠 mtime 签名失效，TTL 只是兜底

_KS = {"items": [], "map": {}, "at": 0.0, "sig": None, "roots": []}
_KS_LOCK = threading.Lock()


def _ks_tool_outdir():
    """工具箱自己的快手落点（上面注释里的第 ② 类来源）。

    正常情况从 `data/output_dirs.json` 取（启动台可改）；`KS_TOOL_OUTDIR`
    被显式设置时以它为准 —— 测试靠这个把合成环境与真实落点彻底隔开。
    """
    if KS_TOOL_OUTDIR is not None:
        return KS_TOOL_OUTDIR
    try:
        return _init_tool_dirs().get("kuaishou") or ""
    except Exception:
        return ""


def ks_ledger_roots():
    """真正要读的台账根目录（过滤掉不存在的、去重、保序）。"""
    cand = [KS_PRIMARY_ROOT]
    cand.append(_ks_tool_outdir())      # 工具箱自己的落点，见 KS_TOOL_OUTDIR 的说明
    try:
        with open(KS_EXTRA_ROOTS, "r", encoding="utf-8-sig") as f:
            extra = json.load(f)
        if isinstance(extra, list):
            cand += [str(x) for x in extra if str(x).strip()]
    except Exception:
        pass
    cand += KS_LEGACY_ROOTS
    out = []
    for p in cand:
        try:
            if p and os.path.isdir(p):
                rp = os.path.realpath(p)
                if rp not in out:
                    out.append(rp)
        except Exception:
            continue
    return out


def _ks_ledger_files():
    """返回 [(根, 台账文件)]，只含真实存在的台账。"""
    out = []
    for r in ks_ledger_roots():
        p = os.path.join(r, "download_log.json")
        if os.path.isfile(p):
            out.append((r, p))
    return out


def _ks_signature():
    """台账 + 作品清单 + 手动映射的 (路径, mtime, size) 三元组。

    任何一处改了签名就变 → 重建索引。比 TTL 可靠：刚下完的作品立刻就可见，
    不用等缓存过期（这一点和抖音侧的 MEDIA_MISS_RESCAN_GAP 是同一个思路）。
    """
    parts = []
    for _r, p in _ks_ledger_files():
        try:
            parts.append((p, os.path.getmtime(p), os.path.getsize(p)))
        except OSError:
            parts.append((p, 0, 0))
    for r in ks_ledger_roots():
        try:
            for n in sorted(os.listdir(r)):
                if n.startswith("works_list") and n.endswith(".json"):
                    fp = os.path.join(r, n)
                    parts.append((fp, os.path.getmtime(fp), os.path.getsize(fp)))
        except OSError:
            pass
    try:
        parts.append((KS_PROFILE_MAP, os.path.getmtime(KS_PROFILE_MAP),
                      os.path.getsize(KS_PROFILE_MAP)))
    except OSError:
        pass
    return tuple(parts)


def _ks_read_ledger(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
    except Exception:
        return []
    return d if isinstance(d, list) else []


def _ks_item(raw, root):
    """台账里的一条 → 归一化作品。返回 None 表示这条没救（无 photoId）。

    ⚠️ 台账**没有**下载时间、体积字段，只有 `file`、`quality`、`duration_ms`。
    时间与体积一律取自**磁盘文件的 stat** —— 这是唯一诚实的来源，
    编一个出来不如不给。（台账里 188 条是失败记录、连 file 都没有，直接丢弃：
    「作品档案」要展示的是下到手的东西，没文件的行留在台账里当审计记录就好。）
    """
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("photoId") or "").strip()
    if not pid:
        return None
    f = str(raw.get("file") or "").strip()
    author, exists, size, mtime = "", False, None, None
    if f:
        try:
            rel = os.path.relpath(os.path.normcase(f), os.path.normcase(root))
        except ValueError:
            rel = ""
        if rel and not rel.startswith(".."):
            seg = [s for s in rel.split(os.sep) if s]
            # 落点约定：<根>\<作者>\标题_[id].mp4。直接躺在根下的作品没有作者目录。
            if len(seg) >= 2:
                author = seg[0]
        else:
            # ⚠️ 文件在「台账所在目录」之外：整份产物被搬走了，或者已经被删掉。
            #
            # 这时 relpath 会以 `..` 开头，按上面的规则取不到作者 —— 以前就一律并进
            # 「(未分类)」。一旦**所有**记录都这样（产物目录整体搬家/清理），几十位
            # 作者就会塌成一位「(未分类)」，界面显示「作者 1 位 / 作品 4,617 条」，
            # 而且再也看不出原来有哪几位（2026-10-30 用户实际遇到）。
            #
            # 落点约定是 <根>\<作者>\<文件>，所以**绝对路径的上级目录名就是作者名**，
            # 与文件还在不在无关。宁可显示「作者对、但文件不在」，
            # 也不要把所有作者混成一堆 —— 后者连"该从回收站还原哪个目录"都看不出来。
            parent = os.path.basename(os.path.dirname(os.path.normpath(f)))
            root_name = os.path.basename(os.path.normpath(root or ""))
            if parent and parent.lower() != (root_name or "").lower() \
                    and not re.fullmatch(r"[A-Za-z]:", parent):
                author = parent
        try:
            exists = os.path.isfile(f)
            if exists:
                size = os.path.getsize(f)
                mtime = os.path.getmtime(f)
        except OSError:
            exists = False
    if not author:
        author = "(未分类)"
    dur = raw.get("duration_ms")
    try:
        dur = round(float(dur) / 1000.0, 1) if dur else 0
    except (TypeError, ValueError):
        dur = 0
    return {
        "photoId": pid,
        "title": str(raw.get("caption") or "").strip(),
        "author": author,
        "file": f,
        "exists": exists,
        "size": size,
        "mtime": mtime,
        "quality": (str(raw.get("quality") or "").strip() or None),
        "durationSec": dur,
        "ok": bool(raw.get("ok")),
        # 作品来源：'single' = 单作品链接下的；'' = 主页批量（老台账也归这一档）。
        # 只用于派生「关注」列表，不影响作品档案 —— 见 ks_authors_all。
        "origin": str(raw.get("origin") or "").strip(),
        "root": root,
    }


def ks_index(force=False):
    """快手作品索引（带 mtime 签名缓存）。结构：items 列表 + map[photoId]。

    同一 photoId 在两处台账都有时保留**文件存在**的那一条 —— 工具箱换过落点，
    老台账里的路径可能已经失效，新台账那条才是真的。
    """
    sig = _ks_signature()
    now = time.time()
    with _KS_LOCK:
        if not force and _KS["sig"] == sig and (now - _KS["at"]) < KS_INDEX_TTL:
            return _KS
        seen = {}
        for root, path in _ks_ledger_files():
            for raw in _ks_read_ledger(path):
                it = _ks_item(raw, root)
                if not it:
                    continue
                # 连 file 都没有的（失败记录、188 条）不进索引：作品档案要展示的是
                # 下到手的东西，没有文件的行留着当审计记录，不该占一张卡片。
                # 删除时走的是原始台账，这类行会原样保留，不会被动到。
                if not it["file"]:
                    continue
                prev = seen.get(it["photoId"])
                if prev is None or (it["exists"] and not prev.get("exists")):
                    seen[it["photoId"]] = it
        items = list(seen.values())
        _KS.update(items=items, map=seen, at=now, sig=sig, roots=ks_ledger_roots())
        print("[ks] 台账索引: %d 个作品 / %d 处台账 %s"
              % (len(items), len(_ks_ledger_files()), _KS["roots"]), flush=True)
        return _KS


def ks_media_lookup(pid):
    """按 photoId 找本地文件（只认台账里记的路径，不接受任意路径）。"""
    pid = str(pid or "").strip()
    if not pid:
        return None
    ent = ks_index()["map"].get(pid)
    return ent if (ent and ent.get("exists")) else None


def ks_works(author=None, q=None, limit=60, offset=0):
    """快手作品列表。字段名与抖音侧对齐，前端可复用同一张卡片组件。"""
    items = list(ks_index()["items"])
    if author:
        items = [x for x in items if x["author"] == author]
    if q:
        ql = str(q).lower()
        items = [x for x in items
                 if ql in (x["title"] or "").lower() or ql in x["author"].lower()
                 or ql in (x["file"] or "").lower()]
    # 有文件时间的排前面（最新在前）；没有时间的垫底，不假装知道顺序
    items.sort(key=lambda x: (x.get("mtime") or 0), reverse=True)
    total = len(items)
    try:
        lim = max(1, min(int(limit), 200))
        off = max(0, int(offset))
    except (TypeError, ValueError):
        lim, off = 60, 0
    page = items[off:off + lim]

    def cover_url(pid):
        """已生成封面的回端点 URL；未生成的回 None（前端程序化占位兜底）。"""
        return ("/api/bridge/ks/covers/%s.jpg" % pid
                if os.path.exists(ks_cover_file(pid)) else None)

    covers_pending = sum(1 for x in items
                         if x.get("exists") and not os.path.exists(ks_cover_file(x["photoId"])))
    return {"total": total,
            "covers": {"pending": covers_pending, "total": total},
            "items": [{
        "id": x["photoId"],
        "awemeId": x["photoId"],          # mediaPlayer 用同一个字段拼播放地址
        "photoId": x["photoId"],
        "type": "video",                  # 快手台账只记视频，没有图文
        "title": x["title"] or "无标题作品",
        "creatorName": x["author"],
        "createTime": None,
        "downloadTime": iso_ts(x.get("mtime")),
        "sizeMB": round((x.get("size") or 0) / 1048576.0, 1) if x.get("size") else None,
        "durationSec": x["durationSec"],
        "quality": x["quality"],
        "filePath": x["file"],
        "localKind": "video" if x["exists"] else None,
        "fav": False,
        "coverPath": cover_url(x["photoId"]),
        "platform": "kuaishou",
    } for x in page]}


_KS_COVER_DIR = os.path.join(HERE, ".data", "ks-covers")
_KS_COVER_LOCK = threading.Lock()
_KS_COVER = {"running": False, "done": 0, "total": 0, "failed": 0}


def ks_cover_file(pid):
    """封面文件路径（不一定存在）。"""
    return os.path.join(_KS_COVER_DIR, "%s.jpg" % pid)


def _ffmpeg_exe():
    ff = shutil.which("ffmpeg")
    if ff:
        return ff
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def ks_cover_generate(pid, video):
    """从本地视频抽一帧生成封面（已存在则跳过）。返回 True=封面就绪。"""
    out = ks_cover_file(pid)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        return True
    if not video or not os.path.exists(video):
        return False
    ff = _ffmpeg_exe()
    if not ff:
        return False
    os.makedirs(_KS_COVER_DIR, exist_ok=True)
    tmp = out + ".tmp.jpg"
    try:
        # -ss 放 -i 前 = 快速 seek；1s 处画面通常已稳定；宽度压到 480 省磁盘
        subprocess.run(
            [ff, "-y", "-loglevel", "error", "-ss", "1", "-i", video,
             "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", tmp],
            timeout=30, check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        os.replace(tmp, out)
        return True
    except Exception:  # noqa: BLE001 - 单个失败不影响其余（损坏视频/编码不支持）
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def ks_covers_ensure(pids):
    """后台线程批量补齐缺失封面。pids 为 photoId 列表（只处理有文件的）。"""
    with _KS_COVER_LOCK:
        if _KS_COVER["running"]:
            return {"ok": True, "running": True, **_KS_COVER}
        # 只收「有本地文件且还没封面」的；无文件的计入 no_file
        # （前端据此区分「待生成」与「文件已丢失」—— 后者给如实说明条）
        todo, no_file = [], 0
        for pid in pids:
            pid = str(pid or "").strip()
            if not pid or os.path.exists(ks_cover_file(pid)):
                continue
            ent = ks_index()["map"].get(pid)
            if ent and ent.get("exists"):
                todo.append((pid, ent["file"]))
            else:
                no_file += 1
        if not todo:
            # 没有需要生成的：**保留上一轮的进度数字** —— 前端轮询靠
            # done>=total 判断「生成完了」，归零会让进度条永远卡住
            # （2026-11-08 实测：87 个全部生成后，前端轮询把计数打回 0/0）。
            return {"ok": True, "running": False, "no_file": no_file, **_KS_COVER}
        _KS_COVER.update(running=True, done=0, total=len(todo), failed=0)

    def worker():
        for pid, video in todo:
            ok = ks_cover_generate(pid, video)
            with _KS_COVER_LOCK:
                _KS_COVER["done"] += 1
                if not ok:
                    _KS_COVER["failed"] += 1
        with _KS_COVER_LOCK:
            _KS_COVER["running"] = False

    threading.Thread(target=worker, daemon=True,
                     name="ks-covers").start()
    return {"ok": True, "running": True, **_KS_COVER}


async def h_ks_covers_ensure(req):
    """POST {pids: [...]} —— 触发后台封面生成（幂等，已在跑则追加无效）。"""
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        return j({"error": "请求体必须是 JSON"}, 400)
    pids = body.get("pids") if isinstance(body, dict) else None
    if not isinstance(pids, list):
        return j({"error": "pids 必须是数组"}, 400)
    return j(ks_covers_ensure(pids))


async def h_ks_covers_status(req):
    """封面生成进度（前端轮询用）。"""
    with _KS_COVER_LOCK:
        return j({"ok": True, **_KS_COVER})


async def h_ks_cover_file(req):
    """GET /api/bridge/ks/covers/<photoId>.jpg —— 读生成的封面图。"""
    pid = str(req.match_info.get("pid", "")).strip()
    # 路由 {pid} 会把「.jpg」一起捕进来 —— 剥掉扩展名再校验
    if pid.lower().endswith(".jpg"):
        pid = pid[:-4]
    # 防目录穿越：只允许字母数字
    if not pid or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pid):
        return j({"error": "非法 id"}, 400)
    fp = ks_cover_file(pid)
    if not os.path.exists(fp):
        return j({"error": "封面未生成"}, 404)
    return web.FileResponse(fp, headers={"Cache-Control": "max-age=86400"})


# ============================================================ 重复视频扫描（抖音 + 快手）
# 「查找重复视频」：跨两个平台的本地视频库，找出**字节完全相同**的重复文件，
# 每组保留一个、其余移入回收站（可恢复）。判定用 SHA-256 内容哈希，不做任何
# 基于标题/时长的模糊匹配 —— 那会把「同名不同内容」或「直播分段」误判成重复。
#
# 设计要点：
#   · 只扫**视频**（MEDIA_VIDEO_EXT），不碰图集图片；
#   · 大小预筛：字节相同必先大小相同，大小唯一的文件**不用哈希**，直接跳过
#     （7 万多个文件里真正需要哈希的只有「同大小的组」，通常个位数）；
#   · 保留规则：优先保留「非辅助目录」下的文件，组内再比大小（大者优先）、
#     时间（新者优先）、路径（字典序兜底，保证结果确定）；
#   · 删除只进 Windows 回收站，不永久删除；路径越界/不存在一律拒绝。
_DEDUP_HELPER_DIRS = {"edge-manual-profile", "login-profile"}
_DEDUP = {"running": False, "progress": {"phase": "idle", "done": 0, "total": 0},
          "result": None, "error": None, "at": 0.0}
_DEDUP_LOCK = threading.Lock()


def _dedup_roots():
    """抖音 + 快手全部视频根，返回 {realpath: platform}。

    不直接复用 _media_roots()：它把工具箱根下的 downloads/ 整目录当成抖音根，
    会连 downloads/kuaishou/ 一起卷进来、还标错平台。这里各自精确到平台。
    """
    roots = {}

    def add(p, plat):
        try:
            if p and os.path.isdir(p):
                rp = os.path.realpath(p)
                roots.setdefault(rp, plat)
        except Exception:
            pass

    # 抖音：config 的 path（权威产物目录）+ 历史基准 downloads/douyin
    add(_douyin_cfg_dir(), "douyin")
    add(os.path.join(TOOLBOX_ROOT, "downloads", "douyin"), "douyin")
    # 快手：台账声明的全部根（含工具落点、额外根、历史根）
    for r in ks_ledger_roots():
        add(r, "kuaishou")
    return roots


def _dedup_helper_path(path):
    """落在「辅助/临时」目录里的文件（快手扫码登录的 edge-manual-profile 等）。"""
    low = [seg.lower() for seg in path.replace("/", os.sep).split(os.sep)]
    return bool(_DEDUP_HELPER_DIRS.intersection(low))


def _file_sha256(path):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _dedup_entry(fp, size, mtime, platform):
    return {
        "path": fp,
        "name": os.path.basename(fp),
        "size": size,
        "mtime": mtime,
        "platform": platform,
    }


def _dedup_choose_keep(entries):
    """从一组字节相同的文件里挑「保留方」。entries: [(fp, size, mtime, platform)]。"""
    primary = [e for e in entries if not _dedup_helper_path(e[0])]
    cands = primary or entries
    # 大者优先 → 新者优先 → 字典序兜底（结果确定，不随 os.walk 顺序变）
    cands.sort(key=lambda e: (-e[1], -e[2], e[0]))
    keep = cands[0]
    return keep, [e for e in entries if e[0] != keep[0]]


def _dedup_scan(progress=None):
    """扫两个平台的视频库，返回重复组。progress(phase, done, total) 可选。"""
    roots = _dedup_roots()
    # 1) 收集视频文件
    entries = []          # [(fp, size, mtime, platform)]
    for root, plat in roots.items():
        base = root.rstrip("\\/").count(os.sep)
        for dp, dns, fns in os.walk(root):
            if dp.count(os.sep) - base >= MEDIA_MAX_DEPTH:
                dns[:] = []
            else:
                dns[:] = [d for d in dns
                          if d.lower() not in MEDIA_SKIP_DIRS
                          and not d.startswith((".", "$"))]
            for fn in fns:
                if os.path.splitext(fn)[1].lower() not in MEDIA_VIDEO_EXT:
                    continue
                fp = os.path.join(dp, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                entries.append((fp, st.st_size, st.st_mtime, plat))
    total = len(entries)
    if progress:
        progress("walk", total, total)

    # 2) 大小预筛 + 哈希
    bysize = {}
    for e in entries:
        bysize.setdefault(e[1], []).append(e)
    dup_groups = []       # 每组 = [(fp, size, mtime, platform), ...]
    hashed = 0
    for size, group in bysize.items():
        if len(group) < 2:
            continue          # 大小唯一 → 不可能与任何文件字节相同
        byhash = {}
        for e in group:
            digest = _file_sha256(e[0])
            hashed += 1
            if digest:
                byhash.setdefault(digest, []).append(e)
            if progress:
                progress("hash", hashed, total)
        for hg in byhash.values():
            if len(hg) >= 2:
                dup_groups.append(hg)

    # 3) 每组定保留方 / 冗余方
    groups = []
    reclaim = 0
    for hg in dup_groups:
        keep, removes = _dedup_choose_keep(hg)
        groups.append({
            "keep": _dedup_entry(*keep),
            "dupes": [_dedup_entry(*e) for e in removes],
        })
        reclaim += sum(e[1] for e in removes)
    return {
        "scanned": total,
        "hashed": hashed,
        "roots": list(roots.keys()),
        "groups": groups,
        "dup_count": sum(len(g["dupes"]) for g in groups),
        "group_count": len(groups),
        "reclaim_bytes": reclaim,
    }


def _dedup_run_async():
    def _progress(phase, done, total):
        with _DEDUP_LOCK:
            _DEDUP["progress"] = {"phase": phase, "done": done, "total": total}

    try:
        result = _dedup_scan(_progress)
        with _DEDUP_LOCK:
            _DEDUP["result"] = result
            _DEDUP["error"] = None
            _DEDUP["at"] = time.time()
    except Exception as exc:                      # 扫描失败绝不能让页面崩
        with _DEDUP_LOCK:
            _DEDUP["error"] = str(exc)
    finally:
        with _DEDUP_LOCK:
            _DEDUP["running"] = False


def start_dedup_scan():
    """启动后台扫描。已在跑就直接返回 False，不叠一个并发扫描。"""
    with _DEDUP_LOCK:
        if _DEDUP["running"]:
            return False
        _DEDUP["running"] = True
        _DEDUP["progress"] = {"phase": "walk", "done": 0, "total": 0}
        _DEDUP["result"] = None
        _DEDUP["error"] = None
    threading.Thread(target=_dedup_run_async, daemon=True).start()
    return True


def _is_within(path, root):
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) \
            == os.path.realpath(root)
    except ValueError:
        return False      # 跨盘符会抛 ValueError，视为不在根下


def _recycle_files(paths):
    """把文件移入 Windows 回收站（可恢复）。返回 {path: True 或错误文案}。

    用 PowerShell 的 Microsoft.VisualBasic FileSystem.DeleteFile(..., SendToRecycleBin)，
    不新增第三方依赖。走 -EncodedCommand（UTF-16LE + base64），中文路径、引号、换行
    都不会在命令行传参里出问题。回报用**下标**而不是回显路径 —— PowerShell stdout
    按控制台代码页（GBK）输出，回显中文路径会乱码；下标映射回 Python 侧的正确路径。
    逐条回报，单个失败不拖累其余。
    """
    if os.name != "nt":
        return {p: "非 Windows 环境，未执行" for p in paths}
    results = {}
    chunk = 60
    for i in range(0, len(paths), chunk):
        part = paths[i:i + chunk]
        script = (
            "Add-Type -AssemblyName Microsoft.VisualBasic\n"
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n"
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "$paths = @(\n"
            + "\n".join("'" + p.replace("'", "''") + "'" for p in part)
            + "\n)\n"
            "for ($i = 0; $i -lt $paths.Count; $i++) {\n"
            "  $p = $paths[$i]\n"
            "  if (-not (Test-Path -LiteralPath $p)) { Write-Output ('MISS ' + $i); continue }\n"
            "  try {\n"
            "    [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($p, 'OnlyErrorDialogs', 'SendToRecycleBin')\n"
            "    Write-Output ('OK ' + $i)\n"
            "  } catch { Write-Output ('ERR ' + $i + ' :: ' + $_.Exception.Message) }\n"
            "}\n"
        )
        enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                capture_output=True, timeout=600, creationflags=NO_WINDOW,
            )
            out = proc.stdout.decode("utf-8", "replace")
        except Exception as exc:
            for p in part:
                results[p] = "回收失败: %s" % exc
            continue
        for line in out.splitlines():
            line = line.strip()
            m = re.match(r"^(OK|MISS|ERR) (\d+)(?: :: (.*))?$", line)
            if not m:
                continue
            status, idx, msg = m.group(1), int(m.group(2)), m.group(3)
            if not (0 <= idx < len(part)):
                continue
            p = part[idx]
            if status == "OK":
                results[p] = True
            elif status == "MISS":
                results[p] = "文件已不存在"
            else:
                results[p] = msg or "回收失败"
    for p in paths:
        results.setdefault(p, "回收失败（无结果回报）")
    return results


async def h_dedup_scan(req):
    """启动重复视频扫描（后台线程，进度走 /status 轮询）。"""
    started = start_dedup_scan()
    return j({"ok": True, "started": started}, status=200 if started else 409)


async def h_dedup_status(req):
    with _DEDUP_LOCK:
        return j({
            "running": _DEDUP["running"],
            "progress": _DEDUP["progress"],
            "result": _DEDUP["result"],
            "error": _DEDUP["error"],
            "at": _DEDUP["at"],
        })


async def h_dedup_delete(req):
    """把确认过的冗余文件移入回收站。只收显式路径，且必须落在已知根下。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    paths = (body or {}).get("paths") or []
    if not isinstance(paths, list) or not paths:
        return j({"error": "没有要删除的文件"}, status=400)
    roots = _dedup_roots()
    valid, rejected = [], []
    for p in paths:
        p = str(p)
        rp = os.path.realpath(p)
        if not os.path.isfile(rp):
            rejected.append({"path": p, "reason": "文件不存在"})
            continue
        if not any(_is_within(rp, r) for r in roots):
            rejected.append({"path": p, "reason": "路径越界"})
            continue
        valid.append(rp)
    if not valid:
        return j({"error": "没有可删除的文件（路径越界或不存在）",
                  "rejected": len(rejected)}, status=400)
    results = _recycle_files(valid)
    ok = sum(1 for v in results.values() if v is True)
    return j({
        "ok": True,
        "deleted": ok,
        "failed": len(valid) - ok,
        "rejected": rejected,
        "results": results,
    })


def _ks_profile_id(value):
    """从「主页 URL」或「作者 ID」里取出作者 ID。"""
    url, pid, _ = _ks_profile_entry(value)
    return pid


_KS_HOST_RE = re.compile(r"^https?://([^/]+)", re.I)
# 快手作者主页 ID 的形态：3x + 一串 base36（实测 3xp3pjmyvy4u2j2 / 3xa6yyk8i6pgsz9 …）
_KS_PROFILE_ID_RE = re.compile(r"^3x[0-9a-z]{6,}$", re.I)


def _ks_profile_entry(value):
    """把用户贴进来的东西归一成「可存的主页地址」。返回 `(url, pid, err)`。

    三种输入都要能处理 —— 用户手上可能是任一种：

    1. **主页链接** `https://www.kuaishou.com/profile/3xp3pjmyvy4u2j2`
       → 归一成规范地址，取出 `pid`。
    2. **裸作者 ID** `3xp3pjmyvy4u2j2` → 拼成规范地址。
    3. **分享短链** `https://v.kuaishou.com/KqKU900L` → **原样保留**，`pid` 为 None。

    ⚠️⚠️ 第 3 条是踩出来的（2026-09-29，同一轮里第二个 bug）：
       短链末段 `KqKU900L` 看着像 ID，其实是**分享码**。原实现把它当成 ID，
       拼成 `www.kuaishou.com/profile/KqKU900L` 存了下来 —— 保存时还提示
       「已保存，现在可以点更新了」，然后「更新」拿到一个不存在的主页，
       一个作品都抓不到。用户看到的就是「快手下载不了」。
       更糟的是界面上那句「打开作者主页 → 分享 → 复制链接」，正好把用户
       往这条路上引（分享给的就是短链）。
       而短链本身**是能直接下载的** —— `kuaishou_headless.py` 会自己跟随跳转
       （本次故障里它正是靠短链解析出「就这样飞 / 3xa6yyk8i6pgsz9」的），
       所以正解是**原样存**，而不是把它"修"成一个假 ID。
    """
    s = str(value or "").strip().strip('"\'')
    if not s:
        return None, None, "链接不能为空"

    # ① 没带协议：当成裸作者 ID
    if not re.match(r"^https?://", s, re.I):
        pid = re.sub(r"[^0-9A-Za-z]", "", s)
        if not _KS_PROFILE_ID_RE.match(pid or ""):
            return None, None, ("这既不是快手主页链接，也不是作者 ID（形如 3x…）。"
                                "请粘贴地址栏里的 https://www.kuaishou.com/profile/3x… ，"
                                "或直接粘贴分享链接 https://v.kuaishou.com/…")
        return "https://www.kuaishou.com/profile/" + pid, pid, None

    m = _KS_HOST_RE.match(s)
    host = (m.group(1) if m else "").lower().split(":")[0]
    if not (host == "kuaishou.com" or host.endswith(".kuaishou.com")):
        return None, None, "只认快手域名（kuaishou.com）的链接"

    path = re.sub(r"[?#].*$", "", s).rstrip("/")
    hit = re.search(r"/profile/([0-9A-Za-z]+)", path)
    if hit:
        pid = hit.group(1)
        return "https://www.kuaishou.com/profile/" + pid, pid, None

    if host.startswith("v."):
        return s, None, None      # 分享短链：原样存，让下载器自己跟随跳转

    return None, None, ("这段链接里没有 /profile/<作者ID>。"
                        "请打开作者主页、复制地址栏的网址；"
                        "或者直接把分享链接（v.kuaishou.com/… ）贴进来。")


def _ks_profile_map_file():
    try:
        with open(KS_PROFILE_MAP, "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _ks_works_lists():
    """收集 (清单文件, 作者ID|None, 作品id 集合)。

    快手下载时会把作者主页的作品清单存成 `works_list_<作者ID>.json` ——
    文件名里那串就是**作者主页 ID**，这是台账里拿不到的信息（台账只记作品）。
    无后缀的 `works_list.json` 是没有作者 ID 时写的，跳过。

    ⚠️ 还要按 ID 形态过滤：同一个目录里躺着 `works_list_pending.json`、
    `works_list_batch.json`、`works_list_patient.json` 这类**工作清单**
    （深滚过程中的临时产物）。它们不是作者 ID，但旧逻辑照单全收 ——
    于是作者卡片上会出现一个 `https://www.kuaishou.com/profile/pending` 的
    "主页"，「更新」按钮点下去必然失败。主页 ID 的形态是 `3x` + base36
    （见 `_KS_PROFILE_ID_RE`），不合形态的一律不当作者认。
    """
    out = []
    for r in ks_ledger_roots():
        try:
            names = sorted(os.listdir(r))
        except OSError:
            continue
        for n in names:
            if not (n.startswith("works_list_") and n.endswith(".json")):
                continue
            fp = os.path.join(r, n)
            pid = os.path.splitext(n)[0][len("works_list_"):]
            if not _KS_PROFILE_ID_RE.match(pid or ""):
                continue          # pending / batch / final … 不是作者 ID
            try:
                with open(fp, "r", encoding="utf-8-sig") as f:
                    data = json.load(f)
                ids = {str(x.get("id") or "").strip() for x in data
                       if isinstance(x, dict)}
                ids.discard("")
            except Exception:
                continue
            if pid and ids:
                out.append((fp, pid, ids))
    return out


def _ks_infer_profiles(items):
    """用「作品 ID 交集」把作者目录名对上主页 ID。

    为什么用交集：台账里**没有**作者 ID，只有落盘目录名（=作者昵称）；
    而 `works_list_<ID>.json` 里只有作品 ID、没有昵称。两边唯一的共同语言是
    **作品 ID 本身** —— 实测 4 份清单与 4 位作者的交集是 477/477、258/267、
    157/161、233/247，对应关系干净可判。
    阈值取「≥3 条命中 且 覆盖该作者作品的 30%」，宁可少认也不认错：
    认错会让「更新」去更新别人的主页。
    """
    lists = _ks_works_lists()
    if not lists:
        return {}
    by_author = {}
    for it in items:
        a = it.get("author")
        if a:
            by_author.setdefault(a, set()).add(it["photoId"])
    out = {}
    for author, pids in by_author.items():
        best_n, best_pid = 0, None
        for _fp, pid, ids in lists:
            n = len(ids & pids)
            if n > best_n:
                best_n, best_pid = n, pid
        if best_pid and best_n >= 3 and best_n >= len(pids) * 0.3:
            out[author] = {"id": best_pid, "hits": best_n, "scanned": len(pids)}
    return out


def ks_authors_all():
    """快手作者聚合（**全部**作者，含未关注的），带主页链接与关注来源标记。

    `followed` 的判据与抖音侧同源：作者名下有没有**非单条链接**来的作品。
    快手台账里没有 url 类型，于是下载器在单条模式下往条目里写了
    `origin: "single"`（见 kuaishou_headless.download_single）；批量走的是
    fast_dl，条目不带 origin = 来源未知 = 按关注处理（老台账同样如此）。
    """
    items = [x for x in ks_index()["items"] if x.get("author")]
    manual = _ks_profile_map_file()
    auto = _ks_infer_profiles(items)
    groups = {}
    for it in items:
        g = groups.setdefault(it["author"], {
            "name": it["author"], "works": 0, "withFile": 0, "profileWorks": 0,
            "sizeBytes": 0, "lastTime": None, "root": it["root"]})
        g["works"] += 1
        if str(it.get("origin") or "") != "single":
            g["profileWorks"] += 1
        if it["exists"]:
            g["withFile"] += 1
            g["sizeBytes"] += it.get("size") or 0
            t = it.get("mtime")
            if t and (g["lastTime"] is None or t > g["lastTime"]):
                g["lastTime"] = t
    with FOLLOW_LOCK:
        state = _follow_load()
    out = []
    for name, g in groups.items():
        url, pid, src = "", None, None
        if manual.get(name):
            # 手动填的可能是分享短链（存原样、pid 为 None）—— 这时也有 url，
            # 「更新」照样能用（下载器会跟随跳转）。
            src = "manual"
            url, pid, _ = _ks_profile_entry(manual[name])
            url = url or ""
            if not url:
                src = None            # 映射里存了坏值 → 当没填，让用户重填
        elif name in auto:
            src, pid = "auto", auto[name]["id"]
            url = "https://www.kuaishou.com/profile/" + pid
        derived = g["profileWorks"] > 0
        entry = state["entries"].get(_follow_key("kuaishou", name))
        if entry is None:
            followed, why = derived, ("" if derived else "single")
        else:
            followed = entry.get("state") == "added"
            why = entry.get("why") or ("manual" if not followed else "")
        out.append({
            "id": "ks-" + name,
            "nickname": name,
            "works": g["works"],
            "withFile": g["withFile"],
            "sizeMB": round(g["sizeBytes"] / 1048576.0, 1),
            "lastTime": iso_ts(g["lastTime"]),
            "profileUrl": url,
            "profileId": pid,
            "profileSource": src,       # auto=从作品清单推断 / manual=手动填的
            "root": g["root"],
            "platform": "kuaishou",
            "followed": followed,
            "why": why,
        })
    out.sort(key=lambda x: -x["works"])
    return out


def ks_authors():
    """「关注」列表（快手）：与抖音侧同一套规则，单条链接来的作者不算关注。"""
    return [a for a in ks_authors_all() if a["followed"]]


def ks_unfollowed_authors():
    """未计入关注、但台账里确实有的快手作者。"""
    return [a for a in ks_authors_all() if not a["followed"]]


def ks_stats():
    """快手库总览。有 file 的才算「作品」，其余如实单列。"""
    items = ks_index()["items"]
    with_file = [x for x in items if x["exists"]]
    roots = ks_index()["roots"]
    ledgers = _ks_ledger_files()
    return {
        "works": len(with_file),
        "records": len(items),
        "authors": len({x["author"] for x in items if x.get("author")}),
        "sizeBytes": sum(x.get("size") or 0 for x in with_file),
        "lastTime": iso_ts(max((x.get("mtime") or 0 for x in with_file), default=0)),
        "roots": roots,
        "ledgers": [p for _r, p in ledgers],
    }


def ks_delete_author(author):
    """从台账里移除某位作者的全部记录（**唯一**的快手写操作）。

    范围与「作者列表里显示的那一组」完全一致：`file` 的落盘目录名 == author。
    磁盘上的视频文件一个不删（与抖音侧同一套语义，用户已确认过）。
    写之前先按时间戳整份备份台账 —— JSON 没有事务，写坏了只能靠备份回来。
    """
    author = str(author or "").strip()
    if not author:
        return {"error": "作者名不能为空"}, 400
    targets = [(root, p) for root, p in _ks_ledger_files()]
    if not targets:
        return {"error": "未找到快手台账文件"}, 404
    backed, removed, kept, per_root = [], 0, 0, {}
    for root, path in targets:
        data = _ks_read_ledger(path)
        keep, gone = [], 0
        for r in data:
            it = _ks_item(r, root)
            if it is not None and it["author"] == author:
                gone += 1          # 命中作者 → 丢弃这条
            else:
                keep.append(r)     # 其它作者 / 无 photoId 的审计行一律原样保留
        if not gone:
            continue
        bak = _ks_backup_ledger(root, path)
        if bak is None:
            return {"error": "备份失败，已中止删除（未改动任何台账）", "file": path}, 500
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(keep, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as e:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return {"error": "写入台账失败: %s（原文件未动）" % e}, 500
        backed.append(bak)
        removed += gone
        kept += len(keep)
        per_root[root] = gone
    ks_index(force=True)
    if not removed:
        return {"error": "库里没有这位作者：%s" % author}, 404
    _follow_forget("kuaishou", author)     # 同抖音侧：别留孤儿关注覆盖
    return {"ok": True, "author": author, "deleted": removed,
            "worksLeft": kept, "backup": backed, "perRoot": per_root}, 200


def _ks_backup_ledger(root, path):
    """整份复制台账到 .tasks/ks-backups/，保留最近 N 份。返回备份路径或 None。"""
    try:
        os.makedirs(KS_BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        tag = re.sub(r"[^\w\-]+", "_", os.path.basename(root))[:24] or "ks"
        dst = os.path.join(KS_BACKUP_DIR, "%s-%s-%s.json" % (stamp, tag, "download_log"))
        n = 1
        while os.path.exists(dst):
            dst = os.path.join(KS_BACKUP_DIR,
                               "%s-%s-%s-%d.json" % (stamp, tag, "download_log", n))
            n += 1
        shutil.copy2(path, dst)
        olds = sorted((os.path.join(KS_BACKUP_DIR, f) for f in os.listdir(KS_BACKUP_DIR)
                       if f.endswith(".json")), key=os.path.getmtime, reverse=True)
        for f in olds[KS_KEEP_BACKUPS:]:
            try:
                os.remove(f)
            except OSError:
                pass
        return dst
    except Exception as e:
        print("[ks] 台账备份失败: %s" % e, flush=True)
        return None


def lr_save_dir():
    """录像根目录：优先读 config.json 的 settings.save_path，回落 ~/LiveRecorder。"""
    try:
        with open(LR_CONFIG, "r", encoding="utf-8") as f:
            path = (json.load(f).get("settings") or {}).get("save_path")
        if path:
            return os.path.expandvars(os.path.expanduser(str(path)))
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "LiveRecorder")


def lr_detect_platform(url):
    """本地识别直播平台（host 关键字，与 live_recorder/platforms/registry.py 同口径）。

    只做域名判断，不联网、不解析短链 —— 短链（v.douyin.com / v.kuaishou.com）是
    视频还是直播无法从 URL 区分，交给录制器首次轮询时解析；这里仅识别明确的直播域名。"""
    from urllib.parse import urlparse
    try:
        host = (urlparse(url or "").netloc or "").lower()
    except Exception:
        return None
    for platform, hints in (
        # chenzhongtech.com 是快手 CDN/移动端域：分享短链的落点常在这里
        # （v.m.chenzhongtech.com/fw/live/…），不认它直播落点会被判成未知平台
        ("kuaishou", ("kuaishou.com", "gifshow.com", "chenzhongtech.com")),
        ("douyin", ("douyin.com", "iesdouyin.com", "amemv.com")),
    ):
        if any(h in host for h in hints):
            return platform
    return None


def lr_running():
    """检测桌面 LiveRecorder 是否已有实例在运行（按进程名 + 命令行匹配 main.py）。

    录制器由工具箱/桥接服务以绝对路径
    ``.../modules/live_recorder/main.py`` 经 pythonw.exe/python.exe 启动，
    故命令行必含 ``live_recorder`` 与 ``main.py`` 两段；这里额外限定进程名是
    python/pythonw，避免把执行本查询的 powershell 自身（其命令行里也含这两个字面量）
    误判成录制器实例。任何异常一律按「未运行」处理。"""
    try:
        import subprocess
        cmd = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and "
            "$_.CommandLine -like '*live_recorder*main.py*' } | "
            "Measure-Object | Select-Object -ExpandProperty Count"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, timeout=10,
            # 启动台页与直播页每次进入都会走到这里（q_launcher / q_live 各一次），
            # 漏了标志就是"每切一次页闪一个黑窗"。
            creationflags=NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "ignore").strip()
        return int(text or "0") > 0
    except Exception:
        return False


def q_live():
    """直播录制模块状态总览（只读，绝不写文件、不发网络请求）。

    Cookie 只报「有没有」，绝不回传内容（脱敏，与 /api/bridge/env 同口径）。"""
    module_ok = os.path.exists(LR_MAIN)
    pyw = lr_python("pythonw")
    py = lr_python("python")
    runtime_ok = bool(pyw or py)

    rooms = []
    login = {"kuaishou": False, "douyin": False}
    cfg_ok = False
    try:
        with open(LR_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        settings = data.get("settings") or {}
        cfg_ok = True
        login = {
            "kuaishou": bool((settings.get("kuaishou_cookie") or "").strip()),
            "douyin": bool((settings.get("douyin_cookie") or "").strip()),
        }
        for r in (data.get("rooms") or []):
            rooms.append({
                "platform": r.get("platform"),
                "room_id": r.get("room_id") or "",
                "nickname": r.get("nickname") or "",
                "enabled": bool(r.get("enabled")),
                "remark": r.get("remark") or "",
            })
    except Exception:
        cfg_ok = False

    save_dir = lr_save_dir()
    recordings = _dir_usage(save_dir)
    # _logs 是 LiveRecorder 自己的日志目录，不是录像产物，从「录像占用」里剔除，
    # 避免把几 MB 日志混进「共 XX GB 录像」的统计口径里。
    if recordings.get("groups"):
        log_bytes = sum(g.get("bytes", 0) for g in recordings["groups"]
                        if g.get("name") == "_logs")
        recordings["groups"] = [g for g in recordings["groups"] if g.get("name") != "_logs"]
        recordings["total"] = max(0, recordings.get("total", 0) - log_bytes)
    return {
        "available": module_ok and runtime_ok,
        "module_ok": module_ok,
        "runtime_ok": runtime_ok,
        "config_ok": cfg_ok,
        "running": lr_running(),
        "save_dir": save_dir,
        "save_exists": os.path.isdir(save_dir),
        "login": login,
        "rooms": rooms,
        "recordings": recordings,
    }


async def h_live(req):
    return j(q_live())


# ---------- 统一启动台（五个 .bat 入口的共享后端） ----------
# 设计意图：五个 .bat 原本各写各的（一个黑窗 CLI、一个无声 start、一个 set /p 中文提示），
# 现在统一成「薄壳 + 同一套网页界面」。本区块只做三件事：
#   1. 报告每个工具的模块/运行时/产物目录是否就绪（只读，绝不写文件）；
#   2. 按 kind 唤起对应的桌面 GUI（快手一键下载器 / 直播录制器）；
#   3. 打开各自产物目录（不存在就如实报 404，不悄悄创建）。
# 真正干活的下载/录制能力全部复用已有端点，这里不重复实现。
#
# 为什么产物目录要分工具报告而不是一个「下载目录」：四个工具的落点根本不同
# （抖音 downloads\douyin、快手 data\kuaishou、直链 downloads\direct、
# 录像 config.json 里可改的 save_path）。合成一个路径必然对不上真正的产物。

# 各工具产物目录（与各自模块的真实约定一致，不要「统一」成同一个）
TOOL_DIRS = {}

# ---------- 产物目录：用户可自定义 ----------
# ⚠️⚠️ **「自定义」必须写进各工具自己的权威配置，不能只存在网页这边。**
# 否则前端改了、真正在干活的那段代码根本不认 —— 改了个寂寞。各自权威：
#   抖音   → modules/douyin/config.yml 的 `path`（cli/main.py 就是 config.get("path")）
#   直播   → modules/live_recorder/config.json 的 settings.save_path（录制器/内核读它）
#   快手   → 原本写死 data\kuaishou，没有可配落点 → 新建本文件 + 下发 KS_OUTDIR 环境变量
#   直链   → 原本由桥接拼死 downloads\direct 的 -o 参数 → 同样存本文件
OUT_DIRS_FILE = os.path.join(TOOLBOX_ROOT, "data", "output_dirs.json")

# 只有这两个工具落在本文件；抖音与直播各自有权威配置，别在这里再存一份
# （存了就会出现「两份真值」，改了一处另一处不跟着变）。
_OUT_DIRS_OWNED = ("kuaishou", "direct", "stripchat", "ty", "ytdlp")


def _load_out_dirs():
    try:
        with open(OUT_DIRS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_out_dirs(d):
    os.makedirs(os.path.dirname(OUT_DIRS_FILE), exist_ok=True)
    tmp = OUT_DIRS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_DIRS_FILE)


def _douyin_cfg_dir():
    """抖音产物目录的权威在 config.yml 的 `path`（run.py 读的就是它）。

    ⚠️ 这里**必须读 config.yml** 而不是照旧回 `ROOT/downloads`：
    后者与实际落点对不上（当前 config.yml 是 `E:/录屏/zb1`），
    页面会显示一个「尚未产生产物」的空目录、把用户引到错的地方，
    「自定义」也就无从谈起（改完还是不生效）。
    """
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        p = (cfg or {}).get("path")
        if p:
            return os.path.expandvars(os.path.expanduser(str(p)))
    except Exception:
        pass
    return os.path.join(ROOT, "downloads")


def _set_douyin_cfg_dir(path):
    """只改 config.yml 顶层的 `path:` 一行，**不整份 yaml 重写**。

    ⚠️ `yaml.safe_dump` 会把 76 行配置全量重排（键序、引号风格都会变），
    而这份文件是用户的、GUI 版也会写它。最小 diff 才不会顺手改掉无关内容。
    """
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    out, hit = [], False
    for ln in lines:
        # 只认**顶层** path（行首无缩进），别误伤嵌套的同名键
        if not hit and re.match(r"^path\s*:", ln):
            out.append("path: %s" % path)
            hit = True
        else:
            out.append(ln)
    if not hit:
        out.insert(0, "path: %s" % path)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, CONFIG_PATH)


def _init_tool_dirs():
    """延迟构造：TOOLBOX_ROOT 等常量在本文件靠后位置定义，模块导入期还不能读。"""
    own = _load_out_dirs()
    return {
        "douyin": _douyin_cfg_dir(),
        "kuaishou": own.get("kuaishou") or os.path.join(TOOLBOX_ROOT, "data", "kuaishou"),
        "bilibili": own.get("bilibili") or os.path.join(TOOLBOX_ROOT, "data", "bilibili"),
        "direct": own.get("direct") or os.path.join(TOOLBOX_ROOT, "downloads", "direct"),
        "ty": own.get("ty") or os.path.join(TOOLBOX_ROOT, "downloads", "ty"),
        "ytdlp": own.get("ytdlp") or os.path.join(TOOLBOX_ROOT, "downloads", "ytdlp"),
        "stripchat": own.get("stripchat") or os.path.join(TOOLBOX_ROOT, "downloads", "stripchat"),
        "live": lr_save_dir(),
    }


def _default_dir(kind):
    """某工具没自定义时的落点（供「恢复默认」与前端显示来源用）。"""
    if kind == "douyin":
        return os.path.join(ROOT, "downloads")
    if kind == "kuaishou":
        return os.path.join(TOOLBOX_ROOT, "data", "kuaishou")
    if kind == "bilibili":
        return os.path.join(TOOLBOX_ROOT, "data", "bilibili")
    if kind == "direct":
        return os.path.join(TOOLBOX_ROOT, "downloads", "direct")
    if kind == "ty":
        return os.path.join(TOOLBOX_ROOT, "downloads", "ty")
    if kind == "ytdlp":
        return os.path.join(TOOLBOX_ROOT, "downloads", "ytdlp")
    if kind == "stripchat":
        return os.path.join(TOOLBOX_ROOT, "downloads", "stripchat")
    if kind == "live":
        return os.path.join(os.path.expanduser("~"), "LiveRecorder")
    return ""


def set_out_dir(kind, raw, create=True):
    """把产物目录写进对应工具的权威配置，返回实际生效的绝对路径。

    ⚠️ 写之前会**真去建目录并做一次写探针** —— 把路径存进配置却下载不进去，
    比当场拒绝更糟（用户要等下一次下载失败才知道）。
    """
    path = os.path.expandvars(os.path.expanduser(str(raw or "").strip()))
    if not path:
        return None, "路径不能为空"
    if os.path.isfile(path):
        return None, "这是一个文件，不是目录：%s" % path
    if create:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".write_probe")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("ok")
            os.remove(probe)
        except Exception as e:
            return None, "目录不可写：%s" % e
    # ⚠️ 只有相对路径才 abspath。绝对路径**原样保留用户写法**
    # （`E:/录屏/zb1` 别被改成 `E:\录屏\zb1`）—— 写进 config.yml 却顺手改了
    # 用户自己填的字符串，属于没必要的惊吓；两者 Windows 都认。
    if not os.path.isabs(path):
        path = os.path.abspath(path)

    if kind == "douyin":
        if not os.path.exists(CONFIG_PATH):
            return None, "未找到抖音配置：%s" % CONFIG_PATH
        _set_douyin_cfg_dir(path)
    elif kind in _OUT_DIRS_OWNED:
        d = _load_out_dirs()
        d[kind] = path
        _save_out_dirs(d)
    else:
        return None, "未知的目录 key：%s" % kind
    return path, None


def q_launcher():
    """启动台总览：五个工具各自就绪态（只读，零副作用）。"""
    dirs = _init_tool_dirs()

    def dir_state(kind, path):
        """目录状态：区分「存在」与「还没有产物」。

        不在这里 makedirs —— 体检接口不该有副作用（与 /api/bridge/env 的磁盘探活同口径）。
        `source` 标明这一路径是各工具的出厂默认还是用户改过，前端据此决定要不要
        给「恢复默认」—— 不能在不知道来源时就显示一个可能把人带到别处的按钮。
        """
        try:
            exists = os.path.isdir(path)
            return {"path": path, "exists": exists,
                    "source": "custom" if os.path.abspath(path) != os.path.abspath(_default_dir(kind) or path)
                              else "default"}
        except Exception as e:
            return {"path": path, "exists": False, "source": "default", "error": str(e)}

    ks_ok = bool(ks_python("python") or ks_python("pythonw"))
    lr_ok = bool(lr_python("python") or lr_python("pythonw"))
    tools = {
        "toolbox": {
            "name": "工具箱主界面",
            "sub": "五个模块的统一入口",
            "ready": os.path.exists(os.path.join(TOOLBOX_ROOT, "toolbox.py")),
            "hint": "tkinter 原生窗口，与 Web 工作台同一套设计令牌",
        },
        "douyin": {
            "name": "抖音下载器",
            "sub": "视频 · 图集 · 主页 · 合集",
            "ready": os.path.exists(RUN_PY) and os.path.exists(CONFIG_PATH),
            "hint": "链接在网页里收，后台任务队列执行，进度与日志在页面内看",
        },
        "kuaishou": {
            "name": "快手一键下载器",
            "sub": "主页批量 · 扫码登录",
            "ready": ks_ok and os.path.exists(KS_PYW),
            "hint": "图形窗口独立运行；网页侧可直接后台下载，无需开窗",
        },
        "live": {
            "name": "直播流录制工具",
            "sub": "开播自动录制 · 分卷 · 无损合并",
            "ready": lr_ok and os.path.exists(LR_MAIN),
            "hint": "独立的桌面录制内核，本页为状态总览与操作入口",
        },
        "direct": {
            "name": "程序下载",
            "sub": "直链 · 断点续传 · SHA-256 校验",
            "ready": os.path.exists(CLAW_PY),
            "hint": "纯标准库直链下载器，支持中转页解析",
        },
        "ty": {
            "name": "TikTok / YouTube 下载",
            "sub": "粘链接即下 · 自动走代理",
            "ready": ks_ok and os.path.exists(TY_HEADLESS),
            "hint": "yt-dlp 原生提取器；境内网络自动探测本地代理（Clash 等），"
                    "探测不到会明确提示先开代理",
        },
        "stripchat": {
            "name": "Stripchat 直播录制",
            "sub": "含 VR · 白标镜像 · mouflon 解密",
            "ready": os.path.exists(SC_RECORDER),
            "hint": "纯标准库后台录制内核；一次录一个房间，停止时自动合并",
        },
        "ytdlp": {
            "name": "yt-dlp 万能下载",
            "sub": "全站通吃 · 音频提取 · 字幕 · 批量",
            "ready": bool(ks_ok) and os.path.exists(YTDLP_HEADLESS),
            "hint": "yt-dlp 官方 ~1754 个站点提取器：Twitter/X、Instagram、微博、Vimeo、"
                    "Twitch 回放等专属模块不覆盖的站都走这里；支持音频提取、字幕、"
                    "Cookie 登录与存档去重",
        },
    }
    return {
        "available": True,
        "root": TOOLBOX_ROOT,
        "tools": tools,
        "dirs": {k: dir_state(k, v) for k, v in dirs.items()},
        # ⚠️ 这里不能再用 lr_running()：它检测的是 2026-11-07 已退役的桌面
        # PyQt 录制器（live_recorder/main.py 进程），那个进程从此不会存在，
        # 总览页就会永远显示「录制器未运行」—— 而直播页（查 daemon）显示
        # 正在录制，两边自相矛盾。录制现在由无界面内核（daemon.py，8519
        # 端口）承担，这里按端口探活，与直播页同一事实源。
        "running": {"live": _daemon_port_busy()},
        "disk": q_env()["disk"],
    }


async def h_launcher(req):
    return j(q_launcher())


async def h_tool_launch(req):
    """唤起某个工具的桌面 GUI。当前只有快手一键下载器与直播录制器是 GUI 程序。

    抖音下载器与程序下载刻意不在这里 —— 它们原本是终端交互程序，
    统一后改由网页收链接、后台任务队列执行（见 /api/bridge/download 与
    /api/bridge/direct/download），不再弹黑窗。传 unknown kind 一律 400，
    不做「反正差不多就启一个」的兜底。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    kind = (body or {}).get("kind") if isinstance(body, dict) else None
    if not kind:
        return j({"error": "缺少 kind"}, status=400)

    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    if kind == "live":
        exe = lr_python("pythonw") or lr_python("python")
        if not exe:
            return j({"error": "直播录制运行时缺失：%s" % LR_RUNTIME}, status=404)
        if not os.path.exists(LR_MAIN):
            return j({"error": "直播录制模块缺失：%s" % LR_MAIN}, status=404)
        """已在运行时必须只切前台，绝不新起第二个进程。

        这里原先无条件 Popen —— 而网页上的「打开录制窗口」按钮走的正是本分支
        （不是 h_live_launch）。内核已在运行时再点一次就会开出第二个实例、
        并发录同一个房间，这正是本项目记录过的真实故障。所以直接把语义
        委托给 h_live_launch（它已经有 lr_running + focus 的正确逻辑），
        避免两处各写一遍而再次分叉。
        """
        return await h_live_launch(req)

    if kind == "kuaishou":
        exe = ks_python("pythonw") or ks_python("python")
        if not exe:
            return j({"error": "快手运行时缺失：%s" % KS_RUNTIME}, status=404)
        if not os.path.exists(KS_PYW):
            return j({"error": "快手一键下载器缺失：%s" % KS_PYW}, status=404)
        # 桌面 GUI 也认 KS_OUTDIR，保持与网页后台下载同一个落点
        env = dict(os.environ)
        ks_out = _init_tool_dirs().get("kuaishou")
        if ks_out:
            env["KS_OUTDIR"] = ks_out
            os.makedirs(ks_out, exist_ok=True)
        try:
            subprocess.Popen([exe, KS_PYW], cwd=KS_TOOLS, creationflags=flags, env=env)
            return j({"ok": True, "kind": kind,
                      "message": "快手一键下载器窗口已启动（若被安全软件拦截请放行）"})
        except Exception as e:
            return j({"error": "启动失败: %s" % e}, status=500)

    return j({"error": "该工具没有独立的桌面窗口，请在页面内直接使用"}, status=400)


async def h_tool_open_dir(req):
    """打开某个工具的产物目录。目录不存在如实报 404，绝不顺手创建。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    kind = (body or {}).get("kind") if isinstance(body, dict) else None
    dirs = _init_tool_dirs()
    if kind not in dirs:
        return j({"error": "未知的目录 key：%s" % kind}, status=400)
    target = dirs[kind]
    if not os.path.isdir(target):
        return j({"error": "目录还不存在（尚未产生下载产物）", "path": target}, status=404)
    try:
        if os.name == "nt":
            _explorer_open_front(target)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", target])
        return j({"ok": True, "path": target})
    except Exception as e:
        return j({"error": "打开失败: %s" % e}, status=500)


def _explorer_open_front(path, select=False):
    """用 explorer 打开目录并尽量把窗口拉到**最前面**。

    select=True 时定位到具体文件（explorer /select，用于作品「打开文件位置」）。
    桥接是无窗口后台进程：从它发起的 Explorer 窗口经常开在当前窗口的
    后面（被桌面/浏览器内容挡住）。开窗后按 Z 序找最新的 CabinetWClass
    （资源管理器）窗口，用「先按一下 Alt」的经典技巧解锁 SetForegroundWindow
    的前台锁，把窗口抢到前台。任何一步失败都静默 —— 窗口已经开出，
    只是层级没抢到，不该因此给用户报错。
    """
    import ctypes
    import ctypes.wintypes as wt
    import subprocess
    import time
    if select and os.path.isfile(path):
        subprocess.Popen('explorer /select,"%s"' % path)
    else:
        subprocess.Popen(["explorer", os.path.normpath(path)])
    try:
        time.sleep(0.7)          # explorer 开窗是异步的，等它建好窗口再抢
        user32 = ctypes.windll.user32
        hwnds = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd, _lparam):
            buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, buf, 64)
            if buf.value == "CabinetWClass":
                hwnds.append(hwnd)
            return True

        user32.EnumWindows(_cb, 0)
        if hwnds:
            # EnumWindows 按 Z 序返回（最顶层的在前），刚开出的窗口在最顶
            hwnd = hwnds[0]
            user32.keybd_event(0x12, 0, 0, 0)          # Alt down：解锁前台锁
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(0x12, 0, 2, 0)          # Alt up（KEYEVENTF_KEYUP）
            user32.ShowWindow(hwnd, 9)                 # SW_RESTORE：最小化时恢复
    except Exception:
        pass


async def h_open_path(req):
    """打开一个本机目录（下载历史「打开目录」按钮用）。

    与 h_tool_open_dir 的差别：路径由**后端自己从数据库查出来**发给前端、
    再原样传回来打开 —— 这里只接受「真实存在的目录」，不存在就 404，
    绝不创建、绝不打开文件。服务只绑 127.0.0.1，无远程面。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    path = str((body or {}).get("path") or "").strip()
    if not path or not os.path.isdir(path):
        return j({"error": "目录不存在：%s" % (path or "(空)")}, status=404)
    try:
        if os.name == "nt":
            _explorer_open_front(path)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return j({"ok": True, "path": path})
    except Exception as e:
        return j({"error": "打开失败: %s" % e}, status=500)


async def h_tool_set_dir(req):
    """把某个工具的产物目录改成用户指定的位置（写进该工具自己的权威配置）。

    ⚠️ **直播是特例**：录像目录归录制器自己管。内核（daemon）在线时必须走内核的
    settings 接口由它落盘 —— 直接改 config.json 会被内核下一次 save() 原样盖回；
    内核离线的情况下才直接写 config.json，但**录制器 GUI 正在跑就必须拒绝**
    （它退出时会全量 asdict 写盘，同样会把改动吃掉，且不报错）。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    kind = body.get("kind")
    dirs = _init_tool_dirs()
    if kind not in dirs:
        return j({"error": "未知的目录 key：%s" % kind}, status=400)
    # path 为空 = 恢复默认
    raw = (body.get("path") or "").strip()

    if kind == "live":
        if not raw:
            return j({"error": "录像目录不支持「恢复默认」，请直接指定一个目录"}, status=400)
        return await _set_live_dir(raw)

    if not raw:                      # 恢复默认
        if kind in _OUT_DIRS_OWNED:
            d = _load_out_dirs()
            d.pop(kind, None)
            _save_out_dirs(d)
        else:
            _set_douyin_cfg_dir(_default_dir(kind))
        path = _init_tool_dirs()[kind]
        return j({"ok": True, "kind": kind, "path": path, "source": "default",
                  "message": "已恢复默认位置"})

    path, err = set_out_dir(kind, raw)
    if err:
        return j({"error": err}, status=400)
    return j({"ok": True, "kind": kind, "path": path, "source": "custom",
              "message": "已保存，之后的下载会落到新位置"})


async def _set_live_dir(raw):
    """改录像根目录：内核在线走内核，离线才直接写 config.json。"""
    target = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if os.path.isfile(target):
        return j({"error": "这是一个文件，不是目录：%s" % target}, status=400)
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except Exception as e:
        return j({"error": "目录不可写：%s" % e}, status=400)

    proxy = await _lr_daemon_post("/settings", {"save_path": target})
    if proxy is not None:
        # 内核在线：由内核落盘（它持有 ConfigStore，别人改会被它盖回）
        if proxy.get("error") or proxy.get("code"):
            return j({"error": proxy.get("error") or "内核拒绝了该目录",
                      "rejected": proxy.get("rejected")}, status=400)
        eff = (proxy.get("applied") or {}).get("save_path") or target
        return j({"ok": True, "kind": "live", "path": eff, "source": "custom",
                  "via": "daemon",
                  "message": "已保存，下一次开始录制时生效"})

    # 内核离线：直接改 config.json。⚠️ 录制器 GUI 在跑就必须拒绝 ——
    # 它退出时 store.save() 会全量 asdict 覆盖回来，且不报任何错。
    if lr_running():
        return j({"error": "录制器正在运行，退出后才会保存配置；请先关闭录制器再改录像目录"},
                 status=409)
    if not os.path.exists(LR_CONFIG):
        return j({"error": "未找到录制器配置：%s" % LR_CONFIG}, status=404)
    try:
        with open(LR_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        st = cfg.get("settings")
        if not isinstance(st, dict):
            st = {}
            cfg["settings"] = st
        st["save_path"] = target
        tmp = LR_CONFIG + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LR_CONFIG)
    except Exception as e:
        return j({"error": "写入录制器配置失败: %s" % e}, status=500)
    # 写完立刻回读：config.json 可能被别人并发改，回读能暴露「写了没生效」
    return j({"ok": True, "kind": "live", "path": lr_save_dir(), "source": "custom",
              "via": "config", "message": "已保存，下一次开始录制时生效"})


async def _lr_daemon_post(path, body):
    """把请求转给录制内核。

    返回 ``None`` = 内核离线/连不上（调用方自行兜底直接改 config.json）；
    返回 dict = 内核的真实响应（含它拒绝时的 error/code，由调用方转成 400）。
    """
    try:
        status, payload = await _proxy(path, method="POST", body=body, timeout=10)
    except Exception:
        return None
    if status == 0 or not isinstance(payload, dict):
        return None
    if status >= 400:
        return {"error": payload.get("error") or ("内核返回 %s" % status),
                "rejected": payload.get("rejected"), "code": status}
    return payload


async def h_tool_pick_dir(req):
    """弹一个系统「选择文件夹」对话框，返回选中的路径。

    ⚠️ 对话框由**桥接服务进程**弹出。它是用户自己的会话里起的（.bat/.vbs 启动），
    所以能正常显示在桌面上；若服务跑在别的会话/被沙箱限制，这里会失败，
    前端据此提示用户直接输入路径 —— 不能让按钮转圈却不给任何反馈。
    """
    if os.name != "nt":
        return j({"error": "当前系统不支持弹出选择框，请直接输入路径"}, status=400)
    try:
        body = await req.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    start = (body.get("start") or "").strip()
    # ⚠️⚠️ **路径必须经临时文件 UTF-8 传回，不能读 PowerShell 的 stdout。**
    #     Windows PowerShell 5.1 的 stdout 走**控制台代码页**（本机 936/GBK），
    #     按 UTF-8 解会把中文路径写坏：实测 `E:\录屏` (GBK C2 BC C6 C1)
    #     被解成 `E:\¼` —— 不仅显示错，`os.makedirs` 还照着这个错名字
    #     **在磁盘上真建出一个叫 ¼ 的目录**，下载全落错地方。
    #     所以让 PowerShell 自己用 UTF-8 写文件，Python 这边读文件。
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "dy_pickdir_%s.txt" % uuid.uuid4().hex)
    ps = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='选择保存到的文件夹';"
        "$d.ShowNewFolderButton=$true;"
        "$d.RootFolder='MyComputer';"
        + ("$d.SelectedPath='%s';" % start.replace("'", "''") if start and os.path.isdir(start) else "") +
        "$r=$d.ShowDialog();"
        "if($r -eq [System.Windows.Forms.DialogResult]::OK){"
        "  [System.IO.File]::WriteAllText('%s', $d.SelectedPath,"
        "    (New-Object System.Text.UTF8Encoding($false))) }" % tmp
    )
    def _run():
        import subprocess
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-NonInteractive", "-Command", ps],
            capture_output=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            if not os.path.exists(tmp):
                return ""
            with open(tmp, "r", encoding="utf-8", errors="replace") as f:
                return f.read().strip()
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass
    try:
        picked = await asyncio.get_running_loop().run_in_executor(None, _run)
    except Exception as e:
        return j({"cancelled": True, "error": "打开选择框失败（可直接输入路径）：%s" % e})
    if not picked:
        return j({"cancelled": True})
    return j({"ok": True, "path": picked})


async def h_tool_selftest(req):
    """直播录制环境自检（复用模块 4 自身的 --selftest，不重复实现检查逻辑）。

    放在后台线程里跑：自检要探测 ffmpeg / playwright / 网络，可能耗时数秒，
    直接在事件循环里 subprocess.run 会把整个桥接服务卡住。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    kind = (body or {}).get("kind") if isinstance(body, dict) else "live"

    if kind == "live":
        exe = lr_python("python") or lr_python("pythonw")
        if not exe or not os.path.exists(LR_MAIN):
            return j({"error": "直播录制运行时或模块缺失"}, status=404)
        cmd, cwd = [exe, LR_MAIN, "--selftest"], LR_DIR
    else:
        return j({"error": "该工具暂无环境自检"}, status=400)

    def _run():
        import subprocess
        try:
            p = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=180,
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                               creationflags=NO_WINDOW)
            out = (p.stdout or b"").decode("utf-8", "replace")
            err = (p.stderr or b"").decode("utf-8", "replace")
            return {"ok": p.returncode == 0, "code": p.returncode,
                    "output": (out + ("\n" + err if err.strip() else "")).strip()}
        except Exception as e:
            return {"ok": False, "code": -1, "output": "自检执行失败: %s" % e}

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _run)
    return j(result)


def live_process_pids():
    """返回所有 LiveRecorder 相关进程的 pid 列表（可能为空）。

    ⚠️ 一个实例会占**两个** pid：`pythonw.exe main.py` 的启动壳 + 它 fork 出的
    真正持有 Qt 窗口的子进程（实测 ppid 关系 2360 → 27152）。
    启动壳自己没有可见窗口，所以只能拿「第一个 pid」去找窗口必然失败 ——
    必须把所有相关 pid 都拿回来再逐个找窗口。

    同样限定进程名，否则执行查询的 shell 自身（命令行含同样字面量）会被误判。
    """
    try:
        import subprocess
        cmd = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "($_.Name -eq 'pythonw.exe' -or $_.Name -eq 'python.exe') -and "
            "$_.CommandLine -like '*live_recorder*main.py*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, timeout=10,
            creationflags=NO_WINDOW,
        )
        text = (out.stdout or b"").decode("utf-8", "ignore").strip()
        return [int(x) for x in text.split() if x.isdigit()]
    except Exception:
        return []


def live_window_pid():
    """返回正在运行的 LiveRecorder 进程 pid（任一），没有则 None。"""
    pids = live_process_pids()
    return pids[0] if pids else None


def focus_live_window():
    """把已在运行的录制器主窗口调到前台。返回 (ok, message)。

    Windows 有前台锁定（SetForegroundWindow 对非前台进程常静默失败），
    所以标准绕过手法是：ShowWindow(SW_RESTORE) 还原最小化 →
    AttachThreadInput 把本线程输入队列挂到目标窗口线程 → BringWindowToTop +
    SetForegroundWindow。全部走 user32，不引入第三方库。

    窗口归属按「录制器相关的任一 pid」匹配 —— 启动壳与窗口进程是父子两个 pid，
    只认第一个会漏掉真正有窗口的那个（实测踩过）。
    """
    import ctypes
    from ctypes import wintypes

    pids = set(live_process_pids())
    if not pids:
        return False, "没有找到正在运行的录制器进程"

    u32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def _cb(hwnd, _):
        wpid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value not in pids:
            return True
        # 只要「可见 + 有标题」的顶层窗口 —— 跳过 Qt 的隐藏辅助窗口
        # （_q_titlebar / ThemeChangeObserverWindow / IME 等都不该被当成主窗口）
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(hwnd, buf, n + 1)
        title = buf.value
        # 主窗口标题必含产品名，据此排除其它有标题的杂项窗口
        if "LiveRecorder" in title:
            found.append((hwnd, title))
        return True

    u32.EnumWindows(WNDENUMPROC(_cb), 0)
    if not found:
        return False, "录制器进程在运行，但没找到可见窗口（可能还没画出来）"

    # 多个实例同时开着时，取最近激活过的那个 —— 更符合「切回我刚才在看的窗口」预期
    hwnd, title = found[0]

    SW_RESTORE = 9
    try:
        u32.ShowWindow(hwnd, SW_RESTORE)
        fg = u32.GetForegroundWindow()
        tid_target = u32.GetWindowThreadProcessId(hwnd, None)
        tid_self = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        if fg and tid_target and tid_target != tid_self:
            attached = bool(u32.AttachThreadInput(tid_self, tid_target, True))
        try:
            u32.BringWindowToTop(hwnd)
            u32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u32.AttachThreadInput(tid_self, tid_target, False)
        return True, title
    except Exception as e:
        return False, "调起窗口失败：%s" % e


async def h_live_launch(req):
    """唤起桌面 LiveRecorder GUI。

    关键行为：**已经在跑就只把窗口调到前台，绝不新起进程。**
    此前这里无条件 Popen，配合前端那个「仍要再开一个」按钮，
    会真的开出第二个实例并发录同一房间 —— 这是本项目记录过的真实故障。
    现在语义变成「打开录制器」而不是「新建录制器」。
    """
    if lr_running():
        ok, info = focus_live_window()
        if ok:
            return j({"ok": True, "focused": True, "title": info,
                      "message": "已切换到正在运行的录制器窗口"})
        # 找得到进程但调不起窗口：如实回报，不静默改成"再开一个"
        return j({"error": "录制器正在运行，但无法把它调到前台：%s" % info,
                  "focused": False}, status=409)

    exe = lr_python("pythonw") or lr_python("python")
    if not exe:
        return j({"error": "直播录制运行时缺失：%s" % LR_RUNTIME}, status=404)
    if not os.path.exists(LR_MAIN):
        return j({"error": "直播录制模块缺失：%s" % LR_MAIN}, status=404)
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        subprocess.Popen([exe, LR_MAIN], cwd=LR_DIR, creationflags=flags)
        return j({"ok": True, "save_dir": lr_save_dir()})
    except Exception as e:
        return j({"error": "启动失败: %s" % e}, status=500)


async def h_live_open_dir(req):
    """打开录像目录。body 带 {"dir": ...} 时打开指定目录（监控清单每行
    的「打开文件夹」），否则打开录像根目录（原行为）。

    安全校验：dir 必须落在录像根目录内部（或就是根目录本身）。路径来自
    内核快照的 output_dir，但接口不能信任任何客户端传来的路径 ——
    不做这层 containment 检查，等于给本机开了一个「explorer 任意路径」
    的远程按钮。
    """
    save_dir = lr_save_dir()
    target = save_dir
    try:
        body = await req.json()
        raw = str((body or {}).get("dir") or "").strip()
    except Exception:
        raw = ""
    if raw:
        real_root = os.path.realpath(save_dir)
        real_target = os.path.realpath(raw)
        # Windows 盘符大小写不敏感，前缀比较统一小写（realpath 已归一）
        root_low = real_root.lower().rstrip(os.sep) + os.sep
        tgt_low = real_target.lower()
        inside = tgt_low == real_root.lower().rstrip(os.sep) or tgt_low.startswith(root_low)
        if not inside:
            return j({"error": "该目录不在录像根目录内，拒绝打开", "path": raw}, status=403)
        target = real_target
    if not os.path.isdir(target):
        return j({"error": "录像目录还不存在（尚未录制过）", "path": target}, status=404)
    try:
        if os.name == "nt":
            _explorer_open_front(target)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", target])
        return j({"ok": True, "path": target})
    except Exception as e:
        return j({"error": "打开失败: %s" % e, "path": target}, status=500)


# 快手直播短链解析（2026-11-13）：v.kuaishou.com 分享链接无法从 URL 区分
# 视频/直播，跟一次重定向看落点 —— 落点带 /fw/live/ 或 subBiz=LIVE_STREAM
# 即直播（实测 v.kuaishou.com/J0QQb5gZ → v.m.chenzhongtech.com/fw/live/<principal>
# ?...&subBiz=LIVE_STREAM）。principal id 换算成桌面房间地址
# live.kuaishou.com/u/<principal>（实测 200）。快手域还包括其 CDN 域
# chenzhongtech.com（短链落点常在这里）。
_MOBILE_UA = ("Mozilla/5.0 (Linux; Android 12; Pixel 6) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")


def _resolve_share_link(url):
    """跟随一次分享短链的重定向，返回 (final_url, html_head)。
    解析失败抛异常，由调用方转成友好的错误响应。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": _MOBILE_UA})
    with urllib.request.urlopen(req, timeout=12) as r:
        final = r.geturl()
        head = (r.read(120_000) or b"").decode("utf-8", "ignore")
    return final, head


def _classify_final_url(final, html_head=""):
    """按重定向落点判定 直播/作品 与平台、房间地址。"""
    from urllib.parse import urlparse
    host = (urlparse(final).netloc or "").lower()
    is_live = ("live.kuaishou.com" in host or "live.douyin.com" in host
               or "/fw/live/" in final or "subBiz=LIVE_STREAM" in final)
    room_url = None
    platform = lr_detect_platform(final)
    if not platform and ("chenzhongtech.com" in host or "kuaishou" in host):
        platform = "kuaishou"
    if is_live:
        if not platform:
            platform = "douyin" if "douyin" in host else "kuaishou"
        if "live.kuaishou.com" in host or "live.douyin.com" in host:
            room_url = final.split("?")[0]
        else:
            m = re.search(r"/fw/live/([A-Za-z0-9_\-]+)", final)
            if m and platform == "kuaishou":
                room_url = "https://live.kuaishou.com/u/" + m.group(1)
            # douyin 的 /fw/live/ 形态暂无可靠换算，交给录制器原样解析
            if not room_url:
                room_url = final.split("?")[0]
    return {"is_live": is_live, "platform": platform or "unknown",
            "room_url": room_url, "final_url": final}


async def h_resolve_link(req):
    """分享短链解析：告诉前端这条 v.kuaishou.com / v.douyin.com 链接是
    直播间还是作品页，直播时给出可直接加入监控的房间地址。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    url = str((body or {}).get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    try:
        final, _head = _resolve_share_link(url)
    except Exception as e:
        return j({"error": "短链解析失败：%s" % humanize_note(str(e)[:120])}, 502)
    out = _classify_final_url(final)
    out.update({"ok": True, "url": url})
    return j(out)


def _extract_live_principal(url):
    """从快手直播链接/短链中提取房间 principal id（房间唯一标识）。

    支持 live.kuaishou.com/u/<id> 与分享短链（跟重定向到 /fw/live/<id>）。
    同一房间以「短链」「桌面链接」「链接+分享文案」等不同形态粘贴时
    raw_input 互不相同，引擎按 raw_input 全等的去重会漏 —— 这里提取
    principal 供添加前与现有清单比对。提取失败返回 None。"""
    from urllib.parse import urlparse
    host = (urlparse(url).netloc or "").lower()
    if "live.kuaishou.com" in host:
        m = re.search(r"/u/([A-Za-z0-9_\-]+)", url)
        return m.group(1) if m else None
    if "v.kuaishou.com" in host or "chenzhongtech.com" in host:
        try:
            final, _head = _resolve_share_link(url)
        except Exception:
            return None
        m = re.search(r"/fw/live/([A-Za-z0-9_\-]+)", final) or re.search(r"/u/([A-Za-z0-9_\-]+)", final)
        return m.group(1) if m else None
    return None


async def h_live_add_room(req):
    """把直播链接加入桌面 LiveRecorder 的监控清单。

    「识别并分流」页/直播录制页贴入 live.douyin.com / live.kuaishou.com 链接后走这里：
    1. 本地识别平台（不联网、不解析短链）；
    2. 去重后原子写入 live_recorder/config.json 的 rooms；
    3. 已在运行则交给录制器自身的热并入（_sync_external_rooms 只增不删）；
       未运行则静默写 config，不弹 GUI（除非前端显式传 launch=true）。
    Cookie 不涉及。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip()
    remark = (body.get("remark") or "").strip()
    # 默认不唤起 GUI：加入监控清单是「页面内静默」操作，不应弹桌面录制器窗口打断用户。
    # 录制器已在运行则热并入；未运行就只写 config，用户可到直播录制页主动「唤起」。
    launch = bool(body.get("launch", False))

    if not url:
        return j({"error": "缺少直播链接（url）"}, status=400)
    platform = lr_detect_platform(url)
    if not platform:
        return j({"error": "无法识别直播平台，请粘贴 live.douyin.com / live.kuaishou.com "
                           "链接或快手分享短链"}, status=400)

    # ⚠️ 内核在线时必须走内核自己的 /rooms/add，绝不能直接写 config.json：
    #    内核持有**内存中的房间清单**并会在轮询周期把它整份回写 config.json，
    #    桥接直接写文件会被覆盖 —— 2026-11-13 实测：这里回了 added:true，
    #    房间却在内核下一轮回写后消失，用户看监控清单永远少一条。
    #    离线时（无内核可覆盖）才走下面的文件写入路径。
    if _daemon_port_busy():
        # 先按 principal 与现有清单去重（引擎只按 raw_input 全等去重，
        # 同一房间的短链/桌面链接/链接+文案三种形态会互相绕过 → 列表出重条）
        st0, dstat = await asyncio.get_running_loop().run_in_executor(
            None, lambda: lr_daemon_call("/status", timeout=8))
        existing_rooms = (dstat or {}).get("rooms") or [] if st0 == 200 else []
        principal = await asyncio.get_running_loop().run_in_executor(
            None, lambda: _extract_live_principal(url))
        if principal:
            for r in existing_rooms:
                rid = (r.get("room_id") or "").strip()
                if rid and (rid == principal or principal in (r.get("raw_input") or "")
                            or rid in url):
                    return j({
                        "ok": True, "added": False, "duplicate": True,
                        "platform": platform,
                        "platform_label": {"kuaishou": "快手", "douyin": "抖音"}.get(platform, platform),
                        "message": "该直播间已在监控清单中（%s）" % (r.get("nickname") or rid),
                        "via_daemon": True,
                    })
        st, payload = await asyncio.get_running_loop().run_in_executor(
            None, lambda: lr_daemon_call("/rooms/add", method="POST",
                                         body={"url": url, "remark": remark,
                                               "platform": platform},
                                         timeout=12))
        if st == 200:
            payload.setdefault("ok", True)
            payload.setdefault("message", "已加入监控清单（录制内核在线，立即生效）")
            payload["via_daemon"] = True
            return j(payload)
        if st:
            # 内核明确拒绝（如判定为视频链接），把原因如实带回
            payload.setdefault("ok", False)
            payload.setdefault("message", payload.get("error") or "录制内核拒绝了该链接")
            return j(payload, status=st if st in (400, 409) else 502)
        # st == 0：连不上（竞态刚下线）→ 落到离线写文件路径

    # 读现有配置（不存在则视为空，稍后整体落盘会带默认 version）
    try:
        with open(LR_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    rooms = data.get("rooms") if isinstance(data.get("rooms"), list) else []

    def _norm(s):
        return (s or "").strip()

    dup = any(
        isinstance(r, dict) and r.get("platform") == platform
        and (_norm(r.get("raw_input")) == url or _norm(r.get("room_id")) == url)
        for r in rooms
    )

    added = False
    if not dup:
        rooms.append({
            "platform": platform,
            "raw_input": url,
            "room_id": "",
            "nickname": "",
            "enabled": True,
            "remark": remark,
        })
        added = True
        payload = {"version": data.get("version", 1), "settings": settings, "rooms": rooms}
        tmp = LR_CONFIG + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, LR_CONFIG)
        except Exception as e:
            return j({"error": "写入配置失败: %s" % e}, status=500)

    running = lr_running()
    launched = False
    if added and launch and not running:
        exe = lr_python("pythonw") or lr_python("python")
        if exe and os.path.exists(LR_MAIN):
            import subprocess as _sp
            flags = getattr(_sp, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                _sp.Popen([exe, LR_MAIN], cwd=LR_DIR, creationflags=flags)
                launched = True
            except Exception:
                launched = False

    label = {"kuaishou": "快手", "douyin": "抖音"}.get(platform, platform)
    if dup:
        message = "该直播链接已在监控清单中"
    elif launched:
        message = "已加入监控清单并唤起直播录制器（在窗口内点「开始监控」即可）"
    elif running:
        message = "已加入监控清单，录制器会自动并入（或重启录制器生效）"
    else:
        message = "已加入监控清单（录制器未启动，可到直播录制页手动唤起）"

    return j({
        "ok": True,
        "added": added,
        "duplicate": dup,
        "platform": platform,
        "platform_label": label,
        "launched": launched,
        "running": running,
        "message": message,
    })


# ---------- 直播录制：无界面守护进程代理 ----------
# 背景：录制内核原本只能由 PyQt6 主窗口持有，网页只能「唤起那个窗口」。
# 现在 live_recorder 多了一个无界面宿主 modules/live_recorder/daemon.py，
# 它在单进程内持有同一个 MonitorEngine，并用本机回环 HTTP 暴露控制面。
# 本区块把这些接口**原样代理**到 8519，网页不直接跨端口访问
# （同源策略 + 页面只认 8518，跨端口 fetch 会被浏览器拦）。
#
# 设计原则：
#   1. 代理层**不做业务判断** —— 房间去重、参数校验、状态语义都在守护进程里，
#      两处各写一遍必然分叉（本项目在 h_tool_launch / h_live_launch 上已经
#      吃过一次这个亏）。这里只转发 + 转述错误。
#   2. 守护进程没起来时**如实回报**（503 + 可操作的建议），不伪造状态、
#      不假装「正在运行」。
LR_DAEMON = os.path.join(LR_DIR, "daemon.py")
LR_DAEMON_PORT = 8519


def lr_daemon_pid():
    """读守护进程锁文件拿 pid（拿不到返回 None）。"""
    try:
        with open(os.path.join(LR_DIR, ".daemon.lock"), "r", encoding="utf-8") as f:
            return int(json.load(f).get("pid") or 0) or None
    except Exception:
        return None


def _quote_path(path):
    """把 path（含 query）里的非 ASCII 部分百分号编码，保留 ``/ ? = & %`` 等结构字符。

    不作为的后果：含中文的 query 会让 urllib 抛 ``UnicodeEncodeError``。
    """
    import urllib.parse

    return urllib.parse.quote(path, safe="/?=&%+#:,")


def lr_daemon_call(path, *, method="GET", body=None, timeout=8):
    """同步调用守护进程接口，返回 (status, payload)。

    刻意用 stdlib ``urllib`` 而不是 aiohttp 客户端：本代理是**阻塞式**转发
    （由调用方放进线程池），而工程里已有的 httpx/aiohttp 用法都在别处，
    为几个转发再引入一个客户端 API 面没有必要。

    ⚠️ path 必须做百分号编码：``urllib.request.Request`` 会把非 ASCII 字符
    按 ``ascii`` 编码，含中文的 query（如按文件名下载 `?path=抖音-xxx.mp4`）
    会直接抛 ``UnicodeEncodeError``。这不是守护进程的问题，是本层传 URL
    的姿势不对 —— 已实测踩到。
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    url = "http://127.0.0.1:%d%s" % (LR_DAEMON_PORT, _quote_path(path))
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    # 沙箱/企业环境可能设了 HTTP_PROXY，会让 localhost 请求也走代理并 502；
    # 显式清空代理，保证只打本机。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"error": raw or ("HTTP %d" % exc.code)}
    except Exception as exc:
        return 0, {"error": "%s: %s" % (type(exc).__name__, exc)}


def lr_daemon_alive(timeout=1.5):
    """守护进程是否在跑（顺带拿到 pid/版本）。"""
    status, payload = lr_daemon_call("/health", timeout=timeout)
    return (status == 200 and bool(payload.get("ok"))), payload


def _daemon_offline_payload():
    """守护进程不在时的统一响应。"""
    exists = os.path.exists(LR_DAEMON)
    return {
        "ok": False,
        "daemon": False,
        "error": ("录制守护进程未运行" if exists
                  else "录制守护进程模块缺失：%s" % LR_DAEMON),
        "hint": ("录制内核现在由无界面守护进程执行，网页的所有录制操作都要它在线。"
                 "点上方「启动录制内核」即可。"
                 if exists else "请检查模块是否完整。"),
    }


async def _proxy(path, *, method, body=None, timeout=8):
    """在 executor 里转发一次请求。

    ⚠️ ``method`` **没有默认值**（刻意为之）。曾经它默认 ``"GET"``，
    结果 ``/shutdown`` 那一路漏写 ``method="POST"``，被悄悄当成 GET 发出去，
    守护进程的 GET 路由里没有 /shutdown，于是回了
    ``404 {"error": "未知接口 /shutdown"}`` —— 表现为「停止录制内核」按钮点了
    没反应，且错误信息完全指向性不明（看起来像守护进程版本旧/没跑）。
    改成就没有默认值：漏传会立刻 ``TypeError``，编译期就能发现。

    返回 ``(status, payload)``：
      * ``status == 0`` → 连不上守护进程（未运行 / 端口被占 / 被拦截）。
        ``payload["error"]`` 是底层异常文本，可用于排查，但**不要**当成
        面向用户的提示（那是 ``_daemon_offline_payload()`` 的职责）。
      * 其余为守护进程真实返回的 HTTP 状态（含 4xx/5xx，业务语义要原样透传）。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, lambda: lr_daemon_call(path, method=method, body=body, timeout=timeout)
    )


def _offline_response(detail=None, status=503):
    """构造「守护进程不在」的统一响应。"""
    payload = _daemon_offline_payload()
    if detail:
        payload["detail"] = detail
    return j(payload, status=status)


def _spawn_daemon():
    """拉起守护进程（幂等：已在跑就返回 None）。返回 (ok, message)。

    ⚠️ 这里有一个必须绕开的坑：``runtime/live-recorder-env/Scripts/python.exe``
    是**重新 exec 的启动器 stub**，``Popen()`` 返回的是 stub 的 pid，真正跑
    ``daemon.py`` 的是它 exec 出来的子进程（pid 不同），而 stub 会**立刻退出**。
    因此不能拿 Popen 的 pid 当「守护进程的 pid」用：
      * 用它判活会得出「已死」→ 再起一个 → 两个 ``MonitorEngine`` 同时写
        config.json 互相覆盖（本项目记录过的真实故障）；
      * 把它回报给网页，界面上的 pid 是错的。
    正确做法是**只认 /health**（真身自报的 pid），并用端口探测判在线。
    """
    alive, info = lr_daemon_alive(timeout=1.2)
    if alive:
        return True, "录制内核已在运行（pid=%s）" % info.get("pid")

    exe = lr_python("pythonw") or lr_python("python")
    if not exe:
        return False, "直播录制运行时缺失：%s" % LR_RUNTIME
    if not os.path.exists(LR_DAEMON):
        return False, "录制守护进程模块缺失：%s" % LR_DAEMON

    # 端口已被占用但 /health 不通 → 是别人（或半死的残留）在用，别硬起
    if _daemon_port_busy():
        return False, ("端口 %d 已被占用但没有响应，可能有一个卡死的录制内核。"
                       "请先结束它再重试。" % LR_DAEMON_PORT)

    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    try:
        subprocess.Popen(
            [exe, LR_DAEMON, "--port", str(LR_DAEMON_PORT)],
            cwd=LR_DIR, creationflags=flags, env=env,
            # 守护进程的输出不接管道：它自己会写日志文件，
            # 接管道而不读会在缓冲区满时把它卡死（本项目修过的 ffmpeg 同类坑）。
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:  # noqa: BLE001
        return False, "启动失败: %s" % exc
    return True, "录制内核正在启动"


def _daemon_port_busy():
    """端口是否已被占用（TCP 连接语义）。"""
    import socket

    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", LR_DAEMON_PORT), timeout=1.0)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _await_daemon(seconds=12.0):
    """等守护进程就绪（Popen 是异步的，且 python.exe 是启动器 stub，
    真身要等 stub exec 完才起来，所以必须轮询而不是等 Popen）。

    判据用 ``/health`` 而不是端口：端口能连上只说明「有人在听」，
    而我们要的是「**我们的**守护进程在跑」——``/health`` 会自报 pid/version，
    可据此确认它真的完成了初始化。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        alive, info = lr_daemon_alive(timeout=1.2)
        if alive:
            return True, info
        time.sleep(0.35)
    return False, {}


async def h_live_daemon_ensure(req):
    """确保录制守护进程在线（网页每次进入录制页都会调它）。"""
    body = {}
    try:
        body = await req.json()
    except Exception:
        body = {}
    autostart = bool((body or {}).get("autostart", True))

    # /health 只在守护进程活着时返回 200，所以这里用 status 判定而非解包
    status, info = await _proxy("/health", method="GET", timeout=1.5)
    if status == 200 and isinstance(info, dict) and info.get("ok"):
        return j({"ok": True, "daemon": True, "pid": info.get("pid"),
                  "version": info.get("version"), "started": False})

    if not autostart:
        return _offline_response(
            info.get("error") if isinstance(info, dict) else None)

    loop = asyncio.get_running_loop()
    ok, message = await loop.run_in_executor(None, _spawn_daemon)
    if not ok:
        payload = _daemon_offline_payload()
        payload["error"] = message
        return j(payload, status=503)

    ok2, info2 = await loop.run_in_executor(None, lambda: _await_daemon(12.0))
    if not ok2:
        return j({"ok": False, "daemon": False,
                  "error": "录制内核启动超时，请查看 %s"
                           % os.path.join(lr_save_dir(), "_logs"),
                  "hint": "也可能是端口 %d 被占用或被安全软件拦截。" % LR_DAEMON_PORT},
                 status=503)
    return j({"ok": True, "daemon": True, "pid": info2.get("pid"),
              "version": info2.get("version"), "started": True,
              "message": "录制内核已启动"})


async def h_live_daemon_status(req):
    """录制内核完整状态（房间实时状态/时长/体积/分段数 + 设置 + 登录态）。"""
    status, payload = await _proxy("/status", method="GET", timeout=10)
    if status != 200:
        offline = _daemon_offline_payload()
        offline["detail"] = payload.get("error") if isinstance(payload, dict) else None
        return j(offline, status=503)
    payload["daemon"] = True
    return j(payload)


async def h_live_daemon_action(req, path, *, method="POST"):
    """通用动作转发：body 原样透传。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    status, payload = await _proxy(path, method=method, body=body, timeout=20)
    if status == 0:
        offline = _daemon_offline_payload()
        offline["detail"] = payload.get("error")
        return j(offline, status=503)
    return j(payload, status=status)


async def h_live_daemon_logs(req):
    """增量拉取录制内核日志（环形缓冲）。"""
    try:
        since = int(req.query.get("since") or 0)
    except ValueError:
        since = 0
    status, payload = await _proxy("/logs?since=%d" % since, method="GET", timeout=8)
    if status != 200:
        return j({"ok": False, "seq": since, "items": [],
                  "error": "录制内核未运行，暂不能读取日志"}, status=503)
    return j(payload)


async def h_live_daemon_logs_login(req):
    """扫码登录会话状态（二维码 base64 + 进度）。

    守护进程不在时返回 503，但**不报错**——网页据此把「扫码」按钮置灰，
    这是正常状态而非故障（与 /daemon/status 同一套语义）。
    """
    status, payload = await _proxy("/login/status", method="GET", timeout=6)
    if status != 200:
        return j({"ok": False, "state": "unavailable",
                  "error": "录制内核未运行"}, status=503)
    return j(payload)


def _wait_daemon_down(seconds=8.0):
    """等守护进程真的不再应答。返回 True 表示它已下线。

    为什么不能只探一次 ``/health``：守护进程收到 ``/shutdown`` 后是
    **异步**收尾的（``_deferred_stop`` 先 sleep 0.25s 把响应发出去，
    之后才 ``server.shutdown()``，并且还要先 ``engine.shutdown()`` 收尾录制）。
    在这段时间里 ``/health`` **仍然通**，单次探测会得出「它还活着」的
    错误结论，进而把一次成功的停止报成 503 —— 已实测踩到。
    正解是轮询等端口真的拒绝连接为止。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not _daemon_port_busy():
            return True
        time.sleep(0.25)
    return False


async def h_live_daemon_shutdown(req):
    """停掉录制守护进程（网页的「停止录制内核」）。

    ⚠️ 这一路特别容易误判：守护进程收到 shutdown 后会**立刻**停掉 HTTP 服务，
    连接可能在响应送达前就被关掉，转发层于是拿到 ``status == 0``
    （实测是 ``ConnectionResetError``）。这**不代表**停止失败 ——
    恰恰相反，服务消失正是它停成功的证据。

    所以判据统一改成「等它真的下线」：只要停止后端口不再应答，就算成功，
    无论是收到了 200 还是连接被重置。这样 200 与 reset 两种表现归一，
    调用方不会因为时序差异看到两种结论。
    """
    status, payload = await _proxy("/shutdown", method="POST", body={}, timeout=8)
    if status == 200:
        return j({"ok": True, "message": "录制内核正在退出（会先收尾合并已录分片）"})

    loop = asyncio.get_running_loop()
    if status == 0:
        # 连接被重置/拒绝：可能它正在停，也可能它本来就没跑。等端口关闭来区分。
        down = await loop.run_in_executor(None, lambda: _wait_daemon_down(8.0))
        if down:
            return j({"ok": True, "already_stopped": True,
                      "message": "录制内核已停止"})
        return _offline_response(payload.get("error") if isinstance(payload, dict) else None)

    return j(payload, status=status)


async def h_live_daemon_download(req):
    """下载录像文件（浏览器直接存盘）。

    为什么需要它：录像可能在 E 盘任意位置，网页无法直接读本地文件；
    浏览器要「另存为」必须由服务端把字节吐出来。
    """
    rel = (req.query.get("path") or "").strip()
    if not rel:
        return j({"error": "缺少 path"}, status=400)

    root = os.path.realpath(lr_save_dir())
    target = os.path.realpath(os.path.join(root, rel))

    # 目录穿越防护：只允许读录像根目录下的文件。
    # 用 realpath + commonpath 而不是 startswith 字符串比较 ——
    # 后者对 `.../直播录制_evil/` 这种前缀相同的兄弟目录会误判放行。
    if os.path.commonpath([root, target]) != root:
        return j({"error": "路径不在录像目录内"}, status=403)
    if not os.path.isfile(target):
        return j({"error": "文件不存在", "path": rel}, status=404)

    from aiohttp import web as _w
    return _w.FileResponse(
        target, headers={"Content-Disposition": 'attachment; filename="%s"'
                         % os.path.basename(target)},
    )


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
            "config_path": cfg_path, "finished_at": None, "kind": "douyin",
            # 存一份本次的覆盖项：任务结束后临时 config 会被删掉，
            # 没有它「重试」就只能回落到 config.yml 的默认值（模式/画质都丢了）。
            "options": options or {}}
    TASKS[task_id] = task
    PENDING.append(task_id)
    loop.create_task(pump_queue())
    return task, None


def start_direct_download(urls, loop, segments=None, jobs=None):
    """直链下载：复用同一任务队列，但命令是 clawfetch.py（纯标准库，无需临时配置）。

    一次可以给多个链接（`urls` 传列表，也兼容单个字符串）—— 走 clawfetch 的
    原生批量模式，而不是在这里建多个任务：
      · 不同域名 → 并行下载（`jobs` 为 None 时由 clawfetch 定，当前是同时 2 个）；
      · 同一域名 → **自动串行**。同一条源站上行被两路连接分食只会一起变慢
        （2026-09-27 那次 397MB 掉到 5KB/s + 502 风暴就是这么来的），
        按 netloc 分链是 clawfetch 里已经写好的策略。
      · 一个任务 = 一份日志 + 一次「取消」，比逐条建任务更好管，也不会出现
        N 个任务各自开 8 连接、把源站打哑的情况。

    `segments`：只支持 1（单连接）。给弱源站用 —— 临时隧道/自建小站扛不住
    多连接时，"先开 8 个、撞 502 再往下退"这一段纯属白等，直接从 1 开始更省事。
    取 None 表示交给 clawfetch 的 auto 自适应（默认行为，不改）。
    `jobs`：多链接时的并发路数（1..4）。None = 交给 clawfetch 的 auto。
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = [u.strip() for u in (urls or []) if (u or "").strip()]
    if not urls:
        return None, "没有可用的下载链接"
    multi = len(urls) > 1
    task_id = "d" + uuid.uuid4().hex[:10]
    log_path = os.path.join(TASK_DIR, f"{task_id}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    log_f.write(f"# 直链任务 {task_id}\n")
    if multi:
        log_f.write(f"# 批量模式：%d 个链接\n" % len(urls))
        for i, u in enumerate(urls, 1):
            log_f.write(f"#   {i}. {u}\n")
        log_f.write("# 并发: %s（同域自动串行，避免抢同一条源站上行）\n"
                    % (f"同时 {jobs} 个" if jobs else "auto（同时 2 个）"))
    else:
        log_f.write(f"# URL: {urls[0]}\n")
    log_f.write(f"# 连接模式: {'单连接（兼容弱源站）' if segments == 1 else 'auto 自适应'}\n"
                f"# 创建: {now_iso()}\n\n")
    log_f.close()
    task = {"id": task_id, "url": urls[0], "urls": urls, "count": len(urls),
            "status": "pending", "created_at": now_iso(),
            "pid": None, "exit_code": None, "log_path": log_path,
            "config_path": None, "finished_at": None, "kind": "direct",
            "segments": segments, "jobs": (jobs if multi else None)}
    TASKS[task_id] = task
    PENDING.append(task_id)
    loop.create_task(pump_queue())
    return task, None


def start_kuaishou_download(url, mode, loop):
    """快手下载：纳入统一后台任务队列，跑无 GUI 的 kuaishou_headless.py。
    mode: 'all' 全部作品 / '1' 仅最新 1 个。"""
    task_id = "k" + uuid.uuid4().hex[:10]
    log_path = os.path.join(TASK_DIR, f"{task_id}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    log_f.write(f"# 快手任务 {task_id}\n# URL: {url}\n# 模式: {mode}\n# 创建: {now_iso()}\n\n")
    log_f.close()
    task = {"id": task_id, "url": url, "status": "pending", "created_at": now_iso(),
            "pid": None, "exit_code": None, "log_path": log_path,
            "config_path": None, "finished_at": None, "kind": "kuaishou", "mode": mode}
    TASKS[task_id] = task
    PENDING.append(task_id)
    loop.create_task(pump_queue())
    return task, None


def start_ty_download(url, mode, loop):
    """TikTok / YouTube 下载：与 B 站同一套任务队列，跑 ty_headless.py。

    mode: 'all' 全部（列表类链接全量）/ 纯数字 N 限量；单视频忽略 mode。
    """
    task_id = "ty" + uuid.uuid4().hex[:10]
    log_path = os.path.join(TASK_DIR, f"{task_id}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    log_f.write(f"# TikTok/YouTube任务 {task_id}\n# URL: {url}\n# 模式: {mode}\n# 创建: {now_iso()}\n\n")
    log_f.close()
    task = {"id": task_id, "url": url, "status": "pending", "created_at": now_iso(),
            "pid": None, "exit_code": None, "log_path": log_path,
            "config_path": None, "finished_at": None, "kind": "ty", "mode": mode}
    TASKS[task_id] = task
    PENDING.append(task_id)
    loop.create_task(pump_queue())
    return task, None


def start_ytdlp_download(url, opts, loop):
    """yt-dlp 万能下载：与 ty/B 站同一套任务队列，跑 ytdlp_headless.py。

    opts 是前端提交的选项 dict（mode/quality/audio_format/subs/subs_langs/
    cookies_browser/cookies_file/archive/sponsorblock），原样存进任务，
    subprocess_popen 的 ytdlp 分支负责翻译成命令行。
    """
    task_id = "yt" + uuid.uuid4().hex[:10]
    log_path = os.path.join(TASK_DIR, f"{task_id}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    log_f.write(f"# yt-dlp万能任务 {task_id}\n# URL: {url}\n# 选项: {opts}\n# 创建: {now_iso()}\n\n")
    log_f.close()
    task = {"id": task_id, "url": url, "status": "pending", "created_at": now_iso(),
            "pid": None, "exit_code": None, "log_path": log_path,
            "config_path": None, "finished_at": None, "kind": "ytdlp", "opts": opts}
    TASKS[task_id] = task
    PENDING.append(task_id)
    loop.create_task(pump_queue())
    return task, None


def start_bilibili_download(url, mode, loop):
    """哔哩哔哩下载：与快手同一套任务队列，跑 bilibili_headless.py。

    URL 自动识别类型（单作品 / 主页批量 / 直播录制），mode 只起调节作用：
    'all' 全部 / 纯数字 N 限量 / 'audio' 仅音频（单作品）/ 'live' 强制直播。
    """
    task_id = "bl" + uuid.uuid4().hex[:10]
    log_path = os.path.join(TASK_DIR, f"{task_id}.log")
    log_f = open(log_path, "w", encoding="utf-8", errors="replace")
    log_f.write(f"# 哔哩哔哩任务 {task_id}\n# URL: {url}\n# 模式: {mode}\n# 创建: {now_iso()}\n\n")
    log_f.close()
    task = {"id": task_id, "url": url, "status": "pending", "created_at": now_iso(),
            "pid": None, "exit_code": None, "log_path": log_path,
            "config_path": None, "finished_at": None, "kind": "bilibili", "mode": mode}
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
        # 快手任务启动时要独占登录 profile 目录（kuaishou_headless 会用它开
        # 持久化浏览器上下文）。两个快手任务同时启动会抢目录锁，后启动的 Edge
        # 直接退出，任务死于「未能从链接提取作品」（2026-10-24 实测 5/6 全灭）。
        # → 快手任务串行：已有一个在跑时跳过排队的快手任务，让其它任务先上。
        ks_busy = any(t.get("kind") == "kuaishou" for t in running)
        idx = None
        for i, tid in enumerate(PENDING):
            t = TASKS.get(tid)
            if not t or t["status"] != "pending":
                idx = i          # 陈旧条目：照旧弹出清掉
                break
            if ks_busy and t.get("kind") == "kuaishou":
                continue
            idx = i
            break
        if idx is None:
            return
        tid = PENDING.pop(idx)
        task = TASKS.get(tid)
        if not task or task["status"] != "pending":
            continue
        log_f = open(task["log_path"], "a", encoding="utf-8", errors="replace")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess_popen(task, log_f, env)
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


def subprocess_popen(task, log_f, env):
    import subprocess
    dirs = _init_tool_dirs()
    if task.get("kind") == "direct":
        # clawfetch 直链下载：纯标准库，任意 Python 均可；输出目录可在启动台改
        out = dirs.get("direct") or DIRECT_DIR
        os.makedirs(out, exist_ok=True)
        # 摘掉继承来的代理变量：直链下载要的是"从这台机器直连那个 URL"，
        # 而不是"绕到某个可能是别人的、随时会消失的代理端口上"。
        dropped = [k for k in _PROXY_ENV_KEYS if k in env]
        env = strip_proxy_env(env)
        if dropped:
            log_f.write("[提示] 已忽略继承的代理设置：%s\n"
                        "       直链将直连；若确实需要走代理，请用命令行加 --proxy <地址>\n"
                        % "、".join(sorted(set(k.upper() for k in dropped))))
        return subprocess.Popen(
            [sys.executable, CLAW_PY]
            + list(task.get("urls") or [task["url"]])
            + ["-o", out] + (
                ["--segments", str(int(task["segments"]))]
                if task.get("segments") else []) + (
                # -j 只在多链接时有意义：单链接走 clawfetch 的单文件路径
                # （-j 是批量模式的开关，传了也不影响，但会误导读日志的人）
                ["-j", str(int(task["jobs"] or 2))]
                if len(task.get("urls") or []) > 1 else []),
            cwd=CLAW_DIR, stdout=log_f, stderr=subprocess.STDOUT,
            env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    if task.get("kind") == "kuaishou":
        # 快手：用 kuaishou-env 解释器跑无 GUI 下载脚本（playwright/requests/yt-dlp 都在该 env）
        exe = ks_python("python") or ks_python("pythonw") or sys.executable
        # ⚠️ 产物目录经 KS_OUTDIR 下发：快手脚本原本把落点写死在 data\kuaishou，
        #    不改环境变量的话前端改了也不生效。子进程会继承它，
        #    因此被它调起的 kuaishou_fast_dl.py 也拿到同一个目录（台账才会跟着走）。
        if dirs.get("kuaishou"):
            env = dict(env)
            env["KS_OUTDIR"] = dirs["kuaishou"]
            os.makedirs(dirs["kuaishou"], exist_ok=True)
        return subprocess.Popen(
            [exe, "-u", KS_HEADLESS, task["url"], task.get("mode") or "all"],
            cwd=KS_TOOLS, stdout=log_f, stderr=subprocess.STDOUT,
            env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    if task.get("kind") == "ty":
        # TikTok / YouTube：复用 kuaishou-env（yt-dlp 装在该环境）跑 ty_headless.py。
        # ⚠️ 与直链下载相反，这里**保留**继承的代理变量：目标站在境内直连不通，
        #    脚本自己会「直连探测→本地代理探测」，环境里有代理变量也一并可用。
        exe = ks_python("python") or ks_python("pythonw") or sys.executable
        if dirs.get("ty"):
            env = dict(env)
            env["TY_OUTDIR"] = dirs["ty"]
            os.makedirs(dirs["ty"], exist_ok=True)
        return subprocess.Popen(
            [exe, "-u", TY_HEADLESS, task["url"], task.get("mode") or "all"],
            cwd=TY_DIR, stdout=log_f, stderr=subprocess.STDOUT,
            env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    if task.get("kind") == "ytdlp":
        # yt-dlp 万能下载：复用 kuaishou-env 解释器（yt-dlp 2026.10.21 在该环境），
        # 跑 modules/ytdlp/ytdlp_headless.py。与 ty 同理**保留**继承的代理变量：
        # 目标站（Twitter/Instagram 等）境内直连不通，脚本自己会做「直连探测→本地
        # 代理探测」，境内站（微博等）直连可达时不会走代理。
        exe = ks_python("python") or ks_python("pythonw") or sys.executable
        if dirs.get("ytdlp"):
            env = dict(env)
            env["YTDLP_OUTDIR"] = dirs["ytdlp"]
            env["YTDLP_ARCHIVE"] = YTDLP_ARCHIVE
            env["YTDLP_CONF"] = YTDLP_CONF
            os.makedirs(dirs["ytdlp"], exist_ok=True)
        o = task.get("opts") or {}
        argv = [exe, "-u", YTDLP_HEADLESS, "download", task["url"],
                "--mode", str(o.get("mode") or "all"),
                "--quality", str(o.get("quality") or "best"),
                "--audio-format", str(o.get("audio_format") or "none"),
                "--subs", str(o.get("subs") or "none"),
                "--subs-langs", str(o.get("subs_langs") or "zh-CN,zh-Hans,en"),
                "--cookies-browser", str(o.get("cookies_browser") or "none")]
        ck = str(o.get("cookies_file") or "").strip()
        if ck and os.path.exists(ck):
            argv += ["--cookies-file", ck]
        if o.get("archive"):
            argv += ["--archive"]
        if o.get("sponsorblock"):
            argv += ["--sponsorblock"]
        return subprocess.Popen(
            argv, cwd=YTDLP_DIR, stdout=log_f, stderr=subprocess.STDOUT,
            env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    if task.get("kind") == "bilibili":
        # 哔哩哔哩：复用 kuaishou-env 解释器（yt-dlp 已装在该环境），
        # 跑 modules/bilibili/bilibili_headless.py；URL 类型由脚本自动识别。
        exe = ks_python("python") or ks_python("pythonw") or sys.executable
        if dirs.get("bilibili"):
            env = dict(env)
            env["BILI_OUTDIR"] = dirs["bilibili"]
            os.makedirs(dirs["bilibili"], exist_ok=True)
        return subprocess.Popen(
            [exe, "-u", BILI_HEADLESS, task["url"], task.get("mode") or "all"],
            cwd=BILI_DIR, stdout=log_f, stderr=subprocess.STDOUT,
            env=env, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    # 抖音：调用工程 run.py CLI
    return subprocess.Popen(
        [sys.executable, RUN_PY, "-c", task["config_path"], "-u", task["url"]],
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
    # 用户主动取消的任务保持「已取消」：taskkill 的退出码必然非 0，
    # 无条件按退出码判定会把它改写成「失败」，让人以为是出错了。
    if task["status"] != "canceled":
        task["status"] = "success" if code == 0 else "failed"
        # ⚠️ 退出码 0 ≠ 真的下到了东西。快手守卫刹车时刻意回 0，若照旧显示
        #    「已完成」，用户看到的就是「绿的成功 + 一个文件都没有」。
        #    单独标出来，并把原因带上去（见 guard_brake_reason）。
        if code == 0:
            why = guard_brake_reason(task)
            if why:
                task["status"] = "braked"
                task["brake_reason"] = why
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


# ── 「直链任务：取回真实直链」───────────────────────────────────────────────
# 直链页收到的常常**不是文件地址，而是一个「取件码」式的页面地址**
# （例如媒体服务的 /download/<id>）。这类地址会随服务端的扫描任务过期而失效，
# 用户复制出去打开，看到的只是一句 "scan task not found or expired"。
#
# 但真正能下的那条地址，clawfetch 在探测阶段就已经解析出来了，并写进了任务日志：
#     [探测] 已从中转页解析到直链:https://host/stream/xxxx
# （批量模式每个链接各走一遍 run()，同一句话照样会打出来。）
# 所以任务中心的「复制链接」不该回放当初粘进去的那条，而该认这一条。
_RE_RESOLVED_DIRECT = re.compile(r"已从中转页解析到直链[:：]\s*(\S+)")
# 解析发生在探测阶段（日志最开头），只读开头就够 —— 下载进度行可以有几万行。
_RESOLVED_HEAD_LINES = 400


def task_resolved_urls(task):
    """直链任务里 clawfetch 解析出的**真实直链**，按日志出现顺序，去重。

    非直链任务（抖音主页 / 快手）没有中转页概念，一律返回空列表 —— 它们的
    url 本来就是给人看的分享地址，复制它是对的。
    读不到日志/还没解析到就返回空列表，由前端回退到原始 url。
    """
    if task.get("kind") != "direct":
        return []
    live = task.get("status") in ("running", "pending")
    if not live:
        # 已结束的任务结果不会再变：算一次缓存到任务上。任务中心每 3s 轮询
        # 一次 /api/bridge/tasks，不能每个轮询都去把日志读一遍。
        cached = task.get("_resolved_cache")
        if cached is not None:
            return cached
    path = task.get("log_path")
    urls = []
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for i, ln in enumerate(f):
                    if i >= _RESOLVED_HEAD_LINES:
                        break
                    m = _RE_RESOLVED_DIRECT.search(ln)
                    if m:
                        u = m.group(1).strip()
                        if u and u not in urls:
                            urls.append(u)
        except OSError:
            urls = []
    if not live:
        task["_resolved_cache"] = urls
    return urls


def task_failed_urls(task):
    """直链批量任务里**失败的那几个链接**（「只重试失败项」用）。

    只认 clawfetch 结束时打的总结块（`[失败] <url> —— <原因>`）：那是唯一带
    成败**结论**的地方。日志里另一处 `[批量] 失败：<url>` 是探测阶段的中间态，
    同一条链接后面可能重试成功 —— 拿它当结论会把已经下好的再下一遍。

    任务被取消 / 中途崩溃、压根没有总结块时返回空列表，调用方据此退回「整批重跑」。
    """
    if task.get("kind") != "direct":
        return []
    path = task.get("log_path")
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _RE_CF_FAIL.search(line)
                if not m:
                    continue
                u = m.group(1).strip()
                if u and u not in out:
                    out.append(u)
    except OSError:
        return []
    return out


# ── 「守卫主动刹车」识别 ────────────────────────────────────────────────────
# 快手侧有一道跨进程的风控守卫（ks_guard），它刹车时**刻意用退出码 0**
# （「守卫是保护，不是依赖」，不该让下载被记成失败）。但这样一来，任务中心
# 会把它显示成绿色的「已完成」——而实际上**一个文件都没下**。
# 2026-09-29 就是被这个坑住的：用户报「快手下载不了」，界面上却是成功。
# 所以这里把日志里的刹车标记认出来，单独给一个状态，并把**为什么**带到界面上。
_GUARD_BRAKE_MARK = "守卫主动刹车"
_GUARD_BRAKE_REASONS = [
    ("已经有一个快手批量任务在跑", "同一风控桶已有一个批量任务在跑（同桶同一时刻只跑一个）"),
    ("今日额度已用完", "今日额度已用完（按自然日计，明天接着跑同一个模式即可）"),
    ("冷却", "正在风控冷却期（等冷却结束再批量；单条仍可用）"),
]


def guard_brake_reason(task):
    """这次是不是「守卫主动刹车」？是就返回一句人话原因，否则返回空串。"""
    path = task.get("log_path")
    if not path or not os.path.exists(path):
        return ""
    try:
        text = tail_log(path, 120)
    except Exception:
        return ""
    if _GUARD_BRAKE_MARK not in text:
        return ""
    lines = text.splitlines()
    # 从刹车标记**往前**找最近的一条具体原因 —— 越靠近标记的越接近真正原因
    stop = len(lines) - 1
    for i, ln in enumerate(lines):
        if _GUARD_BRAKE_MARK in ln:
            stop = i
            break
    for ln in reversed(lines[:stop + 1]):
        for needle, why in _GUARD_BRAKE_REASONS:
            if needle in ln:
                return why
    return "守卫主动刹车（详见任务日志）"


# ---------- 任务进度：从任务日志解析真实进度 ----------
# ⚠️⚠️ 进度**必须来自真实输出**。以前前端把 running 的任务一律写成 50%
#     （api.js: `progress: t.status === 'running' ? 50 : 100`），于是
#     「进度条一直 50% 不动」；同时 total/success 全写 0，导致 components.js
#     的 `total > 0 ? 统计 : '等待开始'` 永远走 else —— **下载完成后还显示
#     「等待开始」**。根因不在渲染层，而在喂给它的数据是假的。
# ⚠️ 因此这里逐个工具解析各自的真实输出，解析不出来就**如实返回 None**
#     （前端渲染不确定态 + 阶段文字），绝不用常数冒充。
_RE_KS_DL = re.compile(r"\[dl\s+(\d+)\s*/\s*(\d+)\]\s+\S+\s+(OK|FAIL)\b")
_RE_KS_PRE = re.compile(r"\[pre\s+(\d+)\s*/\s*(\d+)\]")
_RE_KS_PLAN = re.compile(r"\[plan\]\s*待处理\s+(\d+)")
_RE_KS_SUM = re.compile(r"SUMMARY:\s*下载\s+(\d+)\s*/\s*(\d+),\s*预取失败\s+(\d+)")
# 哔哩哔哩：批量主页下载每个作品完成后打一行 `[bili] 完成 i/N`
_RE_BILI_DONE = re.compile(r"\[bili\] 完成\s+(\d+)\s*/\s*(\d+)")
_RE_TY_DONE = re.compile(r"\[ty\] 完成\s+(\d+)\s*/\s*(\d+)")
# ytdlp 万能下载：逐链接 成功/失败 标记（2026-11-13 起 headless 输出；
# 没有这两行时任务卡计数全 0，下载明明成功却显示「已结束，无文件产出」）
_RE_YTDLP_DONE = re.compile(r"\[ytdlp\] 完成\s+(\d+)\s*/\s*(\d+)")
_RE_YTDLP_FAIL = re.compile(r"\[ytdlp\] 失败\s+(\d+)\s*/\s*(\d+)")
_RE_KS_ROLL = re.compile(r"已收集=(\d+)")
# 阶段2「逐项点亮」的进度：几千作品的号这一阶段要跑二三十分钟，
# 没有它任务中心就一直是不确定态 + 停在阶段1 的数字上，看着像卡死。
_RE_KS_LIT = re.compile(r"阶段2\s+点亮\s+(\d+)\s*/\s*(\d+)")
_RE_KS_COLLECT = re.compile(r"\[collect\]\s*阶段\d\s*.*?[:：]\s*(?:列表项\s*)?(\d+)")
_RE_KS_WORKS = re.compile(r"作品\s+(\d+)\s*个")
_RE_KS_DONE = re.compile(r"\[done\]\s*(.+)")
_RE_DY_URLS = re.compile(r"Found\s+(\d+)\s+URL")
_RE_DY_ITEM = re.compile(
    r"\[进度\]\s*条目\s+(\d+)\s*/\s*(\d+)"
    r"(?:\s*\|\s*成功\s+(\d+)\s+失败\s+(\d+)\s+跳过\s+(\d+)(?:\s+付费\s+(\d+))?)?")
# 「付费」是单列的一桶（CENC 加密的付费/会员内容，本地解不开，不是失败）。
# 旧日志没有这一段，所以整段是可选的，解析时要判 None。
_RE_DY_TBL = re.compile(r"\b(Total|Success|Failed|Skipped|Paid)\s*[│|]\s*(\d+)")
_RE_CF_PCT = re.compile(r"\]\s*(\d+(?:\.\d+)?)\s*%")
_RE_CF_OK = re.compile(r"\[成功\]\s*文件已完整下载")
# 批量（多链接）模式：每下完一个文件 clawfetch 报一次「已完成 k/N」。
# 这是批量任务唯一可靠的进度源 —— 里面的 [成功] 只代表**某一个**文件完成。
_RE_CF_BATCH = re.compile(r"\[批量\]\s*已完成\s*(\d+)\s*/\s*(\d+)")
# 批量结束时的总结行：`[批量总结] 成功 6/11，总耗时 46:10`。
# ⚠️ 这是**唯一**能拿到「成 / 败」拆分的地方：`已完成 k/N` 只说明"处理到第几个"，
#    6 个成功 + 5 个 502 也会走完 11/11。以前没解析它，于是整批 5 个链接失败、
#    任务状态明明是 failed，卡片上却写着「下载中 11/11」、失败 0、进度 99%
#    —— 用户看到的是"全下完了"（2026-11-01 用户就是这么被误导的）。
_RE_CF_SUM = re.compile(r"\[批量总结\]\s*成功\s*(\d+)\s*/\s*(\d+)")
# 总结里逐条列出的失败：`  [失败] https://… —— 服务器错误(HTTP 502)`
_RE_CF_FAIL = re.compile(r"^\s*\[失败\]\s+(\S+)\s+——\s*(.+?)\s*$")
# 单链接任务在**探测阶段**就挂掉的写法（没有 URL、也没有批量总结）：
#   `  [失败] 链接不存在(HTTP 404)` + `  建议:文件可能已被删除…` + `  退出码:5(尝试次数:0)`
# 不认它的话，「重试」单独跑一条坏链时卡片上是 0/0、阶段空，看不出为什么失败。
_RE_CF_FAIL_ONE = re.compile(r"^\s*\[失败\]\s*(.+?)\s*$")
_RE_EXIT = re.compile(r"\[完成\]\s*退出码\s+(-?\d+)")
# 纯装饰行（表格边框、分隔线）：日志尾巴里取到它们等于什么都没说
_RE_DECOR = re.compile(r"^[\s│┌┐└┘├┤┬┴┼─╔╗╚╝║═╠╣╦╩╬=_\-]+$")

_STAGE_ZH = {
    "collect": "收集作品清单", "prefetch": "预取直链", "download": "下载中",
    "prepare": "准备", "parse": "解析链接", "done": "已结束",
}


# 日志尾行常常是框架抛的**原始异常**（`unable to open database file` 这类）。
# 手机上看到的就是一句英文报错 + 一个 traceback 片段，等于没说。这里只翻
# 「能确定判定」的硬故障；翻不了的原样返回，绝不吞掉原始信息。
# 2026-09-25：data\douyin 目录被清掉后，用户看到的就是那一句 sqlite 报错。
_FATAL_HINTS = (
    ("unable to open database file",
     "数据库打不开：抖音历史库所在目录缺失或被占用（工具箱会自动补建 "
     "data\\douyin\\；仍失败就查该目录是否只读）"),
)


def humanize_note(note):
    """把已知的原始报错翻成一句人话，拼在原文前面。"""
    if not note:
        return note
    low = note.lower()
    for needle, hint in _FATAL_HINTS:
        if needle in low:
            return "%s（原始报错：%s）" % (hint, note[:70])
    return note


def task_progress(task, tail_n=6):
    """解析任务日志，返回真实进度。

    返回 ``{total, done, failed, skipped, progress, stage, note, tail, elapsed}``。
    ⚠️ ``progress`` 为 **None** = 总量未知、算不出百分比（例如还在收集清单），
    前端此时渲染不确定态 + 阶段文字，不许拿常数冒充。
    """
    out = {"total": 0, "done": 0, "failed": 0, "skipped": 0, "paid": 0,
           "progress": None, "stage": "", "note": "", "tail": [], "elapsed": 0}
    path = task.get("log_path")
    if not path or not os.path.exists(path):
        return out

    kind = task.get("kind") or "douyin"
    total = done = failed = skipped = paid = 0
    ok_n = fail_n = 0
    pre_total = pre_done = 0
    collected = 0
    # ⚠️ 下载阶段的计数器单独记：百分比只认它。
    #    预取/收集阶段虽然也有 i/N，但那是**另一个尺度**，直接拿来当总进度会
    #    出现「预取 100% → 下载 0%」的倒退，比不动更难看；所以那阶段如实给 None。
    dl_done = dl_total = 0
    lit_done = lit_total = 0
    pct = None
    cf_batch = False
    cf_fail_reason = ""     # 批量里第一条失败的原因（挂到卡片上给用户看）
    cf_single_fail = False  # 单链接任务在探测阶段就失败（没有批量总结可解析）
    stage = ""
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return out

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _RE_KS_DL.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            dl_done, dl_total = done, total
            if m.group(3) == "OK":
                ok_n += 1
            else:
                fail_n += 1
            stage = "download"
            continue
        m = _RE_KS_PRE.search(line)
        if m:
            pre_done, pre_total = int(m.group(1)), int(m.group(2))
            stage = "prefetch"
            continue
        m = _RE_KS_SUM.search(line)
        if m:
            done, total, failed = int(m.group(1)), int(m.group(2)), int(m.group(3))
            stage = "done"
            continue
        m = _RE_BILI_DONE.search(line)
        if m:
            dl_done, dl_total = int(m.group(1)), int(m.group(2))
            ok_n += 1
            stage = "download"
            continue
        m = _RE_TY_DONE.search(line)
        if m:
            dl_done, dl_total = int(m.group(1)), int(m.group(2))
            ok_n += 1
            stage = "download"
            continue
        m = _RE_YTDLP_DONE.search(line)
        if m:
            dl_done, dl_total = int(m.group(1)), int(m.group(2))
            ok_n += 1
            stage = "download"
            continue
        m = _RE_YTDLP_FAIL.search(line)
        if m:
            dl_done, dl_total = int(m.group(1)), int(m.group(2))
            fail_n += 1
            stage = "download"
            continue
        if "[bili-live]" in line:
            stage = "record"
            continue
        m = _RE_KS_PLAN.search(line)
        if m:
            total = max(total, int(m.group(1)))
            if stage not in ("download", "done"):
                stage = "prepare"
            continue
        # ⚠️ 不 continue：阶段2 的进度行同时带「点亮 x/N」和「已收集=N」，两个都要解析
        m = _RE_KS_LIT.search(line)
        if m:
            lit_done, lit_total = int(m.group(1)), int(m.group(2))
        m = _RE_KS_ROLL.search(line) or _RE_KS_COLLECT.search(line)
        if m:
            collected = max(collected, int(m.group(1)))
            if stage not in ("download", "prefetch", "done"):
                stage = "collect"
            continue
        m = _RE_KS_WORKS.search(line)
        if m and not total:
            total = int(m.group(1))
            continue
        m = _RE_DY_ITEM.search(line)
        if m:
            total = int(m.group(2))
            dl_done, dl_total = int(m.group(1)), total
            # 抖音的进度行自带成功/失败/跳过/付费四计数，能拿到就用它（否则失败会被算成成功）
            if m.group(3) is not None:
                done, failed, skipped = int(m.group(3)), int(m.group(4)), int(m.group(5))
                if m.group(6) is not None:
                    paid = int(m.group(6))
            else:
                done = dl_done
            stage = "download"
            continue
        m = _RE_DY_TBL.search(line)
        if m:
            key, val = m.group(1), int(m.group(2))
            if key == "Total":
                total = val
            elif key == "Success":
                done = val
            elif key == "Failed":
                failed = val
            elif key == "Paid":
                paid = val
            else:
                skipped = val
            stage = "done"
            continue
        m = _RE_DY_URLS.search(line)
        if m and not total:
            total = int(m.group(1))
            continue
        m = _RE_CF_SUM.search(line)
        if m:
            # 总结行是终局数据：成/败直接以它为准，覆盖前面「已完成 k/N」的
            # "处理了几个"（那个数把失败也算进去，看着像全成功）。
            ok_sum, total_sum = int(m.group(1)), int(m.group(2))
            done, failed = ok_sum, max(0, total_sum - ok_sum)
            dl_done, dl_total = done, total_sum
            cf_batch = True
            pct = None
            stage = "done"
            continue
        m = _RE_CF_FAIL.search(line)
        if m:
            if not cf_fail_reason:
                cf_fail_reason = m.group(2).strip()
            continue
        m = _RE_CF_FAIL_ONE.search(line)
        if m:
            # 单链接探测失败（没有 URL —— 顺序在 _RE_CF_FAIL 之后，不会误吃批量总结行）
            cf_single_fail = True
            if not cf_fail_reason:
                cf_fail_reason = m.group(1).strip()
            continue
        m = _RE_CF_BATCH.search(line)
        if m:
            dl_done, dl_total = int(m.group(1)), int(m.group(2))
            # 批量模式里 [成功] 只代表某一个文件完成，上面那个分支可能刚把
            # pct 设成 100；这里清掉，让百分比改由「已完成 k/N」算。
            pct = None
            cf_batch = True
            stage = "download"
            continue
        m = _RE_CF_PCT.search(line)
        if m and kind == "direct":
            try:
                pct = float(m.group(1))
            except ValueError:
                pass
            stage = "download"
            continue
        if _RE_CF_OK.search(line):
            pct = 100.0
            stage = "done"
            continue
        if _RE_EXIT.search(line):
            stage = "done"
            continue
        if line.lstrip().startswith("[1]") or "解析链接" in line:
            if not stage:
                stage = "parse"

    # 快手：逐条 OK/FAIL 计数比 `done` 序号更准（并发下序号是「已调度」而非「已完成」）
    # ⚠️ failed 只数**真失败的那几条**，不能把「还没轮到的」也算成失败
    #    （曾经写成 fail_n + (total - ok_n - fail_n)，55 个里下完 2 个会报 53 个失败）。
    if kind == "kuaishou" and (ok_n or fail_n):
        done, failed = ok_n, fail_n
        # 百分比也改用「真完成数」：并发下 `[dl i/N]` 的 i 是调度序号，会略超前
        dl_done = ok_n + fail_n
    # ty / B站 / ytdlp：逐作品完成标记每命中一次 ok_n += 1（失败行 fail_n += 1），
    # 与快手同理，把「真完成数」落进卡片。⚠️ 以前只给 kuaishou 做了这一步——
    # ty/B站/ytdlp 下完了 total/done 仍是 0，前端就把成功任务读成「已结束，无文件产出」。
    if kind in ("ty", "bilibili", "ytdlp") and (ok_n or fail_n):
        done, failed = ok_n, fail_n
        if not total:
            total = dl_total or (ok_n + fail_n)
    # 直链批量（一次多个链接）：任务卡上的「已完成/总数」要用链接数，
    # 而不是文件内分段数 —— 用户看到的进度单位是「第几个链接」。
    if cf_batch:
        done, total = dl_done, dl_total

    out["total"] = total
    out["done"] = done
    out["failed"] = failed
    out["skipped"] = skipped
    out["paid"] = paid

    # 百分比：只在「下载阶段有真实分母」时给；跑满但进程未退出时压在 99，避免假 100%
    # ⚠️ 算不出来就留 None（前端渲染不确定态 + 阶段文字 + 已运行时长），
    #    **不许拿常数冒充** —— 以前那个写死的 50% 就是这么来的。
    if task.get("status") == "success":
        out["progress"] = 100
    elif pct is not None:
        out["progress"] = int(min(99, pct))
    elif dl_total > 0:
        out["progress"] = int(min(99, dl_done * 100.0 / dl_total))
    elif stage == "collect" and lit_total:
        # 收集阶段的最后一段「逐项点亮」有真实分母且单调前进，可以给百分比
        # （它是几千作品大号最耗时的一段，给不出数字就只能一直转不确定态）
        out["progress"] = int(min(99, lit_done * 100.0 / lit_total))

    if stage == "collect" and collected:
        out["stage"] = "%s（已收集 %d）" % (_STAGE_ZH["collect"], collected)
    elif stage == "prefetch" and pre_total:
        out["stage"] = "%s %d/%d" % (_STAGE_ZH["prefetch"], pre_done, pre_total)
    elif stage == "download" and total:
        out["stage"] = "%s %d/%d" % (_STAGE_ZH["download"], done, total)
    else:
        out["stage"] = _STAGE_ZH.get(stage, "")

    # 直链批量收尾：卡片的阶段文字直接写「完成 6/11 · 5 个失败」，
    # 别只留一个"已结束" —— 成败比阶段名重要得多。
    if cf_batch and failed:
        out["stage"] = "完成 %d/%d · %d 个失败" % (done, total, failed)
    elif cf_single_fail and not cf_batch:
        # 单链接探测就挂了：一条链接、零成功。以前这里给的是 0/0 + 空阶段，
        # 卡片上看不出发生了什么（重试单独跑一条坏链时尤其明显）。
        out["total"], out["done"], out["failed"] = 1, 0, 1
        out["stage"] = "失败 1/1"

    # 最近一条有内容的日志 + 尾部若干行（「下载过程中什么都看不到」靠这两样解决）
    # ⚠️ 过滤纯装饰行（表格边框 / 分隔线）：否则尾巴上挂一排 └──┴──┘，等于什么都没说
    body = [l.rstrip() for l in lines if l.strip() and not _RE_DECOR.match(l.strip())]
    if body:
        out["note"] = humanize_note(body[-1][:160])
        out["tail"] = [l[:160] for l in body[-tail_n:]]
    # 批量/单条有失败时，把**原因**顶到 note 上（日志最后一行往往只是边界线/建议行，
    # 说不出到底为什么）。原始日志仍在 tail/日志文件里可查。
    if (cf_batch and failed or cf_single_fail) and cf_fail_reason:
        out["note"] = humanize_note("失败原因：%s" % cf_fail_reason[:140])

    try:
        t0 = datetime.fromisoformat(task.get("created_at") or "")
    except Exception:
        t0 = None
    try:
        t1 = datetime.fromisoformat(task.get("finished_at") or "") if task.get("finished_at") else datetime.now().astimezone()
    except Exception:
        t1 = datetime.now().astimezone()
    if t0:
        out["elapsed"] = max(0, int((t1 - t0).total_seconds()))
    return out


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


async def h_authors_unfollowed(req):
    """未计入关注的作者（只在单链接下载里出现过，或被用户取消了关注）。

    单独一个端点而不是塞进 /api/bridge/authors：关注页的主列表、统计、
    「一键更新」都只该看见关注中的作者，多一个字段迟早有人拿错。
    """
    platform = "kuaishou" if (req.query.get("platform") or "").lower() == "kuaishou" else "douyin"
    if platform == "kuaishou":
        return j({"platform": platform, "authors": ks_unfollowed_authors()})
    rows = q_unfollowed_authors()
    for a in rows:
        a["nickname"] = a["author_name"]
        a["first_time"] = iso_ts(a.pop("first_t", None))
        a["last_time"] = iso_ts(a.pop("last_t", None))
    return j({"platform": platform, "authors": rows})


async def h_author_follow(req):
    """加入 / 取消关注（不删任何作品记录）。body: {platform, author, follow}"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        return j({"error": "请求体必须是 JSON"}, 400)
    platform = "kuaishou" if str(body.get("platform") or "").lower() == "kuaishou" else "douyin"
    author = body.get("author")
    follow = body.get("follow")
    if not isinstance(follow, bool):
        follow = str(follow).lower() in ("1", "true", "yes", "on")
    data, status = set_author_follow(platform, author, follow)
    if status == 200:
        print("[关注] %s %s「%s」" % ("已加入" if follow else "已取消", platform, data["author"]))
    return j(data, status)


async def h_author_delete(req):
    """删除作者信息：清掉该作者在 aweme 表里的全部记录（磁盘文件保留）。

    这是本服务唯一的写操作，前端在「关注管理 → 删除作者信息」里已做二次确认。
    返回里带回备份文件路径，便于用户事后人工回滚。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    data, status = delete_author(body.get("author"))
    if status == 200:
        print("[作者] 已删除 %s 的 %d 条作品记录（备份：%s）"
              % (data["author"], data["deleted"], data.get("backup") or "未生成"))
    return j(data, status)


async def h_aweme(req):
    q = req.query
    d = q_aweme(author=q.get("author"), aweme_type=q.get("type"),
                q=q.get("q"), limit=q.get("limit", 60), offset=q.get("offset", 0))
    attach_media(d.get("items"))    # 补 local / local_kind，前端据此显示播放按钮
    return j(d)


async def h_history(req):
    return j({"history": q_history(req.query.get("limit", 30))})


async def h_stats(req):
    return j(q_stats())


async def h_config(req):
    return j(read_config_sanitized())


# ---------- 设置写入（2026-10-03）----------
# ⚠️ 为什么要这个端点：设置页在 **bridge 模式**下（也就是实际运行时）原来整块
#    换成了「config.yml 只读表格」—— 能看、一点都不能改。原因是只有
#    `GET /api/bridge/config`，没有写入通道。于是「一键更新每人取最新」「一键更新
#    专用画质」这些设置在真实环境里**根本没法改**，只能去编辑 YAML 文件。
#
# 三条硬约束（都来自本文件已有的既定实践，不是新发明的规矩）：
#  ① **白名单**：只有 SETTABLE_KEYS 里的键能写。绝不做「前端传什么键就写什么键」——
#     那个 `/api/bridge/download` 已经会把任意 options 灌进 config.yml（行 3271），
#     再开一个无白名单的写入口等于把 config.yml 变成任意可覆盖。
#  ② **逐键最小 diff**：复用 `_set_douyin_cfg_dir` 的思路，只改目标行，
#     不整份 `yaml.safe_dump` 重排（会把 76 行配置的键序/引号风格全改掉，
#     而这份文件用户的 GUI 版也在写）。
#  ③ **先备份**：写之前留一份 `config.yml.bak`，写坏了能手工回滚。
#     写配置是唯一会破坏性改用户工程文件的动作，必须留退路。
#
# 嵌套键（`number.post` / `increase.post` 这类）用点号路径表示。

# 允许网页改的键 → 取值校验器。**刻意不含**：path（有专门的迁移流程）、
# database_path、proxy、cookies 等 —— 那些要么有副作用、要么是敏感项。
SETTABLE_KEYS = {
    # 下载
    "thread":              ("int", 1, 16),
    "retry_times":         ("int", 0, 10),
    "rate_limit":          ("int", 1, 20),
    "video_quality":       ("enum", ["original", "highest", "lowest",
                                     "1440p", "1080p", "720p", "540p", "480p", "360p"]),
    "video":               ("bool",),
    "music":               ("bool",),
    "cover":               ("bool",),
    "avatar":              ("bool",),
    "json":                ("bool",),
    # 增量与去重
    "database":            ("bool",),
    "redownload_missing_files": ("bool",),
    "increase.post":       ("bool",),
    "increase.like":       ("bool",),
    "increase.mix":        ("bool",),
    "increase.music":      ("bool",),
    "number.post":         ("int", 0, 9999),
    "number.like":         ("int", 0, 9999),
    "number.mix":          ("int", 0, 9999),
    "number.music":        ("int", 0, 9999),
    # 文件与目录
    "folderstyle":         ("bool",),
    "author_dir":          ("enum", ["nickname", "sec_uid", "nickname_uid"]),
    "filename_template":   ("str", 200),
    "download_pinned":     ("bool",),
    # 网络与兜底
    "browser_fallback.enabled":  ("bool",),
    "browser_fallback.headless": ("bool",),
    "browser_fallback.early_stop_known": ("int", 0, 1000),
    "notifications.enabled":     ("bool",),
    # 2026-11-08 设计变更：用户要求设置页**全部选项可改**，database_path /
    # proxy 进白名单（此前被刻意排除）。path（搬移产物）与 cookies 仍排除；
    # 「全部恢复默认」按钮作兜底，写前自动备份 config.yml。
    "database_path":       ("str", 300),
    "proxy":               ("str", 200),
    # 关注管理专用（2026-10-03 新增；它们是**前端**行为，不在 config.yml 里，
    # 写在这里是为了让设置页有地方存 —— 见 PATCH_WEBUI_SETTINGS）
    "creators_update_limit":    ("int", 0, 9999),
    "creators_update_quality":  ("enum", ["", "original", "highest", "lowest",
                                          "1440p", "1080p", "720p", "540p", "480p", "360p"]),
}

# 上面这几个键**不属于 config.yml**，而是网页端设置，存在 webui_settings.json。
# 分开放是为了让写入目标明确：写 config.yml 的键和写网页设置的键，
# 失败模式完全不同（前者影响所有下载器，后者只影响这个界面）。
WEBUI_ONLY_KEYS = ("creators_update_limit", "creators_update_quality")

_WEBUI_SETTINGS_PATH = os.path.join(HERE, ".data", "webui_settings.json")


def _read_webui_settings():
    try:
        with open(_WEBUI_SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_webui_settings(d):
    os.makedirs(os.path.dirname(_WEBUI_SETTINGS_PATH), exist_ok=True)
    tmp = _WEBUI_SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _WEBUI_SETTINGS_PATH)


def _coerce_scalar(raw, spec):
    """按校验器把前端传来的值转成能写进 YAML 的标量。失败抛 ValueError。"""
    kind = spec[0]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        s = str(raw).strip().lower()
        if s in ("true", "1", "on", "yes"):
            return True
        if s in ("false", "0", "off", "no"):
            return False
        raise ValueError("不是布尔值: %r" % raw)
    if kind == "int":
        try:
            n = int(str(raw).strip())
        except Exception:
            raise ValueError("不是整数: %r" % raw)
        lo, hi = spec[1], spec[2]
        if n < lo or n > hi:
            raise ValueError("超出范围 %d~%d: %d" % (lo, hi, n))
        return n
    if kind == "enum":
        s = "" if raw is None else str(raw).strip()
        if s not in spec[1]:
            raise ValueError("可选值只有 %s，收到 %r" % (spec[1], s))
        return s
    if kind == "str":
        s = "" if raw is None else str(raw)
        if len(s) > spec[1]:
            raise ValueError("长度超过 %d" % spec[1])
        return s
    raise ValueError("未知类型 %s" % kind)


def _set_yaml_scalar(key_path, value):
    """只改 config.yml 里 `key_path` 指向的那一行，**不整份重写**。

    点号路径（`increase.post`）按「父键作用域内的直接子键」匹配：
    先定位父键行，再在其作用域内（后续缩进更深的行，直到下一个顶层键）
    找 `child:` 行替换。作用域内子键名唯一（本项目的嵌套配置都满足）。

    ⚠️ 旧实现只认「子键的上一行是父键」——increase/number/browser_fallback
    块里第二个及以后的子键（like/mix/music/headless/early_stop_known…）
    全部匹配失败，表现为设置页「已保存但 config.yml 里没找到对应行，
    未写入」（2026-11-08 实测 reset 一次 missed 8 项后定位）。

    顶层键缺行（如 rate_limit：引擎按 default_config 兜底，config.yml 里
    从来没写过它）→ 在文件末尾追加一行，设置页的改动才能真正生效。
    嵌套键缺行仍放弃：往用户的配置里擅自插一个新结构风险大，如实报 missed。
    """
    parts = key_path.split(".")
    with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    lit = _yaml_literal(value)

    def write(lines_now):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines_now) + "\n")
        os.replace(tmp, CONFIG_PATH)

    if len(parts) == 1:
        pat = re.compile(r"^%s\s*:" % re.escape(parts[0]))
        for i, ln in enumerate(lines):
            if pat.match(ln):
                lines[i] = "%s: %s" % (parts[0], lit)
                write(lines)
                return True
        lines.append("%s: %s" % (parts[0], lit))
        write(lines)
        return True

    if len(parts) == 2:
        parent, child = parts
        pat_parent = re.compile(r"^%s\s*:" % re.escape(parent))
        pat_child = re.compile(r"^(\s+)%s\s*:" % re.escape(child))
        parent_i = -1
        for i, ln in enumerate(lines):
            if pat_parent.match(ln):
                parent_i = i
                break
        if parent_i < 0:
            return False
        for i in range(parent_i + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and ln[0] not in (" ", "\t"):
                break                      # 下一个顶层键：父作用域结束
            m = pat_child.match(ln)
            if m:
                lines[i] = "%s%s: %s" % (m.group(1), child, lit)
                write(lines)
                return True
        return False

    # 更深嵌套：本项目未用到，维持「如实放弃」
    return False


def _yaml_literal(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


async def h_config_save(req):
    """把设置页的改动写下去。请求体 `{"settings": {...}}`（部分提交）。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        return j({"error": "请求体必须是 JSON"}, 400)
    patch = body.get("settings")
    if not isinstance(patch, dict) or not patch:
        return j({"error": "没有要保存的项"}, 400)

    # 先全部校验，**再**动任何文件：半途失败不能留下写了一半的配置。
    plan, errs = [], []
    for k, raw in patch.items():
        spec = SETTABLE_KEYS.get(k)
        if not spec:
            errs.append("%s：不在可修改白名单内" % k)
            continue
        try:
            plan.append((k, _coerce_scalar(raw, spec)))
        except ValueError as e:
            errs.append("%s：%s" % (k, e))
    if errs:
        return j({"error": "有 %d 项没通过校验，未写入任何内容" % len(errs),
                  "details": errs}, 400)

    ykeys = [k for k, _ in plan if k not in WEBUI_ONLY_KEYS]
    wkeys = [k for k, _ in plan if k in WEBUI_ONLY_KEYS]
    if not ykeys and not wkeys:
        return j({"error": "没有可写入的项"}, 400)

    applied, missed = [], []
    try:
        if ykeys:
            if not os.path.exists(CONFIG_PATH):
                return j({"error": "未找到 config.yml"}, 404)
            # ③ 先备份：这是唯一会破坏性改用户工程文件的动作，必须留退路。
            bak = CONFIG_PATH + ".bak"
            shutil.copy2(CONFIG_PATH, bak)
            for k, v in plan:
                if k in WEBUI_ONLY_KEYS:
                    continue
                if _set_yaml_scalar(k, v):
                    applied.append(k)
                else:
                    missed.append(k)
        if wkeys:
            st = _read_webui_settings()
            for k, v in plan:
                if k in WEBUI_ONLY_KEYS:
                    st[k] = v
                    applied.append(k)
            _write_webui_settings(st)
    except Exception as e:
        return j({"error": "写入失败: %s（已备份原 config.yml 为 %s.bak）" % (e, os.path.basename(CONFIG_PATH)),
                  "applied": applied}, 500)

    out = {"ok": True, "applied": applied}
    if missed:
        # 如实说「这几项没找到对应行，没写进去」——不假装全部成功。
        out["missed"] = missed
        out["error"] = ("%d 项在 config.yml 里没找到对应行，未写入：%s"
                        % (len(missed), ", ".join(missed)))
    return j(out)


async def h_config_reset(req):
    """把设置页全部可写键恢复为工程默认值（config/default_config.py）。

    - 仅覆盖 SETTABLE_KEYS 内的键；两类键**不参与重置**：
      · path（存储位置）不在白名单（改它有专门的搬移流程）；
      · database_path 是环境相关键 —— 出厂默认「dy_downloader.db」对本工程
        是错误路径（真实库在 data/douyin/ 下），重置它会让引擎读不到库。
        可修改（在白名单里），但「恢复默认」不碰它。
    - 写前备份 config.yml（与 h_config_save 同一退路）。
    - creators_update_* 是网页端专属键，重置到前端默认（30 / 空）。
    - database_path 恢复出厂值「dy_downloader.db」后，引擎要重启/下次任务
      才按新路径读 —— 确认弹窗里已如实警示。
    """
    # 默认值包在 modules/douyin/config/ 下；CONFIG_PATH 平时就在那个目录，
    # 但测试会把 CONFIG_PATH 指到临时副本 —— 此时按 bridge_server 自身位置
    # （webui/../ = modules/douyin）兜底补 sys.path。
    roots = [os.path.dirname(CONFIG_PATH),
             os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
    for r in roots:
        if r not in sys.path:
            sys.path.insert(0, r)
    try:
        from config.default_config import DEFAULT_CONFIG  # noqa: E402
    except ImportError as e:  # noqa: BLE001
        return j({"error": "无法读取工程默认值 default_config.py：%s" % e}, 500)

    def default_of(key_path):
        cur = DEFAULT_CONFIG
        for part in key_path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None, False
            cur = cur[part]
        return cur, True

    RESET_SKIP = {"database_path"}   # 环境相关键：可改，但「恢复默认」不碰
    plan, skipped = [], []
    for k in SETTABLE_KEYS:
        if k in WEBUI_ONLY_KEYS or k in RESET_SKIP:
            continue
        v, ok = default_of(k)
        if not ok:
            skipped.append(k)
            continue
        plan.append((k, v))

    if not os.path.exists(CONFIG_PATH):
        return j({"error": "未找到 config.yml"}, 404)
    bak = CONFIG_PATH + ".bak"
    shutil.copy2(CONFIG_PATH, bak)

    applied, missed = [], []
    for k, v in plan:
        if _set_yaml_scalar(k, v):
            applied.append(k)
        else:
            missed.append(k)

    wst = _read_webui_settings()
    wst["creators_update_limit"] = 30
    wst["creators_update_quality"] = ""
    _write_webui_settings(wst)

    values = {}
    for k in SETTABLE_KEYS:
        if k in WEBUI_ONLY_KEYS:
            values[k] = 30 if k == "creators_update_limit" else ""
            continue
        if k in RESET_SKIP:
            continue
        v, ok = default_of(k)
        if ok:
            values[k] = v
    out = {"ok": True, "applied": applied, "values": values}
    if missed:
        out["missed"] = missed
    if skipped:
        out["skipped"] = skipped
    return j(out)


async def h_webui_settings(req):
    """读网页端专属设置（不属于 config.yml 的那些键）。"""
    return j({"settings": _read_webui_settings()})


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


async def h_direct_download(req):
    """直链下载（clawfetch，模块 3）：后台跑 clawfetch.py，纳入统一任务队列。

    一次可以提交**多个链接**（`urls` 数组，或 `url` 里塞多行文本）—— 会合并成
    一个批量任务交给 clawfetch：异域并行、同域自动串行。别在这里拆成 N 个任务：
    N 个进程各自开 8 连接会把弱源站直接打哑（这正是 2026-09-27 的故障形态）。

    `segments` 只接受 1（单连接，给弱源站用）；其余值一律按 auto 处理 ——
    多连接阶梯是 clawfetch 自己的自适应职责，不在这里开放，
    否则前端就多了一套要和 --segments 对齐的规则。
    `jobs`：多链接时的并发路数，1..4；给别的值或缺失一律按 auto 处理。
    """
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        return j({"error": "请求体必须是 JSON"}, 400)

    raw_urls = body.get("urls")
    if isinstance(raw_urls, list):
        urls = dedup_urls([str(u).strip() for u in raw_urls if str(u or "").strip()])
    else:
        # 兼容单链接字段：它可能是一整段文本（换行/空格分隔），照样抽出来
        urls = split_urls(str(body.get("url") or ""))
    urls = [u for u in urls if re.match(r"^https?://", u)]
    if not urls:
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    if not os.path.exists(CLAW_PY):
        return j({"error": "clawfetch 模块缺失：%s" % CLAW_PY}, 404)
    try:
        segments = int(body.get("segments") or 0)
    except (TypeError, ValueError):
        segments = 0
    segments = 1 if segments == 1 else None
    try:
        jobs = int(body.get("jobs") or 0)
    except (TypeError, ValueError):
        jobs = 0
    jobs = jobs if 1 <= jobs <= 4 else None
    loop = asyncio.get_running_loop()
    task, err = start_direct_download(urls, loop, segments, jobs)
    if err:
        return j({"error": err}, 409)
    return j({"task": task})


async def h_direct_resolve(req):
    """只解析不下载：跑 clawfetch --resolve，把链接换成直链并把输出原样返回。

    这个开关此前是**死的**：前端直接抛「请在桌面端使用 --resolve」，
    而桌面端入口（启动程序下载.bat）只负责开窗口、根本不接受链接参数 ——
    等于把用户支到一条走不通的路上。这里真正把它接上。

    clawfetch 的 --resolve 一次只处理一个链接，所以多链接时由前端逐个调用
    （每次都要真的去 probe 源站，串起来慢是正常的，不是卡住了）。
    """
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    urls = split_urls(str((body or {}).get("url") or ""))
    if not urls:
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    if not os.path.exists(CLAW_PY):
        return j({"error": "clawfetch 模块缺失：%s" % CLAW_PY}, 404)
    env = strip_proxy_env(dict(os.environ))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    def _run():
        return subprocess.run(
            [sys.executable, CLAW_PY, urls[0], "--resolve"],
            cwd=CLAW_DIR, capture_output=True, timeout=90,
            env=env, creationflags=NO_WINDOW)

    try:
        p = await asyncio.get_running_loop().run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        return j({"error": "解析超时（90 秒）：链接可能在重定向里绕圈", "url": urls[0]}, 504)
    # 成败都回 200 并把完整输出带上：clawfetch 的失败块里有原因和下一步建议，
    # 只回一句「解析失败」会把这些信息全丢掉，用户还是不知道该怎么办。
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    return j({"ok": p.returncode == 0, "output": out, "url": urls[0]})


async def h_ty_download(req):
    """TikTok / YouTube 后台下载：纳入统一任务队列（与快手串行规则无关，
    ty 任务不占登录浏览器，可与快手任务并行 —— 但仍受总并发 2 约束）。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip()
    mode = str((body.get("mode") or "all")).strip()
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    if mode.lower() not in ("all", "1") and not mode.isdigit():
        mode = "all"
    if not os.path.exists(TY_HEADLESS):
        return j({"error": "ty 模块缺失：%s" % TY_HEADLESS}, 404)
    loop = asyncio.get_running_loop()
    task, err = start_ty_download(url, mode, loop)
    if err:
        return j({"error": err}, 409)
    return j({"task": task})


# ============================================================
# yt-dlp 万能下载（模块 7）
# ------------------------------------------------------------
# ty（TikTok/YouTube）与 B 站已有专属模块，这里只收「其它一切站点」。
# 下载走统一任务队列（kind=ytdlp，见 start_ytdlp_download）；
# 预览/配置/升级是短命子进程调用，不入队。
_YTDLP_QUALITIES = ("best", "2160", "1080", "720", "480")
_YTDLP_AUDIO = ("none", "mp3", "m4a", "flac", "wav", "opus")
_YTDLP_SUBS = ("none", "write", "embed")
_YTDLP_BROWSERS = ("none", "edge", "chrome", "firefox", "brave", "opera", "vivaldi")


def _ytdlp_sane_opts(body):
    """把前端提交的选项收敛成白名单内的 dict，避免任意字符串直通命令行。"""
    o = body if isinstance(body, dict) else {}
    mode = str(o.get("mode") or "all").strip()
    if mode != "1" and mode != "all" and not mode.isdigit():
        mode = "all"
    quality = str(o.get("quality") or "best").strip()
    if quality not in _YTDLP_QUALITIES:
        quality = "best"
    audio = str(o.get("audio_format") or "none").strip().lower()
    if audio not in _YTDLP_AUDIO:
        audio = "none"
    subs = str(o.get("subs") or "none").strip().lower()
    if subs not in _YTDLP_SUBS:
        subs = "none"
    browser = str(o.get("cookies_browser") or "none").strip().lower()
    if browser not in _YTDLP_BROWSERS:
        browser = "none"
    langs = str(o.get("subs_langs") or "").strip()
    # 语言串只放行 [字母数字-_ ,]，防注入到 yt-dlp 参数
    if not re.fullmatch(r"[A-Za-z0-9_,\-. ]{0,120}", langs or ""):
        langs = "zh-CN,zh-Hans,en"
    cfile = str(o.get("cookies_file") or "").strip()
    if cfile and not os.path.isfile(cfile):
        cfile = ""
    return {
        "mode": mode, "quality": quality, "audio_format": audio, "subs": subs,
        "subs_langs": langs or "zh-CN,zh-Hans,en",
        "cookies_browser": browser, "cookies_file": cfile,
        "archive": bool(o.get("archive")), "sponsorblock": bool(o.get("sponsorblock")),
    }


async def h_ytdlp_download(req):
    """yt-dlp 万能下载提交：一个 URL 一个任务（与 ty 同口径，前端逐条提交）。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    if not os.path.exists(YTDLP_HEADLESS):
        return j({"error": "ytdlp 模块缺失：%s" % YTDLP_HEADLESS}, 404)
    opts = _ytdlp_sane_opts(body.get("opts") or body)
    loop = asyncio.get_running_loop()
    task, err = start_ytdlp_download(url, opts, loop)
    if err:
        return j({"error": err}, 409)
    return j({"task": task})


async def h_ytdlp_preview(req):
    """下载前预览：跑 ytdlp_headless.py preview（子进程，stdout 出 JSON）。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    url = ((body or {}).get("url") if isinstance(body, dict) else "") or ""
    url = url.strip()
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    if not os.path.exists(YTDLP_HEADLESS):
        return j({"error": "ytdlp 模块缺失：%s" % YTDLP_HEADLESS}, 404)
    exe = ks_python("python") or ks_python("pythonw")
    if not exe:
        return j({"error": "kuaishou-env 运行时缺失（yt-dlp 装在该环境）"}, 404)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["YTDLP_CONF"] = YTDLP_CONF

    def _run():
        return subprocess.run(
            [exe, "-u", YTDLP_HEADLESS, "preview", url],
            cwd=YTDLP_DIR, capture_output=True, timeout=220,
            env=env, creationflags=NO_WINDOW)

    try:
        p = await asyncio.get_running_loop().run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        return j({"error": "预览超时（220 秒）：站点响应慢或需要代理", "url": url}, 504)
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    # stdout 最后一行才是 preview JSON（前面的行是探测日志）
    payload = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    if payload is None:
        return j({"ok": False, "error": (out.strip().splitlines() or ["解析失败"])[-1],
                  "raw": out[-2000:]}, 200)
    return j(payload)


async def h_ytdlp_conf_get(req):
    """高级配置（yt-dlp 原生 conf）：读取 data/ytdlp/yt-dlp.conf。"""
    data = ""
    if os.path.exists(YTDLP_CONF):
        try:
            with open(YTDLP_CONF, "r", encoding="utf-8", errors="replace") as f:
                data = f.read()
        except Exception as e:
            return j({"error": str(e)}, 500)
    return j({"path": YTDLP_CONF, "exists": os.path.exists(YTDLP_CONF), "content": data})


async def h_ytdlp_conf_save(req):
    """保存高级配置。覆写前留 .bak —— 高级用户手编的东西不能被一次误存抹掉。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    content = str((body or {}).get("content") if isinstance(body, dict) else "") or ""
    if len(content) > 65536:
        return j({"error": "配置内容过大（>64KB）"}, 400)
    os.makedirs(os.path.dirname(YTDLP_CONF), exist_ok=True)
    if os.path.exists(YTDLP_CONF):
        try:
            shutil.copyfile(YTDLP_CONF, YTDLP_CONF + ".bak")
        except Exception:
            pass
    tmp = YTDLP_CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp, YTDLP_CONF)
    return j({"ok": True, "path": YTDLP_CONF})


async def h_ytdlp_update(req):
    """在 kuaishou-env 内升级 yt-dlp（pip install -U yt-dlp）。"""
    exe = ks_python("python") or ks_python("pythonw")
    if not exe:
        return j({"error": "kuaishou-env 运行时缺失"}, 404)
    if not os.path.exists(YTDLP_HEADLESS):
        return j({"error": "ytdlp 模块缺失：%s" % YTDLP_HEADLESS}, 404)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    def _run():
        return subprocess.run(
            [exe, "-u", YTDLP_HEADLESS, "update"],
            cwd=YTDLP_DIR, capture_output=True, timeout=600,
            env=env, creationflags=NO_WINDOW)

    try:
        p = await asyncio.get_running_loop().run_in_executor(None, _run)
    except subprocess.TimeoutExpired:
        return j({"error": "升级超时（600 秒）：网络不通或 PyPI 响应慢"}, 504)
    out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
    version = ""
    m = re.search(r"(\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?)", out)
    if m:
        version = m.group(1)
    return j({"ok": p.returncode == 0, "version": version, "output": out[-4000:]})


# ============================================================
# Stripchat 直播录制（模块 6）
# ------------------------------------------------------------
# 与 ty/direct 的本质差别：录制是**长驻进程**（几小时起步），塞进
# 「总并发 2」的统一任务队列会把普通下载全堵住，所以这里单独做进程监护：
#   start → 拉起独立录制进程（隐藏窗口，日志落 data/stripchat/recorder.log）
#   stop  → 先写停止文件让录制器**优雅收尾**（补 #EXT-X-ENDLIST + ffmpeg 合并），
#           等不到再 terminate —— 直接杀会留一堆没收尾的分片，那是废品。
#   status→ 进程存活 + 日志尾部，网页 3 秒轮询一次。
# 一次只录一个房间：同时录两个房间会抢同一个 stop 文件与日志，语义全乱。
_SC_PROC = None            # subprocess.Popen（bridge 进程内单例，重启桥接即失联判定）
_SC_INFO = {}              # 展示元数据：url/user/vr/pid/started_at/outdir

# 与录制器一致的 5 种房间模式（vr + 主站 + 三个白标镜像），入口处先拦一道
_SC_URL_RE = re.compile(
    r"^https?://(?:vr\.stripchat\.com/cam/"
    r"|([\w-]+\.)?stripchat\.com/(?!cam/)"
    r"|([\w-]+\.)?stripol\.com/"
    r"|([\w-]+\.)?camwhorestv\.info/"
    r"|(?:www\.)?xhamsterlive\.com/)[\w.-]+/?$", re.IGNORECASE)


def _sc_alive():
    """本桥监护的进程活着，或认领到的孤儿进程（pidfile 登记）还活着。"""
    if _SC_PROC is not None and _SC_PROC.poll() is None:
        return True
    pid = _sc_discover()
    return pid is not None


def _win_pid_alive(pid):
    """探测任意 pid 是否存活。Windows 不许 os.kill(pid, 0)（会真杀进程），
    用 OpenProcess + GetExitCodeProcess 探测；非 Windows 才用 os.kill 探测。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            STILL_ACTIVE = 259
            h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    else:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def _sc_discover():
    """认领：桥接重启后，从录制器留下的 session.json / recorder.pid 找回在跑的会话。

    返回活着的 pid（并顺手恢复 _SC_INFO 供展示），没有则 None。
    文件过期（进程已退、录制器来不及清理）时顺带清掉，下次不再白读。"""
    global _SC_INFO
    pid = None
    try:
        with open(SC_PIDFILE, "r", encoding="ascii") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if not _win_pid_alive(pid):
        for p in (SC_PIDFILE, SC_SESSION):
            try:
                os.remove(p)
            except OSError:
                pass
        return None
    if not _SC_INFO:
        try:
            with open(SC_SESSION, "r", encoding="utf-8") as f:
                info = json.load(f)
            if isinstance(info, dict):
                info.setdefault("outdir", _init_tool_dirs().get("stripchat"))
                info["adopted"] = True   # 前端可据此提示「监护经重启恢复」
                _SC_INFO = info
        except (OSError, ValueError):
            _SC_INFO = {"pid": pid, "adopted": True,
                        "outdir": _init_tool_dirs().get("stripchat")}
    return pid


def _sc_state(running):
    return {
        "running": running,
        "info": _SC_INFO or None,
        "log": _sc_tail(120),
        "outdir": _init_tool_dirs().get("stripchat"),
    }


def _sc_tail(n=120):
    """日志尾部 n 行。读不到（还没录过）返回空列表，不报错。"""
    try:
        with open(SC_LOG, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-n:]]
    except Exception:
        return []


async def h_stripchat_start(req):
    """开始录制：校验 URL → 拉起隐藏录制进程。已在录 → 409（先停再录）。

    418 自愈：录制器被反爬拦截时以退出码 3 结束，看护任务随即唤起
    真实 Edge（playwright，kuaishou-env）访问房间页抓取 Cookie，
    然后带 Cookie 自动重试一次 —— 用户看到的只是弹了一个小浏览器窗口。"""
    global _SC_PROC
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip().rstrip("/")
    if not _SC_URL_RE.match(url):
        return j({"error": "不支持的房间链接（支持 stripchat / vr.stripchat / "
                           "xhamsterlive / stripol / camwhorestv）"}, 400)
    if _sc_alive():
        return j({"error": "已有录制在进行（一次一个房间），请先停止", "state": _sc_state(True)}, 409)
    if not os.path.exists(SC_RECORDER):
        return j({"error": "录制器缺失：%s" % SC_RECORDER}, 404)

    outdir = _init_tool_dirs().get("stripchat")
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(SC_STATE_DIR, exist_ok=True)
    # 清掉上一轮的停止文件与登记（录制器启动后会重写登记）；日志截断，
    # 网页日志窗只看本轮
    for p in (SC_STOP, SC_PIDFILE, SC_SESSION):
        try:
            os.remove(p)
        except OSError:
            pass
    try:
        open(SC_LOG, "w", encoding="utf-8").close()
    except OSError:
        pass

    # Cookie 新鲜（45 分钟内）就直接带上首跑，省一轮 418 往返
    use_cookies = False
    try:
        if time.time() - os.path.getmtime(SC_COOKIES) < 45 * 60:
            use_cookies = True
            _sc_logline("检测到 %d 分钟内抓取的浏览器 Cookie，直接使用" %
                        int((time.time() - os.path.getmtime(SC_COOKIES)) / 60))
    except OSError:
        pass

    proc = _sc_spawn(url, outdir, with_cookies=use_cookies)
    if proc is None:
        return j({"error": "启动录制失败（详见日志）"}, 500)
    asyncio.create_task(_sc_autoretry(proc, url, outdir))
    return j({"ok": True, "state": _sc_state(True)})


def _sc_spawn(url, outdir, with_cookies):
    """拉起录制进程（隐藏窗口），登记会话信息；失败返回 None。

    解释器优先 kuaishou-env（带 playwright —— 观众模式需要）；
    没有就退回桥接自己的 Python（录制器会自动降级直连模式）。"""
    global _SC_PROC, _SC_INFO
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    exe = ks_python("pythonw") or ks_python("python") or sys.executable
    cmd = [exe, "-u", SC_RECORDER, url,
           "--out", outdir, "--stop-file", SC_STOP, "--log", SC_LOG]
    if with_cookies and os.path.exists(SC_COOKIES):
        cmd += ["--cookies-file", SC_COOKIES]
    try:
        _SC_PROC = subprocess.Popen(cmd, cwd=SC_DIR, creationflags=flags)
    except Exception as e:
        _sc_logline("[错误] 启动录制进程失败：%s" % e)
        return None
    m = re.search(r"([\w.-]+)/?$", url)
    _SC_INFO = {"url": url, "user": m.group(1) if m else "", "vr": "vr." in url,
                "pid": _SC_PROC.pid,
                "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "outdir": outdir}
    return _SC_PROC


def _sc_logline(msg):
    """把桥接自己的编排步骤写进录制日志，网页日志窗据此展示完整故事。"""
    try:
        with open(SC_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except OSError:
        pass


async def _sc_autoretry(proc, url, outdir):
    """看护：录制器因 418（退出码 3）退出时，抓浏览器 Cookie 并重试一次。"""
    loop = asyncio.get_running_loop()
    try:
        rc = await loop.run_in_executor(None, proc.wait)
    except Exception:
        return
    # 只在看护对象仍是当前会话时动作（用户可能已停录/重开）
    if _SC_PROC is not proc or rc != 3:
        return
    _sc_logline("正在唤起 Edge 访问房间页抓取 Cookie（会弹出一个小窗口，约 15~40 秒）…")
    ks_py = ks_python("python") or ks_python("pythonw")
    if not ks_py or not os.path.exists(SC_GRABBER):
        _sc_logline("[错误] 无法自动抓取 Cookie（缺 kuaishou-env 的 playwright 或 grab_cookies.py）")
        _SC_PROC = None
        return
    try:
        grab = await asyncio.create_subprocess_exec(
            ks_py, SC_GRABBER, url, SC_COOKIES,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except Exception as e:
        _sc_logline("[错误] 唤起浏览器失败：%s" % e)
        _SC_PROC = None
        return
    try:
        _, err = await asyncio.wait_for(grab.communicate(), timeout=120)
    except asyncio.TimeoutError:
        grab.kill()
        _sc_logline("[错误] 浏览器抓取超时（120 秒），本轮放弃")
        _SC_PROC = None
        return
    if grab.returncode != 0 or not os.path.exists(SC_COOKIES):
        why = (err or b"").decode("utf-8", "replace").strip()[-200:]
        _sc_logline("[错误] Cookie 抓取失败：%s" % (why or "未知原因"))
        _SC_PROC = None
        return
    _sc_logline("Cookie 抓取成功，带 Cookie 重新开始录制…")
    _sc_spawn(url, outdir, with_cookies=True)
    # 重试不再看护：如果带 Cookie 仍 418，说明拦截升级，如实留在日志里即可


async def h_stripchat_stop(req):
    """停止录制：停止文件 → 最多等 30 秒优雅收尾（ENDLIST+ffmpeg 合并）→ 兜底强杀。

    两种监护形态都要覆盖：本桥亲手拉起的（有 Popen 句柄，terminate 可用），
    以及桥接重启后凭 pidfile 认领的孤儿（没有句柄，只能 taskkill 兜底）。"""
    global _SC_PROC
    adopted_pid = None
    if not (_SC_PROC is not None and _SC_PROC.poll() is None):
        adopted_pid = _sc_discover()
        if adopted_pid is None:
            _SC_PROC = None
            return j({"ok": True, "note": "没有正在进行的录制", "state": _sc_state(False)})
    try:
        open(SC_STOP, "w").close()
    except OSError:
        pass

    def _gone():
        if _SC_PROC is not None:
            return _SC_PROC.poll() is not None
        return not _win_pid_alive(adopted_pid)

    deadline = time.time() + 30
    while time.time() < deadline and not _gone():
        await asyncio.sleep(0.5)
    graceful = True
    if not _gone():
        graceful = False
        if _SC_PROC is not None:
            _SC_PROC.terminate()
            try:
                _SC_PROC.wait(timeout=10)
            except Exception:
                _SC_PROC.kill()
        else:
            import subprocess
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            subprocess.run(["taskkill", "/F", "/PID", str(adopted_pid)],
                           capture_output=True, creationflags=flags)
    _SC_PROC = None
    for p in (SC_STOP, SC_PIDFILE, SC_SESSION):
        try:
            os.remove(p)
        except OSError:
            pass
    return j({"ok": True, "graceful": graceful, "state": _sc_state(False)})


async def h_stripchat_status(req):
    return j(_sc_state(_sc_alive()))


async def h_stripchat_open_dir(req):
    """打开录像目录 —— 与 h_tool_open_dir 同口径：不存在如实 404，不顺手创建。"""
    target = _init_tool_dirs().get("stripchat")
    if not target or not os.path.isdir(target):
        return j({"error": "目录还不存在（尚未录制过）", "path": target}, status=404)
    try:
        if os.name == "nt":
            _explorer_open_front(target)
        else:
            import subprocess
            subprocess.Popen(["xdg-open", target])
        return j({"ok": True, "path": target})
    except Exception as e:
        return j({"error": "打开失败: %s" % e}, status=500)


async def h_kuaishou_launch(req):
    """唤起桌面快手一键下载器，并把链接自动填入并开始下载。

    快手下载强依赖 playwright + msedge + 登录态（未登录会弹扫码），
    无法像 clawfetch 那样纯后台跑；这里唤起 GUI 并把链接经命令行参数传进去，
    由下载器自动填入输入框并自动开始，用户无需手动粘贴。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip()
    exe = ks_python("pythonw") or ks_python("python")
    if not exe:
        return j({"error": "快手运行时缺失：%s" % KS_RUNTIME}, status=404)
    if not os.path.exists(KS_PYW):
        return j({"error": "快手下载器缺失：%s" % KS_PYW}, status=404)
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    cmd = [exe, KS_PYW]
    if url:
        cmd.append(url)
    env = dict(os.environ)
    ks_out = _init_tool_dirs().get("kuaishou")
    if ks_out:
        env["KS_OUTDIR"] = ks_out
        os.makedirs(ks_out, exist_ok=True)
    try:
        subprocess.Popen(cmd, cwd=KS_TOOLS, creationflags=flags, env=env)
        return j({"ok": True, "launched": True, "url": url})
    except Exception as e:
        return j({"error": "启动失败: %s" % e}, status=500)


async def h_kuaishou_status(req):
    """快手登录态 + 模块就绪状态（只读，cookie 只报布尔）。"""
    exe = ks_python("python") or ks_python("pythonw")
    return j({
        "module_ok": os.path.exists(KS_HEADLESS),
        "runtime_ok": bool(exe),
        "logged_in": ks_login_ok(),
    })


# ---------- 快手本地作品库的 HTTP 面（作品档案 / 关注管理的快手标签用） ----------
async def h_ks_works(req):
    q = req.query
    return j(ks_works(author=q.get("author"), q=q.get("q"),
                      limit=q.get("limit", 60), offset=q.get("offset", 0)))


async def h_ks_authors(req):
    return j({"authors": ks_authors()})


async def h_ks_stats(req):
    return j(ks_stats())


async def h_ks_media(req):
    """快手的本地文件流（给 <video> 用）。

    ⚠️ 与抖音侧同一套安全边界：**只接受 photoId**，真实路径一律由服务端台账给出。
    若把路径当参数收，这个接口就是「读本机任意文件」的漏洞。
    ⚠️ 显式发 Accept-Ranges：aiohttp 的 FileResponse 会处理 Range 请求，
    但不发这个头，浏览器可能据此认为不能拖进度条。
    """
    pid = (req.query.get("id") or "").strip()
    if not pid:
        return j({"error": "缺少 id"}, status=400)
    ent = ks_media_lookup(pid)
    if not ent:
        return j({"error": "台账里没有这个作品，或文件已不在磁盘上", "id": pid}, status=404)
    path = ent["file"]
    if not os.path.isfile(path):
        return j({"error": "文件不存在或已被移动", "id": pid}, status=404)
    filename = os.path.basename(path)
    return web.FileResponse(path, headers={
        "Content-Type": MEDIA_MIME.get(os.path.splitext(path)[1].lower(),
                                       "application/octet-stream"),
        "Accept-Ranges": "bytes",
        "Content-Disposition": "inline; filename*=UTF-8''%s" % quote(filename),
        "Cache-Control": "no-store",
    })


async def h_ks_media_reveal(req):
    """在资源管理器里定位该作品的文件（选中，而不是只打开目录）。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    pid = str((body or {}).get("id") or "").strip()
    ent = ks_media_lookup(pid) if pid else None
    if not ent:
        return j({"error": "本地未找到该作品的文件", "id": pid}, status=404)
    try:
        if os.name == "nt":
            _explorer_open_front(ent["file"], select=True)
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(ent["file"])])
        return j({"ok": True, "path": ent["file"]})
    except Exception as e:
        return j({"error": "打开失败: %s" % e}, status=500)


async def h_ks_author_delete(req):
    """从快手台账里移除一位作者的全部记录（先备份，磁盘文件不动）。"""
    try:
        body = await req.json()
    except Exception:
        body = {}
    author = str((body or {}).get("author") or "").strip()
    if not author:
        return j({"error": "作者名不能为空"}, status=400)
    try:
        res, code = ks_delete_author(author)
    except Exception as e:
        return j({"error": "删除失败: %s" % e}, status=500)
    return j(res, status=code)


async def h_ks_author_profile(req):
    """记下/改掉某位作者的主页链接。

    自动推断靠 `works_list_<作者ID>.json` 的作品 ID 交集，只对有清单文件的作者有效
    （例如「郁闷装深沉」那份是无后缀的 works_list.json，拿不到 ID）。
    这条接口是兜底：用户手动贴一次主页链接，之后「更新」就能用了。
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    author = str((body or {}).get("author") or "").strip()
    url = str((body or {}).get("url") or "").strip()
    if not author:
        return j({"error": "作者名不能为空"}, status=400)
    norm, pid, err = _ks_profile_entry(url)
    if err:
        return j({"error": err}, status=400)
    data = _ks_profile_map_file()
    data[author] = norm
    try:
        os.makedirs(KS_PRIMARY_ROOT, exist_ok=True)
        tmp = KS_PROFILE_MAP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, KS_PROFILE_MAP)
    except Exception as e:
        return j({"error": "保存失败: %s" % e}, status=500)
    ks_index(force=True)
    return j({"ok": True, "author": author, "profileId": pid,
              "profileUrl": norm,
              # 短链没有 pid 可校验，前端可以据此提示"已保存，更新时会自动跟随跳转"
              "resolved": bool(pid)})


async def h_kuaishou_login(req):
    """唤起快手扫码登录窗口（kuaishou_login.py，会弹 msedge 窗口，用户扫码后登录态落盘）。
    这是「无缝」链路里唯一需要用户看浏览器的一步，且只需一次。"""
    exe = ks_python("pythonw") or ks_python("python")
    login_py = os.path.join(KS_TOOLS, "kuaishou_login.py")
    if not exe:
        return j({"error": "快手运行时缺失：%s" % KS_RUNTIME}, status=404)
    if not os.path.exists(login_py):
        return j({"error": "登录脚本缺失：%s" % login_py}, status=404)
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        subprocess.Popen([exe, "-u", login_py, "600"], cwd=KS_TOOLS, creationflags=flags)
        return j({"ok": True, "launched": True, "message": "已打开快手扫码登录窗口，扫码完成后自动保存登录态"})
    except Exception as e:
        return j({"error": "启动失败: %s" % e}, status=500)


async def h_kuaishou_download(req):
    """快手后台下载：纳入统一任务队列，进度进任务中心，不弹桌面窗口。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip()
    mode = body.get("mode") or "all"
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    # ⚠️ mode 也要放行：纯数字 =「只下最新 N 个」，`collect` =「只扫清单不下」。
    #    曾经只认 all/1 → 想限量或只想摸清家底的请求被静默改成 all（大号直接开下几千个）。
    mode = str(mode).strip()
    # mode 白名单：all / 1 / collect / resume / 纯数字（限量 N 个）。
    # ⚠️ `resume` 是 2026-09-25 加的「断点续跑」——大号唯一能跑完的姿势，见 headless 的说明。
    if mode.lower() not in ("all", "1", "collect", "resume") and not mode.isdigit():
        mode = "all"
    if not os.path.exists(KS_HEADLESS):
        return j({"error": "快手下载脚本缺失：%s" % KS_HEADLESS}, 404)
    # 未登录直接拒绝，让前端引导扫码（后台跑会在 get_works 后再查登录态，这里提前拦截给即时反馈）
    if not ks_login_ok():
        return j({"error": "未登录快手", "need_login": True}, 401)
    loop = asyncio.get_running_loop()
    task, err = start_kuaishou_download(url, mode, loop)
    if err:
        return j({"error": err}, 409)
    return j({"task": task})


async def h_bilibili_download(req):
    """哔哩哔哩后台下载：URL 自动识别单作品 / 主页批量 / 直播录制。

    mode: 'all'（默认，主页=全部投稿）/ 纯数字 N（主页限量前 N 个）/
    'audio'（单作品仅音频 mp3）/ 'live'（强制直播录制）。
    """
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        body = {}
    url = (body.get("url") or "").strip()
    mode = str((body.get("mode") or "all")).strip() or "all"
    if not re.match(r"^https?://", url):
        return j({"error": "url 必须是 http(s) 链接"}, 400)
    if mode.lower() not in ("all", "audio", "live") and not mode.isdigit():
        mode = "all"
    if not os.path.exists(BILI_HEADLESS):
        return j({"error": "哔哩哔哩下载脚本缺失：%s" % BILI_HEADLESS}, 404)
    loop = asyncio.get_running_loop()
    task, err = start_bilibili_download(url, mode, loop)
    if err:
        return j({"error": err}, 409)
    return j({"task": task})


def _bili_cookie_path():
    return os.path.join(BILI_DIR, ".cookies.json")


def _mask_cookie(raw):
    """脱敏展示：每个分段的值只留前 4 后 2 位。"""
    parts = []
    for pair in str(raw or "").split(";"):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        v = v.strip()
        shown = (v[:4] + "..." + v[-2:]) if len(v) > 10 else "***"
        parts.append("%s=%s" % (k.strip(), shown))
    return "; ".join(parts)


def _write_bili_cookie(cookie, uname="", mid=0):
    """落盘到 modules/bilibili/.cookies.json（bilibili_headless 每次任务读它）。"""
    path = _bili_cookie_path()
    payload = {"cookie": cookie, "updated_at": now_iso()}
    if uname:
        payload["uname"] = uname
    if mid:
        payload["mid"] = mid
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return payload


def _read_bili_cookie_file():
    try:
        with open(_bili_cookie_path(), "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


async def h_bili_cookie_get(req):
    """查看哔哩哔哩 Cookie（脱敏）。"""
    data = _read_bili_cookie_file()
    cookie = str(data.get("cookie") or "")
    return j({
        "has_cookie": bool(cookie),
        "cookie_masked": _mask_cookie(cookie),
        "updated_at": data.get("updated_at") or "",
        "uname": data.get("uname") or "",
        "mid": data.get("mid") or 0,
        "source": "file",
    })


async def h_bili_cookie_set(req):
    """编辑/更新哔哩哔哩 Cookie（整段覆盖；空串=清除）。"""
    try:
        body = await req.json()
    except Exception:
        return j({"error": "请求体必须是 JSON"}, 400)
    if not isinstance(body, dict):
        return j({"error": "请求体必须是 JSON"}, 400)
    cookie = str(body.get("cookie") or "").strip().lstrip("Cookie:").strip()
    path = _bili_cookie_path()
    if not cookie:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return j({"ok": True, "has_cookie": False, "updated_at": ""})
    # 基本合法性：至少要有一个 key=value 分段
    if "=" not in cookie:
        return j({"error": "Cookie 格式无效：应形如 SESSDATA=xxx; buvid3=yyy"}, 400)
    payload = _write_bili_cookie(cookie)
    return j({"ok": True, "has_cookie": True,
              "cookie_masked": _mask_cookie(cookie), "updated_at": payload["updated_at"]})


# ── 哔哩哔哩扫码登录 ────────────────────────────────────────────────────────
# 走官方 passport 的二维码登录接口，**不需要浏览器**（抖音/快手那套要 Playwright
# 抠码，是因为它们没有公开的扫码接口）：
#   generate → 拿 qrcode_key + 登录页 url（url 本身就是二维码内容）
#   poll     → 86101 未扫 / 86090 已扫码待确认 / 0 成功（响应头 Set-Cookie 里就是登录态）
#              / 86038 已过期
# 二维码图片由本工程自带的 qr_svg 渲染（零依赖），前端直接内嵌 SVG。
# 成功时 Cookie 立刻落盘 —— 也就是「扫码获取 Cookie」，用户不用再去 F12 里抄。

BILI_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")
BILI_QR_SESSIONS = {}       # sid -> {key, created_at, state, uname, mid}
BILI_QR_TTL = 300           # 会话最多保留 5 分钟（二维码本身 180 秒过期）
_BILI_COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5",
                     "sid", "buvid3", "buvid4", "b_nut")
_BILI_QR_TIMEOUT = 20


def _bili_headers(extra=None):
    h = {"User-Agent": BILI_UA,
         "Referer": "https://www.bilibili.com/",
         "Origin": "https://www.bilibili.com"}
    if extra:
        h.update(extra)
    return h


def _bili_qr_purge():
    now = time.time()
    for k in [k for k, v in BILI_QR_SESSIONS.items()
              if now - float(v.get("created_at") or 0) > BILI_QR_TTL]:
        BILI_QR_SESSIONS.pop(k, None)


async def _bili_qr_generate():
    import aiohttp
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
    async with aiohttp.ClientSession(
            headers=_bili_headers(),
            timeout=aiohttp.ClientTimeout(total=_BILI_QR_TIMEOUT)) as s:
        async with s.get(url, params={"source": "main-fe-header"}) as r:
            data = await r.json(content_type=None)
    if (data or {}).get("code") != 0:
        raise RuntimeError((data or {}).get("message") or "二维码生成失败")
    d = (data or {}).get("data") or {}
    if not d.get("url") or not d.get("qrcode_key"):
        raise RuntimeError("接口未返回二维码内容")
    return d["url"], d["qrcode_key"]


def _cookie_from_set_cookie(headers):
    """从 Set-Cookie 里挑出登录态字段，拼成 `k=v; k=v` 的 Cookie 头。"""
    out = {}
    for raw in headers:
        first = (raw or "").split(";", 1)[0].strip()
        if "=" not in first:
            continue
        k, v = first.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in _BILI_COOKIE_KEYS and v:
            out[k] = v
    # 顺序固定，便于人眼核对与脱敏展示
    return "; ".join("%s=%s" % (k, out[k]) for k in _BILI_COOKIE_KEYS if k in out)


async def _bili_qr_poll(key):
    """返回 (state, cookie, message)。state ∈ waiting/scanned/confirmed/expired。"""
    import aiohttp
    url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
    async with aiohttp.ClientSession(
            headers=_bili_headers(),
            timeout=aiohttp.ClientTimeout(total=_BILI_QR_TIMEOUT)) as s:
        async with s.get(url, params={"qrcode_key": key,
                                      "source": "main-fe-header"}) as r:
            raw_headers = r.headers.getall("Set-Cookie", [])
            data = await r.json(content_type=None)
    d = (data or {}).get("data") or {}
    code = d.get("code")
    if code == 0:
        return "confirmed", _cookie_from_set_cookie(raw_headers), ""
    if code == 86101:
        return "waiting", "", "等待扫码"
    if code == 86090:
        return "scanned", "", "已扫码，请在手机上确认"
    if code == 86038:
        return "expired", "", "二维码已过期"
    return "waiting", "", str(d.get("message") or (data or {}).get("message") or "")


async def _bili_ensure_buvid(cookie):
    """扫码接口不给 buvid3，而部分清晰度/接口要它，缺了就补一个。"""
    have = {p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p}
    if "buvid3" in have:
        return cookie
    try:
        import aiohttp
        async with aiohttp.ClientSession(
                headers=_bili_headers(),
                timeout=aiohttp.ClientTimeout(total=_BILI_QR_TIMEOUT)) as s:
            async with s.get("https://api.bilibili.com/x/frontend/finger/spi") as r:
                data = await r.json(content_type=None)
        d = (data or {}).get("data") or {}
    except Exception:
        return cookie
    extra = []
    if d.get("b_3"):
        extra.append("buvid3=" + str(d["b_3"]))
    if d.get("b_4"):
        extra.append("buvid4=" + str(d["b_4"]))
    if not extra:
        return cookie
    return (cookie + "; " + "; ".join(extra)).strip("; ")


async def _bili_nav(cookie):
    """核一次登录态并拿昵称。返回 isLogin=None 表示「查不到」，不当作失败。"""
    try:
        import aiohttp
        async with aiohttp.ClientSession(
                headers=_bili_headers({"Cookie": cookie}),
                timeout=aiohttp.ClientTimeout(total=_BILI_QR_TIMEOUT)) as s:
            async with s.get("https://api.bilibili.com/x/web-interface/nav") as r:
                data = await r.json(content_type=None)
        d = (data or {}).get("data") or {}
        return {"isLogin": bool(d.get("isLogin")),
                "uname": d.get("uname") or "",
                "mid": d.get("mid") or 0}
    except Exception:
        return {"isLogin": None, "uname": "", "mid": 0}


async def h_bili_qr_start(req):
    """生成扫码登录二维码（返回会话 id + SVG）。"""
    _bili_qr_purge()
    try:
        qr_url, key = await _bili_qr_generate()
    except Exception as e:
        return j({"error": "二维码获取失败：%s" % str(e)[:120]}, 502)
    try:
        svg = qr_svg.qr_svg(qr_url, ec="M", scale=6, border=2)
    except Exception as e:
        return j({"error": "二维码渲染失败：%s" % str(e)[:80]}, 500)
    sid = uuid.uuid4().hex[:12]
    BILI_QR_SESSIONS[sid] = {"key": key, "created_at": time.time(),
                             "state": "waiting", "uname": "", "mid": 0}
    return j({"session": sid, "svg": svg, "expires_in": 180})


async def h_bili_qr_poll(req):
    """轮询扫码结果；成功即落盘 Cookie。"""
    sid = (req.query.get("session") or "").strip()
    sess = BILI_QR_SESSIONS.get(sid)
    if not sess:
        return j({"state": "expired", "message": "二维码已失效，请刷新"})
    try:
        state, cookie, msg = await _bili_qr_poll(sess["key"])
    except Exception as e:
        # 网络抖动不该把会话打死：原状态返回，前端下一轮继续问
        return j({"state": sess.get("state") or "waiting",
                  "message": "轮询失败：%s" % str(e)[:80]})
    if state != "confirmed":
        sess["state"] = state
        return j({"state": state, "message": msg})

    if "SESSDATA" not in cookie:
        sess["state"] = "failed"
        return j({"state": "failed", "message": "接口未返回登录 Cookie，请重试"})
    cookie = await _bili_ensure_buvid(cookie)
    info = await _bili_nav(cookie)
    if info.get("isLogin") is False:
        sess["state"] = "failed"
        return j({"state": "failed", "message": "登录未生效（Cookie 无效），请重试"})
    payload = _write_bili_cookie(cookie, info.get("uname") or "", info.get("mid") or 0)
    sess.update(state="confirmed", uname=info.get("uname") or "", mid=info.get("mid") or 0)
    return j({"state": "confirmed",
              "uname": info.get("uname") or "",
              "mid": info.get("mid") or 0,
              "cookie_masked": _mask_cookie(cookie),
              "updated_at": payload["updated_at"]})


async def h_bili_qr_cancel(req):
    sid = (req.query.get("session") or "").strip()
    BILI_QR_SESSIONS.pop(sid, None)
    return j({"ok": True})


# ---------- 任务中心持久化（2026-11-08）----------
# TASKS 原本只在内存：桥接一重启全部任务消失，任务中心只剩抖音的
# download_history（持久表）—— 用户看到的就是「只显示抖音下载历史」。
# 现在落盘到 .data/tasks.json：创建/状态变化由 3s 节流循环写盘；
# 启动时恢复，非终态（running/pending）标记为 failed 并在日志里注明。

_TASKS_SNAP = os.path.join(HERE, ".data", "tasks.json")


def _tasks_snapshot():
    out = {}
    for tid, t in TASKS.items():
        it = dict(t)
        # ⚠️ log_path 必须随快照持久化：它指向 .tasks/<id>.log（与任务 id 一一
        # 对应、跨重启稳定）。剥掉它，桥接重启后恢复的任务就没有日志路径，
        # 计数解析全 0 —— 任务明明下载成功却显示「已结束，无文件产出」
        # （2026-11-13 实测：27 条历史任务全军覆没）。
        for k in ("config_path", "_progress_cache", "_resolved_cache"):
            it.pop(k, None)
        out[tid] = it
    return out


def _save_tasks_now():
    try:
        os.makedirs(os.path.dirname(_TASKS_SNAP), exist_ok=True)
        tmp = _TASKS_SNAP + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_tasks_snapshot(), f, ensure_ascii=False, indent=1)
        os.replace(tmp, _TASKS_SNAP)
    except Exception:  # noqa: BLE001 - 持久化失败不阻塞主流程（3s 后再试）
        pass


async def _tasks_persist_loop():
    while True:
        await asyncio.sleep(3)
        _save_tasks_now()


def _load_tasks():
    """启动恢复：非终态（running/pending，子进程已随桥接退出）标记为 failed，
    并在任务日志里注明原因 —— 让用户看得到「中断」而不是永远转圈。"""
    if not os.path.exists(_TASKS_SNAP):
        return
    try:
        with open(_TASKS_SNAP, encoding="utf-8") as f:
            saved = json.load(f)
        n = 0
        for tid, t in saved.items():
            if not isinstance(t, dict) or "status" not in t:
                continue
            t.setdefault("options", {})
            # 旧版快照剥掉了 log_path：按任务 id 回填（日志就在 .tasks/<id>.log），
            # 否则恢复的任务计数全 0，被前端读成「已结束，无文件产出」。
            if not t.get("log_path"):
                t["log_path"] = os.path.join(TASK_DIR, f"{tid}.log")
            t["_progress_cache"] = None
            if t.get("status") in ("running", "pending"):
                t["status"] = "failed"
                t["finished_at"] = now_iso()
                try:
                    if t.get("log_path"):
                        with open(t["log_path"], "a", encoding="utf-8", errors="replace") as f:
                            f.write("\n[桥接重启] 桥接服务退出时任务未结束，已标记为失败；"
                                    "文件产物可能已部分完成。\n")
                except OSError:
                    pass
            TASKS[tid] = t
            n += 1
        if n:
            print("[tasks] 恢复 %d 条历史任务" % n, flush=True)
    except Exception as e:  # noqa: BLE001
        print("[tasks] 恢复失败:", e, flush=True)


async def h_tasks(req):
    # 任务 → 输出目录映射：按 kind 取该工具的产物目录（与「打开目录」同源），
    # 供任务行显示「名称/输出目录」并一键打开。
    _KIND_DIR_KEY = {"douyin": "douyin", "kuaishou": "kuaishou", "direct": "direct",
                     "ty": "ty", "ytdlp": "ytdlp", "bilibili": "bilibili"}
    tool_dirs = _init_tool_dirs()
    out = []
    for t in sorted(TASKS.values(), key=lambda x: x["created_at"], reverse=True):
        item = dict(t)
        item.pop("log_path", None)
        item.pop("config_path", None)
        item.pop("_progress_cache", None)
        item.pop("_resolved_cache", None)
        dk = _KIND_DIR_KEY.get(t.get("kind") or "")
        od = tool_dirs.get(dk) if dk else None
        item["output_dir"] = od or None
        item["output_name"] = os.path.basename(od) if od else None
        # 直链任务额外带上「真实直链」：任务中心的复制按钮认它，
        # 而不是当初粘进去的那个会过期的中转页地址。
        if t.get("kind") == "direct":
            item["resolved_urls"] = task_resolved_urls(t)
        # 进度从日志现算：任务中心每 3s 轮询一次，这里必须给真值。
        # 但**已结束**的任务进度不会再变，算一次缓存起来 —— 否则每个 3s 轮询
        # 都要把全部任务的日志 `read().splitlines()` 全文读一遍再逐行正则。
        if t.get("status") in ("running", "pending"):
            item["progress_info"] = task_progress(t)
        else:
            cached = t.get("_progress_cache")
            if cached is None:
                cached = t["_progress_cache"] = task_progress(t)
            item["progress_info"] = cached
        out.append(item)
    return j({"tasks": out, "history": q_history(20)})


async def h_task_detail(req):
    tid = req.match_info["id"]
    t = TASKS.get(tid)
    if not t:
        return j({"error": "task not found"}, 404)
    item = dict(t)
    item.pop("log_path", None)
    item.pop("config_path", None)
    item.pop("_resolved_cache", None)
    item["log_tail"] = tail_log(t["log_path"], 200)
    item["progress_info"] = task_progress(t)
    if t.get("kind") == "direct":
        item["resolved_urls"] = task_resolved_urls(t)
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
                       capture_output=True, timeout=15, creationflags=NO_WINDOW)
        # 先把状态落成 canceled：taskkill 拿到的退出码必然非 0，
        # watch_task 若照常按退出码判定会把「用户主动取消」显示成「失败」——
        # 批量更新跑一半反悔时，看到的应当是「已取消」而不是「失败」。
        t["status"] = "canceled"
        return j({"ok": True})
    except Exception as e:
        return j({"error": f"终止失败: {e}"}, 500)


async def h_task_retry(req):
    """重试一个已经结束的任务（真正重新拉起下载）。

    ⚠️ 为什么必须补这个接口：任务中心的「重试」在 bridge 模式下原来是**假的** ——
       api.js 的 retryTask() 只把前端 store 里的状态改回 pending，再跑一段本地
       模拟进度，一个请求都没发。用户点了「重试」以为重新下上了，实际什么都没发生。

    真正的重试要重新起进程，而且要按类型分开：
      · 直链批量 → **只重跑失败的那几个链接**（6/11 里成功的那 6 个不该再下一遍，
        它们动辄 1~2 GB）；
      · 抖音 / 快手 / TikTok / B 站 → 用原 URL 与原模式重新入队。
    """
    tid = req.match_info["id"]
    task = TASKS.get(tid)
    if not task:
        return j({"error": "任务已过期：桥接服务重启过，旧任务记录不再可重试"
                           "（任务的 URL 只存在内存里）"}, 404)
    if task.get("status") in ("running", "pending"):
        return j({"error": "任务还在跑，先取消再重试"}, 409)

    loop = asyncio.get_running_loop()
    kind = task.get("kind")
    retried = 1
    only_failed = False
    if kind == "direct":
        failed_urls = task_failed_urls(task)
        urls = failed_urls or list(task.get("urls") or [task["url"]])
        only_failed = bool(failed_urls)
        retried = len(urls)
        new, err = start_direct_download(urls, loop, task.get("segments"), task.get("jobs"))
    elif kind == "kuaishou":
        new, err = start_kuaishou_download(task["url"], task.get("mode") or "all", loop)
    elif kind == "ty":
        new, err = start_ty_download(task["url"], task.get("mode") or "all", loop)
    elif kind == "ytdlp":
        new, err = start_ytdlp_download(task["url"], task.get("opts") or {}, loop)
    elif kind == "bilibili":
        new, err = start_bilibili_download(task["url"], task.get("mode") or "all", loop)
    else:
        # 抖音：options 是提交任务时存下来的那份（老任务没有 → 用 config.yml 默认）
        new, err = start_download(task["url"], task.get("options") or {}, loop)
    if err:
        return j({"error": err}, 409)
    task["retried_by"] = new["id"]       # 留一条线索：旧任务指向新的那次
    return j({"ok": True, "task": new, "retried": retried, "onlyFailed": only_failed})


# ---------- 静态托管 ----------
async def h_index(req):
    # HTML 骨架永不缓存：否则浏览器会拿旧 index.html 引用旧资源，改完前端仍看不到变化。
    return web.FileResponse(
        os.path.join(HERE, "index.html"),
        headers={"Cache-Control": "no-store"},
    )


def make_app():
    app = web.Application(client_max_size=2 * 1024 * 1024)

    # 除 index.html 外的所有响应（静态资源 + /api）统一 no-cache：
    # 带 ETag 走 304 重校验——文件改了立即返回新内容，没改不吃亏。
    # 这样前端改动无需再手动 bump ?v=N 版本号，刷新即见。
    async def _no_cache(_req, resp):
        if not resp.headers.get("Cache-Control"):
            resp.headers["Cache-Control"] = "no-cache"

    app.on_response_prepare.append(_no_cache)

    async def _close_account(_app):
        # 常驻的 Playwright 页面要显式收掉，否则服务退出后会留下孤儿 chromium。
        for svc in (ACCOUNT, KS_ACCOUNT):
            try:
                await svc.close()
            except Exception:
                pass

    app.on_cleanup.append(_close_account)

    async def _on_startup(app):
        _load_tasks()
        asyncio.create_task(_tasks_persist_loop())

    app.on_startup.append(_on_startup)

    app.router.add_get("/", h_index)
    app.router.add_get("/index.html", h_index)
    app.router.add_static("/assets/", os.path.join(HERE, "assets"))
    app.router.add_get("/docs/", lambda req: web.FileResponse(
        os.path.join(HERE, "docs", "assets", "arch-diagram.png")))
    app.router.add_get("/api/bridge/health", h_health)
    app.router.add_get("/api/bridge/env", h_env)
    app.router.add_get("/api/bridge/account/status", h_account_status)
    app.router.add_get("/api/bridge/account/list", h_account_list)
    app.router.add_post("/api/bridge/account/sync", h_account_sync)
    app.router.add_post("/api/bridge/account/login", h_account_login)
    app.router.add_get("/api/bridge/account/login/status", h_account_login_status)
    app.router.add_get("/api/bridge/storage", h_storage_get)
    app.router.add_post("/api/bridge/storage/check", h_storage_check)
    app.router.add_post("/api/bridge/storage/apply", h_storage_apply)
    app.router.add_get("/api/bridge/storage/migrate", h_storage_migrate)
    app.router.add_post("/api/bridge/storage/cancel", h_storage_cancel)
    app.router.add_get("/api/bridge/favfolders", h_fav_folders_get)
    app.router.add_post("/api/bridge/favfolders", h_fav_folders_post)
    app.router.add_get("/api/bridge/favitems", h_fav_items_get)
    app.router.add_post("/api/bridge/favitems", h_fav_items_post)
    app.router.add_get("/api/bridge/authors", h_authors)
    # 「未加入关注」清单 + 加入/取消关注（关注状态是派生+显式覆盖，见 q_authors）
    app.router.add_get("/api/bridge/authors/unfollowed", h_authors_unfollowed)
    app.router.add_post("/api/bridge/author/follow", h_author_follow)
    app.router.add_post("/api/bridge/author/delete", h_author_delete)
    app.router.add_get("/api/bridge/aweme", h_aweme)
    app.router.add_get("/api/bridge/media", h_media)
    app.router.add_post("/api/bridge/media/reveal", h_media_reveal)
    app.router.add_get("/api/bridge/history", h_history)
    app.router.add_get("/api/bridge/stats", h_stats)
    app.router.add_get("/api/bridge/config", h_config)
    app.router.add_post("/api/bridge/config", h_config_save)
    app.router.add_post("/api/bridge/config/reset", h_config_reset)
    app.router.add_get("/api/bridge/webui-settings", h_webui_settings)
    app.router.add_get("/api/bridge/live", h_live)
    app.router.add_post("/api/bridge/live/launch", h_live_launch)
    app.router.add_post("/api/bridge/live/open-dir", h_live_open_dir)
    app.router.add_post("/api/bridge/live/add-room", h_live_add_room)
    # 直播录制：无界面守护进程代理（网页全权控制录制）
    app.router.add_post("/api/bridge/live/daemon/ensure", h_live_daemon_ensure)
    app.router.add_get("/api/bridge/live/daemon/status", h_live_daemon_status)
    app.router.add_get("/api/bridge/live/daemon/logs", h_live_daemon_logs)
    app.router.add_post("/api/bridge/live/daemon/shutdown", h_live_daemon_shutdown)
    app.router.add_get("/api/bridge/live/daemon/download", h_live_daemon_download)
    app.router.add_post("/api/bridge/live/daemon/monitor/start",
                        lambda r: h_live_daemon_action(r, "/monitor/start"))
    app.router.add_post("/api/bridge/live/daemon/monitor/stop",
                        lambda r: h_live_daemon_action(r, "/monitor/stop"))
    app.router.add_post("/api/bridge/live/daemon/monitor/check",
                        lambda r: h_live_daemon_action(r, "/monitor/check"))
    app.router.add_post("/api/bridge/live/daemon/rooms/add",
                        lambda r: h_live_daemon_action(r, "/rooms/add"))
    app.router.add_post("/api/bridge/live/daemon/rooms/remove",
                        lambda r: h_live_daemon_action(r, "/rooms/remove"))
    app.router.add_post("/api/bridge/live/daemon/rooms/toggle",
                        lambda r: h_live_daemon_action(r, "/rooms/toggle"))
    app.router.add_post("/api/bridge/live/daemon/rooms/stop",
                        lambda r: h_live_daemon_action(r, "/rooms/stop"))
    app.router.add_post("/api/bridge/live/daemon/settings",
                        lambda r: h_live_daemon_action(r, "/settings"))
    app.router.add_post("/api/bridge/live/daemon/login/start",
                        lambda r: h_live_daemon_action(r, "/login/start"))
    app.router.add_post("/api/bridge/live/daemon/login/refresh",
                        lambda r: h_live_daemon_action(r, "/login/refresh"))
    app.router.add_post("/api/bridge/live/daemon/login/cancel",
                        lambda r: h_live_daemon_action(r, "/login/cancel"))
    app.router.add_get("/api/bridge/live/daemon/login/status", h_live_daemon_logs_login)
    # 统一启动台（五个 .bat 入口共享）
    app.router.add_get("/api/bridge/launcher", h_launcher)
    app.router.add_post("/api/bridge/tool/launch", h_tool_launch)
    app.router.add_post("/api/bridge/tool/open-dir", h_tool_open_dir)
    # 打开任意本机目录（下载历史行的「打开目录」；路径来自后端查库结果）
    app.router.add_post("/api/bridge/open-dir", h_open_path)
    # 分享短链解析（v.kuaishou.com 等）：判定直播/作品，直播回房间地址
    app.router.add_post("/api/bridge/resolve-link", h_resolve_link)
    app.router.add_post("/api/bridge/tool/set-dir", h_tool_set_dir)
    app.router.add_post("/api/bridge/tool/pick-dir", h_tool_pick_dir)
    app.router.add_post("/api/bridge/tool/selftest", h_tool_selftest)
    app.router.add_post("/api/bridge/download", h_download)
    app.router.add_post("/api/bridge/direct/download", h_direct_download)
    app.router.add_post("/api/bridge/direct/resolve", h_direct_resolve)
    app.router.add_post("/api/bridge/kuaishou/launch", h_kuaishou_launch)
    app.router.add_get("/api/bridge/kuaishou/status", h_kuaishou_status)
    app.router.add_post("/api/bridge/kuaishou/login", h_kuaishou_login)
    app.router.add_post("/api/bridge/kuaishou/download", h_kuaishou_download)
    # 快手本地作品库（作品档案 / 关注管理的快手标签）
    app.router.add_get("/api/bridge/ks/works", h_ks_works)
    app.router.add_post("/api/bridge/ks/covers/ensure", h_ks_covers_ensure)
    app.router.add_get("/api/bridge/ks/covers/status", h_ks_covers_status)
    app.router.add_get("/api/bridge/ks/covers/{pid}", h_ks_cover_file)
    app.router.add_get("/api/bridge/ks/authors", h_ks_authors)
    app.router.add_get("/api/bridge/ks/stats", h_ks_stats)
    app.router.add_get("/api/bridge/ks/media", h_ks_media)
    app.router.add_post("/api/bridge/ks/media/reveal", h_ks_media_reveal)
    app.router.add_post("/api/bridge/ks/author/delete", h_ks_author_delete)
    app.router.add_post("/api/bridge/ks/author/profile", h_ks_author_profile)
    app.router.add_post("/api/bridge/bili/download", h_bilibili_download)
    app.router.add_post("/api/bridge/ty/download", h_ty_download)
    # yt-dlp 万能下载（模块 7：全站通吃 + 音频/字幕后处理）
    app.router.add_post("/api/bridge/ytdlp/download", h_ytdlp_download)
    app.router.add_post("/api/bridge/ytdlp/preview", h_ytdlp_preview)
    app.router.add_get("/api/bridge/ytdlp/conf", h_ytdlp_conf_get)
    app.router.add_post("/api/bridge/ytdlp/conf", h_ytdlp_conf_save)
    app.router.add_post("/api/bridge/ytdlp/update", h_ytdlp_update)
    # Stripchat 直播录制（模块 6：长驻进程监护，不走统一任务队列）
    app.router.add_post("/api/bridge/stripchat/start", h_stripchat_start)
    app.router.add_post("/api/bridge/stripchat/stop", h_stripchat_stop)
    app.router.add_get("/api/bridge/stripchat/status", h_stripchat_status)
    app.router.add_post("/api/bridge/stripchat/open-dir", h_stripchat_open_dir)
    app.router.add_get("/api/bridge/bili/cookie", h_bili_cookie_get)
    app.router.add_post("/api/bridge/bili/cookie", h_bili_cookie_set)
    # 哔哩哔哩扫码登录（官方 passport 二维码；成功即落盘 Cookie）
    app.router.add_post("/api/bridge/bili/qr/start", h_bili_qr_start)
    app.router.add_get("/api/bridge/bili/qr/poll", h_bili_qr_poll)
    app.router.add_post("/api/bridge/bili/qr/cancel", h_bili_qr_cancel)
    # 重复视频扫描（抖音 + 快手一起）
    app.router.add_post("/api/bridge/dedup/scan", h_dedup_scan)
    app.router.add_get("/api/bridge/dedup/status", h_dedup_status)
    app.router.add_post("/api/bridge/dedup/delete", h_dedup_delete)
    app.router.add_get("/api/bridge/tasks", h_tasks)
    app.router.add_get("/api/bridge/task/{id}", h_task_detail)
    app.router.add_post("/api/bridge/cancel/{id}", h_cancel)
    # 重试（真重试：重新起进程；直链批量只补失败的那些链接）
    app.router.add_post("/api/bridge/task/{id}/retry", h_task_retry)
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
    print("  数据库: %s（查询只读）" % (DB_PATH if os.path.exists(DB_PATH) else
                                        DB_PATH + "  ← 未找到！作品档案/关注管理会显示为空"))
    print("  删除作者: 会写 aweme 表（删前自动整库备份到 %s）" % BACKUP_DIR)
    # 一次性回填：把升级前就已经混进关注列表的「单链接作者」认出来（见函数说明）。
    # 只写 .data/follow_state.json，不改数据库；跑过一次就记标记。
    try:
        _marked = follow_legacy_reconcile()
        if _marked:
            print("  关注回填: %d 位作者只有单链接作品，已移出关注列表" % _marked)
    except Exception as _e:
        print("  关注回填: 跳过（%s）" % _e)
    _ledgers = _ks_ledger_files()
    if _ledgers:
        print("  快手台账: %d 处" % len(_ledgers))
        for _r, _p in _ledgers:
            print("      %s" % _p)
    else:
        print("  快手台账: 未找到（作品档案/关注管理的快手标签会显示为空）")
    print("  按 Ctrl+C 停止")
    print("=" * 56)
    _w.run_app(make_app(), host="127.0.0.1", port=PORT, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
