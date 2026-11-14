# -*- coding: utf-8 -*-
r"""快手本地作品库的离线集成测试（不联网、不下载、绝不碰原始台账）。

为什么分两段跑：
    A 段用**合成台账**：删除、备份、分页这些会写盘的操作必须可控，
      合成数据才能断言"只删了目标那位作者"，而且删坏了也无所谓。
    B 段用**真实台账的副本**：作者聚合 / 主页 ID 推断 / 媒体 Range 这些
      依赖真实数据形态（base36 与纯数字两种 photoId 并存、15 条文件已不在、
      4 份 works_list 各自的交集比例），内存造的假数据测不出这些。
      真实台账只读、媒体只读文件，写操作全在临时目录里。

覆盖：
    1. 索引：只收「台账里有 file 的」，失败记录（188 条无 file）不进索引
    2. 作者聚合：按落盘目录名分组，条数与体积对得上
    3. 主页 ID 推断：works_list_<ID>.json 与作者作品求交集，认对不认错
    4. 作品列表：分页、作者筛选、关键词搜索
    5. 媒体流：206 + Content-Range、200 全量、404 不存在、400 缺 id
    6. 媒体接口只认 id —— 传路径/相对路径一律 404，不能读任意文件
    7. 删除作者：只删目标、备份生成且内容可回滚、空名 400、不存在 404
    8. 手动主页映射：保存后 authors 里立刻有 url
    9. 原始台账自始至终未被修改

跑法：
    runtime\douyin-venv\Scripts\python.exe modules\douyin\webui\tests\test_ks_archive.py
"""
import asyncio
import io
import json
import os
import shutil
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))            # webui/

import bridge_server as B                            # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

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


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def touch(path, size=4096):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * size)
    return path


# ---------------------------------------------------------------- 真实台账快照
REAL_ROOTS = [r"E:\录屏\spxz"]
REAL_LEDGER = os.path.join(REAL_ROOTS[0], "download_log.json")
REAL_ITEMS = []
if os.path.isfile(REAL_LEDGER):
    with open(REAL_LEDGER, "r", encoding="utf-8-sig") as f:
        REAL_ITEMS = json.load(f)
REAL_AUTHOR = "粉红琪琪"
REAL_AUTHOR_PID = "3x6jfscnf3r9xgs"      # works_list_3x6jfscnf3r9xgs.json


# ================================================================ A 段：合成数据
def build_synthetic(root):
    """造一份两作者、若干作品的台账 + 真文件（体积可控，便于断言）。"""
    a_dir = os.path.join(root, "甲作者")
    b_dir = os.path.join(root, "乙作者")
    log = []
    for i in range(7):
        p = touch(os.path.join(a_dir, "甲作品_%d_[%d].mp4" % (i, 9000000000000000000 + i)), 2048)
        log.append({"seq": i + 1, "photoId": "aaa%03d" % i, "caption": "甲的作品 %d" % i,
                    "ok": True, "quality": "720p", "duration_ms": 10000 + i, "file": p})
    for i in range(3):
        p = touch(os.path.join(b_dir, "乙作品_%d_[%d].mp4" % (i, 8000000000000000000 + i)), 4096)
        log.append({"seq": i + 1, "photoId": "bbb%03d" % i, "caption": "乙的作品 %d" % i,
                    "ok": True, "quality": "1080p", "duration_ms": 20000, "file": p})
    # 两条"失败记录"：没有 file —— 不该进索引，也不该被删除带走
    log.append({"seq": 99, "photoId": "zzz001", "caption": "失败的一单", "ok": False,
                "err": "status_208005"})
    log.append({"seq": 100, "photoId": "zzz002", "caption": "另一单失败", "ok": False,
                "err": "status_208005"})
    # 一条"有 file 但文件已被删"—— 要出现在列表里（卡片会提示文件不在）
    log.append({"seq": 101, "photoId": "ccc001", "caption": "文件已被删的作品", "ok": True,
                "file": os.path.join(a_dir, "不存在_[7000000000000000000].mp4")})
    write_json(os.path.join(root, "download_log.json"), log)
    return log


async def phase_a(tmp):
    global PASS, FAIL
    print("=" * 66)
    print("A 段：合成台账（删除 / 备份 / 分页 / 媒体）")
    print("=" * 66)
    prim = os.path.join(tmp, "a-primary")
    bak = os.path.join(tmp, "a-backups")
    prof = os.path.join(prim, "author_profiles.json")
    log = build_synthetic(prim)

    B.KS_PRIMARY_ROOT = prim
    B.KS_EXTRA_ROOTS = os.path.join(prim, "ledger_roots.json")   # 不存在 -> 只有 primary
    B.KS_LEGACY_ROOTS = []
    # ⚠️ 工具箱真实落点也必须掐掉：否则 data\output_dirs.json 指的 E:\录屏\kssp\kuaishou
    #    会被当成本环境的根读进来，合成台账里混进真实作者（2026-09-29 踩过）。
    B.KS_TOOL_OUTDIR = ""
    B.KS_PROFILE_MAP = prof
    B.KS_BACKUP_DIR = bak
    B.ks_index(force=True)

    # 隔离自检：合成环境下解析出的根必须全落在临时区里。
    # 真实数据混进来可能让断言"看起来通过"，那比失败更危险，所以单独立一条。
    _roots = B.ks_ledger_roots()
    _outside = [r for r in _roots
                if not os.path.realpath(r).startswith(os.path.realpath(tmp))]
    check_true("A 段隔离：解析出的根全在临时区（无真实台账混入）", not _outside)

    app = B.make_app()
    async with TestClient(TestServer(app)) as cli:
        # ---- 1. 索引与统计 ----
        r = await cli.get("/api/bridge/ks/stats")
        st = await r.json()
        check("stats HTTP", r.status, 200)
        check("索引条数 = 有 file 的 11 条（失败记录与无 file 的都不进）", st["records"], 11)
        check("有文件的 10 条（1 条文件已被删）", st["works"], 10)
        check("作者数 2", st["authors"], 2)

        # ---- 2. 作者聚合 ----
        r = await cli.get("/api/bridge/ks/authors")
        au = (await r.json())["authors"]
        byname = {a["nickname"]: a for a in au}
        check("甲作者作品 8（7 有文件 + 1 文件已删）", byname["甲作者"]["works"], 8)
        check("乙作者作品 3", byname["乙作者"]["works"], 3)
        check_true("甲作者体积 = 7 × 2KB", byname["甲作者"]["sizeMB"] == round(7 * 2048 / 1048576.0, 1),
                   byname["甲作者"]["sizeMB"])
        check_true("合成数据没有 works_list，主页推断不出来（url 为空）",
                   byname["甲作者"]["profileUrl"] == "")

        # ---- 3. 列表：分页 / 作者筛选 / 搜索 ----
        r = await cli.get("/api/bridge/ks/works?limit=5")
        d = await r.json()
        check("works total", d["total"], 11)
        check("分页 limit=5", len(d["items"]), 5)
        r = await cli.get("/api/bridge/ks/works?limit=5&offset=10")
        check("偏移 10 后剩 1 条", len((await r.json())["items"]), 1)
        r = await cli.get("/api/bridge/ks/works?author=" + "%E4%B9%99%E4%BD%9C%E8%80%85")
        check("按作者筛选（乙作者 3 条）", (await r.json())["total"], 3)
        r = await cli.get("/api/bridge/ks/works?q=%E7%94%B2%E7%9A%84")
        check("按标题搜索（甲的作品 0-6 共 7 条）", (await r.json())["total"], 7)
        r = await cli.get("/api/bridge/ks/works?limit=999")
        check_true("limit 有上限（<=200）", len((await r.json())["items"]) <= 200)

        # ---- 4. 媒体流 ----
        fpath = os.path.join(prim, "甲作者", "甲作品_0_[9000000000000000000].mp4")
        r = await cli.get("/api/bridge/ks/media?id=aaa000", headers={"Range": "bytes=0-99"})
        check("Range 请求 -> 206", r.status, 206)
        check("Content-Range 正确", r.headers.get("Content-Range"), "bytes 0-99/2048")
        check("Accept-Ranges 头存在", r.headers.get("Accept-Ranges"), "bytes")
        check("Range 返回 100 字节", len(await r.read()), 100)
        check("Content-Type", r.headers.get("Content-Type"), "video/mp4")
        r = await cli.get("/api/bridge/ks/media?id=aaa000")
        check("全量请求 -> 200", r.status, 200)
        check("全量字节数", len(await r.read()), 2048)
        r = await cli.get("/api/bridge/ks/media?id=zzz001")
        check("无 file 的记录 -> 404", r.status, 404)
        r = await cli.get("/api/bridge/ks/media?id=ccc001")
        check("文件已被删 -> 404", r.status, 404)
        r = await cli.get("/api/bridge/ks/media")
        check("缺 id -> 400", r.status, 400)
        # 只认 id：任何"路径式"输入都必须落到 404，而不是去读盘
        for bad in ("../../boot.ini", r"C:\Windows\win.ini", "..%2F..%2Fetc%2Fpasswd",
                    "aaa000/../../download_log.json"):
            r = await cli.get("/api/bridge/ks/media?id=" + bad)
            check_true("非法 id 被拒（%s）" % bad[:26], r.status in (400, 404), r.status)

        # ---- 5. 删除一位作者 ----
        before = json.load(open(os.path.join(prim, "download_log.json"), encoding="utf-8"))
        r = await cli.post("/api/bridge/ks/author/delete", json={"author": "甲作者"})
        res = await r.json()
        check("删除 HTTP", r.status, 200)
        check("删掉 8 条", res["deleted"], 8)
        # 13 条原始台账 - 被删的 8 条（7 个有文件 + 1 个文件已被删的）= 5
        check("台账里剩 5 条（3 条乙作者 + 2 条失败记录）", res["worksLeft"], 5)
        check_true("生成了备份文件", len(res["backup"]) == 1 and os.path.isfile(res["backup"][0]))
        bak_log = json.load(open(res["backup"][0], encoding="utf-8"))
        check("备份 = 删除前的完整台账", len(bak_log), len(before))
        check_true("备份里含被删作者的全部记录",
                   sum(1 for x in bak_log if "甲作者" in (x.get("file") or "")) == 8)
        after = json.load(open(os.path.join(prim, "download_log.json"), encoding="utf-8"))
        check("台账里乙作者 3 条一条不少",
              sum(1 for x in after if "乙作者" in (x.get("file") or "")), 3)
        check("两条失败记录（无 file）原样保留",
              sum(1 for x in after if not x.get("file")), 2)
        check("磁盘上的文件一个没删", os.path.isfile(fpath), True)
        au = (await (await cli.get("/api/bridge/ks/authors")).json())["authors"]
        check("列表里只剩 1 位作者", [a["nickname"] for a in au], ["乙作者"])

        # ---- 6. 删除的边界 ----
        r = await cli.post("/api/bridge/ks/author/delete", json={"author": "甲作者"})
        check("再删一次 -> 404（不静默成功）", r.status, 404)
        r = await cli.post("/api/bridge/ks/author/delete", json={"author": "  "})
        check("空作者名 -> 400", r.status, 400)

        # ---- 7. 备份份数上限 ----
        for _i in range(B.KS_KEEP_BACKUPS + 3):
            B._ks_backup_ledger(prim, os.path.join(prim, "download_log.json"))
        n = len([f for f in os.listdir(bak) if f.endswith(".json")])
        check_true("反复备份后仍只留上限 %d 份" % B.KS_KEEP_BACKUPS, n == B.KS_KEEP_BACKUPS, n)

        # ---- 8. 手动主页映射 ----
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者", "url": "https://www.kuaishou.com/profile/3xabc123"})
        check("保存主页映射 HTTP", r.status, 200)
        au = (await (await cli.get("/api/bridge/ks/authors")).json())["authors"]
        check("作者列表里立刻有链接", au[0]["profileUrl"],
              "https://www.kuaishou.com/profile/3xabc123")
        check("标记为手动来源", au[0]["profileSource"], "manual")
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者", "url": "不是链接"})
        check("非法链接 -> 400", r.status, 400)
        r = await cli.post("/api/bridge/ks/author/profile", json={"author": "", "url": "x"})
        check("空作者名 -> 400", r.status, 400)

        # ---- 8b. 分享短链绝不能当成「主页 ID」 ----
        # 2026-09-29 的真实事故（同一轮里的第二个 bug）：界面上写着
        # 「打开作者主页 → 分享 → 复制链接」，用户照做贴了
        # https://v.kuaishou.com/KqKU900L；旧实现把末段 `KqKU900L` 当成 ID，
        # 存成 https://www.kuaishou.com/profile/KqKU900L —— 保存还提示"成功了"，
        # 之后「更新」拿一个不存在的主页，一个作品都抓不到，表现就是「下载不了」。
        # 短链其实**能直接下载**（下载器自己会跟随跳转），所以正解是原样存。
        # 注：此时台账里只剩「乙作者」（甲作者在上面的删除测试里被删掉了）。
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者", "url": "https://v.kuaishou.com/KqKU900L"})
        check("分享短链可以保存", r.status, 200)
        body = await r.json()
        check("短链原样保存（不臆造成 /profile/<分享码>）",
              body["profileUrl"], "https://v.kuaishou.com/KqKU900L")
        check_true("没有为短链伪造 profileId", body["profileId"] is None, body["profileId"])
        check("回 resolved=false 让前端如实提示", body["resolved"], False)

        au = (await (await cli.get("/api/bridge/ks/authors")).json())["authors"]
        row = [a for a in au if a["nickname"] == "乙作者"][0]
        check("作者列表里给的就是这条短链", row["profileUrl"],
              "https://v.kuaishou.com/KqKU900L")
        check_true("有链接 → 界面会给「更新」而不是「设置主页」",
                   bool(row["profileUrl"]), row["profileUrl"])

        # 形态归一：规范主页链接 / 裸 ID 都要能收，并去掉 query
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者",
                                 "url": "https://www.kuaishou.com/profile/3xdeadbeef00?x=1"})
        check("主页链接归一成规范地址", (await r.json())["profileUrl"],
              "https://www.kuaishou.com/profile/3xdeadbeef00")
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者", "url": "3x1234567890"})
        check("裸作者 ID 也能收", (await r.json())["profileUrl"],
              "https://www.kuaishou.com/profile/3x1234567890")

        # 拒绝：非快手域名 / 快手域名但拿不到作者 ID
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者",
                                 "url": "https://www.douyin.com/user/abc"})
        check("非快手域名 -> 400", r.status, 400)
        r = await cli.post("/api/bridge/ks/author/profile",
                           json={"author": "乙作者",
                                 "url": "https://www.kuaishou.com/short-video/3xabc"})
        check("快手域名但无 /profile/ -> 400", r.status, 400)

    os.remove(prof)
    B.ks_index(force=True)


# ================================================================ B 段：真实副本
async def phase_b(tmp):
    global PASS, FAIL
    if not REAL_ITEMS:
        print("\n（跳过 B 段：本机没有 %s）" % REAL_LEDGER)
        return
    print()
    print("=" * 66)
    print("B 段：真实台账（只读）—— 聚合 / 主页推断 / 媒体")
    print("=" * 66)
    prim = os.path.join(tmp, "b-primary")
    os.makedirs(prim, exist_ok=True)
    # primary 复制工具箱自己那份（那 1 条「就这样飞」），历史根指向真实目录（只读）
    real_small = os.path.join(B.TOOLBOX_ROOT, "data", "kuaishou", "download_log.json")
    if os.path.isfile(real_small):
        shutil.copy2(real_small, os.path.join(prim, "download_log.json"))
    check_true("工具箱那份台账已复制到临时区", os.path.isfile(os.path.join(prim, "download_log.json")))

    B.KS_PRIMARY_ROOT = prim
    B.KS_EXTRA_ROOTS = os.path.join(prim, "ledger_roots.json")
    write_json(B.KS_EXTRA_ROOTS, REAL_ROOTS)
    B.KS_LEGACY_ROOTS = []
    # 同 A 段：工具箱落点掐掉（它的那 1 条已经复制进 prim 了，不能再被读第二次）
    B.KS_TOOL_OUTDIR = ""
    B.KS_PROFILE_MAP = os.path.join(prim, "author_profiles.json")
    B.KS_BACKUP_DIR = os.path.join(tmp, "b-backups")
    idx = B.ks_index(force=True)

    app = B.make_app()
    async with TestClient(TestServer(app)) as cli:
        st = await (await cli.get("/api/bridge/ks/stats")).json()
        with_file = sum(1 for x in REAL_ITEMS if x.get("file"))
        small_n = 1 if os.path.isfile(
            os.path.join(B.TOOLBOX_ROOT, "data", "kuaishou", "download_log.json")) else 0
        check_true("索引条数 = 历史台账有 file 的 %d 条 + 工具箱那份 %d 条"
                   % (with_file, small_n), st["records"] in (with_file, with_file + small_n),
                   st["records"])
        check_true("规模是真实的（>1000 条）", st["records"] > 1000, st["records"])
        check_true("作者数 >= 4", st["authors"] >= 4, st["authors"])

        au = (await (await cli.get("/api/bridge/ks/authors")).json())["authors"]
        byname = {a["nickname"]: a for a in au}
        check_true("「%s」被识别出来" % REAL_AUTHOR, REAL_AUTHOR in byname)
        if REAL_AUTHOR in byname:
            check("主页 ID 推断正确（来自 works_list 交集）",
                  byname[REAL_AUTHOR]["profileId"], REAL_AUTHOR_PID)
            check("来源标记为 auto", byname[REAL_AUTHOR]["profileSource"], "auto")
            check_true("URL 拼得对", byname[REAL_AUTHOR]["profileUrl"].endswith(REAL_AUTHOR_PID))

        # 媒体：取一条真实存在的作品
        d = await (await cli.get("/api/bridge/ks/works?limit=1")).json()
        if d["items"] and d["items"][0]["localKind"]:
            pid = d["items"][0]["photoId"]
            ent = B.ks_media_lookup(pid)
            r = await cli.get("/api/bridge/ks/media?id=%s" % pid,
                              headers={"Range": "bytes=100-199"})
            check("真实文件 Range -> 206", r.status, 206)
            check("Range 返回 100 字节", len(await r.read()), 100)
            check("Content-Range 的总长 = 文件实际字节数",
                  r.headers.get("Content-Range"), "bytes 100-199/%d" % ent["size"])
            # 全量请求字节数与台账记的 size 必须一致 —— 证明读到的是同一个文件
            r = await cli.get("/api/bridge/ks/media?id=%s" % pid)
            check("全量字节数 = 台账里的 size", len(await r.read()), ent["size"])

    # 原始台账未被触碰
    with open(REAL_LEDGER, "r", encoding="utf-8-sig") as f:
        still = json.load(f)
    check("原始台账条数未变", len(still), len(REAL_ITEMS))
    check_true("原始台账文件仍在原处", os.path.isfile(REAL_LEDGER))

    B.ks_index(force=True)


async def main():
    tmp = tempfile.mkdtemp(prefix="ks-archive-test-")
    keep = dict(primary=B.KS_PRIMARY_ROOT, extra=B.KS_EXTRA_ROOTS, legacy=B.KS_LEGACY_ROOTS,
                prof=B.KS_PROFILE_MAP, bak=B.KS_BACKUP_DIR)
    try:
        await phase_a(tmp)
        await phase_b(tmp)
    finally:
        B.KS_PRIMARY_ROOT = keep["primary"]
        B.KS_EXTRA_ROOTS = keep["extra"]
        B.KS_LEGACY_ROOTS = keep["legacy"]
        B.KS_PROFILE_MAP = keep["prof"]
        B.KS_BACKUP_DIR = keep["bak"]
        B.ks_index(force=True)
        shutil.rmtree(tmp, ignore_errors=True)
        left = os.path.join(B.TASK_DIR, "ks-backups")
        print("\n（测试用的临时目录已清理；真实备份目录 %s %s）"
              % (left, "存在" if os.path.isdir(left) else "未创建（本次没真删过真实作者）"))

    print()
    print("=" * 66)
    print("通过 %d 项，失败 %d 项" % (PASS, FAIL))
    print("=" * 66)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
