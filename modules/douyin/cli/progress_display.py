from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)
from rich.table import Table

console = Console()


class ProgressDisplay:
    _URL_STEP_TOTAL = 6

    def __init__(self):
        self.console = console
        self._progress_ctx: Optional[Progress] = None
        self._progress: Optional[Progress] = None
        self._overall_task_id: Optional[int] = None
        self._url_task_id: Optional[int] = None
        self._item_task_id: Optional[int] = None
        self._url_index = 0
        self._url_total = 0
        self._url_step_completed = 0
        self._item_total = 0
        self._item_completed = 0
        self._single_url_item_mode = False
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0, "paid": 0}

    def _echo(self, line: str) -> None:
        """非终端时打一行纯文本进度（供桥接服务的任务日志解析）。

        ⚠️ Rich 的 Live 进度条**只在终端里渲染**：当 stdout 被重定向到文件
        （桥接服务就是这么收后台任务输出的），它一行都不写 —— 于是任务日志
        里除了开头和结尾的空壳，中间**什么都看不到**，任务中心只能拿假进度充数。
        ⚠️ 因此这里只在 `not is_terminal` 时输出，**终端里的交互体验一点都不变**。
        """
        c = self._active_console()
        # ⚠️ 用 getattr 兜底：测试替身的 console 没有 is_terminal 属性，
        #    取不到时按「是终端」处理（不打印），保证不引入副作用。
        if getattr(c, "is_terminal", True):
            return
        c.print("[进度] " + line, markup=False, highlight=False, soft_wrap=True)

    def show_banner(self):
        banner = """
╔══════════════════════════════════════════╗
║     Douyin Downloader v2.0.0            ║
║     抖音批量下载工具                     ║
╚══════════════════════════════════════════╝
        """
        self._active_console().print(banner, style="bold cyan")

    def create_progress(self) -> Progress:
        return Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            TextColumn("[dim]{task.fields[detail]}"),
            console=self.console,
            transient=True,
            refresh_per_second=6,
        )

    def start_download_session(self, total_urls: int):
        if self._progress is not None:
            return

        self._progress_ctx = self.create_progress()
        self._progress = self._progress_ctx.__enter__()
        self._single_url_item_mode = False
        self._overall_task_id = self._progress.add_task(
            "总体进度",
            total=max(total_urls, 1),
            completed=0,
            detail=f"共 {total_urls} 个 URL",
        )

    def stop_download_session(self):
        self._cleanup_url_tasks()

        if self._progress_ctx is not None:
            self._progress_ctx.__exit__(None, None, None)

        self._progress_ctx = None
        self._progress = None
        self._overall_task_id = None
        self._single_url_item_mode = False

    def start_url(self, index: int, total: int, url: str):
        self._url_index = index
        self._url_total = total
        self._url_step_completed = 0
        self._item_total = 0
        self._item_completed = 0
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0, "paid": 0}

        self._echo(f"URL {index}/{total} · 开始 {self._shorten(url, max_len=72)}")
        self._cleanup_url_tasks()
        if not self._progress:
            return

        self._url_task_id = self._progress.add_task(
            self._format_url_description("待开始"),
            total=self._URL_STEP_TOTAL,
            completed=0,
            detail=self._shorten(url, max_len=72),
        )

    def complete_url(self, result=None):
        if result:
            self._echo(f"URL {self._url_index}/{self._url_total} · 完成 · "
                       f"成功 {result.success} 失败 {result.failed} 跳过 {result.skipped} 付费 {result.paid}")
        else:
            self._echo(f"URL {self._url_index}/{self._url_total} · 完成")
        if self._progress and self._url_task_id is not None:
            detail = ""
            if result:
                detail = (f"成功 {result.success} / 失败 {result.failed} / 跳过 {result.skipped} / 付费 {result.paid}")
            self._progress.update(
                self._url_task_id,
                completed=self._URL_STEP_TOTAL,
                description=self._format_url_description("完成"),
                detail=detail,
            )

        if self._progress and self._overall_task_id is not None:
            if self._single_url_item_mode:
                self._progress.update(self._overall_task_id, completed=self._item_total or 1)
            else:
                self._progress.advance(self._overall_task_id, 1)

    def fail_url(self, reason: str):
        self._echo(f"URL {self._url_index}/{self._url_total} · 失败 · {self._shorten(reason, 60)}")
        if self._progress and self._url_task_id is not None:
            self._progress.update(
                self._url_task_id,
                completed=self._URL_STEP_TOTAL,
                description=self._format_url_description("失败"),
                detail=reason,
            )

        if self._progress and self._overall_task_id is not None:
            if self._single_url_item_mode:
                self._progress.update(self._overall_task_id, completed=self._item_total or 1)
            else:
                self._progress.advance(self._overall_task_id, 1)

    def advance_step(self, step: str, detail: str = ""):
        self._echo(f"URL {self._url_index}/{self._url_total} · {step}"
                   + (f" · {self._shorten(detail, 48)}" if detail else ""))
        if not self._progress or self._url_task_id is None:
            return

        self._url_step_completed = min(self._url_step_completed + 1, self._URL_STEP_TOTAL)
        self._progress.update(
            self._url_task_id,
            completed=self._url_step_completed,
            description=self._format_url_description(step),
            detail=detail,
        )

    def update_step(self, step: str, detail: str = ""):
        if not self._progress or self._url_task_id is None:
            return

        self._progress.update(
            self._url_task_id,
            description=self._format_url_description(step),
            detail=detail,
        )

    def set_item_total(self, total: int, detail: str = ""):
        self._echo(f"条目总数 {total}" + (f" · {self._shorten(detail, 48)}" if detail else ""))
        if not self._progress:
            return

        self._item_total = max(total, 1)
        self._item_completed = 1 if total == 0 else 0
        self._item_stats = {"success": 0, "failed": 0, "skipped": 0, "paid": 0}

        if self._url_total == 1 and self._overall_task_id is not None:
            self._single_url_item_mode = True
            self._progress.update(
                self._overall_task_id,
                total=self._item_total,
                completed=self._item_completed,
                detail=f"共 {total} 个作品",
            )

        description = self._format_item_description()
        item_detail = detail or ("无待下载条目" if total == 0 else "")

        if self._item_task_id is None:
            self._item_task_id = self._progress.add_task(
                description,
                total=self._item_total,
                completed=self._item_completed,
                detail=item_detail,
            )
            return

        self._progress.update(
            self._item_task_id,
            total=self._item_total,
            completed=self._item_completed,
            description=description,
            detail=item_detail,
        )

    def advance_item(self, status: str, detail: str = ""):
        if not self._progress:
            return
        if self._item_task_id is None:
            self.set_item_total(1, "初始化条目进度")
        assert self._item_task_id is not None

        if status in self._item_stats:
            self._item_stats[status] += 1
        if self._item_completed < self._item_total:
            self._item_completed += 1

        # ⚠️ 这行是任务中心百分比的唯一真实来源，必须在计数器更新**之后**打
        # ⚠️ 这一行的形状是任务中心解析器的契约（bridge_server._RE_DY_ITEM）：
        #    「条目 N/M | 成功 x 失败 y 跳过 z」必须保持原顺序，新字段只能**往后加**。
        self._echo(f"条目 {self._item_completed}/{self._item_total}"
                   f" | 成功 {self._item_stats['success']}"
                   f" 失败 {self._item_stats['failed']}"
                   f" 跳过 {self._item_stats['skipped']}"
                   f" 付费 {self._item_stats['paid']}")

        status_map = {"success": "成功", "failed": "失败", "skipped": "跳过", "paid": "付费跳过"}
        status_text = status_map.get(status, status)
        item_detail = f"最近: {status_text} {self._shorten(detail, max_len=36)}"

        self._progress.update(
            self._item_task_id,
            completed=self._item_completed,
            description=self._format_item_description(),
            detail=item_detail,
        )
        if self._single_url_item_mode and self._overall_task_id is not None:
            self._progress.update(
                self._overall_task_id,
                completed=self._item_completed,
                detail=f"共 {self._item_total} 个作品",
            )

    def revise_items(self, transitions, detail: str = ""):
        """把已记入的条目统计从一个桶挪到另一个桶（失败自愈轮专用）。

        ``transitions`` 是 ``{(原状态, 新状态): 条数}``。失败自愈轮重跑的是
        **已经记过账**的条目，所以这里只挪统计、**不推进** ``_item_completed``
        —— 否则 8113 条的总量会被冲成 8280，进度条也会越界。

        末尾仍打一行与 ``advance_item`` 同形的 ``条目 N/M | 成功 .. 失败 ..``
        行：任务中心的百分比解析器（``bridge_server._RE_DY_ITEM``）只认这一
        种形状，不重打的话界面会一直停在自愈前的「失败 167」。
        """
        if not self._progress:
            return
        if self._item_task_id is None:
            self.set_item_total(1, "初始化条目进度")

        moved_total = 0
        for (from_status, to_status), count in (transitions or {}).items():
            if from_status not in self._item_stats or to_status not in self._item_stats:
                continue
            if from_status == to_status:
                continue
            try:
                count = int(count)
            except (TypeError, ValueError):
                continue
            moved = min(max(count, 0), self._item_stats[from_status])
            if moved <= 0:
                continue
            self._item_stats[from_status] -= moved
            self._item_stats[to_status] += moved
            moved_total += moved
        if moved_total <= 0:
            return

        # ⚠️ 这一行的形状是任务中心解析器的契约（bridge_server._RE_DY_ITEM）：
        #    「条目 N/M | 成功 x 失败 y 跳过 z」必须保持原顺序，新字段只能**往后加**。
        self._echo(f"条目 {self._item_completed}/{self._item_total}"
                   f" | 成功 {self._item_stats['success']}"
                   f" 失败 {self._item_stats['failed']}"
                   f" 跳过 {self._item_stats['skipped']}"
                   f" 付费 {self._item_stats['paid']}")
        self._progress.update(
            self._item_task_id,
            description=self._format_item_description(),
            detail=(f"重试补下 {moved_total} 条" if not detail else self._shorten(detail, 36)),
        )
        if self._single_url_item_mode and self._overall_task_id is not None:
            self._progress.update(
                self._overall_task_id,
                detail=f"共 {self._item_total} 个作品",
            )

    def show_result(self, result):
        table = Table(title="Download Summary", show_header=True, header_style="bold magenta")
        table.add_column("Metric", style="cyan")
        table.add_column("Count", justify="right", style="green")

        table.add_row("Total", str(result.total))
        table.add_row("Success", str(result.success))
        table.add_row("Failed", str(result.failed))
        table.add_row("Skipped", str(result.skipped))
        table.add_row("Paid", str(result.paid))

        if result.total > 0:
            success_rate = (result.success / result.total) * 100
            table.add_row("Success Rate", f"{success_rate:.1f}%")

        self._active_console().print(table)

    def print_info(self, message: str):
        self._active_console().print(f"[blue]ℹ[/blue] {message}")

    def print_success(self, message: str):
        self._active_console().print(f"[green]✓[/green] {message}")

    def print_warning(self, message: str):
        self._active_console().print(f"[yellow]⚠[/yellow] {message}")

    def print_error(self, message: str):
        self._active_console().print(f"[red]✗[/red] {message}")

    def _cleanup_url_tasks(self):
        if not self._progress:
            self._url_task_id = None
            self._item_task_id = None
            return

        if self._item_task_id is not None:
            self._progress.remove_task(self._item_task_id)
            self._item_task_id = None
        if self._url_task_id is not None:
            self._progress.remove_task(self._url_task_id)
            self._url_task_id = None

    def _format_url_description(self, step: str) -> str:
        return f"URL {self._url_index}/{self._url_total} · {step}"

    def _format_item_description(self) -> str:
        return (
            "作品下载 "
            f"S:{self._item_stats['success']} "
            f"F:{self._item_stats['failed']} "
            f"K:{self._item_stats['skipped']} "
            f"P:{self._item_stats['paid']}"
        )

    def _active_console(self) -> Console:
        if self._progress:
            return self._progress.console
        return self.console

    @staticmethod
    def _shorten(text: str, max_len: int = 60) -> str:
        normalized = (text or "").strip()
        if len(normalized) <= max_len:
            return normalized
        return f"{normalized[: max_len - 3]}..."
