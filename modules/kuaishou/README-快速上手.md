# 快手视频下载环境 — 快速上手

> 生成日期：2026-09-21 ｜ 完整报告见 `yt-dlp-配置与使用报告.html`

## 环境组成

| 组件 | 位置 | 说明 |
|---|---|---|
| yt-dlp 源码 | `E:\录屏\spxz\yt-dlp` | 官方仓库浅克隆 @ c7fb478 |
| Python 虚拟环境 | `E:\录屏\spxz\ytdlp-env` | Python 3.14，yt-dlp 2026.08.19 从本地源码安装 |
| 下载工具 | `E:\录屏\spxz\tools\kuaishou_dl.py` | 快手分享链接专用（配合 yt-dlp） |
| ffmpeg/ffprobe | 系统 PATH | 7.1.1，用于合成与校验 |

## 文件结构

- **文件结构**
  - 视频按作者分文件夹：`E:\录屏\spxz\郁闷装深沉\`（247 个）、`E:\录屏\spxz\芝士蒲公英\`（477 个，1355MB）
  - 作品清单按作者存：`works_list.json`、`works_list_<作者ID>.json`
- 作品清单与台账：`download_log.json`（主台账）保存在 `E:\录屏\spxz` 根目录

## 快手分享链接下载（本机已验证可用）

### 一键下载器（推荐，双击即用）

双击桌面快捷方式 **“快手一键下载”** → 弹窗粘贴分享链接 → 点“开始下载”→ 自动下载到“郁闷装深沉”文件夹。
- 无登录态时自动弹出扫码窗口；下载完成自动弹窗并打开文件夹
- 支持“全部作品 / 仅最新 1 个”两种模式；台账自动去重
- 源脚本：`E:\录屏\spxz\tools\快手一键下载器.pyw`

### 命令行方式

```powershell
# 全量下载主页所有可公开获取的作品（最优画质自动选择）
& "E:\录屏\spxz\ytdlp-env\Scripts\python.exe" "E:\录屏\spxz\tools\kuaishou_dl.py" "https://v.kuaishou.com/J0MdxkzY" "E:\录屏\spxz" all

# 单个下载: 序号 1 = 最新作品, 2 = 次新 ...
& "E:\录屏\spxz\ytdlp-env\Scripts\python.exe" "E:\录屏\spxz\tools\kuaishou_dl.py" "https://v.kuaishou.com/J0MdxkzY" "E:\录屏\spxz" 1
```

- 主页分享链接会自动解析作者作品列表；`all` 全量下载，逐个间隔 1.2 秒防风控
- 最优画质：自动从 manifest 多码率轨道选 avgBitrate 最高者（优先 mp4 直链）
- 输出文件名：`标题_[作品ID].mp4`；重复运行 `--no-overwrites` 自动跳过已完成
- 下载台账：`download_log.json`（每作品的画质/时长/成功状态）
- 输出文件默认保存在 `E:\录屏\spxz\郁闷装深沉` 子目录（以作者命名；批量下载完成后手动归档即可）

## 普通 yt-dlp 下载（YouTube 等支持站点）

```powershell
& "E:\录屏\spxz\ytdlp-env\Scripts\yt-dlp.exe" -P "E:\录屏\spxz" "<视频URL>"
```

## 校验下载文件

```powershell
ffprobe -v error -show_entries format=duration,size -of json "<视频文件>"
```

## 常见问题

| 现象 | 原因与对策 |
|---|---|
| `Need captcha` | 快手风控，等几分钟重试；不要用破解工具 |
| `Unsupported URL`（yt-dlp 直接下载快手链接） | 正常现象：yt-dlp 原生无快手提取器（官方 issue #14010），快手链接一律走 `kuaishou_dl.py` |
| 作品列表为空 | 分享的可能是单作品链接或作者已删除作品；换 `作品序号` 或确认链接有效性 |
| `作品不可用 status=1040` | 平台标记该作品不可访问（已删除/受限），复测确认后跳过即可 |
| 主页作品需登录 | 未登录状态网页端只公开部分作品，属平台限制 |

## 下载主页全部历史作品（需登录态）

未登录状态下快手只公开约 20 个作品（更多会弹验证码 + 登录墙）。要下载主页全部历史作品：

1. 按《[Cookie-获取指南](E:\录屏\spxz\Cookie-获取指南.md)》导出登录 Cookie 存为 `E:\录屏\spxz\cookie.txt`
2. 运行全量脚本（自动去重已下载的，断点续传）：

```powershell
& "E:\录屏\spxz\ytdlp-env\Scripts\python.exe" "E:\录屏\spxz\tools\kuaishou_full_dl.py" "E:\录屏\spxz\cookie.txt" "E:\录屏\spxz"
```

## 并行加速版（推荐大批量时使用）

```powershell
# 直链预取 + 多线程并行下载（默认 4 线程），画质选择逻辑与串行版完全一致
& "E:\录屏\spxz\ytdlp-env\Scripts\python.exe" "E:\录屏\spxz\tools\kuaishou_fast_dl.py" "E:\录屏\spxz" --workers 4
# 自定义线程数与错峰间隔；--limit N 只下前 N 个；--recollect 重新滚动收集作品清单
& "E:\录屏\spxz\ytdlp-env\Scripts\python.exe" "E:\录屏\spxz\tools\kuaishou_fast_dl.py" "E:\录屏\spxz" --workers 6 --sleep 0.2
```

  - 实测：30 个作品 27.9 秒（串行约 118 秒），提速约 4.2 倍，画质无损（720p 最优码率轨道）。
  - 多作者：支持 --list 指定作品清单 + 主台账去重（配套一键下载器自动按链接识别作者）
需先有扫码登录态（login-profile 目录）与调试 Edge（脚本自动拉起）。

## 回滚 / 清理

本次配置全部落在 `E:\录屏\spxz` 目录内，未写入系统目录、未装全局包：

```powershell
# 完全撤销：删除以下目录即可
Remove-Item -Recurse -Force "E:\录屏\spxz\ytdlp-env"   # Python 虚拟环境
Remove-Item -Recurse -Force "E:\录屏\spxz\yt-dlp"      # 源码
Remove-Item -Recurse -Force "E:\录屏\spxz\tools"       # 工具脚本
```
