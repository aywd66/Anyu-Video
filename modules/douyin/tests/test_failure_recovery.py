"""失败自愈：熔断闸门 + 轮末补跑 + 条目统计修正。

现场（2026-10-01 job bf863446014，8113 条的主页任务）：

* 12:15:00–12:16:42 这 100 秒里连续失败 167 条 —— 视频候选链全灭
  （``All 3 video url candidate(s) failed``）与图集首图失败
  （``Failed downloading image 1``）**同时**发生；
* 之后完全恢复，失败计数停在 167 不动，后续条目全部成功；
* 事后用同一份 cookie 重跑这 167 条，**全部成功**。

结论：那是源站/CDN 对客户端的短时封禁，不是作品问题。旧行为下这 100 秒被
白白烧成 167 条永久「失败」：候选轮扫的退避（1+2+5s）远短于封禁窗口，而且
失败一旦落定就不再重来。本文件覆盖为此加的两道保险：

1. :class:`FailureBurstBreaker` —— 窗口内失败数超阈值就全体停手冷却；
2. :meth:`BaseDownloader._recover_failed_items` —— 主轮跑完后给失败条目
   补跑几轮，并把条目统计从「失败」挪到「成功」。
"""

import asyncio
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from cli.progress_display import ProgressDisplay
from config import ConfigLoader
from control import QueueManager
from core.downloader_base import (
    _FAILURE_RETRY_BASE_COOLDOWN_S,
    _FAILURE_RETRY_MAX_COOLDOWN_S,
    BaseDownloader,
    entry_status,
)
from core.user_downloader import UserDownloader
from storage import FileManager
from storage.file_manager import FailureBurstBreaker

# 与 webui/bridge_server.py 的 _RE_DY_ITEM 逐字一致：任务中心的百分比解析器
# 只认这一种形状，自愈轮修正统计后必须重打一行同样的东西。
_DY_ITEM_LINE = re.compile(
    r"\[进度\]\s*条目\s+(\d+)\s*/\s*(\d+)"
    r"(?:\s*\|\s*成功\s+(\d+)\s+失败\s+(\d+)\s+跳过\s+(\d+))?"
)


class _FakeClock:
    """手动推进的单调时钟，避免用真实 sleep 拖慢测试。"""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# FailureBurstBreaker
# ---------------------------------------------------------------------------


def test_breaker_stays_quiet_below_threshold():
    clock = _FakeClock()
    breaker = FailureBurstBreaker(threshold=10, window_s=30, clock=clock)

    for _ in range(9):
        breaker.record_failure()

    assert breaker.cooldown_remaining == 0


def test_breaker_trips_at_threshold():
    clock = _FakeClock()
    breaker = FailureBurstBreaker(
        threshold=10, window_s=30, base_cooldown_s=20, clock=clock
    )

    for _ in range(10):
        breaker.record_failure()

    assert breaker.cooldown_remaining == pytest.approx(20)


def test_breaker_escalates_cooldown_and_caps_it():
    clock = _FakeClock()
    breaker = FailureBurstBreaker(
        threshold=2, window_s=30, base_cooldown_s=20, max_cooldown_s=50, clock=clock
    )

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(20)

    # 冷却结束、封禁仍在 → 下一轮翻倍
    clock.advance(20)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(40)

    # 第三次撞墙本该 80s，被 max 50s 封顶
    clock.advance(40)
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(50)


def test_breaker_ignores_failures_that_fell_outside_the_window():
    """稀疏失败不等于封禁：窗口外的失败不该累加。"""
    clock = _FakeClock()
    breaker = FailureBurstBreaker(threshold=10, window_s=30, clock=clock)

    for _ in range(9):
        breaker.record_failure()
        clock.advance(31)

    assert breaker.cooldown_remaining == 0


def test_breaker_does_not_extend_its_own_cooldown():
    """冷却期内在途连接还会继续失败，但那时长已经定了，不该被再翻一倍。"""
    clock = _FakeClock()
    breaker = FailureBurstBreaker(
        threshold=2, window_s=30, base_cooldown_s=20, max_cooldown_s=300, clock=clock
    )

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(20)

    clock.advance(5)
    for _ in range(20):
        breaker.record_failure()

    # 仍是首轮那 20s（还剩 15s），没有翻倍成 40s
    assert breaker.cooldown_remaining == pytest.approx(15)


def test_breaker_resets_escalation_only_after_a_quiet_window():
    clock = _FakeClock()
    breaker = FailureBurstBreaker(
        threshold=2, window_s=30, base_cooldown_s=20, max_cooldown_s=300, clock=clock
    )

    breaker.record_failure()
    breaker.record_failure()
    clock.advance(20)
    # 冷却刚过就成功：窗口没安静够，升级档位保留（封禁很可能只是间歇）
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(40)

    # 整整一个窗口都安静 → 收敛回基准
    clock.advance(40)
    clock.advance(31)
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(20)


async def test_breaker_wait_blocks_until_cooldown_clears(monkeypatch):
    clock = _FakeClock()
    breaker = FailureBurstBreaker(
        threshold=1, window_s=30, base_cooldown_s=20, clock=clock
    )
    breaker.record_failure()
    assert breaker.cooldown_remaining == pytest.approx(20)

    real_sleep = asyncio.sleep
    slept: list[float] = []

    async def _fake_sleep(seconds):
        slept.append(seconds)
        clock.advance(seconds)
        await real_sleep(0)

    monkeypatch.setattr("storage.file_manager.asyncio.sleep", _fake_sleep)

    await breaker.wait()

    assert sum(slept) >= 20


async def test_breaker_wait_returns_immediately_when_healthy():
    breaker = FailureBurstBreaker()
    await asyncio.wait_for(breaker.wait(), timeout=1)


def test_breaker_disabled_when_base_cooldown_is_zero():
    """base_cooldown_s=0 = 只统计不刹车（给自己留一条关掉的路）。"""
    clock = _FakeClock()
    breaker = FailureBurstBreaker(threshold=1, base_cooldown_s=0, clock=clock)

    for _ in range(50):
        breaker.record_failure()

    assert breaker.cooldown_remaining == 0


# ---------------------------------------------------------------------------
# 熔断记账在「条目」粒度上（不是 download_file 调用粒度）
# ---------------------------------------------------------------------------


def _breaker(threshold: int, cooldown: float = 10.0) -> FailureBurstBreaker:
    return FailureBurstBreaker(
        threshold=threshold, window_s=30, base_cooldown_s=cooldown, max_cooldown_s=cooldown
    )


def _failing_session(status: int = 500):
    """一个稳定失败的会话（500 不走 httpx 兜底，不会碰网）。"""
    mock_response = AsyncMock()
    mock_response.status = status
    mock_response.headers = {}
    mock_response.content_length = 0
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = ctx
    return session


def _healthy_session(bytes_total: int = 4096):
    mock_response = AsyncMock()
    mock_response.status = 200
    mock_response.content_length = bytes_total
    mock_response.headers = {"Content-Length": str(bytes_total)}

    async def iter_chunked(_size):
        yield b"\0" * bytes_total

    mock_response.content = MagicMock()
    mock_response.content.iter_chunked = iter_chunked
    ctx = AsyncMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.get.return_value = ctx
    return session


async def test_download_file_does_not_feed_the_breaker(tmp_path):
    """回归护栏：按调用记账会让**一条**死链作品自己凑满阈值。

    一条视频最坏是 4 候选 × 4 轮 = 16 次 download_file 失败，全发生在几秒内。
    阈值设成 1：只要 download_file 敢记一次账，这里就会刹车。
    """
    fm = FileManager(str(tmp_path))
    fm.breaker = _breaker(threshold=1)
    session = _failing_session()

    for index in range(4):
        assert (
            await fm.download_file(
                f"https://v3.douyinvod.com/{index}.mp4",
                tmp_path / f"{index}.mp4",
                session=session,
            )
            is False
        )

    assert fm.breaker.cooldown_remaining == 0


async def test_download_file_still_honors_the_cooldown(tmp_path, monkeypatch):
    """闸门那一半必须留着：冷却期内所有 worker 都得停在门口。"""
    fm = FileManager(str(tmp_path))
    waited: list[bool] = []

    async def _spy_wait():
        waited.append(True)

    monkeypatch.setattr(fm.breaker, "wait", _spy_wait)

    assert await fm.download_file(
        "https://v3.douyinvod.com/ok.mp4",
        tmp_path / "ok.mp4",
        session=_healthy_session(),
    )
    assert waited == [True]


async def test_concurrent_workers_are_released_after_the_cooldown(tmp_path):
    """全体 worker 一起等在闸门口时必须全部放行 —— 冷却不能变成死锁。

    冷却期是所有 worker 共用的一个"到点开闸"，不是每个 worker 各排一个队；
    写成后者就会出现「谁也等不到谁」的僵局。这里先把闸门踩下去，再并发放
    8 个 worker 进去，验证它们既真的等了、又都出来了。
    """
    fm = FileManager(str(tmp_path))
    fm.breaker = FailureBurstBreaker(
        threshold=1, window_s=30, base_cooldown_s=0.3, max_cooldown_s=0.3
    )
    session = _healthy_session()
    fm.breaker.record_failure()
    assert fm.breaker.cooldown_remaining > 0

    async def one(index):
        return await fm.download_file(
            f"https://p3.douyinpic.com/{index}.jpg",
            tmp_path / f"{index}.jpg",
            session=session,
        )

    started = time.monotonic()
    results = await asyncio.wait_for(
        asyncio.gather(*(one(index) for index in range(8))), timeout=15
    )
    elapsed = time.monotonic() - started

    assert results == [True] * 8
    # 真的被闸门拦在门口过，而不是直接放行
    assert elapsed >= 0.25


def test_item_level_failures_trip_the_breaker(tmp_path):
    host = _make_host(tmp_path)
    host.file_manager.breaker = _breaker(threshold=3)

    for _ in range(2):
        host._note_transport_failure()
    assert host.file_manager.breaker.cooldown_remaining == 0

    host._note_transport_failure()
    assert host.file_manager.breaker.cooldown_remaining > 0


def test_item_success_clears_the_failure_window(tmp_path):
    """零星死链作品不该攒成假封禁：一次成功就把窗口清空。"""
    host = _make_host(tmp_path)
    host.file_manager.breaker = _breaker(threshold=3)

    host._note_transport_failure()
    host._note_transport_failure()
    host._note_transport_success()
    host._note_transport_failure()

    assert host.file_manager.breaker.cooldown_remaining == 0


async def test_content_level_failures_do_not_trip_the_breaker(tmp_path):
    """「没有可播地址」「DRM 加密」是作品本身的问题，不该拖慢整批。

    这些分支走的是 ``return False`` 而不是 ``_note_transport_failure``；阈值
    设成 1，只要它们误记一次账就会被抓住。
    """
    host = _make_host(tmp_path)
    host.file_manager.breaker = _breaker(threshold=1)
    host.config.update(video=True)

    # 没有可播地址的视频：直接 return False，不记账
    assert (
        await host._download_aweme_assets(
            {"aweme_id": "1", "video": {}, "author": {"nickname": "t"}}, "t"
        )
        is False
    )
    assert host.file_manager.breaker.cooldown_remaining == 0


async def test_breaker_is_absent_when_file_manager_lacks_one(tmp_path):
    """鸭子类型兜底：宿主没有 breaker 时不该炸。"""
    host = _make_host(tmp_path)
    host.file_manager = SimpleNamespace()

    host._note_transport_failure()
    host._note_transport_success()


# ---------------------------------------------------------------------------
# _recover_failed_items
# ---------------------------------------------------------------------------


class _RecordingReporter:
    def __init__(self):
        self.steps: list[tuple[str, str]] = []
        self.transitions: list[dict] = []
        self.advances: list[tuple[str, str]] = []

    def update_step(self, step, detail=""):
        self.steps.append((step, detail))

    def set_item_total(self, total, detail=""):
        pass

    def advance_item(self, status, detail=""):
        self.advances.append((status, detail))

    def revise_items(self, transitions, detail=""):
        self.transitions.append((dict(transitions), detail))


class _FakeAPIClient:
    """够用的假客户端：只有下载路径会碰到的几个属性。"""

    BASE_URL = "https://www.douyin.com"
    headers = {"User-Agent": "pytest-ua"}
    proxy = None

    async def get_session(self):
        return AsyncMock()


class _Host(BaseDownloader):
    async def download(self, parsed_url):  # pragma: no cover - 抽象占位
        raise NotImplementedError


def _make_host(tmp_path, *, passes=2, reporter=None):
    config = ConfigLoader(None)
    config.update(
        path=str(tmp_path),
        failure_retry_passes=passes,
        thread=4,
        proxy="",
    )
    return _Host(
        config=config,
        api_client=_FakeAPIClient(),
        file_manager=FileManager(str(tmp_path)),
        cookie_manager=SimpleNamespace(),
        database=None,
        rate_limiter=None,
        retry_handler=None,
        queue_manager=QueueManager(max_workers=4),
        progress_reporter=reporter,
    )


def _stub_sleep(monkeypatch):
    """把自愈轮的冷却换成"记录 + 不真睡"。"""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(seconds):
        slept.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("core.downloader_base._sleep", _fake_sleep)
    return slept


def test_entry_status_treats_task_exceptions_as_failed():
    assert entry_status({"status": "success"}) == "success"
    assert entry_status({"status": "skipped"}) == "skipped"
    assert entry_status({"status": "failed"}) == "failed"
    assert entry_status(RuntimeError("boom")) == "failed"
    assert entry_status(None) == "failed"
    assert entry_status({"status": "weird"}) == "failed"


async def test_recovery_retries_only_failed_items_and_fixes_statuses(tmp_path, monkeypatch):
    reporter = _RecordingReporter()
    host = _make_host(tmp_path, passes=1, reporter=reporter)
    slept = _stub_sleep(monkeypatch)

    items = [{"aweme_id": str(index)} for index in range(4)]
    statuses = ["success", "failed", "skipped", "failed"]
    attempted: list[str] = []

    async def _process(item):
        attempted.append(item["aweme_id"])
        ok = item["aweme_id"] == "1"
        return {"status": "success" if ok else "failed", "aweme_id": item["aweme_id"]}

    await host._recover_failed_items(items, statuses, _process)

    # 只碰失败的那两条，成功/跳过的不重跑
    assert attempted == ["1", "3"]
    assert statuses == ["success", "success", "skipped", "failed"]
    assert slept == [pytest.approx(_FAILURE_RETRY_BASE_COOLDOWN_S)]
    assert reporter.transitions == [({("failed", "success"): 1}, "第 1 轮补下 1/2 条")]
    # 关键：自愈轮不再推进条目计数（否则 8113 会被冲成 8114）
    assert reporter.advances == []


async def test_recovery_cooldown_doubles_between_passes(tmp_path, monkeypatch):
    host = _make_host(tmp_path, passes=2)
    slept = _stub_sleep(monkeypatch)

    items = [{"aweme_id": "1"}]
    statuses = ["failed"]

    async def _process(item):
        return {"status": "failed", "aweme_id": item["aweme_id"]}

    await host._recover_failed_items(items, statuses, _process)

    assert slept == [
        pytest.approx(_FAILURE_RETRY_BASE_COOLDOWN_S),
        pytest.approx(_FAILURE_RETRY_BASE_COOLDOWN_S * 2),
    ]
    assert statuses == ["failed"]


async def test_recovery_cooldown_is_capped(tmp_path, monkeypatch):
    host = _make_host(tmp_path, passes=4)
    slept = _stub_sleep(monkeypatch)

    async def _process(item):
        return {"status": "failed", "aweme_id": item["aweme_id"]}

    await host._recover_failed_items([{"aweme_id": "1"}], ["failed"], _process)

    # 20 / 40 / 60 / 60（第 4 轮本该 80s，被 _FAILURE_RETRY_MAX_COOLDOWN_S 压住）
    assert slept == [
        pytest.approx(_FAILURE_RETRY_BASE_COOLDOWN_S),
        pytest.approx(_FAILURE_RETRY_BASE_COOLDOWN_S * 2),
        pytest.approx(_FAILURE_RETRY_MAX_COOLDOWN_S),
        pytest.approx(_FAILURE_RETRY_MAX_COOLDOWN_S),
    ]
    assert all(value <= _FAILURE_RETRY_MAX_COOLDOWN_S for value in slept)


async def test_recovery_stops_early_when_nothing_is_failed(tmp_path, monkeypatch):
    host = _make_host(tmp_path, passes=2)
    slept = _stub_sleep(monkeypatch)

    async def _process(item):  # pragma: no cover - 不该被调用
        raise AssertionError("没有失败条目时不该重跑")

    await host._recover_failed_items([{"aweme_id": "1"}], ["success"], _process)

    assert slept == []


async def test_recovery_disabled_with_zero_passes(tmp_path, monkeypatch):
    host = _make_host(tmp_path, passes=0)
    slept = _stub_sleep(monkeypatch)

    async def _process(item):  # pragma: no cover - 不该被调用
        raise AssertionError("failure_retry_passes=0 时不该重跑")

    await host._recover_failed_items([{"aweme_id": "1"}], ["failed"], _process)

    assert slept == []


async def test_recovery_passes_are_clamped(tmp_path):
    """配置里写个离谱的大数也不该真跑那么多轮（每轮都要等冷却）。"""
    assert _make_host(tmp_path, passes=99)._failure_retry_passes() == 4
    assert _make_host(tmp_path, passes=-5)._failure_retry_passes() == 0
    assert _make_host(tmp_path, passes="oops")._failure_retry_passes() == 2


async def test_recovery_reports_when_a_pass_recovers_nothing(tmp_path, monkeypatch):
    reporter = _RecordingReporter()
    host = _make_host(tmp_path, passes=1, reporter=reporter)
    _stub_sleep(monkeypatch)

    async def _process(item):
        return {"status": "failed", "aweme_id": item["aweme_id"]}

    await host._recover_failed_items([{"aweme_id": "1"}], ["failed"], _process)

    assert reporter.transitions == []
    assert any("仍全部失败" in detail for _step, detail in reporter.steps)


# ---------------------------------------------------------------------------
# 端到端：一次「封禁窗口」不该留下永久失败
# ---------------------------------------------------------------------------


def _make_user_downloader(tmp_path, *, passes, reporter):
    config = ConfigLoader(None)
    config.update(path=str(tmp_path), failure_retry_passes=passes, thread=3)
    return UserDownloader(
        config=config,
        api_client=_FakeAPIClient(),
        file_manager=FileManager(str(tmp_path)),
        cookie_manager=SimpleNamespace(),
        database=None,
        rate_limiter=None,
        retry_handler=None,
        queue_manager=QueueManager(max_workers=3),
        progress_reporter=reporter,
    )


async def test_batch_absorbs_a_transient_failure_window(tmp_path, monkeypatch):
    """主轮整整一轮全失败（= 12:15 那 100 秒），自愈轮全部补回。

    这是本次改动的验收用例：用户看到的结果必须是「成功 5 失败 0」，而不是
    旧行为下的「成功 0 失败 5」。
    """
    reporter = _RecordingReporter()
    downloader = _make_user_downloader(tmp_path, passes=2, reporter=reporter)
    _stub_sleep(monkeypatch)

    calls = {"n": 0}

    async def _assets(item, *_args, **_kwargs):
        calls["n"] += 1
        # 前 5 次 = 主轮，全部失败；之后（自愈轮）全部成功。
        return calls["n"] > 5

    monkeypatch.setattr(downloader, "_download_aweme_assets", _assets)
    items = [{"aweme_id": str(index)} for index in range(5)]

    result = await downloader._download_mode_items("post", items, "tester")

    assert (result.total, result.success, result.failed, result.skipped) == (5, 5, 0, 0)
    # 条目计数只在主轮推进过：总量没被自愈轮撑大
    assert len(reporter.advances) == 5
    assert reporter.transitions == [({("failed", "success"): 5}, "第 1 轮补下 5/5 条")]


async def test_batch_keeps_failures_when_recovery_is_off(tmp_path, monkeypatch):
    """关掉自愈时行为退回旧样：失败就是失败，别偷偷把失败算成成功。"""
    reporter = _RecordingReporter()
    downloader = _make_user_downloader(tmp_path, passes=0, reporter=reporter)

    async def _assets(item, *_args, **_kwargs):
        return False

    monkeypatch.setattr(downloader, "_download_aweme_assets", _assets)
    items = [{"aweme_id": str(index)} for index in range(3)]

    result = await downloader._download_mode_items("post", items, "tester")

    assert (result.total, result.success, result.failed) == (3, 0, 3)


# ---------------------------------------------------------------------------
# ProgressDisplay.revise_items
# ---------------------------------------------------------------------------


class _EchoProgress:
    """带 is_terminal=False 的假进度：让 ProgressDisplay._echo 真的吐行。"""

    def __init__(self):
        self.tasks: dict = {}
        self._next_id = 1
        self.lines: list[str] = []
        self.console = SimpleNamespace(is_terminal=False, print=self._print)

    def _print(self, *args, **_kwargs):
        self.lines.append(" ".join(str(arg) for arg in args))

    def add_task(self, description, total, completed=0, detail="", **kwargs):
        task_id = self._next_id
        self._next_id += 1
        self.tasks[task_id] = {
            "description": description,
            "total": total,
            "completed": completed,
            "detail": detail,
        }
        self.tasks[task_id].update(kwargs)
        return task_id

    def update(self, task_id, **kwargs):
        self.tasks[task_id].update(kwargs)

    def advance(self, task_id, advance=1):
        self.tasks[task_id]["completed"] = self.tasks[task_id].get("completed", 0) + advance

    def remove_task(self, task_id):
        self.tasks.pop(task_id, None)


def _make_display(monkeypatch):
    display = ProgressDisplay()
    fake = _EchoProgress()

    class _Ctx:
        def __enter__(self):
            return fake

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(display, "create_progress", lambda: _Ctx())
    display.start_download_session(1)
    display.start_url(1, 1, "https://www.douyin.com/user/MS4")
    display.set_item_total(5, "作品待下载")
    for status in ("success", "failed", "failed", "failed", "skipped"):
        display.advance_item(status, "a")
    return display, fake


def test_revise_items_moves_counters_without_advancing_completed(monkeypatch):
    display, fake = _make_display(monkeypatch)
    assert display._item_stats == {"success": 1, "failed": 3, "skipped": 1, "paid": 0}
    assert display._item_completed == 5

    display.revise_items({("failed", "success"): 2})

    assert display._item_stats == {"success": 3, "failed": 1, "skipped": 1, "paid": 0}
    # 已完成数不动 —— 重跑的是已经记过账的条目
    assert display._item_completed == 5
    assert fake.tasks[display._item_task_id]["completed"] == 5


def test_revise_items_echo_line_still_matches_task_center_parser(monkeypatch):
    display, fake = _make_display(monkeypatch)
    fake.lines.clear()

    display.revise_items({("failed", "success"): 2})

    item_lines = [line for line in fake.lines if "条目" in line]
    assert item_lines, f"没有吐出条目行：{fake.lines}"
    match = _DY_ITEM_LINE.search(item_lines[-1])
    assert match, f"任务中心解析不了这行：{item_lines[-1]}"
    assert match.group(1) == "5"
    assert match.group(2) == "5"
    assert (match.group(3), match.group(4), match.group(5)) == ("3", "1", "1")


def test_revise_items_cannot_go_negative(monkeypatch):
    display, _fake = _make_display(monkeypatch)

    display.revise_items({("failed", "success"): 99})

    assert display._item_stats == {"success": 4, "failed": 0, "skipped": 1, "paid": 0}


def test_revise_items_ignores_unknown_and_noop_buckets(monkeypatch):
    display, fake = _make_display(monkeypatch)
    fake.lines.clear()

    display.revise_items(
        {("failed", "failed"): 1, ("nope", "success"): 1, ("failed", "success"): 0}
    )

    assert display._item_stats == {"success": 1, "failed": 3, "skipped": 1, "paid": 0}
    assert fake.lines == []


def test_revise_items_is_optional_for_reporters(tmp_path):
    """老 reporter（没有 revise_items）不该让自愈轮炸掉。"""
    host_reporter = SimpleNamespace(
        update_step=lambda *_a, **_k: None,
        set_item_total=lambda *_a, **_k: None,
        advance_item=lambda *_a, **_k: None,
    )
    host = _make_host(tmp_path, reporter=host_reporter)
    host._progress_revise_items({("failed", "success"): 1})
