# 视频下载工具箱（Douzy）

四合一视频下载 / 直播录制工具集，合并自四个既有项目：抖音高清下载器、快手主页下载工具集、clawfetch 直链下载器、LiveRecorder 直播录制器。主界面统一入口，各模块后台常驻。

## 功能一览

| 模块 | 功能 |
|---|---|
| `modules/douyin` | 抖音视频/图集/音乐/合集/直播回放下载；批量并发 16 路、失败自动补下、风控刹车；WebUI 浏览器界面 + 移动端页面 |
| `modules/kuaishou` | 快手主页作品批量下载（粘分享链接）；扫码登录；风控守卫（限流刹车、冷却退避、批量独占） |
| `modules/bilibili` | 哔哩哔哩下载（扫码获取登录 Cookie） |
| `modules/clawfetch` | 任意 http/https 直链文件下载 |
| `modules/live_recorder` | 快手/抖音/Stripchat 直播自动录制（开播即录、断流重连、自动转 MP4） |
| `modules/ytdlp` / `modules/ty` / `modules/stripchat` | yt-dlp 封装下载器、TikTok 辅助、Stripchat 房间录制（18+，需系统代理） |
| WebUI | 抖音下载 WebUI（任务/收藏/关注/去重/设置）、工具箱主界面（桥接服务 127.0.0.1:8518） |

## 安装方法

**在原开发机上**：开箱即用——双击 `启动工具箱.bat` 即可，自带运行时（`runtime\` 内三个 venv），无需 pip install。

**在新机器上从 GitHub 克隆时**：⚠️ 本仓库**只含源码与文档**，以下内容按 `.gitignore` 不入库，需要自行准备：

| 未入库内容 | 说明 | 如何准备 |
|---|---|---|
| `runtime/` | 三个模块的 Python venv + 快手登录态 | 参考 `docs/统一依赖清单.md`，用 Python 3.10+ 重建 venv 并安装依赖 |
| `data/` | 数据库、台账、作品清单 | 程序首次运行自动创建 |
| `downloads/` | 下载产物 | 运行时自动生成 |
| `.data/` | 内置浏览器 profile | 运行时自动生成 |
| `modules/live_recorder/config.json` | 录制器配置（含平台 Cookie） | 在「直播录制器 → 设置」里重新配置 |
| `modules/bilibili/.cookies.json` | B 站登录 Cookie | 「扫码获取」重新写入 |
| `modules/douyin/config.yml` | 抖音下载配置 | 复制 `modules/douyin/config.example.yml` 改名后按需修改 |

系统级依赖：**ffmpeg**（直播录制合并/转 MP4 用）、Chrome/Edge（浏览器回退与扫码登录用）。

环境自检：`python tests\selftest.py`（130 项）。

## 运行方式

**唯一入口**：双击 `启动工具箱.bat` —— 直接打开工具箱主界面（原生窗口），自动拉起桥接服务 `127.0.0.1:8518`，全部模块在界面里点按钮统一管理，不弹终端窗口。

高级用法：

```bat
启动工具箱.bat bridge        REM 前台桥接调试
python tests\selftest.py     REM 环境自检
python tools\bench_batch_path.py --help   REM 抖音批量基准测试
```

各模块也可独立命令行运行（见 `docs/使用说明.md`）：
- 抖音：`python modules\douyin\cli\main.py <链接>`；WebUI：`modules\douyin\webui` 下的服务入口
- 快手：`modules\kuaishou\tools\kuaishou_onestop.py`（图形/无头两套）
- 直链：`modules\clawfetch\clawfetch.py <url>`
- 直播录制：主界面 → 直播录制器；守护进程 `modules\live_recorder\daemon.py`

> ⚠️ 所有 `启动*.bat` / `启动*.vbs` 都是 `tools\gen_launchers.py` **生成的**，勿手改（改模板后重跑生成器）。

## 下载产物位置

- 直播录像：`downloads\live_recorder\<平台>\<昵称>\<日期>\`（可在设置中改目录）
- 抖音作品：`modules\douyin\config.yml` 的 `path` 指定（默认 `E:\lp\dysp`，新机器请改）

## 注意事项

1. **凭证安全**：以下文件存有平台登录 Cookie（等同账号凭证），已被 `.gitignore` 过滤、绝不能入库——`modules/live_recorder/config.json`（含快手 Cookie）、`modules/bilibili/.cookies.json`、`.data/`（浏览器 profile）、`modules/douyin/webui/.data/`（账号缓存）及其各 `.bak` 备份。克隆/提交前请勿自行从 ignore 中移除。
2. **快手风控**：批量下载偶发限流属正常。守卫会主动刹车（30 秒窗口失败满 10 条作品即全体冷却，指数退避封顶 5 分钟）并提示何时再试；多开并行、多账号轮流跑**不会**更快，反而加重风控。
3. **抖音限流**：源站会短时封禁客户端后自行恢复；程序内置「碰撞即刹车 + 轮末自动补下」，无需人工介入。
4. **B 站**：部分视频「重复/没声音」是中间流未删导致，处理方式见 README 历史版本与 `docs/使用说明.md`。
5. **画质杠杆**：原画档体积约是 1080p 的 6.5 倍且 CDN 更慢，按需选择。
6. **合规**：下载与录制内容的用途、版权及平台条款合规由使用者自行负责；工具不绕过任何平台权限，不承诺对抗平台后续接口/反爬策略变更。直播录制仅供个人学习与离线备份，禁止商业用途或内容二次分发。

## 文档导航（docs/）

| 文档 | 内容 |
|---|---|
| [GitHub 上传操作文档](docs/GitHub上传操作文档.md) | 本次上传全流程：安全扫描、过滤、提交、授权、推送 |
| [项目结构说明](docs/项目结构说明.md) | 目录树、模块组织原则、依赖去重、重名冲突处理 |
| [功能来源对照表](docs/功能来源对照表.md) | 每个功能来自哪个原项目、依赖什么、入口在哪 |
| [使用说明](docs/使用说明.md) | 各功能按钮/命令行用法、常见问题 |
| [统一依赖清单](docs/统一依赖清单.md) | 三个 venv 的完整依赖 + 系统级共享组件 |
| [验证台账](docs/验证台账.md) | 自检 130 项 + 逐模块真实任务验证记录 |

## 目录一览

```
启动工具箱.bat        ← 唯一入口（双击直达主界面）
modules\              ← 代码（douyin / kuaishou / bilibili / clawfetch / live_recorder / ytdlp / stripchat / ty）
runtime\              ← 自带运行时（不入库）
data\                 ← 台账/数据库/作品清单（不入库）
downloads\            ← 下载产物（不入库）
docs\                 ← 文档
tools\                ← 生成器（启动脚本 / 设计令牌）
tests\                ← 环境自检与守卫自检
```

## 来源项目

| 模块 | 原项目 |
|---|---|
| modules/douyin | 抖音高清下载器 douyin-downloader v2.0.0 |
| modules/kuaishou | 快手主页测试下载工具集 |
| modules/clawfetch | clawfetch v1.4.2 直链下载器 |
| modules/live_recorder | LiveRecorder v1.0.3 直播流自动录制工具 |

原项目均未改动。
