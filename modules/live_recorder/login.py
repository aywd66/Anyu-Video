# -*- coding: utf-8 -*-
"""
登录助手入口

用法
----
::

    python login.py            # 依次登录快手、抖音
    python login.py kuaishou   # 只登录快手
    python login.py douyin     # 只登录抖音

为什么要登录
------------
快手对**未登录访客**关闭了「按房间查询」的接口，并以「请求过快」拦截自动化访问
（实测无头/有头浏览器均被拦截）。登录一次后，即可稳定监控任意指定主播。
Cookie 只会被用于向你关注的平台发起只读的直播间查询请求。

仅供个人学习与离线备份使用，禁止用于商业用途或内容二次分发。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 保证以脚本方式直接运行时也能正确导入包内模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_recorder.platforms.login_helper import main

if __name__ == "__main__":
    sys.exit(main(sys.argv))
