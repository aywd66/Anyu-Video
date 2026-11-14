# -*- coding: utf-8 -*-
r"""「删除作者信息」的离线集成测试（不联网、不下载、绝不碰原始库）。

为什么用真实库的副本跑：
    删除是唯一会写 aweme 表的操作，用内存造的假库测不出真实数据形态
    （作者重名、author_id 为空、同一天大量记录等）。所以先把真实库整份
    复制到临时目录，再把 bridge_server 的 DB_PATH / BACKUP_DIR 指过去，
    然后打真正的 HTTP 端点 —— 逻辑是真代码、数据是真数据、原始库零风险。

覆盖的断言：
    1. 只删目标作者；其它作者、其它表（download_history）一行不少
    2. 删除后 /api/bridge/authors 里不再出现该作者
    3. 备份文件已生成，且内容里含被删掉的那些记录（可回滚）
    4. 备份只保留最近 KEEP_BACKUPS 份，不会无限占磁盘
    5. 空作者名 -> 400；库里没有的作者 -> 404（不静默成功）
    6. 原始库自始至终未被修改

跑法：
    runtime\douyin-venv\Scripts\python.exe modules\douyin\webui\tests\test_author_admin.py
"""
import asyncio
import io
import json
import os
import shutil
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


def count(db, sql, args=()):
    con = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    try:
        return con.execute(sql, args).fetchone()[0]
    finally:
        con.close()


ORIG_DB = B.DB_PATH
if not os.path.exists(ORIG_DB):
    print("跳过：找不到工程数据库 %s" % ORIG_DB)
    sys.exit(0)

TMP = tempfile.mkdtemp(prefix="authadmin-")
TMP_DB = os.path.join(TMP, "dy_downloader.db")
TMP_BAK = os.path.join(TMP, "backups")
shutil.copy2(ORIG_DB, TMP_DB)
# WAL 边角内容也要一起带过来，否则副本可能落后于主库
for suffix in ("-wal", "-shm"):
    if os.path.exists(ORIG_DB + suffix):
        shutil.copy2(ORIG_DB + suffix, TMP_DB + suffix)

ORIG_TOTAL = count(ORIG_DB, "SELECT COUNT(*) FROM aweme")
ORIG_HIST = count(ORIG_DB, "SELECT COUNT(*) FROM download_history")
# 真正的（非临时）备份目录：测试前后必须一模一样 —— 否则说明有代码没走补丁后的路径
REAL_BAK = os.path.join(B.TASK_DIR, "db-backups")
REAL_BAK_BEFORE = sorted(os.listdir(REAL_BAK)) if os.path.isdir(REAL_BAK) else None

B.DB_PATH = TMP_DB
B.BACKUP_DIR = TMP_BAK


async def main():
    total0 = count(TMP_DB, "SELECT COUNT(*) FROM aweme")

    async with TestClient(TestServer(B.make_app())) as cli:
        # ---------- 先看作者清单 ----------
        r = await cli.get("/api/bridge/authors")
        authors = (await r.json())["authors"]
        by_works = sorted(authors, key=lambda a: a["works"])
        small, big = by_works[0], by_works[-1]
        print("=" * 62)
        print("作者清单：%d 位；本次用「%s」(%d 条) 和「%s」(%d 条)"
              % (len(authors), small["author_name"], small["works"],
                 big["author_name"], big["works"]))
        print("=" * 62)

        # ---------- 1. 空作者名 / 不存在的作者 ----------
        r = await cli.post("/api/bridge/author/delete",
                           data=json.dumps({"author": "   "}),
                           headers={"Content-Type": "application/json"})
        check("空作者名 -> 状态码", r.status, 400)
        check_true("  空作者名有明确报错", "error" in await r.json())

        r = await cli.post("/api/bridge/author/delete",
                           data=json.dumps({"author": "这个名字肯定不存在___xyz"}),
                           headers={"Content-Type": "application/json"})
        check("库里没有的作者 -> 状态码", r.status, 404)
        check_true("  不静默成功", (await r.json()).get("ok") is not True)

        # ---------- 2. 删除目标作者 ----------
        r = await cli.post("/api/bridge/author/delete",
                           data=json.dumps({"author": small["author_name"]}),
                           headers={"Content-Type": "application/json"})
        d = await r.json()
        check("删除已有作者 -> 状态码", r.status, 200)
        check("  删除条数 = 该作者作品数", d.get("deleted"), small["works"])
        check_true("  如实声明不动磁盘文件", d.get("filesKept") is True)
        check_true("  返回备份路径", bool(d.get("backup")), d.get("backup"))

        total1 = count(TMP_DB, "SELECT COUNT(*) FROM aweme")
        check("  总记录数按删除量下降", total0 - total1, small["works"])
        check("  该作者记录已清空", count(
            TMP_DB, "SELECT COUNT(*) FROM aweme WHERE author_name=?",
            (small["author_name"],)), 0)
        check("  其它作者未被误伤（大作者）", count(
            TMP_DB, "SELECT COUNT(*) FROM aweme WHERE author_name=?",
            (big["author_name"],)), big["works"])
        check("  download_history 不受影响", count(
            TMP_DB, "SELECT COUNT(*) FROM download_history"), ORIG_HIST)

        # ---------- 3. 备份可用（能回滚） ----------
        bak = d.get("backup")
        check_true("  备份文件确实存在", bool(bak) and os.path.exists(bak))
        if bak and os.path.exists(bak):
            check("  备份里含被删作者的完整记录", count(
                bak, "SELECT COUNT(*) FROM aweme WHERE author_name=?",
                (small["author_name"],)), small["works"])
            check("  备份里总记录数 = 删除前", count(
                bak, "SELECT COUNT(*) FROM aweme"), total0)

        # ---------- 4. 列表里不再出现 ----------
        r = await cli.get("/api/bridge/authors")
        names = [a["author_name"] for a in (await r.json())["authors"]]
        check_true("  该作者已从关注列表消失", small["author_name"] not in names)
        check_true("  其它作者仍在列表里", big["author_name"] in names)

        # ---------- 5. 备份份数有上限 ----------
        for nm in (big["author_name"],) + tuple(
                a["author_name"] for a in by_works[1:3]
                if a["author_name"] != small["author_name"]):
            await cli.post("/api/bridge/author/delete",
                           data=json.dumps({"author": nm}),
                           headers={"Content-Type": "application/json"})
        kept = [f for f in os.listdir(TMP_BAK) if f.endswith(".db")]
        check_true("  备份份数不超过上限 %d" % B.KEEP_BACKUPS,
                   len(kept) <= B.KEEP_BACKUPS, "实际 %d 份" % len(kept))

    # ---------- 6. 原始库零改动 ----------
    check("原始库总记录数未变", count(ORIG_DB, "SELECT COUNT(*) FROM aweme"), ORIG_TOTAL)
    check("原始库 download_history 未变", count(
        ORIG_DB, "SELECT COUNT(*) FROM download_history"), ORIG_HIST)
    after = sorted(os.listdir(REAL_BAK)) if os.path.isdir(REAL_BAK) else None
    check_true("真实备份目录未被测试写入", after == REAL_BAK_BEFORE,
               "测试前 %s / 测试后 %s" % (
                   "无" if REAL_BAK_BEFORE is None else len(REAL_BAK_BEFORE),
                   "无" if after is None else len(after)))


print("=" * 62)
print("删除作者信息（真实库副本 + 真实 HTTP 端点）")
print("=" * 62)
try:
    asyncio.run(main())
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print()
print("=" * 62)
print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
