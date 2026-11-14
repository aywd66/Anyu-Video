# -*- coding: utf-8 -*-
"""本地媒体索引 + 播放流接口 —— 离线自测（零新增依赖，纯标准库 + aiohttp）

跑法（用工程 venv，aiohttp 在里面）：
    runtime\\douyin-venv\\Scripts\\python.exe modules\\douyin\\webui\\tests\\test_media_api.py

覆盖（都是「任务记录点封面播放」这条链路上容易悄悄坏掉的地方）：
  1. 文件名里的 aweme_id 抽取：短数字（1080/_01/日期）不能当 id
  2. 同一作品多清晰度时视频优先；图集按文件名排序
  3. attach_media 给列表补的 local / local_kind / local_images
  4. /api/bridge/media：200 + video 类型 + Accept-Ranges
  5. Range 请求返回 206 与正确的 Content-Range（<video> 拖进度条靠它）
  6. 缺 id / 非数字 id / 本地没有的 id 分别 400 / 400 / 404
  7. 路径穿越（id 里塞 ..）不可能落到文件系统
"""
import asyncio
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # .../webui

from aiohttp import web                              # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

import bridge_server as B                            # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   " + label + (("  " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        print("  [FAIL] " + label + (("  " + str(extra)) if extra else ""))


def build_fixture(root):
    """造一棵和真实落盘一致的目录树：<作者>_<uid>/videos|images/..._<aweme_id>..."""
    v_ok = "1234567890123456789"      # 19 位，正常作品
    v_dup = "2345678901234567890"     # 同作品两个清晰度
    img = "3456789012345678901"       # 图集 3 张
    short = "4567890123456789012"     # 文件名里还混着 1080 / 0 这类短数字

    def put(rel, data=b"x" * 4096):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    put(r"作者甲_abcd/videos/看看这个_%s_normal_1080_0.mp4" % v_ok, b"\x00\x00\x00\x18ftypisom" + b"v" * 4096)
    put(r"作者甲_abcd/videos/重复作品_%s_adapt_lowest_1080_1.mp4" % v_dup)
    put(r"作者乙_efgh/videos/重复作品_%s_normal_720_0.mp4" % v_dup)
    for i in (1, 2, 3):
        put(r"作者丙_ijkl/images/图集_%s_%02d.webp" % (img, i))
    put(r"作者甲_abcd/videos/短数字不能当id_%s_1080_0.mp4" % short)
    put(r"作者甲_abcd/videos/没有id的文件_normal.mp4")          # 无 id，不入索引
    put(r"作者丙_ijkl/ledger/日志.jsonl", b"{}")                # 非媒体，不入索引
    return v_ok, v_dup, img, short


def main():
    tmp = tempfile.mkdtemp(prefix="media-test-")
    try:
        v_ok, v_dup, img, short = build_fixture(tmp)

        # 把「候选下载根」指向临时目录：不动真实配置，也不扫真实磁盘
        B._media_roots = lambda: [tmp]
        B._MEDIA.update(map={}, at=0.0, miss_at=0.0)

        print("== 1. 索引构建 ==")
        idx, nfiles = B._media_scan()
        print("  索引 %d 个 id / 扫到 %d 个媒体文件" % (len(idx), nfiles))
        check("认出 19 位 id", v_ok in idx)
        check("短数字（1080/0/日期）没被当成 id", "1080" not in idx and "0" not in idx)
        check("同一作品多清晰度：视频取一个（不重复）", idx[v_dup]["video"] is not None)
        check("视频数=1", bool(idx[v_dup]["video"]) and len(idx[v_dup]["images"]) == 0)
        check("图集 3 张且有序", len(idx[img]["images"]) == 3, idx[img]["images"])
        check("图集顺序 _01/_02/_03",
              [os.path.basename(p).rsplit("_", 1)[1][:2] for p in idx[img]["images"]] == ["01", "02", "03"])
        check("无 id 的文件不入索引（但照样算扫到的文件）", nfiles == 8, nfiles)
        check("索引里只有 4 个作品（去重后）", len(idx) == 4, sorted(idx))
        check("has_media 标记正确", idx[v_ok]["has_media"] is True)

        print("\n== 2. attach_media（列表接口补的字段） ==")
        items = [{"aweme_id": v_ok}, {"aweme_id": short}, {"aweme_id": img},
                 {"aweme_id": "9999999999999999999"}]
        B.attach_media(items)
        for it in items:
            print("   ", it["aweme_id"], "local=%s kind=%s imgs=%s"
                  % (it["local"], it["local_kind"], it["local_images"]))
        check("视频 → kind=video", items[0]["local"] and items[0]["local_kind"] == "video")
        check("（同一条已被索引覆盖）", items[1]["local_kind"] == "video")
        check("图集 → kind=image + 张数", items[2]["local_kind"] == "image" and items[2]["local_images"] == 3)
        check("本地没有的 → local=False（前端据此不给播放按钮）",
              items[3]["local"] is False and items[3]["local_kind"] is None)

        print("\n== 3. /api/bridge/media ==")
        asyncio.run(serve_checks(v_ok, img))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n通过 %d 项，失败 %d 项" % (PASS, FAIL))
    return 1 if FAIL else 0


async def serve_checks(v_ok, img):
    app = web.Application()
    app.router.add_get("/api/bridge/media", B.h_media)
    async with TestServer(app) as srv:
        client = TestClient(srv)
        await client.start_server()

        r = await client.get("/api/bridge/media", params={"id": v_ok})
        body = await r.read()
        check("正常 id → 200", r.status == 200, r.status)
        check("Content-Type 是 video/mp4", r.headers.get("Content-Type") == "video/mp4",
              r.headers.get("Content-Type"))
        check("声明 Accept-Ranges: bytes", r.headers.get("Accept-Ranges") == "bytes")
        check("返回的就是那个文件的字节", body[4:8] == b"ftyp", body[4:8])

        r = await client.get("/api/bridge/media", params={"id": v_ok},
                             headers={"Range": "bytes=1024-2047"})
        body = await r.read()
        check("Range → 206", r.status == 206, r.status)
        check("Content-Range 正确", r.headers.get("Content-Range") == "bytes 1024-2047/4108",
              r.headers.get("Content-Range"))
        check("只回 1024 字节", len(body) == 1024, len(body))

        r = await client.get("/api/bridge/media", params={"id": img, "kind": "image", "i": 1})
        check("图集第 2 张 → 200 image/*",
              r.status == 200 and (r.headers.get("Content-Type") or "").startswith("image/"),
              r.headers.get("Content-Type"))
        await r.read()

        r = await client.get("/api/bridge/media", params={"id": img, "kind": "image", "i": 99})
        check("图集越界索引 → 回退到最后一张（不 500）", r.status == 200, r.status)
        await r.read()

        r = await client.get("/api/bridge/media")
        check("缺 id → 400", r.status == 400, r.status)
        await r.read()
        r = await client.get("/api/bridge/media", params={"id": "../../config.yml"})
        check("路径穿越 → 400（100% 落不到文件系统）", r.status == 400, r.status)
        await r.read()
        r = await client.get("/api/bridge/media", params={"id": "9999999999999999999"})
        d = await r.json()
        check("本地没有 → 404 且带原因", r.status == 404 and "本地未找到" in d.get("error", ""),
              d.get("error"))
        await client.close()


if __name__ == "__main__":
    sys.exit(main())
