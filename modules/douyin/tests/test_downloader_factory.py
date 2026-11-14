import pytest

from auth import CookieManager
from config import ConfigLoader
from control import QueueManager, RateLimiter, RetryHandler
from core.api_client import DouyinAPIClient
from core.downloader_factory import DownloaderFactory
from core.mix_downloader import MixDownloader
from core.music_downloader import MusicDownloader
from core.user_downloader import UserDownloader
from core.video_downloader import VideoDownloader
from storage import FileManager


@pytest.mark.asyncio
async def test_downloader_factory_routes_supported_types(tmp_path):
    config = ConfigLoader()
    config.update(path=str(tmp_path))
    file_manager = FileManager(str(tmp_path))
    cookie_manager = CookieManager(str(tmp_path / ".cookies.json"))
    api_client = DouyinAPIClient({})

    common = dict(
        config=config,
        api_client=api_client,
        file_manager=file_manager,
        cookie_manager=cookie_manager,
        database=None,
        rate_limiter=RateLimiter(max_per_second=5),
        retry_handler=RetryHandler(max_retries=1),
        queue_manager=QueueManager(max_workers=1),
    )

    try:
        assert isinstance(DownloaderFactory.create("video", **common), VideoDownloader)
        assert isinstance(DownloaderFactory.create("gallery", **common), VideoDownloader)
        assert isinstance(DownloaderFactory.create("user", **common), UserDownloader)
        assert isinstance(DownloaderFactory.create("collection", **common), MixDownloader)
        assert isinstance(DownloaderFactory.create("music", **common), MusicDownloader)
        # 工厂必须把链接类型作为「作品来源」注给下载器 —— WebUI 的「关注」列表
        # 就是靠它区分「主页批量」与「单作品链接」的。在工厂统一注入，是因为
        # CLI / 服务端 / 重试三条路径都走这里，漏一处就会留下来源不明的作品。
        assert DownloaderFactory.create("video", **common).origin == "video"
        assert DownloaderFactory.create("user", **common).origin == "user"
    finally:
        await api_client.close()


def test_downloader_factory_returns_none_for_unknown_type():
    result = DownloaderFactory.create(
        "unknown",
        config=None,
        api_client=None,
        file_manager=None,
        cookie_manager=None,
    )
    assert result is None
