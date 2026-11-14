# -*- coding: utf-8 -*-
"""重复视频扫描（抖音 + 快手）—— 离线自测（零新增依赖，纯标准库 + aiohttp）

跑法（用工程 venv，aiohttp 在里面）：
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_dedup.py

覆盖（「任务记录 → 查找重复视频」这条链路上容易悄悄坏掉的地方）：
  1. 字节完全相同 → 检出为一组；大小唯一 → 不哈希、直接跳过
  2. 只扫视频扩展名，图集图片不参与
  3. 保留规则：优先非辅助目录（edge-manual-profile 等），再比大小、时间
  4. /api/bridge/dedup/scan + /status 启动与轮询
  5. /api/bridge/dedup/delete：空参数 400、越界路径拒绝、不存在拒绝、
     合法路径交给回收站（monkeypatch 掉，不真删）
  6. 单文件失败不拖累其余（回收站逐条回报）
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # .../webui

import bridge_server as B  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   " + label + (("  " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  [FAIL] " + label + (("  " + str(extra)) if extra else ""))


def write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)
    return p


def setup_roots():
    """造两个隔离的临时根（抖音 + 快手），并 monkeypatch 掉扫描/回收的真实现。"""
    tmp = tempfile.mkdtemp(prefix="dedup-test-")
    dy = os.path.join(tmp, "douyin")
    ks = os.path.join(tmp, "kuaishou")
    os.makedirs(dy, exist_ok=True)
    os.makedirs(ks, exist_ok=True)
    B._dedup_roots = lambda: {os.path.realpath(dy): "douyin",
                              os.path.realpath(ks): "kuaishou"}
    recycled = {}
    B._recycle_files = lambda paths: _fake_recycle(paths, recycled)
    return dy, ks, recycled


def _fake_recycle(paths, recycled):
    out = {}
    for p in paths:
        if os.path.exists(p):
            recycled[p] = True
            out[p] = True
        else:
            out[p] = "文件已不存在"
    return out


def test_scan_finds_byte_identical():
    dy, ks, _ = setup_roots()
    a = write(os.path.join(dy, "作者甲", "同一作品_a.mp4"), b"VIDEO" * 1000)
    b = write(os.path.join(ks, "作者甲", "同一作品_b.mp4"), b"VIDEO" * 1000)
    c = write(os.path.join(dy, "作者乙", "唯一作品.mp4"), b"OTHER" * 800)   # 大小唯一，不哈希
    r = B._dedup_scan()
    check("字节相同的两个文件被检出一组", r["group_count"] == 1 and r["dup_count"] == 1, r)
    check("扫描到 3 个视频、哈希了同大小的 2 个", r["scanned"] == 3 and r["hashed"] == 2,
          (r["scanned"], r["hashed"]))
    check("可释放体积 = 冗余文件大小", r["reclaim_bytes"] == 5000, r["reclaim_bytes"])
    g = r["groups"][0]
    check("每组 1 保留 + 1 删除", len(g["dupes"]) == 1 and g["keep"] and g["dupes"][0])
    return a, b, c


def test_unique_size_is_not_hashed():
    dy, ks, _ = setup_roots()
    for i, n in enumerate((10, 20, 30, 40)):
        write(os.path.join(dy, "作者甲", "v%d.mp4" % i), b"x" * n)
    r = B._dedup_scan()
    check("大小全唯一 → 0 次哈希、0 组重复", r["hashed"] == 0 and r["group_count"] == 0, r)


def test_images_are_not_scanned():
    dy, ks, _ = setup_roots()
    write(os.path.join(dy, "图集.jpg"), b"x" * 100)
    write(os.path.join(ks, "图集.webp"), b"x" * 100)
    write(os.path.join(dy, "视频.mp4"), b"x" * 100)
    r = B._dedup_scan()
    check("只扫视频，图片不参与", r["scanned"] == 1, r["scanned"])


def test_keep_rule_prefers_non_helper():
    dy, ks, _ = setup_roots()
    write(os.path.join(ks, "edge-manual-profile", "h.mp4"), b"SAME" * 500)
    real = write(os.path.join(ks, "作者甲", "r.mp4"), b"SAME" * 500)
    r = B._dedup_scan()
    keep = r["groups"][0]["keep"]["path"]
    check("辅助目录里的文件被淘汰、保留真实库那份", keep == real, keep)


def test_keep_rule_newest_wins_on_equal_size():
    dy, ks, _ = setup_roots()
    old = write(os.path.join(dy, "作者甲", "old.mp4"), b"SAME" * 500)
    new = write(os.path.join(dy, "作者甲", "new.mp4"), b"SAME" * 500)
    os.utime(old, (1000000, 1000000))
    os.utime(new, (2000000, 2000000))
    r = B._dedup_scan()
    keep = r["groups"][0]["keep"]["path"]
    check("同大小保留时间新的那份", keep == new, keep)


async def _client():
    server = TestServer(B.make_app())
    client = TestClient(server)
    await client.start_server()
    return client


def test_endpoints():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        dy, ks, recycled = setup_roots()
        inside = write(os.path.join(dy, "作者甲", "a.mp4"), b"x" * 100)
        outside = write(os.path.join(tempfile.mkdtemp(), "外.mp4"), b"x" * 100)
        client = await _client()
        try:
            # scan 启动
            r = await client.post("/api/bridge/dedup/scan")
            check("scan 启动返回 200", r.status == 200, r.status)

            # status 轮询到结束
            done = False
            for _ in range(200):
                s = await client.get("/api/bridge/dedup/status")
                st = await s.json()
                if not st["running"]:
                    done = True
                    break
                await asyncio.sleep(0.05)
            check("status 最终停在 running=False", done)

            # delete：空参数 → 400
            r = await client.post("/api/bridge/dedup/delete",
                                  data=json.dumps({"paths": []}),
                                  headers={"Content-Type": "application/json"})
            check("delete 空参数 400", r.status == 400, r.status)

            # delete：越界路径 → 400 且不回收
            r = await client.post("/api/bridge/dedup/delete",
                                  data=json.dumps({"paths": [outside]}),
                                  headers={"Content-Type": "application/json"})
            d = await r.json()
            check("delete 越界路径 400", r.status == 400, (r.status, d))
            check("越界路径未被回收", outside not in recycled)

            # delete：合法路径 → 200 且交给回收站
            r = await client.post("/api/bridge/dedup/delete",
                                  data=json.dumps({"paths": [inside, os.path.join(dy, "不存在.mp4")]}),
                                  headers={"Content-Type": "application/json"})
            d = await r.json()
            check("delete 合法路径 200", r.status == 200, d)
            check("合法路径被回收、不存在路径被拒绝",
                  recycled.get(inside) is True and d.get("deleted") == 1, d)
        finally:
            await client.close()

    loop.run_until_complete(_run())
    loop.close()


def main():
    print("=== 重复视频扫描离线自测 ===")
    test_unique_size_is_not_hashed()
    test_images_are_not_scanned()
    test_keep_rule_prefers_non_helper()
    test_keep_rule_newest_wins_on_equal_size()
    test_scan_finds_byte_identical()
    test_endpoints()
    print("\n结果：%d 通过 / %d 失败" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
