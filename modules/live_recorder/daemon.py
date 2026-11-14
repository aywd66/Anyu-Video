# -*- coding: utf-8 -*-
"""
无界面录制守护进程（web 控制台的后端内核）

定位
----
本项目原本只有一种运行形态：``main.py`` 拉起 PyQt6 主窗口，由窗口持有
``MonitorEngine``。网页（WebUI）只能做「状态总览 + 唤起那个窗口」。

本模块把录制内核从窗口里剥出来：**在单个无界面进程内持有同一个
``MonitorEngine``**，通过本机回环 HTTP 暴露控制面，让网页直接驱动录制。
PyQt6 窗口仍然可以启动，作为可选的高级入口（改名、导出日志等）。

为什么这件事是可行的
--------------------
业务层（``core/ recorder/ platforms/ config/ utils/``）**零 PyQt 依赖**，
``MonitorEngine`` 早已具备完整的程序化控制面（``start/stop/add_room/
remove_room/set_room_enabled/stop_room_recording/force_check_now/
snapshot/counts``）。需要搬走的只有 ``ui/`` 那约 2000 行里的交互部分。

关键约束（违反必坏）
--------------------
1. **单个 ``MonitorEngine`` 只能被一个进程持有。** ``ConfigStore.save()`` 是
   ``asdict(self.config)`` 全量写盘，两个进程同时改 config.json 会互相覆盖
   —— 这是本项目记录过的真实故障。因此本进程用**独占锁文件**保证单实例，
   且网页侧不再直接写 config.json（改配置走本进程的接口）。
2. **``QrLoginSession`` 不是线程安全的**（Playwright 同步 API 要求
   创建与使用在同一线程）。扫码会话固定由一个专职线程串行处理。
3. **不能 import 任何 PyQt 模块**，否则无界面环境下会连带拉进 Qt 依赖链。

命令行
------
::

    python daemon.py                    # 前台运行（Ctrl+C 优雅退出）
    python daemon.py --port 8519        # 换端口
    python daemon.py --no-auto-start    # 即使配置里开着自动监控也不自动开
    python daemon.py --check            # 探测是否已有守护进程在跑，打印 JSON

仅供个人学习与离线备份使用，禁止用于商业用途或内容二次分发。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import threading
import time
from collections import deque
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# 保证以脚本方式直接运行时也能正确导入包内模块
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live_recorder import __app_name__, __version__
from live_recorder.config.settings import ConfigStore, RoomEntry
from live_recorder.core.events import ENGINE_EVENTS, EventBus
from live_recorder.core.monitor import MonitorEngine
from live_recorder.utils.logger import configure_logging, get_logger

logger = get_logger(__name__)

#: 默认端口。刻意与 WebUI 桥接服务的 8518 分开：
#: 两者职责不同（桥接服务是页面服务 + 模块总控，本进程只持有录制内核），
#: 且桥接服务可能被用户单独重启，不该连累正在进行的录制。
DEFAULT_PORT = 8519

#: 锁文件名。桥接服务要读它拿 pid，所以这里留成模块级常量：
#: 名字只能有一个来源，否则两处各写一个字符串，改名时必然漏改一处。
_LOCK_NAME = ".daemon.lock"


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _app_dir() -> Path:
    return Path(__file__).resolve().parent


def _lock_path() -> Path:
    """锁文件位置：优先用环境变量（测试/多实例调试用），默认在模块目录下。"""
    override = os.environ.get("LIVE_RECORDER_DAEMON_LOCK")
    if override:
        return Path(override)
    return _app_dir() / _LOCK_NAME


def _port_responding(port: int, timeout: float = 0.6) -> bool:
    """端口上是否有人应答（TCP 连接语义，不做 HTTP 请求）。

    为什么需要它：venv 的 ``python.exe`` 是**重新 exec 的启动器 stub**，
    ``Popen()`` 拿到的父 pid 与真正跑代码的子 pid 不同，且 stub 会立刻退出。
    只看 pid 存活会得出「进程已死」的错误结论，进而在已有守护进程时
    再起一个（两个 ``MonitorEngine`` 同时改 config.json → 静默互相覆盖）。
    端口应答是**唯一不受 stub 影响**的在线证据。
    """
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        return True
    except OSError:
        return False
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass



def _json_default(obj: Any) -> Any:
    """``json.dumps`` 的兜底编码器。

    ``MonitorEngine.snapshot()`` 的行里带 ``last_check``（``datetime``），
    直接 ``json.dumps`` 会抛 ``TypeError`` —— 而且是在**写响应体**的时候抛，
    此时 HTTP 状态行还没发出去，调用方只会看到连接被重置，
    报错信息完全没有指向性（实测踩过）。所以这里统一兜底。
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    return str(obj)


def _pid_alive(pid: int) -> bool:
    """判断 pid 是否活着。Windows 下用 OpenProcess 探测，避免引入 psutil。

    ⚠️ ``OpenProcess`` 失败**不等于**进程已死 —— 打开系统进程（如 pid 4
    ``System``）会因权限不足返回 ``ERROR_ACCESS_DENIED``(5)，而 pid 不存在
    返回 ``ERROR_INVALID_PARAMETER``(87)（实测两者都拿不到句柄）。
    把权限不足当「已死」会让锁被错误接管；这里按保守方向处理：
    **拿不到句柄但错误码不是「无效参数」一律视为活着**（宁可拒绝启动，
    也不要在旧进程还活着时接管它）。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87
        k32 = ctypes.windll.kernel32
        k32.SetLastError(0)
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return k32.GetLastError() != ERROR_INVALID_PARAMETER
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class SingleInstanceLock:
    """独占锁：保证同机只有一个录制守护进程。

    为什么必须是「独占」而不是「检测后提示」：两个守护进程各持一份
    ``MonitorEngine``，退出时都会 ``ConfigStore.save()`` 全量写盘，
    后来者会把前者的房间清单/登录态原样盖掉，且不报任何错。
    宁可第二个进程直接拒绝启动，也不要允许这种静默数据损坏。

    实现上刻意用 pid 探测而不是文件锁（``fcntl``/``msvcrt``）：
    Windows 的 ``msvcrt.locking`` 在进程被强杀时不会自动释放，
    留下一把永远锁不上的锁，反而更糟。pid 探测在强杀场景下自愈。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.held = False
        self.owner_pid: Optional[int] = None  # 冲突时记录占用者的 pid
        self.owner_port: Optional[int] = None

    def acquire(self, port: int) -> bool:
        """尝试获取锁。返回 False 表示已有存活实例。

        判活顺序刻意是「先看端口，再看 pid」：
        venv 的 ``python.exe`` 是启动器 stub，pid 存活与否**不可靠**
        （stub 退出而真身还在，或反之）。端口有应答才是在线的确定性证据；
        端口不可用时再退到 pid 判断（覆盖「进程在跑但还没绑上端口」的窗口期）。
        """
        existing = self._read()
        if existing:
            pid = int(existing.get("pid") or 0)
            old_port = int(existing.get("port") or 0)
            # 1) 端口还应答 → 一定有实例在跑（stub 的存在与否都不影响这个结论）
            if old_port and _port_responding(old_port):
                self.owner_pid = pid
                self.owner_port = old_port
                return False
            # 2) 端口不应答，但记录的 pid 还活着 → 保守拒绝（可能是启动中的窗口期）
            if pid and pid != os.getpid() and _pid_alive(pid):
                self.owner_pid = pid
                self.owner_port = old_port
                return False
        self._write(port)
        self.held = True
        return True

    def release(self) -> None:
        """释放锁。只删自己写的那份（避免误删后来者的锁）。"""
        if not self.held:
            return
        current = self._read()
        if current and int(current.get("pid") or 0) == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False

    def _read(self) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, port: int) -> None:
        """写锁文件。

        同时记下 ``ppid``：venv 的 ``python.exe`` 是重新 exec 的启动器 stub，
        拉我们的那个父进程（stub）会先退出，记下来方便排查「明明起来了却说没起」。
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({
                    "pid": os.getpid(),
                    "ppid": os.getppid(),
                    "port": port,
                    "started_at": time.time(),
                }),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写锁文件失败（不影响运行）：%s", exc)


# --------------------------------------------------------------------------
# 运行日志环形缓冲
# --------------------------------------------------------------------------
class LogRing:
    """把业务日志同时留一份在内存里，供网页实时拉取。

    为什么不直接让网页去读日志文件：日志文件在录像目录（可能被迁到别的盘），
    且文件是追加写的、没有「本次会话从哪开始」的概念。环形缓冲只保留最近
    N 条，网页按 seq 增量拉取，语义清晰且零磁盘 IO。
    """

    def __init__(self, capacity: int = 600) -> None:
        self._buf: deque = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0
        self._handler: Optional[Any] = None

    def install(self) -> None:
        """挂到根日志器上。已在录制器主窗口跑时不会冲突（不同进程）。"""
        import logging

        if self._handler is not None:
            # 幂等：重复调用不允许再挂第二个 handler —— 每多挂一个，环形
            # 缓冲里每条日志就多一份（网页上呈现为逐行重复）。
            return

        ring = self

        class _Sink(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                try:
                    ring.append(record.levelname, self.format(record))
                except Exception:  # noqa: BLE001 - 日志不能反过来搞崩业务
                    pass

        handler = _Sink()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S"))
        handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(handler)
        self._handler = handler

    def append(self, level: str, text: str) -> None:
        with self._lock:
            self._seq += 1
            self._buf.append({"seq": self._seq, "level": level, "text": text})

    def since(self, seq: int) -> Dict[str, Any]:
        """返回 seq 之后的全部日志（seq=0 表示要全部）。"""
        with self._lock:
            items = [it for it in self._buf if it["seq"] > seq]
            return {"seq": self._seq, "items": items}


# --------------------------------------------------------------------------
# 扫码登录会话（固定线程串行）
# --------------------------------------------------------------------------
class LoginSession:
    """网页驱动的扫码登录。

    ``QrLoginSession`` 内部用 Playwright 同步 API，**必须**创建与使用在同一
    线程里。所以这里起一个专职线程跑会话，其它线程只通过加锁的共享状态读写
    二维码与进度。状态机：

        idle -> starting -> waiting -> success / failed / cancelled

    二维码 PNG 以 base64 放在 ``qr_b64`` 里；网页凭 ``qr_version`` 判断是否
    换了新码（平台会定时刷新二维码，不刷新的话用户扫的是过期码）。
    """

    def __init__(self, store: ConfigStore) -> None:
        self._store = store
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._session: Optional[Any] = None
        self._cancel = threading.Event()
        #: 登录成功后的回调（由宿主注入；默认什么也不做，便于单独使用本类）
        self.on_login_changed: Callable[[], None] = lambda: None

        self.platform = ""
        self.state = "idle"
        self.message = ""
        self.qr_b64: Optional[str] = None
        self.qr_version = 0
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.cookie_chars = 0

    # ------------------------------------------------------------------ 对外
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "platform": self.platform,
                "state": self.state,
                "message": self.message,
                "qr": self.qr_b64,
                "qr_version": self.qr_version,
                "error": self.error,
                "cookie_chars": self.cookie_chars,
                "elapsed": (
                    (self.finished_at or time.time()) - self.started_at
                    if self.started_at else 0.0
                ),
            }

    def start(self, platform: str, *, timeout: float = 300.0) -> bool:
        """开始一个登录会话。已有会话在跑时返回 False（调用方报 409）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self.platform = platform
            self.state = "starting"
            self.message = "正在启动浏览器…"
            self.qr_b64 = None
            self.qr_version = 0
            self.error = ""
            self.cookie_chars = 0
            self.started_at = time.time()
            self.finished_at = 0.0
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run, args=(platform, timeout),
                name="qr-login", daemon=True,
            )
            self._thread.start()
            return True

    def cancel(self) -> bool:
        """取消当前会话（关掉浏览器）。"""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                return False
            self._cancel.set()
            self.message = "正在取消…"
        return True

    def refresh(self) -> None:
        """让会话立刻重取一次二维码/登录态（网页点「刷新二维码」时调）。"""
        session = self._session
        if session is None:
            return
        try:
            code = session.current_qr()
            self._publish_qr(code)
        except Exception:  # noqa: BLE001
            pass

    def shutdown(self) -> None:
        """进程收尾时关闭会话。"""
        self.cancel()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=8)

    # ------------------------------------------------------------------ 内部
    def _set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _publish_qr(self, code: Optional[bytes]) -> None:
        if not code:
            return
        b64 = base64.b64encode(code).decode("ascii")
        with self._lock:
            # 内容没变就不递增版本号 —— 网页凭版本号决定是否重画，
            # 每次都递增会让图片每秒闪一次。
            if b64 != self.qr_b64:
                self.qr_b64 = b64
                self.qr_version += 1

    def _run(self, platform: str, timeout: float) -> None:
        from live_recorder.platforms.qr_login import PlaywrightUnavailable, QrLoginSession

        session = None
        try:
            session = QrLoginSession(
                platform,
                headless=False,          # 必须有头：无头态下平台可能不渲染二维码
                on_status=lambda msg: self._set(message=msg),
            )
            with self._lock:
                self._session = session

            self._set(state="waiting", message="正在打开登录页…")
            code = session.open()
            self._publish_qr(code)

            if session.is_logged_in():
                self._finish_success(session, platform, "已经是登录状态，直接导出凭证。")
                return

            if code:
                self._set(message="二维码已就绪，请用手机 App 扫码并在手机上确认。")
            else:
                self._set(message="页面未自动显示二维码，请在弹出的浏览器窗口里点「登录」后扫码。")

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self._cancel.is_set():
                    self._set(state="cancelled", message="已取消扫码登录。")
                    return
                if session.is_logged_in():
                    self._finish_success(session, platform, "扫码成功，凭证已保存。")
                    return
                # 平台会定期换码，持续回读让网页上的图始终是当前有效的那个
                try:
                    self._publish_qr(session.current_qr())
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(1.5)

            self._set(state="failed", message="等待扫码超时，未保存任何内容。",
                      finished_at=time.time())

        except PlaywrightUnavailable as exc:
            self._set(state="failed", error=str(exc), message="无法启动浏览器。",
                      finished_at=time.time())
            logger.warning("扫码登录不可用：%s", exc)
        except Exception as exc:  # noqa: BLE001 - 登录失败不该搞崩守护进程
            self._set(state="failed", error="%s: %s" % (type(exc).__name__, exc),
                      message="登录过程出错。", finished_at=time.time())
            logger.exception("扫码登录异常：%s", exc)
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # noqa: BLE001
                    logger.debug("关闭登录会话失败", exc_info=True)
            with self._lock:
                self._session = None

    def _finish_success(self, session: Any, platform: str, message: str) -> None:
        """把 Cookie 写进配置。写的是本进程持有的 store，不是重新读盘 ——
        重新读盘会把本进程内存里的房间清单变更丢掉。"""
        cookie = session.cookie_string()
        if not cookie:
            self._set(state="failed", message="扫码成功但未取到有效 Cookie。",
                      finished_at=time.time())
            return
        from live_recorder.platforms.qr_login import SPECS

        field = SPECS[platform]["config_field"]
        with self._store._lock:  # noqa: SLF001 - 同包内部，读改写需要与 save 串行
            setattr(self._store.config, field, cookie)
            self._store.config.normalize()
            self._store.save()
        count = session.cookie_count()
        self._set(state="success", message=message, cookie_chars=len(cookie),
                  finished_at=time.time())
        logger.info("已保存扫码登录凭证（%d 个 Cookie，%d 字符）", count, len(cookie))
        # 登录态变了，让调度器立刻重判一次（快手未登录时很多房间判不出来）
        try:
            self.on_login_changed()
        except Exception:  # noqa: BLE001 - 回调异常不该影响登录结果
            logger.debug("登录变更回调异常", exc_info=True)


# --------------------------------------------------------------------------
# 守护进程主体
# --------------------------------------------------------------------------
class RecorderDaemon:
    """持有 ``MonitorEngine`` 的无界面宿主。"""

    def __init__(self, *, auto_start: bool = True) -> None:
        self.store = ConfigStore().load()
        self.bus = EventBus()
        self.engine = MonitorEngine(self.store, self.bus)
        self.logs = LogRing()
        self.login = LoginSession(self.store)
        self.login.on_login_changed = self._on_login_changed

        self.started_at = time.time()
        self._auto_start = auto_start
        self._shutting_down = False

        # 录制完成/告警等事件进日志缓冲，网页不用轮询文件
        self.bus.subscribe(self._on_event)
        self.logs.install()

    # ------------------------------------------------------------------ 生命周期
    def boot(self) -> None:
        log_dir = Path(self.store.config.save_path) / "_logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            # configure_logging 会替换根 handler，所以环形缓冲要在它之后挂
        except Exception:  # noqa: BLE001
            pass

        if self._auto_start and self.store.config.auto_start_monitor and self.store.rooms:
            logger.info("配置里开着自动监控且有 %d 个对象，自动恢复监控",
                        len(self.store.rooms))
            self.engine.start()
        else:
            logger.info("录制守护进程就绪（监控未启动，等网页指令）")

    def shutdown(self) -> None:
        """优雅停机：停监控 → 无损合并 → 落盘。

        顺序不能反：先 ``engine.shutdown()`` 收尾录制（分片合并可能耗时），
        再 ``store.save()``，否则会把「正在录制」的状态写进配置。
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        logger.info("守护进程正在退出，正在收尾录制任务……")
        try:
            self.login.shutdown()
        except Exception:  # noqa: BLE001
            logger.debug("关闭登录会话失败", exc_info=True)
        try:
            self.engine.shutdown()
            self.store.save()
        except Exception:  # noqa: BLE001
            logger.exception("收尾时出错")
        logger.info("已安全退出")

    def _on_login_changed(self) -> None:
        """登录成功后立刻重判一次开播状态。"""
        if self.engine.is_running:
            try:
                self.engine.force_check_now()
            except Exception:  # noqa: BLE001
                logger.debug("触发立即检测失败", exc_info=True)

    def _on_event(self, event: str, payload: Dict[str, Any]) -> None:
        """把引擎事件转成网页能读的一行日志。"""
        if event == ENGINE_EVENTS.RECORDING_STARTED:
            self.logs.append("INFO", "开始录制：%s" % (payload.get("nickname") or "未知主播"))
        elif event == ENGINE_EVENTS.RECORDING_FINISHED:
            self.logs.append("INFO", "录制完成：%s" % Path(payload.get("file") or "").name)
        elif event == ENGINE_EVENTS.QUALITY_FALLBACK:
            self.logs.append("WARNING", "画质降级：%s → %s"
                             % (payload.get("nickname", ""), payload.get("quality_label", "")))
        elif event == ENGINE_EVENTS.DISK_PAUSED:
            self.logs.append("WARNING", "磁盘空间不足，已暂停全部录制")
        elif event == ENGINE_EVENTS.DISK_RESUMED:
            self.logs.append("INFO", "磁盘空间恢复，继续录制")
        elif event == ENGINE_EVENTS.ALERT:
            self.logs.append("WARNING", payload.get("message") or "发生告警")

    # ------------------------------------------------------------------ 状态
    def status(self) -> Dict[str, Any]:
        """给网页的完整状态快照（只读，绝不改文件）。"""
        rows = self.engine.snapshot()
        counts = self.engine.counts()
        cfg = self.store.config
        return {
            "ok": True,
            "app": __app_name__,
            "version": __version__,
            "pid": os.getpid(),
            "uptime": time.time() - self.started_at,
            "running": self.engine.is_running,
            "disk_paused": self.engine.disk_paused,
            "counts": counts,
            "rooms": rows,
            "settings": {
                "save_path": cfg.save_path,
                "poll_interval": cfg.poll_interval,
                "quality_preference": cfg.quality_preference,
                "enable_fallback": cfg.enable_fallback,
                "max_file_size_gb": cfg.max_file_size_gb,
                "max_concurrent_recordings": cfg.max_concurrent_recordings,
                "convert_to_mp4": getattr(cfg, "convert_to_mp4", False),
                "keep_original_after_mp4": getattr(cfg, "keep_original_after_mp4", True),
                "min_free_disk_gb": cfg.min_free_disk_gb,
                # 桌面窗口并入网页后补齐（getattr 兜底：旧 config 没这些键时给默认值）
                "check_concurrency": getattr(cfg, "check_concurrency", 4),
                "max_monitor_rooms": getattr(cfg, "max_monitor_rooms", 200),
                "max_duration_minutes": getattr(cfg, "max_duration_minutes", 0),
                "reconnect_interval": getattr(cfg, "reconnect_interval", 15),
                "stall_timeout": getattr(cfg, "stall_timeout", 60),
                "max_reconnect_attempts": getattr(cfg, "max_reconnect_attempts", 0),
                "ffmpeg_path": getattr(cfg, "ffmpeg_path", "ffmpeg"),
                "playwright_enabled": getattr(cfg, "playwright_enabled", True),
                "headless_browser": getattr(cfg, "headless_browser", True),
            },
            "login": {
                "kuaishou": bool((cfg.kuaishou_cookie or "").strip()),
                "douyin": bool((cfg.douyin_cookie or "").strip()),
            },
            "login_session": self.login.snapshot(),
        }

    # ------------------------------------------------------------------ 动作
    def start_monitor(self) -> Dict[str, Any]:
        if not self.store.rooms:
            return {"error": "还没有监控对象，请先添加直播间链接", "code": 400}
        self.engine.start()
        return {"ok": True, "running": True, "message": "监控已启动"}

    def stop_monitor(self) -> Dict[str, Any]:
        if not self.engine.is_running:
            return {"ok": True, "running": False, "message": "监控本来就没有运行"}
        n = self.engine.counts().get("recording", 0)
        self.engine.stop()
        msg = "监控已停止" + ("，%d 个录制任务已收尾合并" % n if n else "")
        return {"ok": True, "running": False, "message": msg}

    def force_check(self) -> Dict[str, Any]:
        if not self.engine.is_running:
            return {"error": "监控未运行，请先启动监控", "code": 409}
        self.engine.force_check_now()
        return {"ok": True, "message": "已触发一次立即检测"}

    def add_room(self, url: str, remark: str = "", platform: str = "") -> Dict[str, Any]:
        url = (url or "").strip()
        if not url:
            return {"error": "缺少直播间链接", "code": 400}

        # 平台：前端给了就用，否则本地按域名判（不联网、不解析短链 ——
        # 短链是视频还是直播无法从 URL 分辨，交给首次轮询解析）
        if not platform:
            platform = _detect_platform(url)
        if not platform:
            return {
                "error": "无法识别直播平台，请粘贴 live.douyin.com 或 live.kuaishou.com 链接",
                "code": 400,
            }

        entry = RoomEntry(platform=platform, raw_input=url, remark=remark or "")
        if not self.engine.add_room(entry):
            return {"ok": True, "added": False, "duplicate": True,
                    "platform": platform,
                    "message": "该直播链接已在监控清单中"}
        return {
            "ok": True, "added": True, "duplicate": False, "platform": platform,
            "message": "已加入监控清单" + ("，监控运行中会自动开始检测" if self.engine.is_running
                                    else "，点「开始监控」后生效"),
        }

    def remove_room(self, key: str) -> Dict[str, Any]:
        if not self.engine.remove_room(key):
            return {"error": "监控清单里没有这一项", "code": 404}
        return {"ok": True, "message": "已从监控清单移除"}

    def toggle_room(self, key: str, enabled: bool) -> Dict[str, Any]:
        if not self.engine.set_room_enabled(key, enabled):
            return {"error": "监控清单里没有这一项", "code": 404}
        return {"ok": True, "enabled": enabled,
                "message": "已%s" % ("启用" if enabled else "停用")}

    def stop_room(self, key: str) -> Dict[str, Any]:
        if not self.engine.stop_room_recording(key):
            return {"error": "该房间当前没有正在进行的录制", "code": 409}
        return {"ok": True, "message": "已停止该房间的录制，正在合并分片"}

    #: 允许网页改的设置项 → 解析函数。
    #: 白名单而不是「谁传就改谁」：config.json 里还有 cookie、auto_start_monitor
    #: 这类不该由网页随手改（或被误传成 null 清空）的字段。
    #:
    #: 范围刻意与 ``AppConfig.normalize()`` 保持一致 —— 两处不一致的话，
    #: 接口会回显一个用户要的值、落盘却是另一个（实测踩过：传 0.2 回显 0.2、
    #: 但 normalize 夹到 0.5，磁盘上是 0.5）。宁可这里就夹好。
    _SETTING_CASTS = {
        "save_path": lambda v: str(v).strip(),
        "poll_interval": lambda v: max(10, min(300, int(v))),
        "quality_preference": lambda v: ("origin" if str(v) == "origin" else "high"),
        "enable_fallback": lambda v: bool(v),
        "max_file_size_gb": lambda v: max(0.5, min(64.0, float(v))),
        "max_concurrent_recordings": lambda v: max(1, min(50, int(v))),
        "convert_to_mp4": lambda v: bool(v),
        "keep_original_after_mp4": lambda v: bool(v),
        "min_free_disk_gb": lambda v: max(0.0, min(500.0, float(v))),
        # —— 2026-11-07 桌面窗口并入网页后补齐的字段（与设置对话框对齐）——
        "check_concurrency": lambda v: max(1, min(20, int(v))),
        "max_monitor_rooms": lambda v: max(50, min(1000, int(v))),
        "max_duration_minutes": lambda v: max(0, min(1440, int(v))),
        "reconnect_interval": lambda v: max(3, min(600, int(v))),
        "stall_timeout": lambda v: max(10, min(300, int(v))),
        "max_reconnect_attempts": lambda v: max(0, min(999, int(v))),
        "ffmpeg_path": lambda v: (str(v).strip() or "ffmpeg"),
        "playwright_enabled": lambda v: bool(v),
        "headless_browser": lambda v: bool(v),
    }

    def update_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """按白名单局部更新设置并落盘。

        改保存目录前会验证目标可用（能创建/可写），否则如实拒绝 ——
        把路径写进配置但录不出来，比直接报错更糟。
        """
        if not isinstance(patch, dict):
            return {"error": "请求体必须是对象", "code": 400}

        applied: Dict[str, Any] = {}
        rejected: Dict[str, str] = {}
        for name, raw in patch.items():
            cast = self._SETTING_CASTS.get(name)
            if cast is None:
                rejected[name] = "不支持在网页上修改"
                continue
            try:
                value = cast(raw)
            except (TypeError, ValueError) as exc:
                rejected[name] = "取值无效：%s" % exc
                continue
            if name == "save_path":
                if not value:
                    rejected[name] = "路径不能为空"
                    continue
                try:
                    target = Path(os.path.expandvars(os.path.expanduser(value)))
                    target.mkdir(parents=True, exist_ok=True)
                    probe_file = target / ".write_probe"
                    probe_file.write_text("ok", encoding="utf-8")
                    probe_file.unlink()
                except OSError as exc:
                    rejected[name] = "目录不可写：%s" % exc
                    continue
                value = str(target)
            applied[name] = value

        if not applied:
            return {"error": "没有可应用的修改", "rejected": rejected, "code": 400}

        with self.store._lock:  # noqa: SLF001 - 读改写必须与 save 串行
            for name, value in applied.items():
                setattr(self.store.config, name, value)
            self.store.config.normalize()
            # normalize() 可能进一步收敛（它是配置层的最后一道兜底），
            # 所以回显要读**盘上真正生效的那个值**，不是用户传进来的值 ——
            # 否则接口会自信地回一个没生效的数字。
            effective = {name: getattr(self.store.config, name) for name in applied}
            self.store.save()

        if any(applied[k] != effective[k] for k in applied):
            logger.info("设置已被配置层收敛：%s",
                        ", ".join("%s %s→%s" % (k, applied[k], effective[k])
                                  for k in applied if applied[k] != effective[k]))
        logger.info("设置已更新：%s", ", ".join(sorted(applied)))
        # 轮询间隔变了要尽快生效；录制并发/体积上限在下一次分卷判断时生效
        if "poll_interval" in applied and self.engine.is_running:
            try:
                self.engine.force_check_now()
            except Exception:  # noqa: BLE001
                logger.debug("触发立即检测失败", exc_info=True)

        return {"ok": True, "applied": effective, "rejected": rejected,
                "settings": self.status()["settings"]}


def _detect_platform(url: str) -> str:
    """按域名判平台（与 platforms/registry.py 同口径，但只做本地判断）。"""
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    for platform, hints in (
        ("kuaishou", ("kuaishou.com", "gifshow.com")),
        ("douyin", ("douyin.com", "iesdouyin.com", "amemv.cn", "amemv.com")),
    ):
        if any(h in host for h in hints):
            return platform
    return ""


# --------------------------------------------------------------------------
# HTTP 层
# --------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    """只监听回环地址的极简 JSON 接口。

    刻意不引入 aiohttp/Flask：本进程的依赖必须与 ``live-recorder-env``
    已有的保持一致（PyQt6/httpx/playwright），不能为了几个接口再装 web 框架
    —— 那会破坏「整个目录拷走即可用」。
    """

    server_version = "LiveRecorderDaemon/" + __version__
    daemon: "RecorderDaemon"  # 由 _serve 注入到类上

    # ------------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        """默认会把每个请求打到 stderr；改走 logger 以便归入环形缓冲。"""
        logger.debug("daemon %s - %s", self.address_string(), fmt % args)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self) -> Dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if n <= 0:
            return {}
        try:
            raw = self.rfile.read(n).decode("utf-8", "ignore")
            data = json.loads(raw or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _wrap(self, result: Dict[str, Any]) -> None:
        """把动作函数的返回统一成 (payload, status)。"""
        if not isinstance(result, dict):
            self._json({"ok": True}, 200)
            return
        status = int(result.pop("code", 200) or 200)
        if "error" in result and status == 200:
            status = 400
        self._json(result, status)

    # ------------------------------------------------------------------ 路由
    def _guard(self, fn: Any) -> None:
        """把动作函数包一层：任何异常都要变成 500 + 可读信息。

        不包的话异常会冒到 ``BaseHTTPRequestHandler`` 之外，连接被直接掐断，
        调用方看到的是 ``RemoteDisconnected`` —— 排查时完全没有指向性
        （实测：snapshot 里的 datetime 序列化失败就表现成这样）。
        """
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 接口层必须兜住一切
            logger.exception("接口执行异常：%s", exc)
            try:
                self._json({"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            except Exception:  # noqa: BLE001 - 连错误响应都发不出去就算了
                pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._guard(self._do_get)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        self._guard(self._do_post)

    def _do_get(self) -> None:
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        d = self.daemon

        if path == "/health":
            # ppid 一并报出：venv 的 python.exe 是启动器 stub，拉起方看到的
            # 是 stub 的 pid，而这里报的是真身 pid。带上 ppid 让调用方能对上号，
            # 不至于把「同一个守护进程」误认成两个。
            self._json({"ok": True, "pid": os.getpid(), "ppid": os.getppid(),
                        "version": __version__, "uptime": time.time() - d.started_at})
        elif path == "/status":
            self._json(d.status())
        elif path == "/logs":
            try:
                since = int((query.get("since") or ["0"])[0])
            except ValueError:
                since = 0
            self._json(d.logs.since(since))
        elif path == "/login/status":
            self._json({"ok": True, **d.login.snapshot()})
        else:
            self._json({"error": "未知接口 %s" % path}, 404)

    def _do_post(self) -> None:
        path = urlparse(self.path).path
        body = self._body()
        d = self.daemon

        if path == "/monitor/start":
            self._wrap(d.start_monitor())
        elif path == "/monitor/stop":
            self._wrap(d.stop_monitor())
        elif path == "/monitor/check":
            self._wrap(d.force_check())
        elif path == "/rooms/add":
            self._wrap(d.add_room(
                body.get("url") or "", body.get("remark") or "",
                body.get("platform") or "",
            ))
        elif path == "/rooms/remove":
            self._wrap(d.remove_room(body.get("key") or ""))
        elif path == "/rooms/toggle":
            self._wrap(d.toggle_room(body.get("key") or "", bool(body.get("enabled"))))
        elif path == "/rooms/stop":
            self._wrap(d.stop_room(body.get("key") or ""))
        elif path == "/settings":
            self._wrap(d.update_settings(body))
        elif path == "/login/start":
            platform = (body.get("platform") or "").strip()
            if platform not in ("kuaishou", "douyin"):
                self._json({"error": "平台必须是 kuaishou 或 douyin"}, 400)
            elif not d.login.start(platform):
                self._json({"error": "已有一个扫码会话在进行中"}, 409)
            else:
                self._json({"ok": True, "platform": platform})
        elif path == "/login/refresh":
            d.login.refresh()
            self._json({"ok": True})
        elif path == "/login/cancel":
            self._json({"ok": d.login.cancel()})
        elif path == "/shutdown":
            # 只允许本机调用（服务本身就只 bind 在回环地址上）
            threading.Thread(target=_deferred_stop, args=(self.server,), daemon=True).start()
            self._json({"ok": True, "message": "守护进程正在退出"})
        else:
            self._json({"error": "未知接口 %s" % path}, 404)


def _deferred_stop(server: ThreadingHTTPServer) -> None:
    """先把响应发出去，再停服务 —— 否则调用方会看到连接被重置。

    ``0.25s`` 是实测够用的下限：``BaseHTTPRequestHandler`` 在小响应体下
    会立刻 flush，但转发层（桥接服务）还要把字节读完。给得太短会看到
    ``ConnectionResetError``，调用方无法区分「停成功」与「服务崩了」；
    给得太长则让用户觉得点「停止」后界面卡住。0.5s 两头都稳。
    """
    time.sleep(0.5)
    try:
        server.shutdown()
    except Exception:  # noqa: BLE001
        pass


def _serve(daemon: RecorderDaemon, port: int) -> ThreadingHTTPServer:
    _Handler.daemon = daemon
    # bind 127.0.0.1：录制控制面绝不能暴露到局域网
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    return server


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def probe(port: int = DEFAULT_PORT) -> Dict[str, Any]:
    """探测本机是否已有守护进程在跑（``--check`` 用）。"""
    import urllib.request

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://127.0.0.1:%d/health" % port, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            return {"running": True, "port": port, **data}
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "port": port, "error": str(exc)}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="%s 无界面录制守护进程（网页控制台的后端内核）" % __app_name__
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="监听端口（仅回环地址），默认 %d" % DEFAULT_PORT)
    parser.add_argument("--no-auto-start", action="store_true",
                        help="即使配置里开着自动监控也不自动开始")
    parser.add_argument("--check", action="store_true",
                        help="只探测是否已有守护进程在跑，打印 JSON 后退出")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        print("%s v%s" % (__app_name__, __version__))
        return 0

    if args.check:
        print(json.dumps(probe(args.port), ensure_ascii=False))
        return 0

    # pythonw 启动时 stdout/stderr 都是 None，日志必须能在这种环境下工作
    configure_logging()

    lock = SingleInstanceLock(_lock_path())
    if not lock.acquire(args.port):
        logger.error("已有录制守护进程在运行（pid=%s，端口=%s），本进程退出。",
                     lock.owner_pid, lock.owner_port)
        print(json.dumps({
            "error": "已有录制守护进程在运行",
            "pid": lock.owner_pid,
            "port": lock.owner_port,
        }, ensure_ascii=False))
        return 3

    daemon = RecorderDaemon(auto_start=not args.no_auto_start)
    daemon.boot()

    try:
        server = _serve(daemon, args.port)
    except OSError as exc:
        logger.error("端口 %d 不可用：%s", args.port, exc)
        lock.release()
        return 4

    logger.info("录制守护进程已启动：http://127.0.0.1:%d (pid=%d)", args.port, os.getpid())

    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        logger.info("收到中断信号")
    finally:
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        daemon.shutdown()
        lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
