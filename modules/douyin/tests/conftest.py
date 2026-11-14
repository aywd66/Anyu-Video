# -*- coding: utf-8 -*-
"""pytest 全局夹具。

目前只做一件事：**把「作品列表接口风控冷却」标记隔离掉**。

那个标记是给「一键更新」提速用的：命中过 403 就落一个 30 分钟 TTL 的文件，
期间后续任务直接走浏览器兜底、不再白试接口。它是**跨进程的真实运行时状态**，
放在 `modules/douyin/.state/post_api_blocked.json`。

问题在于：谁在本机真跑过一次（哪怕是我手动验证），这个文件就留在盘上，
于是所有走 `_collect_api_items` 的用例都会在第一行被短路成「分页受限」——
21 个用例集体变红，而代码其实是对的。测试必须与环境无关，所以每个用例
开始前把它清掉。
"""
import os

import pytest


@pytest.fixture(autouse=True)
def _clear_post_api_block_marker():
    """每个用例前删掉风控标记文件（不存在也没关系）。"""
    try:
        from core.user_modes.post_strategy import _POST_API_BLOCK_MARKER
    except Exception:  # pragma: no cover — 极早期导入失败时不阻塞测试
        yield
        return
    try:
        os.remove(_POST_API_BLOCK_MARKER)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    yield
