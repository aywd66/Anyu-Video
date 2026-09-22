# Douzy Web UI · 抖音下载器网页版

> ## ★ 桥接模式（已集成到工程）
>
> 本目录已集成到工程根目录（E:\录屏\dysp\dyxz\webui\）。
> 双击 `启动WebUI.bat`（或用工程 venv 运行 `python webui/bridge_server.py`），
> 浏览器打开 <http://127.0.0.1:8518> 即可使用**真实数据**：
>
> - **关注**：真实作者库（来自数据库 aweme 表，按作品数排序）
> - **作品档案**：真实已下载作品（1019+ 条，真实封面与落盘路径，分页加载）
> - **任务中心**：真实下载历史（download_history 表）+ 实时任务进度 + 日志查看
> - **链接下载**：粘贴抖音链接提交真实下载（调用工程 CLI run.py，自动排队，并发 2）
> - **设置**：实时展示 config.yml（敏感字段自动脱敏，只读）
>
> 数据安全边界：数据库全程**只读**（mode=ro）；下载走工程自身 CLI，增量去重照常生效，
> 不会重复下载已有作品；不修改工程任何现有文件；配置中的 Cookie 等敏感信息不会返回给网页。
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
| 关注 | `#/creators` | img/desktop/004.png | 作者库：搜索、筛选、双视图、同步、备注、下载 |
| 收藏与喜欢 | `#/favorites` | img/desktop/005.png | 收藏视频/收藏合集/喜欢三个 Tab，同步账号 |
| 多链排队 | `#/queue` | —（桌面端"多链排队"） | 批量粘贴链接、解析去重、一键入队 |
| 任务中心 | `#/tasks` | img/desktop/006.png | 统计条、状态筛选、搜索、取消/重试/移除 |
| 作品档案 | `#/archive` | —（桌面端"作品档案"） | 本地作品库：搜索、类型筛选、收藏、导出 TSV 清单 |
| 设置 | `#/settings` | — | 对应工程 config.yml 的可视化表单 + 数据台账 |

另有全局能力：网络诊断弹窗、数据台账弹窗（侧边栏底部）、移动端响应式（≤900px 收纳为顶栏）。

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
