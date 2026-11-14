# -*- coding: utf-8 -*-
r"""「关注」派生规则 + 加入/取消关注 的离线集成测试（不联网、不下载、用临时库）。

要证明的事（对应 2026-10-30 的需求）：
    1. 单作品链接下到的作者**不进**关注列表，但作品照样在库里；
    2. 关注列表、作者数、作品数只统计关注中的作者 —— 单链接下载不干扰统计；
    3. 「一键更新」的目标集合 = 关注列表（前端直接读 /api/bridge/authors），
       所以只要这个接口不含单链接作者，批量更新就不会去爬没关注的人；
    4. 用户明确「加入关注」后才进来，「取消关注」只移出列表、不删作品；
    5. 老库（没有 origin 列 / 历史作品 origin 为空）行为不变，且能一次性回填；
    6. 快手侧同一套规则（台账条目 origin=single 视为单链接）。

跑法：
    runtime\douyin-venv\Scripts\python.exe modules\douyin\webui\tests\test_follow_split.py
"""
import asyncio
import io
import json
import os
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # webui/

import bridge_server as B                          # noqa: E402
from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("  [OK]   %s -> %s" % (name, got))
    else:
        FAIL += 1
        print("  [FAIL] %s -> %s（期望 %s）" % (name, got, want))


def check_true(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s%s" % (name, (" -> " + str(detail)) if detail != "" else ""))
    else:
        FAIL += 1
        print("  [FAIL] %s（%s）" % (name, detail))


AWEME_DDL = """
CREATE TABLE aweme (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aweme_id TEXT UNIQUE NOT NULL,
    aweme_type TEXT NOT NULL,
    title TEXT,
    author_id TEXT,
    author_name TEXT,
    create_time INTEGER,
    download_time INTEGER,
    file_path TEXT,
    metadata TEXT,
    author_sec_uid TEXT,
    cover_urls TEXT NOT NULL DEFAULT '',
    job_id TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT ''
)
"""
HIST_DDL = """
CREATE TABLE download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    url_type TEXT NOT NULL,
    download_time INTEGER,
    total_count INTEGER,
    success_count INTEGER,
    config TEXT
)
"""


def make_db(path, rows, hist=(), with_origin=True, ts_step=1):
    """建一个临时库。rows: (author, works, origin)；hist: (url_type, total, ts)。"""
    con = sqlite3.connect(path)
    con.execute(AWEME_DDL if with_origin else
                AWEME_DDL.replace(",\n    origin TEXT NOT NULL DEFAULT ''\n", "\n"))
    con.execute(HIST_DDL)
    i = 0
    for author, works, origin in rows:
        for _ in range(works):
            i += 1
            ts = 1700000000 + i * ts_step
            cols = ["aweme_id", "aweme_type", "title", "author_id", "author_name",
                    "author_sec_uid", "create_time", "download_time", "file_path"]
            vals = ["aw%04d" % i, "video", "t%d" % i, "uid-" + author, author,
                    "sec-" + author, ts, ts, "D:/v/%d.mp4" % i]
            if with_origin:
                cols.append("origin")
                vals.append(origin)
            con.execute("INSERT INTO aweme (%s) VALUES (%s)"
                        % (",".join(cols), ",".join("?" * len(cols))), vals)
    for url_type, total, ts in hist:
        con.execute("INSERT INTO download_history (url, url_type, download_time,"
                    " total_count, success_count) VALUES (?,?,?,?,?)",
                    ("u://" + url_type, url_type, ts, total, total))
    con.commit()
    con.close()


def reset_origin_cache():
    B._ORIGIN_COL["ok"] = False
    B._ORIGIN_COL["checked"] = False


TMP = tempfile.mkdtemp(prefix="followsplit-")
B.FOLLOW_PATH = os.path.join(TMP, "follow_state.json")


async def part_douyin():
    print("=" * 62)
    print("一、抖音：派生规则 / 统计 / 加入取消关注")
    print("=" * 62)
    db = os.path.join(TMP, "main.db")
    make_db(db, [
        ("主页作者", 3, "user"),      # 主页批量下过 → 关注
        ("单链作者", 1, "video"),     # 只有一条单作品链接 → 不关注
        ("多单链作者", 2, "video"),   # 下了两条单链接，仍未关注
        ("老数据作者", 1, ""),        # 来源未知（升级前的行）→ 默认关注
    ])
    B.DB_PATH = db
    reset_origin_cache()

    async with TestClient(TestServer(B.make_app())) as cli:
        r = await cli.get("/api/bridge/authors")
        authors = (await r.json())["authors"]
        names = sorted(a["author_name"] for a in authors)
        check("关注列表作者", names, ["主页作者", "老数据作者"])
        check("关注列表作品数合计", sum(a["works"] for a in authors), 4)

        r = await cli.get("/api/bridge/authors/unfollowed?platform=douyin")
        others = (await r.json())["authors"]
        check("未加入关注作者", sorted(a["nickname"] for a in others),
              ["单链作者", "多单链作者"])
        check("未加入关注作品数合计", sum(a["works"] for a in others), 3)
        check_true("未加入关注的原因如实标注",
                   all(a["why"] == "single" for a in others),
                   [a["why"] for a in others])
        check("两类作者作品数之和 = 库里总数", 4 + 3, 7)

        # 加入关注：单链作者进列表
        r = await cli.post("/api/bridge/author/follow",
                           data=json.dumps({"platform": "douyin", "author": "单链作者",
                                            "follow": True}),
                           headers={"Content-Type": "application/json"})
        check("加入关注 -> 状态码", r.status, 200)
        check_true("加入关注 -> 返回 followed", (await r.json()).get("followed") is True)
        authors = (await (await cli.get("/api/bridge/authors")).json())["authors"]
        check("加入后进关注列表", sorted(a["author_name"] for a in authors),
              ["主页作者", "单链作者", "老数据作者"])
        others = (await (await cli.get(
            "/api/bridge/authors/unfollowed?platform=douyin")).json())["authors"]
        check("加入后退出未关注清单", [a["nickname"] for a in others], ["多单链作者"])

        # 取消关注：只移出列表，作品不动
        r = await cli.post("/api/bridge/author/follow",
                           data=json.dumps({"platform": "douyin", "author": "主页作者",
                                            "follow": False}),
                           headers={"Content-Type": "application/json"})
        check("取消关注 -> 状态码", r.status, 200)
        authors = (await (await cli.get("/api/bridge/authors")).json())["authors"]
        check("取消后不在关注列表", "主页作者" in [a["author_name"] for a in authors], False)
        others = (await (await cli.get(
            "/api/bridge/authors/unfollowed?platform=douyin")).json())["authors"]
        by = {a["nickname"]: a for a in others}
        check("取消关注的原因标注", by.get("主页作者", {}).get("why"), "manual")
        check("作品记录没被删（库里还是 3 条）", sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM aweme WHERE author_name='主页作者'").fetchone()[0], 3)
        check("未关注清单里的作品数", by.get("主页作者", {}).get("works"), 3)

        # 不存在的作者：拒绝
        r = await cli.post("/api/bridge/author/follow",
                           data=json.dumps({"platform": "douyin", "author": "查无此人",
                                            "follow": True}),
                           headers={"Content-Type": "application/json"})
        check("库里没有的作者 -> 404", r.status, 404)

        # 作品档案不受影响：单链作者的作品仍能查
        r = await cli.get("/api/bridge/aweme?author=%E5%8D%95%E9%93%BE%E4%BD%9C%E8%80%85")
        check("作品档案仍能查到单链作者的作品", (await r.json()).get("total"), 1)


async def part_legacy():
    print()
    print("=" * 62)
    print("二、老库：没有 origin 列 / 历史回填")
    print("=" * 62)
    # ① 老库（无 origin 列）→ 全部按关注处理，绝不因为缺列把作者踢出去
    db = os.path.join(TMP, "old.db")
    make_db(db, [("甲", 2, ""), ("乙", 1, "")], with_origin=False)
    B.DB_PATH = db
    reset_origin_cache()
    check("老库：全部算关注", len(B.q_authors()), 2)
    check("老库：未关注为空", len(B.q_unfollowed_authors()), 0)
    check("老库：原列结构未变（没有 origin）",
          "origin" in {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(aweme)")},
          False)
    # 桥接启动时会补这一列（与 CLI 同一句 DDL），补完老数据仍是关注
    check_true("补 origin 列成功", B._ensure_aweme_origin_column())
    reset_origin_cache()
    check("补列后老数据仍算关注", len(B.q_authors()), 2)

    # ② 一次性回填：只下过一条单作品链接的老行被移出关注
    db2 = os.path.join(TMP, "legacy.db")
    # ts_step=400：让三位作者的时间戳各差 400 秒，回填的 ±60 秒窗口才互不干扰
    # 剧本人生 at +400，正常作者 at +800，批量作者 +1200 起
    make_db(db2, [("剧本人生", 1, ""), ("正常作者", 1, ""), ("批量作者", 4, "user")],
            hist=[("video", 1, 1700000400),     # 与「剧本人生」那条作品同秒
                  ("user", 1, 1700000800)],     # 与「正常作者」同秒，但是主页下载
            ts_step=400)
    B.DB_PATH = db2
    reset_origin_cache()
    B.FOLLOW_PATH = os.path.join(TMP, "follow_legacy.json")
    check("回填前：3 位都算关注", len(B.q_authors()), 3)
    check("回填命中 1 位", B.follow_legacy_reconcile(), 1)
    names = [a["author_name"] for a in B.q_authors()]
    check("回填后关注列表", sorted(names), ["批量作者", "正常作者"])
    others = B.q_unfollowed_authors()
    check("回填后未关注", [a["author_name"] for a in others], ["剧本人生"])
    check("回填原因", others[0]["why"], "legacy-single")
    check("回填幂等（第二次不再动）", B.follow_legacy_reconcile(), 0)
    check("回填没删任何作品", sqlite3.connect(db2).execute(
        "SELECT COUNT(*) FROM aweme").fetchone()[0], 6)
    # 加回来就一条记录的事
    B.set_author_follow("douyin", "剧本人生", True)
    check("回填结果可一键恢复", len(B.q_authors()), 3)


async def part_kuaishou():
    print()
    print("=" * 62)
    print("三、快手：台账 origin=single 不算关注")
    print("=" * 62)
    B.FOLLOW_PATH = os.path.join(TMP, "follow_ks.json")
    items = [
        # 主页批量（批量链路写台账不带 origin）
        {"photoId": "p1", "author": "快手主页作者", "exists": True, "size": 100,
         "mtime": 1700000000, "origin": "", "root": "D:/ks"},
        {"photoId": "p2", "author": "快手主页作者", "exists": True, "size": 100,
         "mtime": 1700000001, "origin": "", "root": "D:/ks"},
        # 单作品链接（download_single 写入 origin=single）
        {"photoId": "p3", "author": "快手单链作者", "exists": True, "size": 100,
         "mtime": 1700000002, "origin": "single", "root": "D:/ks"},
    ]
    B.ks_index = lambda force=False: {"items": items, "roots": ["D:/ks"]}
    B._ks_infer_profiles = lambda _items: {}
    B._ks_profile_map_file = lambda: {}
    authors = B.ks_authors()
    check("快手关注列表", [a["nickname"] for a in authors], ["快手主页作者"])
    others = B.ks_unfollowed_authors()
    check("快手未关注", [a["nickname"] for a in others], ["快手单链作者"])
    check("快手未关注原因", others[0]["why"], "single")
    # 关注一位已关注的作者作品后来加了单条下载：仍然关注
    items.append({"photoId": "p4", "author": "快手主页作者", "exists": True,
                  "size": 100, "mtime": 1700000003, "origin": "single", "root": "D:/ks"})
    check("已关注作者后又单链下了一条 → 仍是关注",
          [a["nickname"] for a in B.ks_authors()], ["快手主页作者"])


async def main():
    if os.path.exists(B.FOLLOW_PATH):
        os.remove(B.FOLLOW_PATH)
    await part_douyin()
    await part_legacy()
    await part_kuaishou()
    print()
    print("=" * 62)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("临时目录：%s" % TMP)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
