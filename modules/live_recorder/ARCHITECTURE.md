# LiveRecorder 架构说明文档

本文档描述 LiveRecorder 的模块划分、职责边界、线程模型与数据流转方式，
并说明若干关键设计决策的取舍理由，便于后续维护与二次开发。

---

## 一、总体架构

系统采用**四层架构**，层与层之间通过明确定义的接口通信，禁止跨层直接调用：

```
┌──────────────────────────────────────────────────────────────────────┐
│  ④ 界面层  ui/                                                        │
│     MainWindow · LogPanel · AddRoomDialog · SettingsDialog           │
│     LoginDialog（扫码登录）                                           │
│     职责：渲染状态、收集用户操作                                       │
└───────────────▲──────────────────────────────┬───────────────────────┘
                │ EventBus 事件 + 状态快照       │ start/stop/add/remove
                │ （桥接为 Qt 信号投递主线程）    │ apply_settings
┌───────────────┴──────────────────────────────▼───────────────────────┐
│  ② 监控调度层  core/                                                  │
│     MonitorEngine · EventBus · RoomRuntime                            │
│     职责：轮询调度、状态决策、录制任务编排、全局磁盘保护                 │
└───────────────┬──────────────────────────────┬───────────────────────┘
                │ resolve_stream(parser, ...)   │ StreamRecorder(...)
                ▼                               ▼
┌──────────────────────────────┐  ┌──────────────────────────────────────┐
│  ① 平台解析层  platforms/     │  │  ③ 录制层  recorder/                  │
│   BaseLiveParser              │  │   StreamRecorder                      │
│   KuaishouParser              │  │   merge_segments / remux_to_mp4       │
│   DouyinParser                │  │   storage（目录/命名/磁盘）            │
│   registry（注册表+降级入口）  │  │   ffmpeg_utils（进程调用封装）          │
│   职责：链接解析、开播检测、   │  │   职责：拉流录制、分片、重连、合并      │
│         原画流地址获取         │  │                                       │
└──────────────┬───────────────┘  └───────────────┬──────────────────────┘
               │                                  │
               ▼                                  ▼
     httpx · Playwright                    ffmpeg / ffprobe

        跨层公共设施：utils/logger.py（日志与缓冲区）、config/settings.py（配置持久化）
```

**依赖方向严格单向**：界面层 → 调度层 → {解析层, 录制层} → 公共设施。
反向通知统一通过 `EventBus` 与日志缓冲区的**观察者模式**完成，不存在反向 import。

---

## 二、分层职责详解

### ① 平台解析层 `platforms/`

**唯一职责**：把「用户输入」变成「可拉取的流地址」。所有平台差异都封锁在这一层。

| 模块 | 关键内容 | 说明 |
|---|---|---|
| `base.py` | `BaseLiveParser`、`RoomInfo`、`StreamInfo`、`LiveStatus`、`StreamQuality`、`StreamFormat`、`PlatformError` / `RoomUnavailableError` / `RiskControlError` / `EmptyResponseError` | 定义抽象契约与数据模型；实现通用的重试、节流、请求头构造、会话管理 |
| `kuaishou.py` | `KuaishouParser` | 快手：短链重定向 → **从路径 `/u/<id>` 取主播 ID** → **推荐流索引（首选）** → `livedetail`（已失效）→ 浏览器兜底（含风控识别与落地校验） |
| `douyin.py` | `DouyinParser` | 抖音：短链重定向 → `room_id`/`web_rid` 换算 → `web/enter` 接口 → 按画质键位选取流地址；含 Playwright 兜底与身份校验 |
| `playwright_helper.py` | `BrowserSession` | 启动真实 Chromium 监听网络请求抓取播放地址；返回落地 URL 与**风控判定**供调用方校验；支持注入 Cookie 与首页预热 |
| `qr_login.py` | `QrLoginSession`、`SPECS` | 扫码登录会话：打开登录页 → **自动点出登录弹窗** → 定位二维码 → 轮询登录态 → 导出 Cookie。命令行与界面共用 |
| `login_helper.py` | `interactive_login`、`login_and_save` | 命令行登录助手（复用 `qr_login`，在终端打印进度；快手必需） |
| `registry.py` | `get_parser`、`detect_platform`、`resolve_stream` | 平台注册表 + 三级降级取流入口 |
| `utils.py` | `build_headers`、`random_profile`、`throttle`、`dig`、`extract_url` | 反风控工具与安全的嵌套取值工具 |

#### 异常语义（决定上层行为，改动需谨慎）

平台层的异常类型不是「分类好看」，而是**直接决定调度层与录制层怎么反应**：

| 异常 | 含义 | 上层行为 |
|---|---|---|
| `RoomUnavailableError` | 房间确实不可用（未开播 / 内容受限） | **不降级、不兜底、不重连**，录制层立即收尾；列表显示「未开播」 |
| `RiskControlError` | 被风控拦截（403/429/444 或验证页） | **不重试**（越试封得越久），状态置为「未知」并保持现状 |
| `EmptyResponseError` | HTTP 200 但响应体为空（`RiskControlError` 子类） | 平台层可针对性补一次首访 Cookie 后重试一次 |
| `PlatformError` | 其它接口 / 解析问题 | 走三级降级与重连 |

> **`RoomUnavailableError` 为什么必须与普通 `PlatformError` 分开？**
> 若把「房间已下播」当成普通接口异常，`resolve_stream` 会继续做画质降级、
> 再启动浏览器兜底；而抖音的下播页会**自动播放推荐直播**，最终录到别的房间。
> 把「不可用」判定提前，是阻断这条错误路径的关键。

#### 会话指纹与 Cookie 域校验

两点反风控细节，都是「看起来能跑但会踩坑」的地方：

1. **会话级固定指纹**：`BaseLiveParser.__init__` 通过 `random_profile()` 随机一次
   UA / Accept-Language，整个会话复用。真实浏览器在同一会话内 UA 是固定的；
   每个请求重新随机会造成 Cookie 与 UA 不匹配，反而形成明显的自动化特征。
2. **Cookie 必须校验域**：httpx 的 `CookieJar` 是全局共享的。抖音分享页
   `webcast.amemv.com` 会写入一个 `ttwid`，与 `live.douyin.com` 的同名 Cookie **域不同**，
   不会随请求发送到主域。若只按「jar 里存在 ttwid」判断就会误以为已就绪，
   导致 `enter` 接口稳定返回「200 + 空体」。因此 `_ensure_cookies` 用
   `_has_domain_cookie()` 按域后缀匹配。

#### 浏览器兜底的身份校验

`capture_streams()` 除了返回候选地址，还会记录两个关键信息供调用方校验：

1. `final_url()` —— 页面**最终停留**的地址。调用方据此校验落地房间号是否与目标一致，
   不一致则丢弃结果。这是「录错房间」风险的第二道闸门（第一道是 `fetch_stream` 里的
   开播状态前置判定）。
2. `page_blocked()` —— 页面是否被风控拦截。判据是正文出现「请求过快 / 请稍后重试 /
   验证码 / 安全验证」等字样，或最终地址跳到验证域名。

**为什么必须区分「风控」与「未开播」？**
被风控的页面同样抓不到流；更糟的是，它往往**仍在播放推荐位内容**。
实测：请求快手直播间 A 时，页面自动跳转到了房间 B 并播放了 B 的流。
若把这种情形判为「未开播」，用户会看到「明明在播却显示未开播」；
若不校验落地地址，就会直接录到别人的直播。

#### 快手：为什么首选「推荐流索引」

快手网页端对**未登录访客**关闭了所有「按房间查询」的接口（详见 `kuaishou.py`
模块头的实测记录），因此不能照搬抖音「按房间查接口」的思路。

`live_api/liveroom/reco` 是唯一仍可用的入口，它一次返回约 10 个**在播房间**及其
**完整流地址**（`liveStream.playUrls.h264.adaptationSet.representation[].url`），
并且支持翻页与 `categoryId` 过滤。

于是快手侧的取流顺序为：

```
推荐流索引（纯 HTTP，缓存 TTL 45s，多房间共用）
    │ 未命中
    ▼
livedetail（多已失效，仅在未标记降级时试一次，用于发现接口恢复）
    │ 未命中
    ▼
浏览器兜底（预热首页 → 抓包 → 风控识别 + 落地校验）
    │ 失败
    ▼
RiskControlError / PlatformError
```

> **索引是「共享 + 带 TTL」的**，这点很重要：监控 N 个快手房间时，
> 每个 TTL 周期只多付出「页数」次请求，而不是 N × 页数 次。
> 索引结构见 `_load_reco_index()`；条目由 `_reco_entry_to_info()` 生成。
>
> **已知代价**：未登录时只能覆盖推荐流里出现的房间。要监控任意指定主播，
> 必须通过 `login_helper.py` 登录（见 README 第 6 步）。

#### 三个核心抽象方法

```python
resolve_room(raw_input) -> str          # 分享链接/房间号 → 规范化房间标识
check_live(room_id) -> RoomInfo         # 检测开播状态（异常返回 UNKNOWN，不抛异常）
fetch_stream(room_id, quality) -> StreamInfo   # 获取指定画质的流地址
```

#### 三级降级取流策略（`registry.resolve_stream`）

```
偏好画质（原画）
    │ 失败（RoomUnavailableError）────────────► 直接上抛，降级/兜底都不会有结果
    │ 失败（其它 PlatformError）
    ▼
另一档画质（超清 720P）        ← 触发「降级事件」，写入日志
    │ 失败
    ▼
Playwright 浏览器兜底          ← 直接监听网络请求抓取播放地址
    │                          （抓到后校验页面落地房间号，不一致则丢弃）
    │ 失败
    ▼
抛出 PlatformError → 调度层安排重连
```

> **为什么 `check_live` 不抛异常？**
> 一次网络抖动或风控拦截不应该被误判为「下播」，否则会导致录制被错误中断。
> 因此 `check_live` 在网络异常时返回 `LiveStatus.UNKNOWN`，由调度层决定「保持现状」。
>
> 但 `fetch_stream` 相反：它**必须**在一开始就把「房间已下播」判定出来并抛出
> `RoomUnavailableError`。这里多打一次请求、少走一次兜底，换来的是「绝不录错房间」。

#### 为什么快手的流地址解析用「递归收集」？

不同版本的快手接口中，流地址的嵌套层级不一致
（有时是 `data.liveStream.playUrls[0].adaptationSet.representation[].url`，
有时是 `data.liveStream.playUrls[0].url`）。
递归遍历整棵 JSON 树并收集所有形似播放地址的字符串，同时对每个节点记录
`height` / `bitrate` / `name` 等画质元信息向下传递，这样 schema 微调时无需改代码。
代价是略微牺牲了一点解析性能（可忽略）。

---

### ② 监控调度层 `core/`

**唯一职责**：决定「什么时间、对哪个房间、做什么动作」。

#### `MonitorEngine`

| 方法 | 职责 |
|---|---|
| `start()` / `stop()` | 启停调度线程；`stop()` 会同时停止所有录制任务 |
| `add_room()` / `remove_room()` / `set_room_enabled()` | 监控对象的增删与启停（**非阻塞**，解析在轮询时异步完成） |
| `snapshot()` | 返回全部对象的状态快照（供界面表格每秒刷新） |
| `counts()` | 返回概览统计（对象数、录制中数量、磁盘剩余） |
| `_tick()` | 一轮「并发检测 → 应用结果」的完整流程 |
| `_check_disk_guard()` | 全局磁盘保护：不足时暂停全部录制，恢复后自动继续 |
| `_start_recording()` | 创建并启动 `StreamRecorder` |

#### `RoomRuntime`  运行时状态

一个监控对象的全部可变状态：

```
entry（持久化配置） · status（状态机） · nickname · title · online_count
last_check · last_error · recorder · recording_since · last_final_path
```

**状态机**：

```
                    ┌──────────┐
     添加对象 ─────► │ pending  │  待解析
                    └────┬─────┘
              解析成功    │
          ┌──────────────┴──────────────┐
          ▼                             ▼
    ┌──────────┐   检测到开播      ┌──────────┐
    │ offline  │ ────────────────► │ recording│◄────┐
    └──────────┘                   └────┬─────┘     │
          ▲                             │           │ 拉流中断
          │ 检测到下播          ┌───────┴───────┐   │
          │                     ▼               ▼   │
          │              ┌──────────┐   ┌────────────┐
          └──────────────┤ merging  │   │reconnecting│
                         └────┬─────┘   └────────────┘
                              │  合并完成
                              ▼
                         offline

   任意状态 ──磁盘不足──► paused ──空间恢复──► 回到正常轮询
   任意状态 ──解析/录制异常──► error
   用户停用 ──────────────► disabled
```

#### `EventBus`

极简的线程安全「发布-订阅」通道。业务线程 `publish`，界面层 `subscribe`。
回调在**发布者线程**中同步执行，因此界面层的桥接对象必须只用 Qt 信号转发，
不能在回调里直接操作控件。

事件类型：

| 事件 | 触发时机 |
|---|---|
| `MONITOR_STARTED` / `MONITOR_STOPPED` | 监控启停 |
| `ROOM_ADDED` / `ROOM_REMOVED` / `ROOM_RESOLVED` / `ROOM_UPDATED` | 监控对象变化 |
| `RECORDING_STARTED` / `RECORDING_FINISHED` | 录制开始 / 结束（含成品路径） |
| `QUALITY_FALLBACK` | 画质降级 |
| `DISK_PAUSED` / `DISK_RESUMED` | 磁盘保护触发 / 恢复 |
| `ALERT` | 需要弹窗提醒用户的告警 |

---

### ③ 录制层 `recorder/`

**唯一职责**：把一个流地址变成磁盘上完整的、可播放的文件集合。

#### `StreamRecorder`（核心）

输入：`RoomEntry` + `stream_provider`（无参可调用对象）+ `AppConfig` + 事件回调。

> **为什么流地址是「回调」而不是「值」？**
> 平台的播放地址普遍带时效（通常几分钟到几十分钟）。
> 如果只在开始时解析一次，长直播在重连时就会用到过期地址。
> 因此**每个分片开始前都重新调用 `stream_provider()`**，保证地址始终新鲜。

**录制主循环**：

```
while 未停止:
    ├─ 磁盘检查：不足 → 上报 DISK_LOW 并退出
    ├─ 取流：stream_provider()  →  失败则等重连间隔后重试
    └─ 录制一个分片 _record_one_segment()：
          启动 ffmpeg（-c copy 直存）
          监控循环（每秒一次）：
              ├─ 进程已退出           → NORMAL（通常为主播下播）
              ├─ 收到停止信号         → 优雅写 'q' 退出 → STOPPED
              ├─ 文件 ≥ 4GB 或超时长  → 优雅退出 → ROTATE（立即开新分片）
              ├─ 文件长时间无增长     → 强制终止 → STALL（等重连间隔后重试）
              └─ 磁盘不足             → 优雅退出 → DISK_LOW
    根据退出原因决定：继续 / 换分片 / 等待重连 / 退出循环
```

**分片收尾 `_finalize()`**：

```
收集所有有效分片
    ├─ 无分片 → 记录警告，结束
    └─ 有分片 → merge_segments() 无损合并
                 ├─ 成功且分片数 > 1 → 清理原始分片
                 └─ 开启转封装 → remux_to_mp4()
    → 上报 EVT_STOPPED（含成品路径）
```

#### 关键实现细节

**优雅退出**：停止 ffmpeg 时向它的 `stdin` 写入 `q`，让 ffmpeg 正常写完文件尾部再退出。
这保证 FLV 文件的索引完整、可被后续 concat 合并。
若 10 秒内未响应，再降级为 `terminate()` / `kill()`。

**stderr 排空**：ffmpeg 的 `stderr` 由独立线程持续读取。
若不排空，管道缓冲区（Windows 约 64KB）写满后会**阻塞 ffmpeg 进程**，
表现为「录制突然停止但进程还在」。线程同时保留最后 60 行用于失败诊断。

**启动即失败的判定**：进程返回码非 0、存活时间短、且输出文件无数据 → 判定为参数/环境错误，
而非「主播下播」。此时若错误信息提示参数不被识别，会自动去掉网络重连参数重试一次
（兼容部分老版本 ffmpeg）。

**兼容性兜底**：部分平台的 CDN 或老版本 ffmpeg 不接受 `-rw_timeout` / `-reconnect` 参数。
首次遇到「参数不被识别」类错误时，置位 `_omit_network_options`，后续改用精简参数。

#### `merger.py` 三级无损合并

| 级别 | 方式 | 适用场景 |
|---|---|---|
| 1 | `-f concat -c copy` | 常规情况，速度最快 |
| 2 | 逐段 `-avoid_negative_ts make_zero` 归一化后再 concat | 某分片起始时间戳未归零，导致合并时长异常 |
| 3 | TS 容器二进制拼接 | ffmpeg 合并失败时的绝对无损兜底 |

**时长校验**：第 1 级合并后会用 ffprobe 比对成品时长与各分片时长总和，
偏差超过 25% 即认为时间戳异常，自动升级到第 2 级重试。
这是实测中发现的真实问题（分片容器时长元数据残留会导致合并时长翻倍）的针对性修复。

#### `storage.py`

- `sanitize_name()` —— 清洗 Windows 非法字符、去除首尾点与空格、限制长度
- `build_output_dir()` —— 生成「平台 / 主播昵称 / 日期」三级目录
- `build_segment_path()` / `build_merged_path()` —— 严格按命名规则生成文件路径
- `get_free_space_gb()` —— 磁盘剩余空间检测（路径不存在时向上回溯到最近存在的父目录）

#### `ffmpeg_utils.py`

- `resolve_ffmpeg()` —— 三级查找：配置路径 → 项目 `bin/` 目录 → 系统 PATH
- `run_ffmpeg()` —— 同步执行封装（隐藏控制台窗口、超时、编码统一处理）
- `probe_duration()` —— 由 ffmpeg 路径推导 ffprobe 路径并探测媒体时长
- `parse_ffmpeg_error()` —— 从输出中提炼人类可读的错误摘要

---

### ④ 界面层 `ui/`

**唯一职责**：呈现状态、收集操作。不含任何业务逻辑。

#### 线程安全的核心手段：Qt 信号桥接

```
业务线程（调度线程 / 录制线程）
    │  bus.publish(...)  /  LOG_BUFFER.append(...)
    ▼
_LogBridge / _UiBridge（QObject，主线程创建）
    │  emit(record)      ← 跨线程发出信号
    ▼
Qt 事件队列（AutoConnection 自动使用 QueuedConnection）
    │
    ▼
主线程槽函数 → 操作控件
```

因此**所有控件操作都发生在主线程**，无需额外加锁。

#### 状态刷新策略

- **表格**：`QTimer` 每秒调用 `engine.snapshot()` 拉取快照。
  先比对 key 列表，结构未变则**逐格原地更新**（避免重建导致的闪烁与选中丢失），
  仅在增删对象时重建表格并还原选中项。
- **状态栏**：同一周期刷新对象数、录制中数量、磁盘剩余空间。
- **日志**：事件驱动，实时追加（`QPlainTextEdit` 设置 `maximumBlockCount` 限制内存）。

#### 各组件

| 组件 | 职责 |
|---|---|
| `MainWindow` | 整体布局、工具栏、定时刷新、右键菜单、退出收尾 |
| `LogPanel` | 日志显示、级别过滤（默认 INFO，可切 DEBUG）、导出 |
| `AddRoomDialog` | 批量输入（每行一条）、平台选择、从文本文件导入、批内去重 |
| `SettingsDialog` | 五个选项卡（监控 / 录制 / 异常处理 / 账号 / 环境），含 ffmpeg 可用性检测与扫码登录入口 |
| `LoginDialog` | 扫码登录：内嵌显示二维码、显示进度、成功后返回 Cookie |

#### 扫码登录（`LoginDialog` + `platforms/qr_login.py`）

快手对**未登录访客**关闭了按房间查询的接口，所以「登录」是一个必须让用户能顺利完成的
功能。这里踩过一个坑：`live.kuaishou.com` 首页**不会自动弹出二维码**，
必须先点右上角的「登录」才会出现扫码弹窗——所以流程里必须显式包含「点出弹窗」这一步。

```
LoginDialog（主线程）
    │ start()
    ▼
LoginWorker（QThread，独占一个 Playwright 会话）
    │  QrLoginSession.open()
    │      goto 首页 → 点击「登录」入口 → 在弹窗里定位二维码
    │  emit qr_ready(bytes) ─────────────► 主线程 QLabel 直接画出二维码
    │  每 0.4s 查一次 is_logged_in()
    │  每 2.0s 重新读二维码，变了就再 emit（二维码过期自动换新）
    │  emit succeeded(cookie) ──────────► 主线程回填 Cookie 并存盘
    ▼
session.close()  ← 对话框关闭时 cancel() + wait() 保证浏览器一定被关掉
```

三个设计要点：

1. **Playwright 同步接口必须单线程使用**，所以整个会话（启动、读二维码、查登录态、关闭）
   都放在 `LoginWorker.run()` 里；主线程只通过信号拿数据和改界面，不触碰会话对象。
2. **二维码在程序内和浏览器里是同一张**（同一次会话），扫哪张都能登录；
   程序内的那张是「从页面 DOM 里把图片抠出来」显示的，因此**不受浏览器窗口是否被遮挡影响**。
   定位策略：优先 `data:image` 的 `<img>`，其次未被 CSS 模糊的 `<canvas>`，
   且只接受边长 100~420px、接近正方形的元素——避免把直播画面误认成二维码。
3. **登录态判定只用「登录后才会出现的 Cookie」**（快手 `passToken` / `userId` /
   `kuaishou.live.web_st`，抖音 `sessionid` / `sid_tt` / `passport_csrf_token`），
   这些名字在未登录时一定不存在，因此不会误判。

---

## 三、线程模型

| 线程 | 数量 | 职责 | 生命周期 |
|---|---|---|---|
| **主线程（Qt）** | 1 | 界面渲染、用户交互、配置读写 | 程序全程 |
| **调度线程** | 1 | `MonitorEngine._schedule_loop`：周期轮询、状态决策 | `start()` → `stop()` |
| **检测线程池** | ≤ `check_concurrency`（默认 6） | 并发执行各房间的网络检测 | 每轮 `_tick()` 内临时创建 |
| **录制线程** | 每个在录房间 1 条 | `StreamRecorder._run`：拉流、监控、分片、收尾 | 开播 → 下播（含合并） |
| **ffmpeg 子进程** | 每个在录房间 1 个 | 实际拉流与写盘 | 每个分片一个进程 |
| **stderr 排空线程** | 每个 ffmpeg 1 条 | 持续读取并保留最后若干行输出 | 随 ffmpeg 结束 |
| **登录线程** | ≤ 1（按需创建） | `LoginWorker`：扫码登录会话 | 打开登录对话框 → 登录成功或取消 |
| **HTTP 服务（仅冒烟测试）** | 1 | 本地模拟直播流 | 测试期间 |

### 并发安全的约定

1. `MonitorEngine` 内部所有对 `_runtimes` 的读写都在 `self._lock`（`RLock`）保护下进行。
2. **检测在网络线程池中并行执行，但结果的应用（决策 + 状态变更）统一回到调度线程串行处理**，
   避免并发修改运行时状态。
3. `StreamRecorder` 的统计信息通过 `_lock` 保护，`stats()` 返回不可变快照。
4. **结果配对使用对象本身而非 key**：首次检测会把分享链接解析为 room_id，
   此时 `runtime.key` 会变化，若用 key 配对会导致结果错配（这是一个已修复的隐蔽 bug）。

---

## 四、完整数据流转示例

以「某个抖音直播间从开播到录制完成」为例：

```
① 用户操作
   MainWindow._add_rooms()
     → AddRoomDialog 收集 RoomEntry(platform="douyin", raw_input="https://v.douyin.com/xxx")
     → MonitorEngine.add_room(entry)
         · 校验上限与去重
         · 创建 RoomRuntime（status = "pending"）
         · 写入 ConfigStore 并落盘 config.json
         · bus.publish(ROOM_ADDED)
         · _wake_event.set()  ← 唤醒调度线程，无需等下一个轮询周期

② 调度线程 _tick()
     ├─ _check_disk_guard()   磁盘正常
     ├─ 收集 enabled 的 RoomRuntime
     └─ 线程池并发执行 _check_one(runtime)
          ├─ detect_platform()            → "douyin"
          ├─ DouyinParser.resolve_room()  → 短链重定向 → "123456789"
          │    · 迁移 runtime 到新的 key
          │    · store.save()，publish(ROOM_RESOLVED)
          └─ DouyinParser.check_live("123456789")
               · _ensure_cookies()  → 访问首页拿 ttwid
               · GET /webcast/room/web/enter/
               → RoomInfo(status=LIVE, nickname="某某主播")

③ 应用结果 _apply_check_result()
     ├─ 回填 nickname → store.save() + publish(ROOM_UPDATED)
     └─ 状态为 LIVE 且未在录制 → _start_recording(runtime)
          ├─ 检查并发录制上限
          ├─ 构造 stream_provider（闭包，持有 room_id 与 config）
          └─ StreamRecorder(...).start()   ← 启动录制线程
               bus.publish(RECORDING_STARTED)

④ 录制线程
     _prepare_output_dir()  → <保存路径>/抖音/某某主播/2026-09-23/
     while 未停止:
        ├─ stream_provider()
        │    → registry.resolve_stream(parser, room_id, config)
        │        · fetch_stream(ORIGIN)  → flv_pull_url["FULL_HD1"]
        │        · 失败则降级 → publish(QUALITY_FALLBACK)
        │        · 再失败则 Playwright 兜底
        ├─ build_segment_path(..._001.flv)
        ├─ Popen(ffmpeg -c copy -f flv ...)  ← 直存原始流，不转码
        └─ 监控循环：尺寸 / 时长 / 卡顿 / 磁盘 / 停止信号

        （若中途卡顿 → STALL → 等待 15s → 序号 +1，写入 _002.flv）

⑤ 下播
     下一轮 _tick() 检测到 OFFLINE
       → recorder.stop("live_ended")
            ├─ 向 ffmpeg stdin 写 'q' 优雅退出
            ├─ _finalize()
            │    ├─ merge_segments([_001, _002], ..._merged.flv)
            │    │     · concat 合并 → ffprobe 时长校验 → 异常则归一化重试
            │    ├─ 清理原始分片 _001 / _002
            │    └─ 可选 remux_to_mp4()
            └─ publish(RECORDING_FINISHED, {file: "..._merged.flv"})

⑥ 界面响应
     每秒 QTimer → engine.snapshot() → 表格原地更新
     EventBus 事件 → _UiBridge 信号 → 主线程槽函数 → 状态栏提示 / 弹窗
     日志缓冲 → _LogBridge 信号 → 主线程 → 日志窗口追加一行
```

---

## 五、配置持久化

`config/settings.py` 中的 `ConfigStore` 负责 `config.json` 的读写：

```json
{
  "version": 1,
  "settings": { "poll_interval": 60, "save_path": "...", ... },
  "rooms": [
    { "platform": "douyin", "raw_input": "...", "room_id": "...", "nickname": "...", "enabled": true }
  ]
}
```

- **原子写入**：先写 `config.json.tmp`，再用 `os.replace()` 覆盖，避免断电产生半截 JSON
- **容错加载**：文件损坏 / 字段缺失时回退默认值，保证程序总能启动
- **配置归一化**：`AppConfig.normalize()` 把所有越界值收敛到合理区间（统一兜底）
- **路径可覆盖**：环境变量 `LIVE_RECORDER_CONFIG` 可指定其他配置路径，便于多实例/便携部署
- **重启恢复**：`auto_start_monitor` 记录退出时的监控状态，下次启动自动恢复监控任务

---

## 六、扩展指南

### 新增一个平台（例如 B 站）

1. 在 `platforms/` 下新建 `bilibili.py`，继承 `BaseLiveParser`：

```python
class BilibiliParser(BaseLiveParser):
    platform = "bilibili"
    display_name = "哔哩哔哩"

    def resolve_room(self, raw_input: str) -> str: ...
    def check_live(self, room_id: str) -> RoomInfo: ...
    def fetch_stream(self, room_id: str, quality: StreamQuality) -> StreamInfo: ...
```

2. 在 `platforms/registry.py` 中注册：

```python
_PARSER_CLASSES = {
    "kuaishou": KuaishouParser,
    "douyin": DouyinParser,
    "bilibili": BilibiliParser,          # ← 新增
}
PLATFORM_LABELS = {
    "kuaishou": "快手",
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",               # ← 新增
}
_HOST_HINTS = {
    ...
    "bilibili": ("bilibili.com", "b23.tv"),   # ← 新增
}
```

3. 完成。调度层、录制层、界面层**无需任何改动**，平台会自动出现在下拉框中。

### 调整录制策略

| 需求 | 修改位置 |
|---|---|
| 改变 ffmpeg 拉流参数 | `recorder/ffmpeg_recorder.py` → `_build_ffmpeg_command()` |
| 改变分片/重连触发条件 | `recorder/ffmpeg_recorder.py` → `_monitor_process()` |
| 改变合并策略 | `recorder/merger.py` → `merge_segments()` |
| 改变文件命名规则 | `recorder/storage.py` → `build_segment_path()` |
| 新增配置项 | `config/settings.py` → `AppConfig`，并在 `ui/settings_dialog.py` 添加控件 |

### 平台接口变更的排查流程

0. **先跑 `python e2e_check.py --quick`** —— 它会明确指出是「解析 / 开播状态 / 取流」
   哪一环出的问题，避免盲改代码。注意区分几个易混淆的现象：
   - `enter` 返回 200 + 空体 → 缺首访 Cookie（**不是**风控，也不是接口变更）
   - 固定返回 `status_code=4001038` → 房间号与 `web_rid` 混用
   - 返回验证页 / 403 / 444 → 真正的风控，应降低轮询频率而非改代码
   - 快手 `livedetail` 对**任意**房间号都返回同一份约 700 字节响应 → 接口已失效
     （注意：**在播房间也一样**，别误判为「未开播」；用「乱码 ID 返回同样内容」来确认）
1. 浏览器打开直播间，`F12` → Network，筛选接口关键字
   （抖音找 `enter`，快手找 `reco`）
2. 对比请求 URL / 参数 / 响应结构与代码中的常量
3. 优先只需修改常量（`WEB_ENTER_API` / `REFLOW_API` / `RECO_API` / `DETAIL_API`）
4. 若字段层级变化：
   - 抖音：调整 `dig()` 的路径参数
   - 快手：通常无需修改（递归收集策略对层级变化不敏感）
5. 若出现新增的签名校验：优先确认 `playwright_helper.py` 的兜底是否生效
6. 改完务必重跑 `e2e_check.py`，并确认「下播房间取流直接判定不可用」仍然通过
   （防「录错房间」回归）

> **查快手接口时的省事入口**：`POST /live_api/liveroom/reco`
> （body `{"source": 2, "page": 1, "pageSize": 20}`）会返回一批在播房间及其
> 完整流地址，未登录即可用。它同时是生产环境的首选数据源和调试样本来源。

> **查抖音接口时的省事入口**：`https://live.douyin.com/webcast/feed/`
> 返回一批在播房间，含 `share_url` 与各自 `flv_pull_url`，无需登录。

> **逆向前端资源**：快手的接口映射表嵌在前端 bundle 里
> （`p2-game.kskwai.com/udata/pkg/KS-GAME-WEB/pc-live-next/js/app.*.js`），
> 里面把 `/live_api/*` 映射到真实的 `/rest/...` 路径，排查接口别名时很有用。

---

## 七、测试策略

`smoke_test.py` 提供**完全离线**的端到端验证，不访问任何第三方平台：

| 测试 | 验证内容 |
|---|---|
| 1. ffmpeg 可用性 | 定位与版本调用 |
| 2. 多分片无损合并 | segment 复用器切片 → concat 合并 → 时长比对 |
| 3. MP4 转封装 | `-c copy` 转封装后时长一致性 |
| 4. 录制器生命周期 | 目录结构、命名规则、事件序列、产出非空 |
| 5. 自动分卷与合并 | **限速 HTTP 服务模拟实时直播流** → 强制分卷 → 合并完整性 → 分片清理 |

第 5 项的设计要点：本地静态服务器会瞬间传完整个文件，导致体积阈值永远来不及触发。
因此在测试中给 HTTP 服务加了**限速**（45 KB/s），让数据按接近真实直播的速率到达，
从而真实复现「录制过程中触发分卷」的场景。

运行：

```bash
python smoke_test.py           # 跑完自动清理临时文件
python smoke_test.py --keep    # 保留产物以便人工检查
```

#### 真实链路自检 `e2e_check.py`

`smoke_test.py` 只能证明「录制链路本身是好的」，无法回答「是不是平台接口变了」。
`e2e_check.py` 专门补上这一段：它**真实访问平台**，并与上面的离线测试形成互补。

| 检查项 | 验证内容 |
|---|---|
| A1 样本获取 | 从抖音 `webcast/feed/` 取候选，并**逐个用程序自身逻辑复核**在播状态 |
| A2 房间标识换算 | 分享链接（19 位 `room_id`）→ `web_rid` 自动换算 |
| A3 开播状态 | `check_live` 判定为「直播中」且解析出昵称 |
| A4 取流 | 取得可拉流地址（原画 FLV 优先） |
| A5 真实录制 | 用真实 `StreamRecorder` 录制 → 产出非空文件 → **完整解码**测量时长 |
| A6 目录与命名 | 「平台/主播昵称/日期」三级 + 文件名规则 |
| A7 转封装 | 成品无损转封装为 MP4 |
| A8 安全闸门 | **合成响应**验证：`status=4` + 空流地址时必须抛 `RoomUnavailableError`（不得回落为普通 `PlatformError` 而触发浏览器兜底） |
| A9 分卷与合并 | 调小分片阈值强制分卷 → 多分片无损合并 → 时长完整 |
| B1 分享链解析 | 快手两条分享链必须解析出**主播** ID（不得取到分享者 `efid`） |
| B2 推荐流索引 | 推荐流可用（当前唯一可批量取流入口），至少命中一个在播房间 |
| B3 状态与取流 | 索引命中的房间判定为「直播中」并取得原画 FLV |
| B4 真实录制 | 用真实 `StreamRecorder` 录制快手流 → 产出非空文件 → 完整解码 |
| C1 登录流程一致性 | 命令行（`login_helper`）与界面（`qr_login`）共用同一套平台定义 |
| C2 二维码可取到 | 打开登录页后**自动点出登录弹窗**并抓到 PNG 二维码（无头验证，不需人工扫码） |
| C3 二维码可复读 | 同一会话可反复读取二维码（界面轮询刷新依赖此能力） |
| C4 不误判登录 | 未扫码时 `is_logged_in()` 必须为假 |
| C5 会话可关闭 | `close()` 可安全重复调用（对话框可能被反复开关） |
| D1~D5 界面入口 | 账号页有扫码按钮 / 保留手动粘贴 / 未登录提示 / 状态随输入更新 / 主工具栏有「扫码登录」 |

三个设计要点：

1. **样本必须复核**：抖音 `feed` 与快手 `reco` 都是推荐流，条目会滞后
   （刚下播的房间仍标 `status=2`／`isLiving=true`）。脚本因此收集**一批**候选，
   每个都用 `check_live` 复核，并在正式使用前再复核一次——直播场次长短不一，
   几秒内下播是常态。
2. **A8 用合成响应而非真实房间**：真实房间的下播状态随时会变，无法作为稳定断言；
   而这条路径的判据只取决于「`room` 节点存在但 `status != 2`」这一响应形态，
   因此直接构造该响应做确定性回归，是这个 bug 唯一可靠的守护方式。
3. **C 段不需要人工扫码**：它只验证「登录入口 → 二维码图片」这一段。
   登录态判定本身依赖真实账号，不可能自动化，因此用「已知的登录后才有」的
   Cookie 名做反向断言（未扫码时必须为假）。

> **快手校验为什么基于推荐流而不是指定房间？** 因为未登录时按房间查询的接口
> 不可用（见「已知限制 8」）。用推荐流里的房间验证，能覆盖「取流 → 录制 → 落地」
> 这条完整链路；登录后同样的代码路径会作用于你指定的任意房间。

> **偶发失败怎么判断？** 平台是真实环境，主播随时可能下播。若 A5/B4 报出
> 「录制产出有效 FLV 文件」失败，脚本会同时打印录制事件序列与最近日志：
> 出现 `stream_ended` / ffmpeg 404 之类的记录即为**平台侧下播**（重跑即可），
> 与代码缺陷无关。注意 `best_duration()` 已对空路径做保护，不会再因
> 「没有产出文件」而抛异常、把后续检查项一起吞掉。

运行时长为「录制秒数 × 2 + 若干秒网络耗时」，可用 `--quick` 跳过录制部分：

```bash
python e2e_check.py              # 完整自检
python e2e_check.py --quick      # 只验证解析 / 状态 / 取流
python e2e_check.py --keep       # 保留录制产物
python e2e_check.py --only login # 只验证扫码登录取码（无头，约 10 秒）
python e2e_check.py --only ui    # 只验证界面扫码入口（约 3 秒）
```

---

## 八、已知限制

1. **平台接口依赖**：快手 / 抖音的接口无官方文档且可能变更，需要不定期维护解析层。
2. **抖音签名**：网页接口的动态签名算法会更新，纯 HTTP 方案可能间歇性失败，
   依赖 Playwright 兜底（会增加资源占用与启动延迟）。
3. **DRM 加密流**：部分付费/加密直播流无法录制，`-c copy` 会得到加密数据。
4. **不转码**：本工具刻意不做转码以保证画质与性能，因此输出格式取决于平台提供的流。
5. **平台风控**：监控对象过多或轮询过密仍可能触发风控，请合理配置参数。
6. **macOS 兼容性**：代码已按跨平台方式编写（路径使用 `pathlib`、进程调用分支处理、
   隐藏窗口标志按平台判断），但尚未在 macOS 上实测。
7. **FLV 时长元数据**：直播流的 FLV 通常不写 `duration` 字段，部分播放器会把时长
   显示为 0。这不是文件损坏；程序内部用「完整解码」测量时长。开启「转封装为 MP4」
   可获得带完整时长索引的文件（仍是 `-c copy` 无损）。
8. **快手必须登录才能监控指定主播**：快手对未登录访客关闭了按房间查询的接口，
   并以「请求过快」拦截自动化访问——实测**无头 Chromium、有头 Chromium、有头
   真实 Chrome 三者都会被拦截**，说明这不是指纹问题。未登录时只能用推荐流索引，
   覆盖范围有限（可通过增大页数提升）。解决方式：点工具栏「扫码登录」、
   或走「设置 → 账号 → 扫码登录快手」、或双击 `login.bat`，登录一次即可。
9. **快手推荐流索引的时效性**：索引 TTL 为 45 秒（`RECO_CACHE_TTL`）。
   若某房间刚开播且尚未进入推荐流，本轮检测会返回「未知」而不是「未开播」——
   这是刻意的保守策略，避免把「没查到」误报成「没开播」。
10. **扫码登录需要人工介入**：二维码本质上是让用户用手机完成一次身份确认，无法自动化。
    因此这一环必然是「弹出二维码 → 用户扫码 → 轮询登录态」。程序能做的只有把二维码
    **一定显示出来**、自动刷新、并在成功后自动保存凭证。

---

## 九、合规声明

本项目仅供个人学习、技术研究与离线备份使用。
严禁商业用途，严禁对录制内容进行二次分发或公开传播。
请遵守各平台服务条款及相关法律法规，尊重内容创作者的合法权益。
