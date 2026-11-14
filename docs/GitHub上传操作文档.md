# GitHub 上传操作文档

> 记录 2026-11-14 将本项目（`E:\lp\lp\douzy`）上传到 GitHub 的完整操作过程，供后续复现或排查。

## 一、上传前安全扫描（防止敏感信息泄漏）

### 1.1 项目概况

- 磁盘总量约 6.3 GB、33,967 个文件，其中绝大多数是运行产物（`runtime/` 2.6G、`data/` 2.2G、`.data/` 371M、`downloads/` 35M）。
- Git 实际跟踪的仅 301 个源码/文档文件，无单文件超过 5MB。
- 仓库已有本地提交历史（master 分支），尚无远程。

### 1.2 发现的敏感文件及处置

| 发现 | 风险 | 处置 |
|---|---|---|
| `modules/live_recorder/config.json.bak-*`（5 个备份） | **含真实快手登录 Cookie**（passToken / kuaishou.live.web_st，等同账号凭证） | 加入 `.gitignore`（`config.json.bak-*`），不入库 |
| `.data/launcher-profile`（浏览器 profile，371MB） | 含浏览器 Cookie、登录态、自动填充数据 | 加入 `.gitignore`（`.data/`），不入库 |
| `modules/douyin/webui/.data/` | `account_cache/` 存有抖音/快手账号缓存 JSON | 加入 `.gitignore`，不入库 |
| `modules/douyin/core/.state/` | 下载器运行态状态文件 | 加入 `.gitignore` |
| `modules/douyin/config.yml.bak` / `config.yml.testbak` | 配置备份（检查后无 Cookie，但属本机配置） | 加入 `.gitignore` |
| `modules/live_recorder/.daemon.lock` | 守护进程锁文件 | 加入 `.gitignore` |
| `*.lnk` 快捷方式 | 含本机绝对路径 | 加入 `.gitignore` |
| `modules/bilibili/.cookies.json` | B 站登录 Cookie | 原有规则已覆盖，验证通过 |

### 1.3 历史提交扫描

- 对全部历史 blob 扫描 `web_st=<40位以上长值>`：**无命中**，历史中无真实 Cookie。
- `sk-` 命中均为测试假密钥（`sk-test`、`sk-DEADBEEF12345678`、LEAK-PROBE 哨兵值），安全。
- `passToken` 命中均为代码中的字段名常量（`LOGIN_KEYS` 元组、注释、测试 demo 字符串），非真实凭证。

### 1.4 提交前最终扫描

`git add -A` 后对暂存内容再跑一轮密钥正则（passToken/web_st/api_key/secret/token/password 赋值模式）：**无命中**。

### 1.5 `.gitignore` 补充内容

在原 `.gitignore` 末尾追加了「上传 GitHub 前补充的安全过滤（2026-11-14 安全扫描）」一节，覆盖上表全部条目，并用 `git check-ignore` 逐条验证生效。

## 二、Git 提交

```bash
cd /e/lp/lp/douzy
git add -A                                   # 142 个文件变更（含旧启动器 .bat 的删除，属磁盘真实状态）
git commit -m "feat: 安全加固 + 新增模块..."
git log --oneline -3                         # 确认提交：856d2c2
```

## 三、gh CLI 安装（便携版）

本机无 gh，从镜像下载官方便携版并解压（官方直链 github.com 直连不稳，走镜像）：

```bash
mkdir -p "$HOME/.workbuddy/bin-gh"
curl -sSL -o "$HOME/.workbuddy/bin-gh/gh.zip" \
  "https://ghproxy.net/https://github.com/cli/cli/releases/download/v2.104.0/gh_2.104.0_windows_amd64.zip"
cd "$HOME/.workbuddy/bin-gh" && unzip -o -q gh.zip   # 得到 bin/gh.exe
GH="$HOME/.workbuddy/bin-gh/bin/gh.exe"
"$GH" --version    # gh version 2.104.0
```

## 四、GitHub 授权（OAuth 设备码流程）

本机无存储凭证，采用 GitHub 设备码授权（无需在终端输密码）：

```bash
# 1. 获取设备码
curl -sS -X POST "https://github.com/login/device/code" -H "Accept: application/json" \
  -d "client_id=178c6fc778ccc68e1d6a&scope=repo,workflow,read:org"
# 返回 user_code（形如 91B6-848D）与 verification_uri（https://github.com/login/device）
# 2. 用户在浏览器打开 verification_uri，输入 user_code，点击 Authorize
# 3. 轮询换 token（authorization_pending 时每 5 秒重试）
curl -sS -X POST "https://github.com/login/oauth/access_token" -H "Accept: application/json" \
  -d "client_id=178c6fc778ccc68e1d6a&device_code=<device_code>&grant_type=urn:ietf:params:oauth:grant-type:device_code"
# 4. 用 access_token 登录 gh 并接管 git 凭证
printf "%s" "<access_token>" | "$GH" auth login --with-token
"$GH" auth setup-git          # git push 走 gh 的凭证助手
"$GH" auth status             # 登录账号：aywd66，scopes: repo, workflow, read:org
```

> 网络说明：本机 `github.com` 需经代理（环境变量 `https_proxy=http://127.0.0.1:2205`）访问；`api.github.com` 可直连。git 推送继承代理环境变量，无需额外配置。

## 五、创建远程仓库并推送

```bash
GH="$HOME/.workbuddy/bin-gh/bin/gh.exe"
cd /e/lp/lp/douzy
"$GH" repo create douzy --private --source=. --remote=origin --push
# 等价于：创建私有仓库 aywd66/douzy → 添加 remote origin → 推送 master
git remote -v                 # 验证
"$GH" repo view aywd66/douzy  # 验证仓库可见
```

## 六、上传后验证清单

- [ ] `git ls-files | wc -l` 确认入库文件数合理（应 ≈ 440，不含 runtime/data/downloads/.data）
- [ ] GitHub 网页上搜索 `passToken`、`web_st=`、`cookies`：确认无真实凭证
- [ ] `git status` 干净（除忽略文件外无遗漏）
- [ ] 仓库可见性、README 渲染正常

## 七、后续日常更新流程

```bash
cd /e/lp/lp/douzy
git add -A && git commit -m "..." && git push
```

提交前建议重跑一次密钥扫描：

```bash
git diff --cached --name-only -z | xargs -0 grep -l -E \
  "(passToken[\"']?\s*[:=]\s*[\"'][A-Za-z0-9]{20,}|web_st=[A-Za-z0-9]{40,}|(api_key|secret|token)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-]{20,})"
```

无输出即安全。
