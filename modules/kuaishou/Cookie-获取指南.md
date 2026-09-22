# Cookie 获取指南（用于下载主页全部 252 个历史作品）

> 快手对**未登录**访问已关闭全量列表（弹滑块验证码 + "登录后观看作者更多视频内容"）。
> 下载全部历史作品需要你提供一次登录态 Cookie，之后脚本全自动完成。

## 步骤（约 1 分钟）

1. 用 Chrome/Edge 打开 <https://www.kuaishou.com> 并**登录**（扫码即可）
2. 登录后按 **F12** 打开开发者工具 → 选 **Network（网络）** 标签
3. 刷新页面，在 Network 列表点**任意一条 www.kuaishou.com 的请求**
4. 右侧 **Request Headers（请求标头）** 里找到 **Cookie:** 那一行
5. **右键 → 复制值（Copy Value）**，把整串粘贴保存为文件：
   `E:\录屏\spxz\cookie.txt`（内容就是那一长串 `kpf=PC_WEB; kpn=...; passToken=...`）

## 然后运行

```powershell
& "E:\录屏\spxz\ytdlp-env\Scripts\python.exe" "E:\录屏\spxz\tools\kuaishou_full_dl.py" "E:\录屏\spxz\cookie.txt" "E:\录屏\spxz"
```

脚本会自动：校验登录 → 分页拉取全部 252 个作品 → 与已下载的 19 个去重 →
逐个以最优画质下载 → 更新 `download_log.json` 台账与 `failed_list.json` 失败清单。

## 安全说明

- Cookie 只保存在本地 `cookie.txt`，仅用于你自己的账号访问你指定的公开主页
- 下载完成后可删除 `cookie.txt`；如担心安全，可在快手 App 里退出登录使该 Cookie 失效
- Cookie 有效期一般 7 天以上；若中途提示过期，重新导出一次即可
- 已下载的 19 个会自动跳过，中断后重跑即从断点继续
