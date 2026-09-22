# -*- coding: utf-8 -*-
"""探测真实 File To Link Bot 中转页结构，提取真实文件直链。"""
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

URL = "https://fares-successfully-vertex-consult.trycloudflare.com/download/83942d892579074f"
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")

out = r"C:\Users\Mechrevo\.openclaw-autoclaw\workspace\DELIVERY\clawfetch\tests\real_page_snapshot.html"
with open(out, "w", encoding="utf-8") as f:
    f.write(html)
print("HTML_LEN", len(html), "saved ->", out)

links = re.findall(r'href="([^"]+)"', html)
links += re.findall(r"url=([^\"'>]+)", html)
links += re.findall(r"open\(['\"]([^'\"]+)['\"]", html)
links += re.findall(r"location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", html)
links += re.findall(r"window\.location\s*=\s*['\"]([^'\"]+)['\"]", html)
print("ALL_LINKS:")
for l in sorted(set(links)):
    print("  ", l)

stream_refs = re.findall(r"[/a-zA-Z0-9_.-]*stream[/a-zA-Z0-9_.?=-]*", html)
print("STREAM_REFS:", sorted(set(stream_refs)))

# 找出所有可能带 token 的路径
token_refs = re.findall(r'["\'](/[^"\']*83942d892579074f[^"\']*)["\']', html)
print("TOKEN_REFS:", sorted(set(token_refs)))

# 打印包含 script 的关键片段（去 emoji 后前 3000 字符）
body = html.replace("🎬", "[movie]")
idx = body.find("<script")
if idx >= 0:
    print("---- SCRIPT SNIPPET ----")
    print(body[idx:idx + 3000])
