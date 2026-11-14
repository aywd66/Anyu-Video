# Douzy Web UI · 抖音下载器网页版

> ## ★ 桥接模式（已集成到工程）
>
> 本目录已集成到工程根目录（E:\录屏\dysp\dyxz\webui\）。
> 双击工程根的 `启动工具箱.bat` 即自动拉起桥接并打开主界面；
> 前台调试用 `启动工具箱.bat bridge`（或用工程 venv 运行
> `python modules/douyin/webui/bridge_server.py`；2026-11-07 起旧的 `启动WebUI.bat` 已删除），
> 浏览器打开 <http://127.0.0.1:8518> 即可使用**真实数据**：
>
> - **关注**：真实作者库（抖音来自数据库 aweme 表 / 快手来自 download_log.json 台账），按作品数排序。
>   只收录**主页来源**的作者 —— 单作品链接下到的作品照样进「作品档案」，但不会把作者
>   算成关注；这些人统一出现在列表下方的「查看 / 加入关注」里，由你决定要不要加。
>   这样「一键更新」与页头统计（作者 N 位 / 作品 M 条）都不会被单链接下载干扰。
> - **作品档案**：真实已下载作品（抖音 1019+ 条、快手 1140 条，真实封面与落盘路径，分页加载），
>   顶部标签可在**抖音 / 快手**两库之间切换
> - **任务中心**：真实下载历史（download_history 表）+ 实时任务进度 + 日志查看
> - **链接下载**：粘贴抖音链接提交真实下载（调用工程 CLI run.py，自动排队，并发 2）
> - **设置**：实时展示 config.yml（敏感字段自动脱敏，只读）
>
> 数据安全边界：查询一律以 **`mode=ro` 只读**打开；下载走工程自身 CLI，增量去重照常生效，
> 不会重复下载已有作品；不修改工程任何现有文件；配置中的 Cookie 等敏感信息不会返回给网页。
>
> **受控写通道共四处**（代码里搜 `db_connect_rw` 与 `_backup_ledger` 即可定位）：
>
> 1. **关注 → 删除作者信息（抖音）**：作者是通过 `aweme` 表按 `author_name` 聚合出来的，
>    所以让一位作者连同记录一起消失只能删他的作品记录。这条链路：
>    只按作者名精确删 `aweme`、**磁盘上的视频/图片文件一律不动**、其它表不碰、
>    删前自动整库备份到 `.tasks/db-backups/`（保留最近 2 份，可人工回滚）。
> 2. **关注 → 删除作者信息（快手）**：只从 `download_log.json` 台账里剔除该作者的记录行，
>    **磁盘文件一律不动**、别人的记录与无 photoId 的审计行原样保留；
>    删前把台账备份到 `.tasks/ks-backups/`（保留最近 5 份，可人工回滚）。
> 3. **关注 → 设置主页**（快手）：把作者主页链接记进 `data/kuaishou/author_profiles.json`，
>    只写这一份映射文件，不碰台账与媒体。
> 4. **关注 → 加入 / 取消关注**：只写 `.data/follow_state.json`（显式覆盖，优先于派生结果）。
>    **不删任何作品记录、不动磁盘**；取消关注后作品仍在「作品档案」里，随时可以加回来。
>
> 未启动桥接服务时（直接双击 index.html）自动回退为演示模式，之前保留的本地演示数据不受影响。

---

以 [douyin-downloader](E:\录屏\dysp\dyxz) 工程官方桌面端（Douzy）的界面截图为参考实现的**纯前端网页工作台**。
零依赖、零构建：不需要 Node、不需要 npm install，浏览器直接打开即可运行，内置一套可重置的演示数据。

---

## 1. 运行方式（任选其一）

### 方式 A：直接打开（最简单）

双击 `index.html`，在浏览器中打开即可（Chrome / Edge 均可）。
所有资源都是本地相对路径，不依赖网络。

### 方式 B：本地静态服务（推荐，体验更接近真实部署）

```bash
# 在本目录执行任意一条：
python -m http.server 8940
# 或
npx serve .
```

然后浏览器访问 <http://127.0.0.1:8940>。

### 环境要求

- 任何现代浏览器（Chrome / Edge / Firefox / Safari 最近两年版本）
- 无需安装任何依赖；Python 仅在方式 B 中用来自起静态服务

## 2. 页面与功能总览

| 页面 | 路由 | 对应参考截图 | 主要功能 |
|---|---|---|---|
| 抖音下载 | `#/douyin` | img/desktop/001.png | 粘贴链接 → 类型识别 → 选择模式/画质 → 发起下载 |
| TikTok | `#/tiktok` | img/desktop/002.png | 公开内容免登录下载（演示） |
| YouTube | `#/youtube` | img/desktop/003.png | 视频/MP3/字幕三种格式（演示） |
| 关注 | `#/creators` | img/desktop/004.png | 作者库：搜索、筛选、双视图、备注、下载；**一键更新全部作者最新作品**、**逐位更新**、**取消关注（档案保留）**、**删除作者信息**（清台账/库记录，磁盘文件保留）；列表下方「未加入关注」入口可把只下过单链接的作者加回来；顶部标签切换**抖音 / 快手** |
| 收藏与喜欢 | `#/favorites` | img/desktop/005.png | **当前登录账号**的作品/喜欢/收藏/关注（抖音四个 Tab 全量；快手只有作品 + 关注动态，平台限制）+「我的标记」本地收藏夹；顶部切换抖音/快手；支持扫码重登、手动同步、单条/批量下载未落地作品 |
| 多链排队 | `#/queue` | —（桌面端"多链排队"） | 批量粘贴链接、解析去重、一键入队 |
| 任务中心 | `#/tasks` | img/desktop/006.png | 统计条、状态筛选、搜索、取消/重试/移除 |
| 作品档案 | `#/archive` | —（桌面端"作品档案"） | 本地作品库：搜索、类型筛选、收藏、导出 TSV 清单；**点封面直接播放**（本地视频）或查看图集；顶部标签切换**抖音 / 快手** |
| 设置 | `#/settings` | — | 对应工程 config.yml 的可视化表单 + 数据台账 |

另有全局能力：网络诊断弹窗、数据台账弹窗（侧边栏底部）、移动端响应式（≤900px 收纳为顶栏）。

### 作品档案里的「点封面播放」

侧边栏「记录」组里的**任务记录**页就是作品档案。桥接服务按
`aweme_id` 在下载目录里扫出真实文件（不依赖库里那份对不上的 `file_path`）：

- 封面中央有播放角标 = **本地确实有文件**，点封面（或卡片上的播放按钮）即弹出播放器；
  视频用 `<video controls>` 直接播，图文作品可翻页看大图，弹窗底部可「在文件夹中打开」（资源管理器定位）。
- 没有角标的卡片说明文件已不在本地（被删/被移走），点它只会如实提示，不会假装能播。
- 视频流走 `GET /api/bridge/media?id=<aweme_id>`（支持 Range，所以**进度条可以拖**）。
  该接口只接受 `aweme_id`、不接受路径，无法用来读本机任意文件。

### 账号中心（收藏与喜欢）

这一页的前四个 Tab 读的是**当前登录账号**的真实内容，数据通路和下载器不同：

```
前端 pages/favorites.js
   │  GET /api/bridge/account/{status,list}
   ▼
bridge_server.py  ── 5 个端点（见下表）
   │
   ▼
account_service.py ── 常驻一个 Playwright 页面（首页上下文 + 完整登录 Cookie）
   │                  页面 SDK 自动补 uifid / x-secsdk-web-signature
   ▼
.data/account_cache/douyin.json  ── 归一化后的元数据缓存
   │  卡片上点「下载」
   ▼
现有任务队列 /api/bridge/download（复用，不新开下载通路）
```

| 接口 | 作用 |
|---|---|
| `GET /api/bridge/account/status` | 昵称/头像/五项计数、各 tab 已缓存条数、同步进度、登录状态 |
| `GET /api/bridge/account/list?tab=post\|like\|collect\|following` | 读某个 tab 的缓存（`limit` 可选） |
| `POST /api/bridge/account/sync` `{tabs?}` | 触发后台同步，立即返回；进度靠轮询 status |
| `POST /api/bridge/account/login` | 打开有头浏览器扫码登录，成功后写回 `.cookies.json` |
| `GET /api/bridge/account/login/status` | 登录流程状态：`idle / waiting / ok / failed` |

为什么必须走「页面通道」而不是普通 HTTP：

- `aweme/post`、`aweme/favorite`、`aweme/listcollection`、`user/following/list`
  全部被抖音 ArgusSecurityPlugin 门禁，纯 aiohttp 请求一律
  `403 Blocked by ArgusSecurityPlugin Uifid Not Found`；补上 uifid 后变成
  `Signature Not Found`（签名只能由页面 SDK 现算）。
- 页面里的 `fetch` 会被 webmssdk 挂钩自动补签名，所以 `page.evaluate(fetch)` 才是稳定通路。
- **必须在首页上下文翻页**，不能在用户主页：主页自己会发同一条接口，把接下来那几个
  `max_cursor` 先消费掉，我们再去问同一个游标只会换回空壳（只剩 `status_code`）。
- 翻页要 3 秒节流：0.8 秒连发时第二页就开始回空壳。空页**不代表到底了**，要重试。
- Cookie 必须整份带上（含 `sessionid`）：不带登录态时页面是未登录态，只给第一页
  （响应里带 `not_login_module` 和「看更多最新作品」引导）。

刷新策略是**手动**的：点「同步」才拉接口。喜欢/关注动辄几百条、要翻三十多页
（实测全量 6 分 37 秒），自动轮询等于拿风控额度换新鲜度。

Tab 上的数字用**实际同步到的条数**，账号卡保留抖音官方的统计数字 —— 两者会不一致
（私密/已删/审核中的作品接口不返回），并排展示会让人误以为丢数据。

### 快手账号中心（能力边界，如实说明）

同一页顶部有「抖音 / 快手」切换。**快手能做的事比抖音少**，这不是没做完，是平台不给：

| 能力 | 抖音 | 快手 | 说明 |
|---|---|---|---|
| 我的作品 | ✅ | ✅ | 快手走 `visionProfilePhotoList`（userId + pcursor） |
| 我的喜欢 | ✅ | ❌ | 快手网页版个人主页**没有「喜欢」入口**（实测 2026-10-17，只有「作品」） |
| 我的收藏 | ✅ | ❌ | 同上，没有「收藏」入口 |
| 我的关注 | ✅ 名单 | ⚠️ 关注动态 | 快手只给「关注的人最近发的作品」流（`page: "follow"`），不给「我关注了谁」的名单 |

三个必须知道的实现约束：

1. **快手主会话 Cookie 是 session cookie**。`kuaishou.server.web_st` 不落盘，
   浏览器一关就没了，`kuaishou.server.webday7_st` 也不会自动续期（实测重启后
   `checkLogin` 立刻变 false）。所以快手的「扫码登录」是**登录 + 同步在同一个
   浏览器实例里一口气做完**，不能像抖音那样"登录一次、以后靠 Cookie"。
2. **点「同步」可能直接提示登录失效** —— 那是正常现象，点「重新登录」扫码即可，
   扫完会自动同步一次。
3. **快手作品的下载不走 `run.py`**。卡片上的「下载」调的是
   `POST /api/bridge/kuaishou/download`（`kuaishou_headless.py`），
   把快手链接丢给抖音那套只会解析失败。

接口与抖音共用同一组路径，只是多一个 `platform` 参数：

| 接口 | 差异 |
|---|---|
| `GET /api/bridge/account/status?platform=kuaishou` | 多返回 `capabilities`（哪些 tab 真支持） |
| `GET /api/bridge/account/list?platform=kuaishou&tab=post\|follow` | tab 只有 post / follow |
| `POST /api/bridge/account/sync` `{platform:"kuaishou"}` | 登录失效时 `sync.error` 给的是可操作提示 |
| `POST /api/bridge/account/login` `{platform:"kuaishou"}` | 开有头 Edge，扫码成功后立刻同步 |

### 快手数据接入（作品档案 / 关注）


抖音与快手是**两套完全独立的引擎**：抖音是 SQLite(`aweme` 表) + CLI `run.py`，
快手是 JSON 台账(`download_log.json`) + `kuaishou_headless.py`（yt-dlp/playwright）。
两个页面的**顶部标签**（抖音 / 快手）在两者间切换，两库互不干扰。

| UI 动作 | 快手后端接口 |
|---|---|
| 作品列表（分页/搜索） | `GET /api/bridge/ks/works?q=&limit=&offset=` |
| 作者库 | `GET /api/bridge/ks/authors` |
| 统计条 | `GET /api/bridge/ks/stats` |
| 视频流（支持 Range） | `GET /api/bridge/ks/media?id=<photoId>` |
| 在文件夹中打开 | `POST /api/bridge/ks/media/reveal` `{id}` |
| 删除作者台账记录 | `POST /api/bridge/ks/author/delete` `{author}` |
| 记录/修改作者主页 | `POST /api/bridge/ks/author/profile` `{author, url}` |

设计要点（都是踩过才知道的）：

- **台账根目录可配置**：默认读工具箱自身 `data/kuaishou/download_log.json`；
  命令行版留下的历史台账通过 `data/kuaishou/ledger_roots.json`（一个字符串数组）声明，
  当前指向 `E:\录屏\spxz`。多处台账按 `photoId` 去重，优先保留文件仍存在的那条。
- **作者名取自落盘路径**（`file` 的倒数第二段），**时间与体积取自磁盘文件 `stat`**
  （台账里没有这两个字段），列表按 mtime 排序 —— 这是唯一诚实的来源。
- **作者主页 ID 靠作品 ID 交集推断**：下载时留下的 `works_list_<ID>.json` 里那串就是主页 ID，
  但它只有作品 ID、没有昵称；台账只有昵称、没有主页 ID。两边唯一的共同语言是**作品 ID 本身**，
  所以用交集反推（阈值：命中 ≥3 条且覆盖 ≥30%）。推断成功的标 `profileSource: "auto"`，
  推不出来的在卡片上给「设置主页」按钮手动填。
- **索引带 mtime 签名缓存**：失效判据是 (路径, mtime, size) 三元组，比 TTL 可靠 —— 刚下完的作品立刻可见。
- **统计与列表数可以不等**：`ks_stats()` 同时回 `works`(文件在磁盘) 与 `records`(台账行数)，
  差额是「文件已被删」的记录。前端 hint 会写明，避免看起来像 bug。
- **平台色相区分**：无封面时卡片配色种子改用 `id` 哈希（原先只按封面 URL 取色，
  快手作品没有 `cover` 字段，上千张卡片会全同色）。
- **快手卡片不给「收藏」按钮**：收藏是本地 `localStorage` 概念，快手侧没接。

## 3. 目录结构

```
douyin-downloader-ui/
├── index.html                  入口（按顺序加载全部脚本）
├── README.md                   本文件
├── docs/
│   ├── 修改指南.md             后期修改手册（换文案/配色/加页面…）
│   └── 自检清单.md             交付自检记录与复核结果
├── assets/
│   ├── css/
│   │   ├── tokens.css          ★ 设计变量（颜色/字号/间距/圆角，改主题只动它）
│   │   ├── base.css            Reset 与基础元素
│   │   ├── layout.css          应用骨架（侧边栏/主区/响应式）
│   │   └── components.css      组件样式（按钮/卡片/表格/弹窗…）
│   ├── js/
│   │   ├── config.js           ★ 集中配置（全部文案/导航/选项/设置表单 schema）
│   │   ├── icons.js            内联 SVG 图标库（无 emoji 图标）
│   │   ├── utils.js            通用工具（格式化/防抖/剪贴板/localStorage）
│   │   ├── mock-data.js        ★ 演示数据（首次运行灌入，可整体替换）
│   │   ├── store.js            ★ 单一数据层（读写 localStorage，全站唯一出口）
│   │   ├── api.js              ★ 数据适配层（mock ⇄ 真实后端切换口）
│   │   ├── router.js           Hash 路由
│   │   ├── components.js       共享组件（任务行/作品卡/弹窗/提示条…）
│   │   ├── app.js              应用骨架（侧边栏/账号卡/全局弹窗）
│   │   └── pages/              每个页面一个文件，互不依赖
│   │       ├── workbench.js    三平台工作台共用模板
│   │       ├── tasks.js / creators.js / favorites.js
│   │       └── queue.js / archive.js / settings.js
│   └── img/
│       ├── logo.png            工程原始 Logo
│       └── thumbs/             12 张演示缩略图（缺失时界面自动降级为程序化封面）
```

## 4. 数据说明（重要）

- **全部为演示数据**：作者、作品、任务、收藏均为虚构（作者名沿用官方桌面端参考截图中的演示名单），
  不包含任何真实用户隐私。
- 数据统一存放在浏览器 `localStorage` 的 `dyui.*` 键下，可在「设置 → 数据台账」或侧边栏「台账」中：
  - 查看每个键的占用
  - 导出完整 JSON
  - 一键重置为演示初始态
- 首次打开自动灌入演示数据；清空 localStorage 后刷新即恢复初始态。

## 5. 对接真实后端

网页版当前运行在 mock 模式（`assets/js/api.js` 顶部 `mode: 'mock'`）。
后端工程已内置 REST 服务，启动：

```bash
# 在工程目录 E:\录屏\dysp\dyxz 下
pip install fastapi uvicorn
python run.py --serve --serve-port 8000
```

然后把 `api.js` 里的 `mode` 改为 `'rest'`（`REST_BASE` 默认即 `http://127.0.0.1:8000`）。
接口对应关系：

| UI 动作 | 后端接口 |
|---|---|
| 提交下载 | `POST /api/v1/download` `{url}` |
| 任务状态 | `GET /api/v1/jobs/{job_id}` |
| 任务列表 | `GET /api/v1/jobs` |
| 健康检查（网络诊断弹窗） | `GET /api/v1/health` |

页面代码不需要改动；作者库/作品档案/设置在 rest 模式下仍读本地层，
需要全部走后端时按 `docs/修改指南.md` 第 6 节扩展 api.js 即可。

## 6. 打包与发布

纯静态站点，把整个目录原样上传到任意静态托管即可（Nginx / GitHub Pages / Vercel / 对象存储）：

```bash
# 例：Nginx
server { listen 80; root /var/www/douyin-ui; index index.html; }
# 例：一条命令临时分享
npx serve . --listen 8080
```

注意：`file://` 直开与静态托管两种方式都已验证；若托管在子路径下，无需任何配置改动（全部为相对路径）。

## 7. 版本与回滚

- 本项目以 git 仓库交付，首次交付已打标签 `v1.0`。
- 回滚方式见 `docs/修改指南.md` 第 8 节（含具体命令）。

## 8. 已知边界（如实说明）

- 浏览器沙箱不允许网页直接打开本地文件夹，「打开所在目录」按钮在网页版以路径弹窗形式呈现；
  接入真实后端后可由后端接口唤起。
- 任务进度为前端模拟（mock 模式）；rest 模式下进度来自后端 job 轮询。
- 登录账号、Cookie 均为演示态文案，网页版不读写任何真实凭据。
