from __future__ import annotations

import asyncio
from functools import partial
from typing import Any, Dict, List, Optional

from core.downloader_base import BaseDownloader, DownloadResult, entry_status
from core.user_modes.base_strategy import BaseUserModeStrategy
from utils.logger import setup_logger

logger = setup_logger("MixDownloader")


def derive_mix_collection_dir(mix_detail: Optional[Dict[str, Any]], mix_id: Any) -> str:
    """Folder name for a 合集's own directory.

    Prefer the mix's display name (``mix_name`` → ``title``); fall back to the
    ``mix_id`` so downloads never collapse into a bare shared ``mix`` dir.
    Shared by the fresh-download (:class:`MixDownloader`) and retry
    (:mod:`core.retry_executor`) paths so a retried item lands in the SAME
    ``<author>/mix/<collection>/`` folder as its originally-downloaded peers.
    """
    name = ""
    if isinstance(mix_detail, dict):
        name = (mix_detail.get("mix_name") or mix_detail.get("title") or "").strip()
    return name or str(mix_id)


class MixDownloader(BaseDownloader):
    async def download(self, parsed_url: Dict[str, Any]) -> DownloadResult:
        result = DownloadResult()

        mix_id = parsed_url.get("mix_id")
        if not mix_id:
            logger.error("No mix_id found in parsed URL")
            return result

        aweme_list = await self._collect_mix_aweme_list(str(mix_id))

        result.total = len(aweme_list)
        self._progress_set_item_total(result.total, "合集作品待下载")
        self._progress_update_step("下载合集", f"mix_id={mix_id}，待处理 {result.total} 条")

        mix_detail = await self._get_mix_detail(str(mix_id))
        author_name = (
            (mix_detail.get("author") or {}).get("nickname")
            if isinstance(mix_detail, dict)
            else None
        ) or "mix"

        # Give each 合集 its own folder: <author>/mix/<合集名>/... .
        collection_dir = derive_mix_collection_dir(mix_detail, mix_id)

        async def _process_aweme(item: Dict[str, Any], *, report: bool = True):
            aweme_id = item.get("aweme_id")

            def _finish(status: str) -> Dict[str, Any]:
                if report:
                    self._progress_advance_item(status, str(aweme_id or "unknown"))
                return {"status": status, "aweme_id": aweme_id}

            if not aweme_id:
                return _finish("failed")

            try:
                if not await self._should_download(str(aweme_id)):
                    saved = await self._collect_comments_for_existing_aweme(
                        item,
                        author_name,
                        mode="mix",
                        collection_dir=collection_dir,
                    )
                    return _finish("success" if saved else "skipped")

                outcome: Dict[str, str] = {}
                success = await self._download_aweme_assets(
                    item, author_name, mode="mix", collection_dir=collection_dir,
                    outcome=outcome,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Mix aweme %s failed with an unexpected error", aweme_id)
                return _finish("failed")
            if success:
                return _finish("success")
            # 付费/会员内容：CENC 密文解不开，属预期内下不来（与 UserDownloader 同口径）
            if outcome.get("status") == "paid":
                return _finish("paid")
            return _finish("failed")

        download_results = await self.queue_manager.download_batch(_process_aweme, aweme_list)
        statuses = [entry_status(entry) for entry in download_results]
        # 与 UserDownloader 同一条自愈路径：源站的短时封禁不该变成本次的永久失败。
        await self._recover_failed_items(
            aweme_list, statuses, partial(_process_aweme, report=False)
        )
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

    async def _collect_mix_aweme_list(self, mix_id: str) -> List[Dict[str, Any]]:
        fetch_mix_aweme = getattr(self.api_client, "get_mix_aweme", None)
        if not callable(fetch_mix_aweme):
            logger.error("API client has no get_mix_aweme implementation")
            return []

        aweme_list: List[Dict[str, Any]] = []
        has_more = True
        cursor = 0
        number_limit = int(self.config.get("number", {}).get("mix", 0) or 0)

        while has_more:
            await self.rate_limiter.acquire()
            raw_page = await fetch_mix_aweme(mix_id, cursor=cursor, count=20)
            page = BaseUserModeStrategy._normalize_page_data(raw_page)
            items = page.get("items", [])
            if not items:
                break

            for item in items:
                aweme = self._extract_aweme_from_item(item)
                if aweme:
                    aweme_list.append(aweme)

            if number_limit > 0 and len(aweme_list) >= number_limit:
                aweme_list = aweme_list[:number_limit]
                break

            has_more = bool(page.get("has_more", False))
            next_cursor = int(page.get("max_cursor", 0) or 0)
            if has_more and next_cursor == cursor:
                logger.warning(
                    "Mix pagination cursor did not advance (%s), stop to avoid loop",
                    cursor,
                )
                break
            cursor = next_cursor

        return aweme_list

    async def _get_mix_detail(self, mix_id: str) -> Optional[Dict[str, Any]]:
        getter = getattr(self.api_client, "get_mix_detail", None)
        if not callable(getter):
            return None
        try:
            return await getter(mix_id)
        except Exception as exc:
            logger.warning("Get mix detail failed: %s", exc)
            return None

    @staticmethod
    def _extract_aweme_from_item(item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(item, dict):
            return None
        if item.get("aweme_id"):
            return item
        for key in ("aweme", "aweme_info", "aweme_detail"):
            value = item.get(key)
            if isinstance(value, dict) and value.get("aweme_id"):
                return value
        return None
