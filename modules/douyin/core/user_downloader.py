from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Set

import aiofiles

from core.downloader_base import BaseDownloader, DownloadResult, entry_status
from core.metadata import build_author_home_url
from core.user_mode_registry import UserModeRegistry
from utils.logger import setup_logger

logger = setup_logger("UserDownloader")


def _user_info_summary(user_info: Dict[str, Any]) -> Dict[str, Any]:
    safe_keys = (
        "uid",
        "sec_uid",
        "short_id",
        "unique_id",
        "nickname",
        "aweme_count",
        "favoriting_count",
        "follower_count",
        "following_count",
        "total_favorited",
    )
    return {key: user_info.get(key) for key in safe_keys if key in user_info}


class UserDownloader(BaseDownloader):
    SELF_COLLECT_MODES = {"collect", "collectmix"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode_registry = UserModeRegistry()
        self._mode_strategy_cache: Dict[str, Any] = {}

    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        download_started = time.monotonic()
        result = DownloadResult()
        sec_uid = parsed_url.get("sec_uid")
        if not sec_uid:
            raise RuntimeError("无法从链接中解析出用户 ID，请确认链接是否完整")

        modes = self._configured_modes()
        if not self._validate_mode_scope(sec_uid, modes):
            return result

        sec_uid = await self._resolve_self_alias(sec_uid, modes)

        logger.info(
            "User download started: sec_uid=%s modes=%s number=%s increase=%s",
            sec_uid,
            modes,
            self.config.get("number", {}),
            self.config.get("increase", {}),
        )
        user_info = await self._resolve_user_info_logged(sec_uid, modes)
        self._progress_report_author(
            nickname=user_info.get("nickname"),
            sec_uid=user_info.get("sec_uid") or sec_uid,
        )
        await self._save_author_home_url(sec_uid, user_info, modes)
        await self._save_homepage_screenshot(sec_uid, user_info, modes)
        self._progress_update_step("下载模式", f"模式: {', '.join(modes)}")

        seen_aweme_ids: Set[str] = set()
        for mode in modes:
            mode_result = await self._download_mode_logged(mode, sec_uid, user_info, seen_aweme_ids)
            if mode_result is not None:
                self._merge_result(result, mode_result)

        logger.info(
            "User download finished: duration_ms=%s total=%s success=%s failed=%s "
            "skipped=%s paid=%s",
            int((time.monotonic() - download_started) * 1000),
            result.total,
            result.success,
            result.failed,
            result.skipped,
            result.paid,
        )
        return result

    async def _resolve_self_alias(self, sec_uid: str, modes: List[str]) -> str:
        """把 ``/user/self`` 里的 ``self`` 换成登录账号真实 sec_uid。

        抖音网页版自己主页的地址栏形态是 ``/user/self``，``self`` 是"当前
        登录者"的别名而不是 sec_uid：直接透传给
        ``/aweme/v1/web/user/profile/other/?sec_user_id=self`` 会拿到
        ``status_code=2 UserId不合法``、``user={}``，旧代码把这个空结果当成
        "用户信息拿不到"，抛出误导性的 Cookie 失效错误。

        收藏夹模式（collect/collectmix）按 cookie 身份分页，不需要真实
        sec_uid，保持 ``self`` 原样走既有分支。
        """
        if sec_uid != "self":
            return sec_uid

        normalized_modes = {str(mode or "").strip() for mode in modes}
        if normalized_modes.issubset(self.SELF_COLLECT_MODES):
            return sec_uid

        self._progress_update_step("识别账号", "解析当前登录账号主页")
        try:
            self_info = await self.api_client.get_self_info()
        except Exception as exc:  # noqa: BLE001 - 任何失败都归为"解析不出账号"
            logger.warning("Resolve /user/self failed: %s", exc)
            self_info = None

        resolved = str((self_info or {}).get("sec_uid") or "").strip()
        if not resolved:
            raise RuntimeError(
                "无法识别当前登录账号，请重新登录抖音，"
                "或改用自己主页的完整链接（形如 https://www.douyin.com/user/MS4w...）"
            )
        logger.info("Resolved /user/self alias: sec_uid=%s", resolved)
        return resolved

    async def _save_author_home_url(
        self,
        sec_uid: str,
        user_info: Dict[str, Any],
        modes: List[str],
    ) -> None:
        if not self._as_bool(self.config.get("author_url", False)):
            return

        normalized_modes = {str(mode or "").strip() for mode in modes}
        if sec_uid == "self" or normalized_modes.issubset(self.SELF_COLLECT_MODES):
            return

        effective_sec_uid = str(user_info.get("sec_uid") or sec_uid).strip()
        author_url = build_author_home_url(effective_sec_uid)
        if not author_url or effective_sec_uid == "self":
            logger.warning("Author homepage URL skipped because sec_uid is unavailable")
            return

        author_name = str(user_info.get("nickname") or "unknown")
        try:
            author_dir = self.file_manager.get_author_dir(
                author_name,
                author_sec_uid=effective_sec_uid,
                author_dir_style=self.config.get("author_dir") or "nickname",
            )
            async with aiofiles.open(
                (author_dir / "author_url.txt").resolve(), "w", encoding="utf-8"
            ) as output:
                await output.write(f"{author_url}\n")
        except Exception as exc:
            logger.warning("Author homepage URL failed for %s: %s", effective_sec_uid, exc)

    async def _save_homepage_screenshot(
        self,
        sec_uid: str,
        user_info: Dict[str, Any],
        modes: List[str],
    ) -> None:
        if not self._as_bool(self.config.get("homepage_screenshot", False)):
            return

        normalized_modes = {str(mode or "").strip() for mode in modes}
        if sec_uid == "self" or normalized_modes.issubset(self.SELF_COLLECT_MODES):
            return

        effective_sec_uid = str(user_info.get("sec_uid") or sec_uid).strip()
        if not effective_sec_uid or effective_sec_uid == "self":
            logger.warning("Homepage screenshot skipped because sec_uid is unavailable")
            return

        author_name = str(user_info.get("nickname") or "unknown")
        try:
            author_dir = self.file_manager.get_author_dir(
                author_name,
                author_sec_uid=effective_sec_uid,
                author_dir_style=self.config.get("author_dir") or "nickname",
            )
            saved = await self.api_client.save_user_homepage_screenshot(
                effective_sec_uid,
                (author_dir / "主页截图.png").resolve(),
                profile=user_info,
            )
            if not saved:
                logger.warning("Homepage screenshot was not saved for %s", effective_sec_uid)
        except Exception as exc:
            logger.warning("Homepage screenshot failed for %s: %s", effective_sec_uid, exc)

    def _configured_modes(self) -> List[str]:
        modes_config = self.config.get("mode", ["post"])
        if isinstance(modes_config, str):
            return [modes_config]
        if isinstance(modes_config, list):
            return [str(mode).strip() for mode in modes_config if str(mode).strip()]
        return ["post"]

    async def _resolve_user_info_logged(self, sec_uid: str, modes: List[str]) -> Dict[str, Any]:
        started = time.monotonic()
        user_info = await self._resolve_user_info(sec_uid, modes)
        if not user_info:
            logger.error(
                "Failed to get user info: sec_uid=%s duration_ms=%s",
                sec_uid,
                int((time.monotonic() - started) * 1000),
            )
            raise RuntimeError("获取用户信息失败，请检查 Cookie 是否有效或重新登录抖音")
        logger.info(
            "User info resolved: duration_ms=%s summary=%s",
            int((time.monotonic() - started) * 1000),
            _user_info_summary(user_info),
        )
        return user_info

    async def _download_mode_logged(
        self,
        mode: str,
        sec_uid: str,
        user_info: Dict[str, Any],
        seen_aweme_ids: Set[str],
    ) -> Optional[DownloadResult]:
        strategy = self._get_mode_strategy(mode)
        if strategy is None:
            logger.warning("Unsupported user mode: %s", mode)
            return None
        self._progress_update_step("下载模式", f"开始处理 {mode} 作品")
        started = time.monotonic()
        logger.info("User mode started: mode=%s strategy=%s", mode, type(strategy).__name__)
        result = await strategy.download_mode(sec_uid, user_info, seen_aweme_ids=seen_aweme_ids)
        logger.info(
            "User mode finished: mode=%s duration_ms=%s total=%s success=%s "
            "failed=%s skipped=%s paid=%s unique_seen=%s",
            mode,
            int((time.monotonic() - started) * 1000),
            result.total,
            result.success,
            result.failed,
            result.skipped,
            result.paid,
            len(seen_aweme_ids),
        )
        return result

    @staticmethod
    def _merge_result(target: DownloadResult, source: DownloadResult) -> None:
        target.total += source.total
        target.success += source.success
        target.failed += source.failed
        target.skipped += source.skipped
        target.paid += source.paid

    def _validate_mode_scope(self, sec_uid: str, modes: List[str]) -> bool:
        normalized_modes = {str(mode or "").strip() for mode in modes}
        has_collect_mode = bool(normalized_modes & self.SELF_COLLECT_MODES)
        has_regular_mode = bool(normalized_modes - self.SELF_COLLECT_MODES)

        if has_collect_mode and sec_uid != "self":
            # Desktop "我的内容 / 下载本收藏夹" sends the real self sec_uid
            # together with a ``collects_id`` filter — by the time the
            # request reaches here the sidecar has already verified via
            # the cookie scope (``_resolve_viewer_sec_uid``) that the
            # caller is the logged-in user, so a real sec_uid + collect
            # mode + collects_id is the legit my-content path. Without
            # this branch ``download()`` would short-circuit and produce
            # an empty DownloadResult, which the JobManager renders as
            # the silent "已完成 0 项" failure.
            collects_id = (str(self.config.get("collects_id") or "")).strip()
            if not collects_id:
                logger.error(
                    "Modes collect/collectmix only support "
                    "/user/self?showTab=favorite_collection or "
                    "my-content 下载本收藏夹 (collects_id required)"
                )
                return False
        if has_collect_mode and has_regular_mode:
            logger.error("Modes collect/collectmix cannot be combined with post/like/mix/music")
            return False
        return True

    def _filter_pinned_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if self._download_pinned_enabled():
            return items
        return [item for item in items if not self._is_pinned_aweme(item)]

    def _download_pinned_enabled(self) -> bool:
        return self._as_bool(self.config.get("download_pinned", False))

    @staticmethod
    def _is_pinned_aweme(item: Dict[str, Any]) -> bool:
        value = item.get("is_top")
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def _increase_enabled(self, mode: str = "post") -> bool:
        """该模式是否处于「增量」语义（只下新的）。

        早停只在增量下成立：增量意味着「更早的已经下过、不用再看」，
        而强制重下（increase=false）必须把整份列表扫全。
        """
        raw = self.config.get("increase", {})
        if not isinstance(raw, dict):
            return True
        return self._as_bool(raw.get(mode, True))

    async def _resolve_user_info(self, sec_uid: str, modes: List[str]) -> Optional[Dict[str, Any]]:
        normalized_modes = {str(mode or "").strip() for mode in modes}
        if sec_uid == "self" and normalized_modes.issubset(self.SELF_COLLECT_MODES):
            self._progress_update_step("获取作者信息", "使用当前登录账号收藏夹上下文")
            return {
                "uid": "self",
                "sec_uid": "self",
                "nickname": "self",
            }

        # Desktop my-content "下载本收藏夹" path: real sec_uid + collect
        # mode + collects_id filter. The cookie scope upstream already
        # guarantees this is the viewer themselves, so we can skip the
        # network round-trip via ``api_client.get_user_info``.
        if (
            normalized_modes.issubset(self.SELF_COLLECT_MODES)
            and (str(self.config.get("collects_id") or "")).strip()
        ):
            self._progress_update_step("获取作者信息", "使用当前登录账号收藏夹上下文")
            return {
                "uid": sec_uid,
                "sec_uid": sec_uid,
                "nickname": "self",
            }

        self._progress_update_step("获取作者信息", f"sec_uid={sec_uid}")
        return await self.api_client.get_user_info(sec_uid)

    def _get_mode_strategy(self, mode: str):
        normalized_mode = (mode or "").strip()

        # The "collect" strategy supports an optional ``collects_id`` filter
        # that constrains paging to a single folder (desktop "我的收藏 / 下载
        # 本收藏夹"). When the filter is set we bypass the cache so the next
        # call with a different (or absent) filter doesn't reuse a stale
        # strategy bound to the previous folder. The no-filter path keeps
        # caching to preserve the existing CLI behaviour.
        if normalized_mode == "collect":
            return self._make_collect_strategy()

        if normalized_mode in self._mode_strategy_cache:
            return self._mode_strategy_cache[normalized_mode]

        strategy_cls = self.mode_registry.get(normalized_mode)
        if strategy_cls is None:
            return None

        strategy = strategy_cls(self)
        self._mode_strategy_cache[normalized_mode] = strategy
        return strategy

    def _make_collect_strategy(self):
        """Construct the collect strategy, threading ``collects_id`` from
        the per-job config when present. Caches only the no-filter path
        (matching the historic CLI behaviour) so a subsequent call with a
        different filter doesn't pick up a stale binding.
        """
        strategy_cls = self.mode_registry.get("collect")
        if strategy_cls is None:
            return None

        raw_filter = self.config.get("collects_id")
        collects_id = (str(raw_filter).strip() if raw_filter is not None else "") or None

        if collects_id is None:
            cached = self._mode_strategy_cache.get("collect")
            if cached is not None:
                return cached
            strategy = strategy_cls(self)
            self._mode_strategy_cache["collect"] = strategy
            return strategy

        # Filtered path is request-scoped — never cached.
        return strategy_cls(self, collects_id=collects_id)

    async def _download_mode_items(
        self,
        mode: str,
        items: List[Dict[str, Any]],
        author_name: str,
        seen_aweme_ids: Optional[Set[str]] = None,
        *,
        author_sec_uid: Optional[str] = None,
    ) -> DownloadResult:
        if seen_aweme_ids is None:
            seen_aweme_ids = set()
        deduped_items: List[Dict[str, Any]] = []
        local_seen: Set[str] = set()

        for item in items:
            aweme_id = str(item.get("aweme_id") or "").strip()
            if not aweme_id:
                continue
            if aweme_id in seen_aweme_ids or aweme_id in local_seen:
                continue
            local_seen.add(aweme_id)
            seen_aweme_ids.add(aweme_id)
            deduped_items.append(item)

        result = DownloadResult()
        result.total = len(deduped_items)
        self._progress_set_item_total(result.total, "作品待下载")
        self._progress_update_step("下载作品", f"待处理 {result.total} 条")

        # Accumulate per-aweme DB records and flush in a single transaction
        # at the end — avoids one fsync per item across the whole batch.
        db_batch: Optional[List[Dict[str, Any]]] = [] if self.database else None
        increase_config = self.config.get("increase", {})
        force_download = (
            isinstance(increase_config, dict)
            and mode in increase_config
            and not bool(increase_config.get(mode))
        )

        async def _process_aweme(item: Dict[str, Any], *, report: bool = True):
            aweme_id = item.get("aweme_id")

            def _finish(status: str) -> Dict[str, Any]:
                # 自愈轮重跑的是已经记过账的条目，不再推进条目计数（见
                # BaseDownloader._recover_failed_items）。
                if report:
                    self._progress_advance_item(status, str(aweme_id or "unknown"))
                return {"status": status, "aweme_id": aweme_id}

            try:
                if not await self._should_download(
                    str(aweme_id or ""),
                    force=force_download,
                ):
                    saved = await self._collect_comments_for_existing_aweme(
                        item,
                        author_name,
                        mode,
                        author_sec_uid=author_sec_uid,
                    )
                    return _finish("success" if saved else "skipped")

                outcome: Dict[str, str] = {}
                success = await self._download_aweme_assets(
                    item,
                    author_name,
                    mode=mode,
                    db_batch=db_batch,
                    author_sec_uid=author_sec_uid,
                    outcome=outcome,
                )
            except asyncio.CancelledError:
                # 用户取消要继续往上走，别当成「这条作品失败了」。
                raise
            except Exception:
                # 兜底成一条失败而不是让异常逃出去：逃出去的话条目计数就少了
                # 一条（进度条停住），而统计修正又是按「已记过账」算的。
                logger.exception("Aweme %s failed with an unexpected error", aweme_id)
                return _finish("failed")
            if success:
                return _finish("success")
            # 付费/会员内容下不来是预期内的（CENC 密文，重试也没用）：
            # 记「付费跳过」，不计入失败，也不进失败自愈轮。
            if outcome.get("status") == "paid":
                logger.info(
                    "Aweme %s 付费跳过：%s", aweme_id, outcome.get("note") or "付费/会员内容"
                )
                return _finish("paid")
            return _finish("failed")

        download_results = await self.queue_manager.download_batch(_process_aweme, deduped_items)

        statuses = [entry_status(entry) for entry in download_results]
        # 源站的短时封禁不该变成本次任务的永久失败：主轮跑完后补跑几轮。
        await self._recover_failed_items(
            deduped_items, statuses, partial(_process_aweme, report=False)
        )

        if db_batch:
            await self.database.add_aweme_batch(db_batch)

        for status in statuses:
            if status == "success":
                result.success += 1
            elif status == "skipped":
                result.skipped += 1
            elif status == "paid":
                result.paid += 1
            else:
                result.failed += 1

        return result

    # 向后兼容：旧测试仍直接调用 post 下载入口。
    async def _download_user_post(self, sec_uid: str, user_info: Dict[str, Any]) -> DownloadResult:
        strategy = self._get_mode_strategy("post")
        if strategy is None:
            return DownloadResult()
        return await strategy.download_mode(sec_uid, user_info, seen_aweme_ids=set())

    async def _recover_user_post_with_browser(
        self,
        sec_uid: str,
        user_info: Dict[str, Any],
        aweme_list: List[Dict[str, Any]],
        *,
        item_filter: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    ) -> None:
        browser_cfg = self.config.get("browser_fallback", {}) or {}
        if not browser_cfg.get("enabled", True):
            logger.info("Browser fallback skipped: disabled by config")
            return

        number_limit = self.config.get("number", {}).get("post", 0)
        # 在分页受限场景下，user_info.aweme_count 常常不可靠（经常只返回 20）
        # 媒体筛选需要先拿到详情才能判断，不能用原始 ID 数提前截断。
        expected_count = 0 if item_filter else int(number_limit or 0)
        # number 未设上限时，用主页作品总数当扫描目标：让页面内翻页一路翻到
        # 真正的列表末尾，而不是撞上翻页页数上限就停。仅当 aweme_count 明显
        # 大于已采集量时才采信，避免受限时失真的「20」把翻页提前掐断。
        if expected_count <= 0 and not item_filter:
            try:
                profile_count = int(user_info.get("aweme_count") or 0)
            except (TypeError, ValueError):
                profile_count = 0
            if profile_count > len(aweme_list):
                expected_count = profile_count
                logger.info(
                    "Browser fallback target from profile aweme_count: %s (existing=%s)",
                    profile_count,
                    len(aweme_list),
                )
        if self._post_recovery_limit_reached(aweme_list, number_limit, item_filter):
            logger.info(
                "Browser fallback skipped: number limit reached existing=%s limit=%s",
                len(aweme_list),
                number_limit,
            )
            return

        fallback_started = time.monotonic()
        # 「一键更新」的早停依据：本地已下载的 aweme_id 集合。
        # ⚠️ 只在增量模式（increase.post=true）下给：强制重下时必须把整份列表
        #    扫全，否则用户点「重下」只会拿回最新那几条。
        known_ids = None
        known_stop = 40
        if self._increase_enabled("post"):
            try:
                await self._ensure_local_aweme_index()
                known_ids = set(self._local_aweme_ids or set())
                # 「本地库」不能只看文件名索引：文件被移动/改名过的作品仍在
                # 下载历史里，一并算作已下载（与 _should_download 的兜底一致）。
                if self.database is not None:
                    try:
                        known_ids |= await self.database.get_downloaded_aweme_ids_by_author(
                            str(user_info.get("sec_uid") or sec_uid)
                        )
                    except Exception as exc:  # noqa: BLE001 - 查不到就只用文件名索引
                        logger.debug("Author DB ids unavailable for early stop: %s", exc)
                raw_early = browser_cfg.get("early_stop_known", 40)
                try:
                    browser_cfg_early = int(raw_early)
                except (TypeError, ValueError):
                    browser_cfg_early = 40
                if browser_cfg_early <= 0 or not known_ids:
                    # 0 = 明确关闭早停
                    known_ids = None
                    logger.info("Early stop disabled (early_stop_known=%s)", raw_early)
                else:
                    known_stop = browser_cfg_early
            except Exception as exc:  # noqa: BLE001 — 早停是优化，取不到就退回全量扫描
                logger.debug("Early-stop index unavailable, scanning full list: %s", exc)
                known_ids = None
        logger.info(
            "Browser fallback started: existing=%s expected_count=%s headless=%s "
            "max_scrolls=%s idle_rounds=%s timeout_s=%s early_stop_known=%s",
            len(aweme_list),
            expected_count,
            bool(browser_cfg.get("headless", False)),
            int(browser_cfg.get("max_scrolls", 240) or 240),
            int(browser_cfg.get("idle_rounds", 8) or 8),
            int(browser_cfg.get("wait_timeout_seconds", 600) or 600),
            (known_stop if known_ids is not None else "off"),
        )
        try:
            browser_aweme_ids = await self.api_client.collect_user_post_ids_via_browser(
                sec_uid,
                expected_count=expected_count,
                headless=bool(browser_cfg.get("headless", False)),
                max_scrolls=int(browser_cfg.get("max_scrolls", 240) or 240),
                idle_rounds=int(browser_cfg.get("idle_rounds", 8) or 8),
                wait_timeout_seconds=int(browser_cfg.get("wait_timeout_seconds", 600) or 600),
                known_ids=known_ids,
                known_stop=known_stop,
            )
        except Exception:
            logger.exception(
                "Browser fallback failed: duration_ms=%s",
                int((time.monotonic() - fallback_started) * 1000),
            )
            return

        browser_aweme_items: Dict[str, Dict[str, Any]] = {}
        browser_post_stats: Dict[str, int] = {}
        if hasattr(self.api_client, "pop_browser_post_aweme_items"):
            try:
                browser_aweme_items = self.api_client.pop_browser_post_aweme_items() or {}
            except Exception as exc:
                logger.debug("Fetch browser post items skipped: %s", exc)
        if hasattr(self.api_client, "pop_browser_post_stats"):
            try:
                browser_post_stats = self.api_client.pop_browser_post_stats() or {}
            except Exception as exc:
                logger.debug("Fetch browser post stats skipped: %s", exc)

        if not browser_aweme_ids:
            logger.warning(
                "Browser fallback returned no aweme_id: duration_ms=%s stats=%s",
                int((time.monotonic() - fallback_started) * 1000),
                browser_post_stats,
            )
            return

        existing_ids = {str(item.get("aweme_id")) for item in aweme_list if item.get("aweme_id")}
        missing_ids = [aweme_id for aweme_id in browser_aweme_ids if aweme_id not in existing_ids]
        if not missing_ids:
            logger.info(
                "Browser fallback found no missing items: browser_ids=%s existing=%s "
                "duration_ms=%s",
                len(browser_aweme_ids),
                len(existing_ids),
                int((time.monotonic() - fallback_started) * 1000),
            )
            return

        logger.warning(
            "Recovering aweme details from browser list, missing count=%s",
            len(missing_ids),
        )
        detail_failed = 0
        detail_success = 0
        reused_from_browser_items = 0
        total_missing = len(missing_ids)
        for index, aweme_id in enumerate(missing_ids, start=1):
            if self._post_recovery_limit_reached(aweme_list, number_limit, item_filter):
                break

            if index == 1 or index == total_missing or index % 5 == 0:
                self._progress_update_step("浏览器回补", f"补全详情 {index}/{total_missing}")

            detail = browser_aweme_items.get(str(aweme_id))
            if not detail:
                try:
                    await self.rate_limiter.acquire()
                    detail = await self.api_client.get_video_detail(aweme_id, suppress_error=True)
                except Exception as exc:
                    detail_failed += 1
                    logger.warning(
                        "Browser fallback detail fetch failed for aweme_id=%s: %s",
                        aweme_id,
                        exc,
                    )
                    continue
                if detail:
                    detail_success += 1
            else:
                reused_from_browser_items += 1
            if not detail:
                detail_failed += 1
                continue
            author = detail.get("author", {}) if isinstance(detail, dict) else {}
            detail_sec_uid = author.get("sec_uid") if isinstance(author, dict) else None
            if detail_sec_uid and str(detail_sec_uid) != str(sec_uid):
                logger.warning(
                    "Skip aweme_id=%s due to mismatched sec_uid (%s)",
                    aweme_id,
                    detail_sec_uid,
                )
                continue
            aweme_list.append(detail)

        self._progress_update_step(
            "浏览器回补",
            f"回补完成，复用 {reused_from_browser_items}，补拉成功 {detail_success}，失败 {detail_failed}",
        )
        logger.warning(
            "Browser fallback summary: duration_ms=%s merged_ids=%s selected_ids=%s "
            "post_items=%s post_pages=%s reused=%s detail_success=%s detail_failed=%s",
            int((time.monotonic() - fallback_started) * 1000),
            browser_post_stats.get("merged_ids", 0),
            browser_post_stats.get("selected_ids", len(browser_aweme_ids)),
            browser_post_stats.get("post_items", len(browser_aweme_items)),
            browser_post_stats.get("post_pages", 0),
            reused_from_browser_items,
            detail_success,
            detail_failed,
        )
        if detail_failed > 0:
            logger.warning(
                "Browser fallback detail fetch failed: %s/%s",
                detail_failed,
                total_missing,
            )
        self._verify_scan_total(aweme_list, user_info)

    @staticmethod
    def _verify_scan_total(
        aweme_list: List[Dict[str, Any]],
        user_info: Dict[str, Any],
    ) -> None:
        """扫描结束后与主页作品总数对账。

        少扫必须在任务日志里留下 ERROR 级证据（任务中心默认只收 ERROR），
        否则「主页 1794 / 实扫 1192」这种静默缺漏用户根本无从发现。
        差 2 条以内视为正常波动（扫描期间新增/删除作品）。
        """
        try:
            profile_count = int(user_info.get("aweme_count") or 0)
        except (TypeError, ValueError):
            return
        deficit = profile_count - len(aweme_list)
        if deficit > 2:
            logger.error(
                "作品扫描数量与主页不一致：实扫 %s 条，主页显示 %s 条（缺 %s 条）。"
                "常见原因：接口分页被风控截断 / 回补阶段部分详情拉取失败。"
                "可稍后重试、重新登录刷新 Cookie，或调大 browser_fallback 配置后重跑。",
                len(aweme_list),
                profile_count,
                deficit,
            )

    @staticmethod
    def _post_recovery_limit_reached(
        items: List[Dict[str, Any]],
        number_limit: Any,
        item_filter: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]],
    ) -> bool:
        limit = int(number_limit or 0)
        if limit <= 0:
            return False
        eligible_items = item_filter(items) if item_filter else items
        return len(eligible_items) >= limit
