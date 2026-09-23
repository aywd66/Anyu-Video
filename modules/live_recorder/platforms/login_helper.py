# -*- coding: utf-8 -*-
"""
交互式登录助手（命令行）

用途
----
快手网页端对**未登录访客**关闭了「按房间查询」的接口（``livedetail`` 对任意
房间号都返回同一份占位响应），并以「请求过快」拦截自动化访问。实测：无头
Chromium、有头 Chromium、有头真实 Chrome **三者都会被拦截**，说明这不是
浏览器指纹问题，而是访客级别的访问限制。

因此，要监控**任意指定主播**，唯一可靠的途径是带上登录后的 Cookie。

注意
----
快手首页 ``live.kuaishou.com`` **不会自动弹出二维码**，必须先点右上角
「登录」才会出现扫码弹窗。本模块复用 :mod:`platforms.qr_login` 的完整流程
（打开页面 → 点出登录弹窗 → 等待扫码 → 导出 Cookie），并在终端同步打印进度。

命令行使用::

    python login.py kuaishou     # 登录快手
    python login.py douyin       # 登录抖音
    python login.py              # 两个平台依次登录
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Callable, Optional

# 允许以脚本方式直接运行（python login.py）
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_recorder.config.settings import ConfigStore  # noqa: E402
from live_recorder.platforms.qr_login import (  # noqa: E402
    SPECS,
    PlaywrightUnavailable,
    QrLoginSession,
    platform_label,
)
from live_recorder.utils.logger import configure_logging, get_logger  # noqa: E402

logger = get_logger(__name__)

#: 兼容旧代码：平台 -> 配置字段名
PLATFORMS = {name: spec for name, spec in SPECS.items()}


def interactive_login(
    platform: str,
    *,
    timeout: float = 300.0,
    on_status: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """打开浏览器让用户扫码登录，返回 Cookie 字符串（未登录成功则返回 ``None``）。

    参数
    ----
    platform:  ``kuaishou`` 或 ``douyin``
    timeout:   等待扫码的最长秒数
    on_status: 进度回调（用于界面提示或控制台输出）
    """
    spec = SPECS.get(platform)
    if spec is None:
        raise ValueError(f"暂不支持的平台：{platform}")

    label = platform_label(platform)

    def notify(message: str) -> None:
        if on_status is not None:
            on_status(message)
        else:
            print(message, flush=True)

    session = QrLoginSession(platform, headless=False, on_status=notify)
    try:
        session.open()
        if session.is_logged_in():
            notify(f"{label}已经是登录状态，直接导出凭证。")
            return session.cookie_string()

        notify(f"二维码已显示在浏览器窗口，请用手机{label} App 扫码并确认。")
        notify(f"（提示：{spec['hint']}）")

        deadline = time.monotonic() + timeout
        next_hint = 0
        while time.monotonic() < deadline:
            if session.is_logged_in():
                cookie = session.cookie_string()
                notify(f"已获取{label}登录凭证，共 {session.cookie_count()} 个 Cookie。")
                return cookie or None
            remaining = int(deadline - time.monotonic())
            if remaining <= 60 and remaining % 20 == 0 and remaining != next_hint:
                next_hint = remaining
                notify(f"等待扫码中……剩余 {remaining} 秒")
            time.sleep(1.0)

        notify(f"等待{label}登录超时，未保存任何内容。")
        return None
    except PlaywrightUnavailable as exc:
        notify(f"无法启动浏览器：{exc}")
        return None
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            logger.debug("关闭登录会话失败", exc_info=True)


def save_cookie(platform: str, cookie: str) -> Path:
    """把 Cookie 写入配置文件并返回配置路径。"""
    spec = SPECS[platform]
    store = ConfigStore().load()
    setattr(store.config, spec["config_field"], cookie)
    store.config.normalize()
    store.save()
    return store.path


def login_and_save(
    platform: str,
    *,
    timeout: float = 300.0,
    on_status: Optional[Callable[[str], None]] = None,
) -> bool:
    """登录并保存，返回是否成功。"""
    cookie = interactive_login(platform, timeout=timeout, on_status=on_status)
    if not cookie:
        return False
    path = save_cookie(platform, cookie)
    message = f"凭证已保存到：{path}"
    (on_status or print)(message)
    return True


def main(argv: list[str]) -> int:
    """命令行入口。"""
    configure_logging()
    targets = [a.lower() for a in argv[1:] if a.lower() in SPECS]
    if not targets:
        targets = ["kuaishou", "douyin"]

    print("=" * 62)
    print("直播录制器 · 扫码登录助手")
    print("=" * 62)
    print(
        "说明：快手对未登录访客限制了按房间查询的接口，并会拦截自动化访问。\n"
        "      登录一次后，即可稳定监控你指定的任意主播。\n"
        "      接下来会自动打开登录页并弹出二维码，请用手机 App 扫码。\n"
        "      本程序只把 Cookie 用于向你关注的平台发起只读的直播间查询请求。\n"
    )

    ok_count = 0
    for platform in targets:
        label = platform_label(platform)
        print("-" * 62)
        print(f"【{label}】")
        try:
            if login_and_save(platform):
                ok_count += 1
        except PlaywrightUnavailable as exc:
            print(f"  ✘ {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✘ 登录过程出错：{type(exc).__name__}: {exc}")

    print("-" * 62)
    if ok_count:
        print(f"完成：成功登录 {ok_count} 个平台。现在可以双击 start.bat 启动程序。")
        return 0
    print(
        "未完成任何平台的登录。\n"
        "你可以稍后重试；也可以在程序里点击「设置 → 账号 → 扫码登录」，\n"
        "或手动把 Cookie 粘贴到该页的输入框里。"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
