#!/usr/bin/env python3
"""Rebuild config.yml from the clean example with our customizations."""
import yaml

with open("config.example.yml", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

# 全量模式定制（萧羽羽/芋泥椰奶全量已完成，日常用增量）
cfg["link"] = []                       # 链接全部走快捷方式粘贴，不写死在这里
cfg["progress"]["quiet_logs"] = False  # 控制台显示进度
cfg["number"]["post"] = 0              # 0 = 不限量
cfg["download_pinned"] = True          # 包含置顶作品（萧羽羽差的 4 条就是置顶）
cfg["folderstyle"] = False             # 不建单作品文件夹，视频直接放在作者目录下
cfg["group_by_mode"] = False           # 去掉 post/like 这一层类型目录
cfg["increase"]["post"] = False        # 本轮全量用；跑完改回 True（见 finish_incremental.py）
cfg["browser_fallback"]["idle_rounds"] = 15
cfg["browser_fallback"]["wait_timeout_seconds"] = 900
cfg["auto_cookie"] = True              # 从 config/cookies.json 自动加载登录态
cfg.pop("cookies", None)               # 移除示例里的占位 Cookie，避免毒化请求

with open("config.yml", "w", encoding="utf-8") as f:
    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

print("config.yml rebuilt OK")
for key in ("link", "download_pinned", "auto_cookie"):
    print(key, "=", cfg.get(key))
print("number.post =", cfg["number"]["post"])
print("increase.post =", cfg["increase"]["post"])
print("browser_fallback.idle_rounds =", cfg["browser_fallback"]["idle_rounds"])
