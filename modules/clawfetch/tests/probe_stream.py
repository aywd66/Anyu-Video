# -*- coding: utf-8 -*-
"""探测 /stream/ 端点：大小、Range 支持、Content-Disposition。"""
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE = "https://fares-successfully-vertex-consult.trycloudflare.com"

# 1) HEAD /stream/
req = urllib.request.Request(BASE + "/stream/83942d892579074f", method="HEAD",
                             headers={"User-Agent": "Mozilla/5.0"})
try:
    r = urllib.request.urlopen(req, timeout=30)
    print("HEAD_STATUS", r.status)
    for k, v in r.headers.items():
        print("  %s: %s" % (k, v))
except Exception as e:
    print("HEAD_ERROR", type(e).__name__, e)

# 2) GET Range 0-0
req2 = urllib.request.Request(BASE + "/stream/83942d892579074f",
                              headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-0"})
try:
    r2 = urllib.request.urlopen(req2, timeout=30)
    print("RANGE_STATUS", r2.status)
    for k, v in r2.headers.items():
        print("  %s: %s" % (k, v))
    print("RANGE_BODY_LEN", len(r2.read()))
except Exception as e:
    print("RANGE_ERROR", type(e).__name__, e)
