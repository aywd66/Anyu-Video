# Stripchat 直播录制（模块 6）

对 OneRecord（`E:\kk\lp\OneRecord\libs\feiyu-extractor-1.0.1.jar`）中 Stripchat
提取链路的独立复刻。逆向分析产物见工作区
`draft-b60dd085-.../stripchat-analysis.md` 与 `stripchat_recorder.py` 头注释。

## 能力

- 房间链接：`stripchat.com/{房间}`、`vr.stripchat.com/cam/{房间}`（VR）、
  `xhamsterlive.com`、`stripol.com`、`camwhorestv.info`（三个白标镜像）
- 直播状态判定：`public` 且摄像头可用才可录；`private / groupShow /
  virtualPrivate / p2pVoice / idle` 视为不可录
- 播放列表：`edge-hls.doppiocdn{com,org,live,net}` 4 路 CDN 顺序尝试，
  master 列表选最高 BANDWIDTH 变体
- **mouflon 分片解密**：m3u8 的 `#EXT-X-MOUFLON:PSCH` 行查 11 对密钥表得
  pkey，分片 URL 做 反转 → Base64 补位解码 → 与 SHA-256(pkey) 循环异或
- fMP4 分片自动拼 init 头；停止时补 `#EXT-X-ENDLIST` 并 ffmpeg concat 合并；
  无 ffmpeg 时保留分片 + `playlist.m3u8`（可直接用播放器打开）

## 工具箱集成

- 桥接端点：`POST /api/bridge/stripchat/start|stop|open-dir`、
  `GET /api/bridge/stripchat/status`
- 录制器是**长驻独立进程**（不走统一任务队列，避免占住「总并发 2」），
  一次一个房间；停止用停止文件优雅收尾，30 秒等不到才强杀
- 运行工件：`data/stripchat/recorder.log`（网页日志窗 tail 它）、
  `data/stripchat/stop`（停止信号）；录像落 `downloads/stripchat/`
  （`房间名_时间戳/` 目录，目录内有合并后的 `.ts` 成品）
- 启动入口：双击工程根的 `启动工具箱.bat` 打开主界面 → 直播流录制页的
  **「Stripchat 房间录制」** 区块（在登录态区块下方）

## 已知边界

- **反爬现状（2026-10 实测）**：stripchat 部署了多层对抗——TLS 指纹墙
  （Python/Node 的握手直接被断，SSL EOF/超时）、API 层 418、无头浏览器检测。
  因此 urllib 直连路线已基本不可用（保留为无 playwright 环境的降级）。
- **观众模式（当前主路线）**：真实 Chrome（用户指定路径
  `C:\Program Files\Google\Chrome\Application\chrome.exe`）+ 专属持久配置
  （`data/stripchat/browser-profile/`）打开房间页，旁听页面播放器自己的
  m3u8 流量，分片在页面内下载——不伪造任何请求。首次使用若弹人机验证，
  手动通过一次，配置会记住。窗口即录制会话，录制期间勿关。
- **出口 IP**：频繁请求会被临时拉黑（连真实浏览器都断连）。遇到时等
  30–60 分钟冷却或换节点，先用自己浏览器确认房间页能打开再录。
- mouflon 密钥表内置 11 对（提取自 OneRecord 资源文件）；平台换钥后需更新
  `stripchat_recorder.py` 里的 `MOUFLON_KEYS`。
- VR 播放列表 URL 的 `[vr]` 后缀拼写按原实现字节码复刻，未在线核对。

## 命令行直接用

```
python modules\stripchat\stripchat_recorder.py "https://stripchat.com/房间名" --out D:\录像
```
（Ctrl+C 停止并合并）
