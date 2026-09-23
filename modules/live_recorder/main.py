# -*- coding: utf-8 -*-
"""
程序入口

用法
----
::

    python main.py               # 启动图形界面
    python main.py --selftest    # 无界面自检（检查依赖、ffmpeg、配置读写、平台解析）
    python main.py --version     # 输出版本号

仅供个人学习与离线备份使用，禁止用于商业用途或内容二次分发。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 保证以脚本方式直接运行时也能正确导入包内模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_recorder import __app_name__, __version__
from live_recorder.config.settings import ConfigStore, RoomEntry
from live_recorder.utils.logger import configure_logging, get_logger

logger = get_logger(__name__)


def run_selftest() -> int:
    """无界面自检：逐项检查运行环境并输出报告。"""
    print(f"\n=== {__app_name__} v{__version__} 环境自检 ===\n")
    failures: list = []

    # 1) 依赖检查
    print("[1/5] 检查 Python 依赖……")
    for module_name in ("PyQt6", "httpx"):
        try:
            __import__(module_name)
            print(f"      ✔ {module_name}")
        except ImportError:
            print(f"      ✘ {module_name} 未安装（pip install -r requirements.txt）")
            failures.append(module_name)

    optional = {}
    for module_name in ("playwright", "requests"):
        try:
            __import__(module_name)
            optional[module_name] = True
        except ImportError:
            optional[module_name] = False

    # 2) ffmpeg 检查
    print("\n[2/5] 检查 ffmpeg……")
    from live_recorder.recorder.ffmpeg_utils import (
        ffmpeg_available,
        ffmpeg_version,
        resolve_ffmpeg,
    )

    if ffmpeg_available("ffmpeg"):
        print(f"      ✔ {ffmpeg_version('ffmpeg')[:70]}")
        print(f"      ✔ 路径：{resolve_ffmpeg('ffmpeg')}")
    else:
        print("      ✘ 未找到可用的 ffmpeg，录制功能不可用")
        failures.append("ffmpeg")

    # 3) 配置读写检查
    # 注意：这里必须用临时文件做读写验证。自检是「只读诊断」，
    # 若直接 save() 到真实配置文件，会把用户已保存的监控清单覆盖成空/演示数据。
    print("\n[3/5] 检查配置读写……")
    import tempfile

    real_path = ConfigStore().path
    with tempfile.TemporaryDirectory(prefix="lr_selftest_") as tmp_dir:
        probe = ConfigStore(Path(tmp_dir) / "config.json")
        probe.load()
        probe.config.normalize()
        probe.rooms.append(
            RoomEntry(platform="douyin", raw_input="https://live.douyin.com/123456789",
                      room_id="123456789", nickname="自检示例")
        )
        try:
            probe.save()
            reloaded = ConfigStore(probe.path).load()
            ok = len(reloaded.rooms) == 1 and reloaded.rooms[0].room_id == "123456789"
            if ok:
                print("      ✔ 配置读写正常（使用临时文件验证，未改动实际配置）")
                print(f"      ✔ 实际配置文件：{real_path}")
                print(f"        {'已存在' if real_path.exists() else '尚未创建（首次保存时生成）'}")
            else:
                print("      ✘ 配置回读结果不一致")
                failures.append("config")
        except OSError as exc:
            print(f"      ✘ 配置写入失败：{exc}")
            failures.append("config")

    # 保存目录从实际配置读取，保证报告反映用户真实设置
    store = ConfigStore()
    store.load()

    # 4) 保存目录检查
    print("\n[4/5] 检查保存目录与磁盘空间……")
    from live_recorder.recorder.storage import ensure_dir, get_free_space_gb

    try:
        target = ensure_dir(store.config.save_path)
        free_gb = get_free_space_gb(store.config.save_path)
        print(f"      ✔ 保存目录：{target}")
        print(
            f"      ✔ 剩余空间：{free_gb:.1f} GB（阈值 {store.config.min_free_disk_gb} GB）"
            + ("  ⚠ 低于阈值" if free_gb < store.config.min_free_disk_gb else "")
        )
    except OSError as exc:
        print(f"      ✘ 保存目录不可用：{exc}")
        failures.append("storage")

    # 5) 平台解析层检查（离线部分：只做链接解析，不联网）
    print("\n[5/5] 检查平台解析层（离线解析）……")
    from live_recorder.platforms import get_parser
    from live_recorder.platforms.registry import detect_platform

    samples = [
        ("https://live.douyin.com/123456789", "douyin"),
        ("https://v.douyin.com/iABCDEF/", "douyin"),
        ("https://live.kuaishou.com/u/3xf8dh7tqmd9bps", "kuaishou"),
        ("https://v.kuaishou.com/abcDEF", "kuaishou"),
    ]
    for text, expected in samples:
        detected = detect_platform(text)
        mark = "✔" if detected == expected else "✘"
        print(f"      {mark} 识别 {text[:46]:<48} -> {detected}")
        if detected != expected:
            failures.append(f"detect:{text}")

    # 房间号解析（不涉及网络）
    try:
        douyin = get_parser("douyin")
        rid = douyin.resolve_room("https://live.douyin.com/123456789")
        ok = rid == "123456789"
        print(f"      {'✔' if ok else '✘'} 抖音房间号解析 -> {rid}")
        if not ok:
            failures.append("douyin.resolve_room")

        kuaishou = get_parser("kuaishou")
        kid = kuaishou.resolve_room("https://live.kuaishou.com/u/3xf8dh7tqmd9bps")
        ok = kid == "3xf8dh7tqmd9bps"
        print(f"      {'✔' if ok else '✘'} 快手用户ID解析 -> {kid}")
        if not ok:
            failures.append("kuaishou.resolve_room")
    except Exception as exc:  # noqa: BLE001
        print(f"      ✘ 解析层异常：{exc}")
        failures.append("parser")

    # 汇总
    print("\n--- 可选组件 ---")
    for name, present in optional.items():
        print(f"      {'✔' if present else '○'} {name}{'' if present else '（未安装，不影响基础功能）'}")
    if not optional.get("playwright"):
        print("        提示：抖音接口带动态签名时，安装 playwright 可显著提升解析成功率：")
        print("              pip install playwright && playwright install chromium")

    print("\n=== 自检结果 ===")
    if failures:
        print(f"  ✘ 发现 {len(failures)} 项问题：{', '.join(failures)}")
        return 1
    print("  ✔ 全部通过，可以直接运行：python main.py")
    return 0


def run_gui() -> int:
    """启动图形界面。"""
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        # 依赖缺失时给出「当前用的是哪个解释器」+ 可直接复制粘贴的修复命令，
        # 避免用户把依赖装到另一个 Python 上后反复踩坑。
        print("未安装 PyQt6，无法启动图形界面。", file=sys.stderr)
        print(f"当前使用的解释器：{sys.executable}", file=sys.stderr)
        print("", file=sys.stderr)
        print("请任选一种方式安装依赖：", file=sys.stderr)
        print("  方式一：直接装进当前解释器", file=sys.stderr)
        print(f'      "{sys.executable}" -m pip install -r requirements.txt', file=sys.stderr)
        print("  方式二：在项目内建虚拟环境（推荐，Windows 可双击 setup.bat）", file=sys.stderr)
        print(f'      "{sys.executable}" -m venv .venv', file=sys.stderr)
        print("      .venv\\Scripts\\python.exe -m pip install -r requirements.txt", file=sys.stderr)
        print("", file=sys.stderr)
        print("说明：start.bat 会优先使用项目下的 .venv，找不到时才回退到 PATH 中的 python。", file=sys.stderr)
        return 2

    # 先加载配置，以便日志目录跟随配置
    store = ConfigStore().load()

    log_dir = Path(store.config.save_path) / "_logs"
    try:
        configure_logging(log_dir=log_dir)
    except OSError:
        configure_logging()

    app = QApplication(sys.argv)
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName(f"{__app_name__} 直播流自动录制工具")
    app.setApplicationVersion(__version__)

    # 延迟导入：确保 QApplication 已创建
    from live_recorder.ui.main_window import MainWindow

    window = MainWindow(store)
    window.show()

    logger.info("%s v%s 界面已启动", __app_name__, __version__)
    return app.exec()


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description=f"{__app_name__} - 快手/抖音直播流自动录制工具（仅供个人学习与离线备份使用）"
    )
    parser.add_argument("--selftest", action="store_true", help="执行无界面环境自检")
    parser.add_argument("--version", action="store_true", help="输出版本号")
    args = parser.parse_args()

    if args.version:
        print(f"{__app_name__} v{__version__}")
        return 0

    if args.selftest:
        configure_logging()
        return run_selftest()

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
