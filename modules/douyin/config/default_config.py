from typing import Any, Dict

DEFAULT_CONFIG: Dict[str, Any] = {
    "path": "./Downloaded/",
    # 处理内容开关：默认只保存视频本体。封面 / 音乐 / 头像 / 作品 JSON 都是
    # 附带产物，绝大多数用户并不需要，默认全开会平白多出几倍文件和请求。
    # 只影响新配置；已有 config.yml 里显式写过的值不受影响。
    "video": True,
    "music": False,
    "cover": False,
    "avatar": False,
    "json": False,
    "start_time": "",
    "end_time": "",
    "folderstyle": True,
    # 命名模板：渲染时可用变量见 utils/naming.py:ALLOWED_VARIABLES。默认保持
    # 与历史行为一致（`{date}_{title}_{id}`），用户可在设置中改写。
    "filename_template": "{date}_{title}_{id}",
    "folder_template": "{date}_{title}_{id}",
    # 作者目录层命名方式：
    #   "nickname"    - 作者昵称（默认，最直观，但重名会合并、改名会分裂）
    #   "sec_uid"     - 作者 sec_uid（稳定唯一，但不直观）
    #   "nickname_uid" - 昵称_sec_uid（直观 + 唯一）
    # 切换只影响后续下载，不会迁移已存在的目录。
    "author_dir": "nickname",
    # 是否按下载模式（post / like / mix …）再分一层子文件夹。
    #   True  - 作者目录下再分 post/like/... （默认，与历史行为一致）
    #   False - 不分模式层，文件直接落在作者目录下（复刻 legacy 布局，无 POST 文件夹）
    "group_by_mode": True,
    "download_pinned": False,
    # 下载博主作品时，是否在作者根目录覆盖保存主页地址文本。
    "author_url": False,
    # 下载博主作品时，是否在作者根目录覆盖保存一张主页首屏截图。
    "homepage_screenshot": False,
    "mode": ["post"],
    "number": {
        "post": 0,
        "like": 0,
        "allmix": 0,
        "mix": 0,
        "music": 0,
        "collect": 0,
        "collectmix": 0,
    },
    # 增量下载首先检查磁盘主文件。磁盘缺失（文件被删/被移动）时：
    # False = 数据库里仍有该作品的下载记录就继续跳过 —— 也就是「下载过的永远算下过」，
    #         删除本地文件不会让它被重新下载（默认，符合「已下载自动跳过」的预期）；
    # True  = 重新下载（旧行为：把删文件当成「想再要一份」）。
    # 注意：数据库不可用（database: false）或记录已被删除时，没有依据可判，仍会下载。
    "redownload_missing_files": False,
    # 各模式是否启用增量下载；False 会强制重下并原子覆盖当前筛选范围。
    "increase": {
        "post": True,
        "like": True,
        "allmix": True,
        "mix": True,
        "music": True,
    },
    # 批量下载的并发路数（每个 worker 同一时刻占一条下载连接）。
    # 5 路在实测里只能跑出 3.3MB/s，16 路稳定到 9~11MB/s（再往上收益递减：
    # 28 路只多 1.4x）。UI 的「并发线程数」上限也是 16。
    "thread": 16,
    "retry_times": 3,
    "rate_limit": 2,
    # 批量任务跑完之后，把记成「失败」的作品再补跑几轮（0 = 关闭）。
    # 源站/CDN 的短时封禁（实测出现过连续 100 秒全失败、之后自行恢复）不该
    # 变成本次任务的永久失败；详见 core/downloader_base._recover_failed_items。
    "failure_retry_passes": 2,
    "proxy": "",
    # 视频下载画质。可选值：
    #   "original" - 原画：探测上传原片（ratio=default，转码档列表之外，可比
    #                最高转码档大数倍），比最高转码档大则优先下载；探测失败
    #                退回最高转码档。代价是每条作品多一次探测请求（超时 10s）
    #   "highest"  - 最高转码档（默认）：只在 bit_rate 阶梯里挑，不发探测请求
    #   "lowest"   - 最低可用档（省流量）
    #   "1440p" / "1080p" / "720p" / "540p" / "480p" / "360p"
    #              - 指定分辨率，匹配不到时自动降级到最接近的可用档
    # 注：实际可用档位取决于原视频上传质量；完整尺寸按短边匹配，仅有 width 时兼容旧响应。
    "video_quality": "highest",
    "database": True,
    "database_path": "dy_downloader.db",
    "progress": {
        "quiet_logs": True,
    },
    "transcript": {
        "enabled": False,
        "model": "gpt-4o-mini-transcribe",
        "output_dir": "",
        "response_formats": ["txt", "json"],
        "api_url": "https://api.openai.com/v1/audio/transcriptions",
        "api_key_env": "OPENAI_API_KEY",
        "api_key": "",
        # When true (default), the desktop sidecar runs the source video
        # through ffmpeg locally and uploads only the extracted mono mp3
        # to the transcription endpoint. Saves bandwidth and avoids the
        # OpenAI 25 MiB single-file ceiling. Set to false to fall back to
        # uploading the source video itself (legacy behaviour). The UI
        # deliberately does not surface this toggle — see
        # ``.kiro/specs/transcript-audio-extract-and-ui`` Requirement 1.
        "upload_audio_only": True,
    },
    "auto_cookie": False,
    "browser_fallback": {
        "enabled": True,
        "headless": False,
        "max_scrolls": 240,
        "idle_rounds": 8,
        "wait_timeout_seconds": 600,
        # 增量早停：连续 N 条已在本地即停止翻页。40 = 2 屏；0 = 关闭。
        "early_stop_known": 40,
    },
    # 下载完成通知（可选）。providers 支持 bark / telegram / webhook。
    "notifications": {
        "enabled": False,
        "on_success": True,
        "on_failure": True,
        "providers": [],
    },
    # 评论采集（可选）。启用后每个作品会额外生成 *_comments.json。
    "comments": {
        "enabled": False,
        "include_replies": False,
        "max_comments": 0,  # 0 = 不限
        "page_size": 20,
    },
    # 直播录制（可选）。由 live.douyin.com / /follow/live/ 链接触发。
    "live": {
        "max_duration_seconds": 0,  # 0 = 直到流结束
        "chunk_size": 65536,
        "idle_timeout_seconds": 30,
    },
    # REST API 服务模式（可选，需 fastapi + uvicorn）。
    "server": {
        "max_jobs": 500,  # 内存中保留的 job 条数上限（不含 in-flight）
        "job_ttl_seconds": 86400,  # 完成态 job 保留时间（秒）
    },
}
