# -*- coding: utf-8 -*-
r"""ks_guard.py — 快手接口风控的「刹车」（跨进程持久化）。

## 为什么需要这个文件

快手 PC 站的 `visionVideoDetail` 接口有一道**服务端风控**：撞上之后返回
`status_208005`，而且**不会自己好**。2026-09-25 的实测时间线：

| 时间 | 这个脚本干了什么 | 结果 |
|---|---|---|
| 09-24 20:24 | 连调 55 个详情接口 | ✅ 55 成功、0 失败 |
| 09-25 14:13 | 连调 12 个 | ✅ 12 成功、0 失败 |
| 09-25 16:53 | **深滚收集 1790 个作品**后开始调详情 | ❌ 第 12 个起全 208005 |
| 09-25 21:03 | 重试 | ❌ 18/19 失败 |
| 09-25 21:19 | 又重试（脚本自己等了 12 分钟 × 若干轮） | ❌ 仍然全灭，并记下 26 个 208005 |

结论有两条，都很硬：

1. **正常量级不会被拦。** 55 个连调、12 个连调都是 0 失败 —— 所以「这接口
   天生只能连调 2 个」的说法是错的（旧代码注释里那段结论是撞墙后观察到的，
   把「已被封的状态」误当成了「接口的固有配额」）。
2. **撞墙之后短时间内重试毫无意义。** 旧的重试策略是「等 12 分钟、最多 8 轮」
   （共 96 分钟）—— 实测 4 小时 26 分钟后**仍然是封的**。这个 96 分钟纯属白等，
   而且每等一轮就多打一轮请求，可能反过来**延长**惩罚窗口。

于是本模块把风控当成一个**有状态的外部条件**来对待，而不是每轮重新乐观重试：

- **冷却（cooling）**：撞墙即把「冷却截止时间」写盘。之后任何入口启动时先看它，
  没到期就**立刻收工**并说清几时再来 —— 不再原地睡上几小时。
  冷却时长按撞墙次数**指数退避**（90 分钟 → 3 小时 → 6 小时 → …，封顶 24 小时）。
- **预算（budget）**：按自然日给「收集条目数」和「详情调用数」各设一个上限，
  跨进程累计。唯一真正触发风控的动作就是「一口气要几千个」，预算把这个动作
  从「可能发生」改成「默认做不到，想超必须显式开口」。
- **独占（lease）**：同一个账号、同一时刻只允许一个**批量**进程。两个进程并行
  会把当日额度**各领一份**（你看到 400、实际打 800），而并行打接口恰好就是
  09-25 撞墙的那个形状。见下方 `acquire_run()`。

## 取舍（写清楚，免得看起来像万能药）

- 这是**避免触发**，不是**突破限制**。它换来的「一劳永逸」是「日常使用几乎不会
  再撞上」，代价是「存量大的号只能慢慢啃」。想一次啃完 8191 个，本模块就是
  故意不让你这么干。
- 预算默认值是从上表那条 55-OK / 1790-blocked 的边界上取的**保守值**，不是
  精确的官方阈值（平台也不会公开）。可用环境变量放宽，但放宽就等于自己承担风险。
- 状态文件落在**固定锚点** `data/kuaishou`，刻意**不跟产物目录走**（与
  `download_log.json` 台账相反 —— 台账跟目录走是对的，风控状态跟目录走就等于
  给刹车装了个看不出来的旁路）。理由与踩坑记录见 `out_dir()`。

## 一个尚未定论的问题：风控的粒度

上表能确定的是「量大了会撞」，**不能**确定阈值是绑在**账号**上还是绑在
**单个页面上下文**上 —— 因为「冷却 5h24m 后单条成功」那次同时满足了两件事
（换了新页面 **且** 冷却已过），两个变量混在一起，分不开。

这件事直接决定「多开窗口能不能提速」：账号级 → 多窗口纯有害；页面级 →
多窗口真的能 n 倍。判别实验在 `ks_granularity_probe.py`（默认不跑，手动执行）。
在拿到结果之前，本模块按**账号级**（更保守的那一面）行事。

## 按什么分桶（「多个 ck 有用吗」的答案就在这）

阈值绑在哪一层，同时决定了「多几个 cookie / 多几个账号」有没有收益。
社区逆向资料指向**设备**（`did` cookie），而不是账号：

- 快手 PC 站有**两个各自独立的身份 cookie**：`did`（设备，36 位）与
  `userId`（账号，10 位）。
- 同一个浏览器 profile 里登出再登另一个号 → `userId` 变、**`did` 不变**
  （它是设备标识，不是会话标识）。只有换 `--user-data-dir`
  （即换一份登录 profile）才会拿到新的 `did`。

所以本模块**按 `did` 分桶**，不按 `userId`：

| 情形 | `did` | 桶 | 当日额度 |
|---|---|---|---|
| 同一设备上换账号 | 不变 | **同一个** | 一份（**不给两份**） |
| 换个 profile 目录 / 换台机器 / 别处抓的 ck | 变了 | 不同 | 各一份 |

这样选是因为**猜错的代价不对称**：万一平台其实按设备判，按 `userId` 分桶就等于
把刹车拆了（两个号各 400 → 实际打出去 800）；反过来只是慢一点，没有损失。
每个桶各自记冷却、各自持租约 —— 见下方「单实例租约」。
桶与设备/账号的对应关系记在 `data/kuaishou/idents.json`，用
`python modules\kuaishou\tools\ks_ident.py` 可以直接看。


## 关掉它

设 `KS_GUARD_OFF=1` 可整套跳过（预算与冷却都不生效）。给「我知道我在干嘛」的场合用。
"""

import atexit
import hashlib
import json
import os
import threading
import time
import uuid

# ── 冷却参数 ──────────────────────────────────────────────────────────────
# 首次 90 分钟起退避。为什么不是 12 分钟：旧代码实测 12 分钟 × 8 轮无效，
# 4.4 小时后仍封。宁可一次等够，也别反复去戳（反复戳可能刷新惩罚窗口）。
BASE_COOLDOWN_MIN = 90
COOLDOWN_CAP_MIN = 24 * 60

# ── 当日预算默认值 ────────────────────────────────────────────────────────
# 依据：09-24 收集 55 个 + 连调 55 个详情 → 0 失败；09-25 收集 1790 个 →
# 第 12 个详情就撞墙。取一个明显落在安全侧、又能干完日常活的数。
# 2026-10-24 应用户要求去掉「当日预算」限制:默认改为无上限(冷却/独占租约保留)。
# 仍可用 KS_MAX_COLLECT_PER_DAY / KS_MAX_DETAIL_PER_DAY 显式设回一个正数。
DEFAULT_MAX_COLLECT_PER_DAY = 10 ** 9
DEFAULT_MAX_DETAIL_PER_DAY = 10 ** 9

_STATE_NAME = "risk_state.json"
_IDENT_REG = "idents.json"


def _find_root():
    """与 kuaishou_fast_dl / kuaishou_headless 完全同一套根目录解析。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here)),
              os.path.dirname(os.path.dirname(os.path.dirname(here)))):
        if os.path.isdir(os.path.join(d, "data", "kuaishou")):
            return d
    return os.path.dirname(here)


def out_dir(explicit=None):
    """状态落点：显式传入 > **固定锚点** `data/kuaishou`。

    ⚠️⚠️ **刻意不跟 `KS_OUTDIR` 走**（这一点与 `download_log.json` 台账相反）。

    台账跟产物目录走是对的（换目录 = 换个空台账，作品重下一遍可见）。
    但风控状态是**账号的属性，不是产物目录的属性** —— 让它可以被「换个产物目录」
    绕过，等于给刹车装了个不明显的旁路开关。

    这个坑当天就踩到了：只在 `E:\\...\\kssp\\kuaishou` 写了冷却，另一次运行
    走默认目录 `data/kuaishou`，冷却**当场失效**、又打了一轮接口。
    → 所以锚定在登录态的同一个根下，跟产物目录无关。

    诊断/测试可以用 `KS_GUARD_HOME` 显式指定落点（**故意的**旁路，名字就写明了用途；
    不像 `KS_OUTDIR` 那样会「顺手」把刹车关掉）。
    """
    return (explicit
            or os.environ.get("KS_GUARD_HOME")
            or os.path.join(_find_root(), "data", "kuaishou"))


def state_path(explicit=None):
    return os.path.join(out_dir(explicit), _STATE_NAME)


def _disabled():
    return (os.environ.get("KS_GUARD_OFF") or "").strip() not in ("", "0", "false", "False")


def _cap(env_key, default):
    """预算上限可被环境变量覆盖；非法值一律回落默认，绝不因为配置手滑而炸掉下载。"""
    try:
        v = int(os.environ.get(env_key) or 0)
        return v if v > 0 else default
    except Exception:
        return default


def _today():
    return time.strftime("%Y-%m-%d")


def _blank():
    return {
        "day": _today(),
        "collect_used": 0,
        "detail_used": 0,
        "blocked_until": 0.0,      # epoch 秒；0 = 没在冷却
        "blocked_at": 0.0,
        "block_count": 0,          # 连续撞墙次数，用于指数退避
        "last_reason": "",
    }


def _read_json(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _write_json(p, obj):
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def _is_record(d):
    """一条「桶的状态」长这样（有 day 字段）；旧格式整个文件就是这样一条。"""
    return isinstance(d, dict) and "day" in d


def _load_blob(explicit=None):
    """把盘上的东西归一成 v2 结构：`{"buckets": {桶: 状态}, "legacy": 状态|None}`。

    v1（2026-09-25 之前的扁平格式）**不报错也不丢弃** —— 整份当 `legacy`，
    等第一个真实身份出现时被认领一次（见 `_adopt_legacy`）。
    """
    raw = _read_json(state_path(explicit))
    if not isinstance(raw, dict):
        return {"buckets": {}, "legacy": None}
    if isinstance(raw.get("buckets"), dict):
        leg = raw.get("legacy")
        return {"buckets": dict(raw["buckets"]),
                "legacy": leg if _is_record(leg) else None}
    if _is_record(raw):
        return {"buckets": {}, "legacy": raw}
    return {"buckets": {}, "legacy": None}


def _write_blob(blob, explicit=None):
    return _write_json(state_path(explicit),
                       {"version": 2,
                        "buckets": dict((blob or {}).get("buckets") or {}),
                        "legacy": (blob or {}).get("legacy")})


def load(explicit=None, bucket=None):
    """读状态。任何异常（文件坏、目录没权限、并发写坏）都回落成空白状态 ——

    这个模块的存在是为了**保护下载**，它自己绝不能成为新的故障点。

    ⚠️ **`default` 桶就存在「未认领」那个槽里**（也就是 v1 的扁平格式）。
    两者本来就是同一个概念：「还不知道这是谁的额度」。分开存会出现两份
    同源记录，等真身份来认领时重复计算。
    """
    st = _blank()
    try:
        blob = _load_blob(explicit)
        b = _bucket_of(bucket)
        rec = blob.get("legacy") if b == BUCKET_DEFAULT else blob["buckets"].get(b)
        if isinstance(rec, dict):
            for k in st:
                if k in rec:
                    st[k] = rec[k]
    except Exception:
        pass
    if st.get("day") != _today():
        # 跨自然日：预算归零，但**冷却不归零**（风控窗口跟自然日无关）
        st["day"] = _today()
        st["collect_used"] = 0
        st["detail_used"] = 0
    return st


def save(st, explicit=None, bucket=None):
    """写状态。**只写自己那个桶**（读-改-写整份文件，其余桶原样保留）。

    这一点很重要：多个桶（多份登录态）可能同时在不同窗口里跑，
    如果这里整份覆盖，后写的会把先写的那个桶的计数抹掉。
    """
    try:
        blob = _load_blob(explicit)
        b = _bucket_of(bucket)
        if b == BUCKET_DEFAULT:
            blob["legacy"] = st
        else:
            blob["buckets"][b] = st
        return _write_blob(blob, explicit)
    except Exception:
        return False


# ── 身份：桶 = 设备（did）──────────────────────────────────────────────────
# 「多个 ck 有没有用」的答案就是这段代码。详见模块 docstring 的「按什么分桶」。
BUCKET_DEFAULT = "default"
_IDENT_KEYS = ("did", "userId")     # did 是分桶依据；userId 仅展示 + 兜底


def ident_parts(cookies):
    """从 cookie 列表里挑出身份字段（纯取值，不依赖 playwright，可离线测）。"""
    got = {}
    try:
        for c in cookies or []:
            if not isinstance(c, dict):
                continue
            n = c.get("name")
            v = c.get("value") or ""
            if n in _IDENT_KEYS and v and n not in got:
                got[n] = str(v)
    except Exception:
        pass
    return got


def bucket_of(parts):
    """由身份字段算桶名。**did 优先**（设备）；没有 did 才退回 userId。

    返回 12 位十六进制串（落盘用，短且文件名安全）；拿不到任何身份 → 空串。
    """
    p = parts or {}
    v = p.get("did") or p.get("userId") or ""
    if not v:
        return ""
    return hashlib.sha256(str(v).encode("utf-8")).hexdigest()[:12]


def _bucket_of(bucket=None):
    return str(bucket or _bucket or BUCKET_DEFAULT)


def current_bucket():
    return _bucket


def set_identity(bucket, explicit=None):
    """声明**本进程属于哪个桶**。⚠️ 必须在任何冷却/预算检查**之前**调用。

    传空串 = 身份未知 → 落在 `default` 桶，行为与没有这套机制时完全一致
    （所以这个改动在「查不到身份」时是惰性的，不会凭空改掉老行为）。
    """
    global _bucket
    try:
        _bucket = str(bucket or "").strip() or BUCKET_DEFAULT
        if _bucket != BUCKET_DEFAULT:
            _adopt_legacy(_bucket, explicit)
    except Exception:
        pass
    return _bucket


def _adopt_legacy(bucket, explicit=None):
    """把 v1 的扁平状态**一次性认领给第一个出现的真实桶**。

    为什么认领而不是丢掉：升级那一刻盘上正躺着一个还没到期的冷却（当天 23:03），
    而它就是这台设备撞出来的。丢掉等于让用户一升级就又去打接口。
    为什么认领而不是让所有桶共享：共享就回到了「换个号也被前一个号的冷却拦住」，
    那正是这次要修的东西 —— 也是「多个 ck」最容易踩的坑。
    """
    try:
        blob = _load_blob(explicit)
        leg = blob.get("legacy")
        if not isinstance(leg, dict):
            return False
        if bucket in blob["buckets"]:
            blob["legacy"] = None          # 该桶已有自己的状态 → 旧状态过期，丢弃
        else:
            blob["buckets"][bucket] = leg
            blob["legacy"] = None
        return _write_blob(blob, explicit)
    except Exception:
        return False


# ── 身份注册表：profile 目录 ↔ 桶 ─────────────────────────────────────────
# 「这个 profile 是哪台设备/哪个号」只能从 cookie 里读出来，而读 cookie 要启动
# 浏览器（值在 Windows 上是 DPAPI 加密的，纯 Python 读不出来）。可是冷却检查
# 必须发生在启动浏览器**之前**（冷却期要 0.5 秒内收工、不拉浏览器）。
#
# 解法：把答案**缓存**下来 —— 登录时（身份刚刚确定、且是低频动作）写一次，
# 之后所有入口直接查表，不启动浏览器。
#
# 缓存缺失 = 身份未知 = 落 default 桶 = 与旧行为一致（不是错误路径）。

def ident_reg_path(explicit=None):
    return os.path.join(out_dir(explicit), _IDENT_REG)


def _norm_profile(profile):
    return os.path.normcase(os.path.abspath(str(profile)))


def remember_identity(profile, cookies, explicit=None):
    """记下「这个 profile 是哪个设备/账号」。登录时、以及每次读到 cookie 时调。

    返回桶名（记不住就返回空串）。
    """
    parts = ident_parts(cookies)
    b = bucket_of(parts)
    if not b or not profile:
        return ""
    try:
        reg = _read_json(ident_reg_path(explicit))
        if not isinstance(reg, dict):
            reg = {}
        reg[_norm_profile(profile)] = {"bucket": b,
                                       "did": parts.get("did", ""),
                                       "userId": parts.get("userId", ""),
                                       "at": time.time()}
        _write_json(ident_reg_path(explicit), reg)
    except Exception:
        pass
    return b


def identity_for_profile(profile, explicit=None):
    """查这个 profile 的桶（**不启动浏览器**）。没有记录 → 空串。"""
    if not profile:
        return ""
    try:
        reg = _read_json(ident_reg_path(explicit))
        rec = (reg or {}).get(_norm_profile(profile)) or {}
        return str(rec.get("bucket") or "")
    except Exception:
        return ""


def profile_of_bucket(bucket, explicit=None):
    """反查：这个桶对应哪个 profile（诊断用，可能为空）。"""
    try:
        reg = _read_json(ident_reg_path(explicit)) or {}
        for p, rec in reg.items():
            if str((rec or {}).get("bucket") or "") == str(bucket):
                return p
    except Exception:
        pass
    return ""


def set_identity_for_profile(profile, explicit=None):
    """启动时用：查注册表 → 设桶。查不到 → default 桶（与旧行为一致）。"""
    return set_identity(identity_for_profile(profile, explicit), explicit)


def read_profile_cookies(profile):
    """读一份登录 profile 的 cookie，只为了确认它是哪个设备/哪个号。

    ⚠️ 这是**整份守卫里唯一会启动浏览器的地方**，而且只在「注册表里没有这份
    profile」时才会被调用（正常情况——登记过——启动只查一次 JSON）。
    能不做就不做，因为启动浏览器要几秒，而冷却期收工要求 0.5 秒。

    值在 Windows 上是 DPAPI 加密的，纯 Python 读不出明文，所以必须借浏览器
    解密 —— 这也是为什么身份要**缓存**在 `idents.json` 里，而不是每次现读。

    失败一律返回空列表（守卫不启动浏览器也必须是能活的）。
    """
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            c = p.chromium.launch_persistent_context(
                str(profile), channel="msedge", headless=True, locale="zh-CN",
                args=["--window-position=-32000,-32000"],
                viewport={"width": 1, "height": 1})
            cks = c.cookies()
            c.close()
        return cks or []
    except Exception:
        return []


def buckets_snapshot(explicit=None):
    """诊断用：列出盘上所有桶的状态。

    返回 `[(桶名, 状态, 是否未认领), ...]`。`default` 桶就是「未认领」那个槽
    （两者是同一个概念，见 `load()` 的说明），所以统一按后者报。
    """
    out = []
    try:
        blob = _load_blob(explicit)
        leg = blob.get("legacy")
        if _is_record(leg):
            out.append((BUCKET_DEFAULT, _fill(leg), True))
        for b, rec in sorted((blob.get("buckets") or {}).items()):
            if isinstance(rec, dict):
                out.append((b, _fill(rec), False))
    except Exception:
        pass
    return out


def _fill(rec):
    st = _blank()
    for k in st:
        if k in rec:
            st[k] = rec[k]
    return st


def switch_bucket(bucket, mode="batch", explicit=None):
    """启动时身份未登记、读到真 cookie 后才发现真桶 → 把口径换过去。

    只在「**首次**用某份登录态」这一个场景发生（跑过登录或 `ks_ident.py`
    之后注册表里就有了，启动时直接查到，走不到这里）。

    为什么不能就这么算了：守卫的冷却与额度都是**按桶**记的，刚才问的是
    `default` 桶。不换口径 = 这次的用量记到别人头上，而真桶自己的冷却没被问过。
    所以这里补三件事：换桶 → 补问该桶的冷却 → 把租约从旧桶挪到新桶。

    返回 `(是否可以继续, 说明) `；`False` 表示真桶正在冷却，调用方应直接收工。
    """
    global _bucket
    try:
        if not bucket or bucket == _bucket:
            return True, ""
        set_identity(bucket, explicit)
        if blocked_info(explicit)[0]:                 # ① 补问真桶的冷却
            return False, cooling_message(explicit)
        if _held_token and not _inherited:            # ② 租约挪到新桶
            release_run(explicit)
            _ok, _holder = acquire_run(mode, explicit)
            if not _ok:
                return False, conflict_message(_holder, explicit)
        return True, ("[guard] 已登记本窗口身份：桶 %s。"
                      "（启动时未登记，方才按 default 问的额度，现已改按真桶记）" % bucket)
    except Exception:
        return True, ""


def bucket_line(explicit=None):
    """一行「我是谁」，进任务日志 —— 多份登录态同时用的时候，这张脸必须有。"""
    if _disabled():
        return ""
    if _bucket == BUCKET_DEFAULT:
        return ("[guard] 本窗口风控桶：**未登记**（按 default 桶计额度，"
                "可能跟其它未登记的登录态合并计算）")
    return ("[guard] 本窗口风控桶：%s（按设备 did 分桶；额度与冷却按桶计）" % _bucket)


# ── 冷却 ──────────────────────────────────────────────────────────────────
def blocked_info(explicit=None, now=None, bucket=None):
    """返回 `(是否冷却中, 剩余秒数, 截止时间字符串)`。

    冷却中的意思是：**此刻不要去打快手接口**，打了大概率还是 208005，
    而且可能把惩罚窗口往后推。

    `bucket` 只给诊断工具用（一次问多个桶时不能改全局身份）。
    """
    if _disabled():
        return (False, 0, "")
    st = load(explicit, bucket)
    now = now if now is not None else time.time()
    left = float(st.get("blocked_until") or 0) - now
    if left <= 0:
        return (False, 0, "")
    return (True, left, time.strftime("%Y-%m-%d %H:%M", time.localtime(st["blocked_until"])))


def cooling_message(explicit=None, bucket=None):
    """冷却中时给出一句人话，直接进任务日志。"""
    on, left, until = blocked_info(explicit, bucket=bucket)
    if not on:
        return ""
    mins = int(left // 60) + 1
    return ("[guard] 快手接口仍在风控冷却里 —— 大约 %d 分钟后（%s 之后）再试。"
            "现在跑也只会把 208005 又攒一遍，所以直接收工。"
            "进度都在台账里，不会丢。" % (mins, until))


def note_block(explicit=None, reason=""):
    """记一次撞墙：写冷却截止时间，并让下次撞墙等得更久（指数退避）。"""
    st = load(explicit)
    st["block_count"] = int(st.get("block_count") or 0) + 1
    n = st["block_count"]
    mins = min(BASE_COOLDOWN_MIN * (2 ** (n - 1)), COOLDOWN_CAP_MIN)
    now = time.time()
    st["blocked_until"] = now + mins * 60
    st["blocked_at"] = now
    st["last_reason"] = str(reason or "")[:200]
    save(st, explicit)
    return mins


def note_ok(explicit=None):
    """这一轮打得很顺 —— 清掉冷却，退避计数归零。"""
    st = load(explicit)
    if st.get("blocked_until") or st.get("block_count"):
        st["blocked_until"] = 0.0
        st["block_count"] = 0
        st["last_reason"] = ""
        save(st, explicit)


def clear(explicit=None):
    """人工解封（改完登录态、换了网络之后想立刻重试时用）。"""
    st = load(explicit)
    st["blocked_until"] = 0.0
    st["block_count"] = 0
    st["last_reason"] = ""
    save(st, explicit)


# ── 当日预算 ──────────────────────────────────────────────────────────────
def allow_collect(want, explicit=None):
    """申请「收集 N 个作品」的额度，返回**实际允许**的数量。

    收集本身（滚主页）也是要打接口的：09-25 那次就是先深滚了 1790 个，
    之后详情接口才开始全灭。所以收集量同样得管。
    """
    if _disabled():
        return int(want)
    st = load(explicit)
    cap = _cap("KS_MAX_COLLECT_PER_DAY", DEFAULT_MAX_COLLECT_PER_DAY)
    left = max(0, cap - int(st.get("collect_used") or 0))
    return max(0, min(int(want), left))


def collect_left(explicit=None):
    """今日还剩多少收集额度。

    收集是**边滚边发现**的，事前不知道总数，所以调用方拿不到一个准确 want ——
    这里直接返回剩余额度当上限。关掉守卫时返回一个大数（等于不限）。
    """
    if _disabled():
        return 10 ** 9
    st = load(explicit)
    cap = _cap("KS_MAX_COLLECT_PER_DAY", DEFAULT_MAX_COLLECT_PER_DAY)
    return max(0, cap - int(st.get("collect_used") or 0))


def commit_collect(n, explicit=None):
    if _disabled() or n <= 0:
        return
    st = load(explicit)
    st["collect_used"] = int(st.get("collect_used") or 0) + int(n)
    save(st, explicit)


def allow_detail(want, explicit=None):
    """申请「调 N 次详情接口」的额度，返回实际允许的数量。"""
    if _disabled():
        return int(want)
    st = load(explicit)
    cap = _cap("KS_MAX_DETAIL_PER_DAY", DEFAULT_MAX_DETAIL_PER_DAY)
    left = max(0, cap - int(st.get("detail_used") or 0))
    return max(0, min(int(want), left))


def commit_detail(n, explicit=None):
    if _disabled() or n <= 0:
        return
    st = load(explicit)
    st["detail_used"] = int(st.get("detail_used") or 0) + int(n)
    save(st, explicit)


def budget_line(explicit=None):
    """几行状态，进任务日志开头，让用户一眼看到「我是谁」和今天还剩多少额度。

    ⚠️ 第一行是**身份**（哪个桶）。多份登录态/多个窗口同时用的时候，
    任务日志里没有这张脸，出了事根本分不清是哪个身份撞的。
    """
    if _disabled():
        return "[guard] 已关闭（KS_GUARD_OFF=1）—— 预算与冷却都不生效"
    st = load(explicit)
    ccap = _cap("KS_MAX_COLLECT_PER_DAY", DEFAULT_MAX_COLLECT_PER_DAY)
    dcap = _cap("KS_MAX_DETAIL_PER_DAY", DEFAULT_MAX_DETAIL_PER_DAY)
    cused = int(st.get("collect_used") or 0)
    dused = int(st.get("detail_used") or 0)
    line = ("[guard] 今日额度：收集 %d/%d、详情 %d/%d"
            % (cused, ccap, dused, dcap))
    if int(st.get("block_count") or 0):
        line += " | 已连续撞墙 %d 次" % st["block_count"]
    line += run_holder_line(explicit)
    return "\n".join(x for x in (bucket_line(explicit), line) if x)


def budget_exhausted_message(explicit=None):
    st = load(explicit)
    ccap = _cap("KS_MAX_COLLECT_PER_DAY", DEFAULT_MAX_COLLECT_PER_DAY)
    dcap = _cap("KS_MAX_DETAIL_PER_DAY", DEFAULT_MAX_DETAIL_PER_DAY)
    msg = ("[guard] 今日额度已用完（收集上限 %d、详情上限 %d）。"
           "这是**故意**的：唯一真正触发快手风控的动作就是「一口气要几千个」，"
           "所以这里把动作拆到多天。进度在台账里，明天接着跑同一个模式即可。"
           "确实要今天继续，可显式放宽：KS_MAX_COLLECT_PER_DAY / KS_MAX_DETAIL_PER_DAY。"
           % (ccap, dcap))
    if _bucket != BUCKET_DEFAULT:
        msg += ("\n[guard] 注意：这是**桶 %s** 的额度用完了，不是「今天不能下了」——"
                "如果还有**另一个设备**的登录态（桶不同），它有自己的份额度，"
                "用 `KS_PROFILE` 指向那份 profile 开另一个窗口即可。"
                "同一台设备上再登几个号**不增加**额度（桶按 did 算）。" % _bucket)
    return msg


# ── 单实例租约 ────────────────────────────────────────────────────────────
# 为什么需要它（问「能不能多开几个窗口并行跑」时的答案就在这里）：
#
#   预算是「读-判断-写」三步，**不是原子的**。两个窗口同时启动，两个进程
#   都读到 `detail_used = 0`、都算出「还剩 400」→ 实际打出去 800 个。
#   守卫的全部意义就是不让那个动作发生，结果多开一个窗口就把它翻倍了。
#   而且两路并行打接口，恰好就是 09-25 撞出 208005 的那个形状。
#
#   所以批量入口必须先领租约。**单条不领** —— 和冷却一致，单条在冷却期实测能用，
#   拦它是添堵。
#
# ⚠️ 租约是**按桶**的（`run.<桶>.lock`），不是全局一把。因为桶 = 设备：
#   - 同一个设备上的两个窗口 → 同一个桶 → 抢同一把锁 → 第二个被拦（**这才对**，
#     这两个窗口在平台眼里就是同一个身份，一起打只是让请求形态更激进）；
#   - 两台设备（两份 profile）→ 不同桶 → 各拿各的锁 → **互不干扰**。
#   一把全局锁会把后一种情况也误拦，那样「多个 ck」根本没法用。
#
# 判活只靠**心跳时间戳**，绝不查 pid：
#   Windows 上 `os.kill(pid, 0)` 不是「探活」，它会真的把目标进程 TerminateProcess
#   掉。一个探活函数把对方杀了，没有比这更糟的 bug。
#   心跳方案还有个好处：进程被 `taskkill /F`（atexit 不执行）留下的租约会在
#   `RUN_STALE_SEC` 之后自动作废，不会把用户永久卡在「已有一个任务在跑」。

_RUN_NAME = "run.lock"
RUN_STALE_SEC = 180     # 心跳超过这么久没刷新 → 视为持有者已死，租约作废
_RUN_BEAT_SEC = 30      # 心跳间隔（留 6 次余量，不跟 RUN_STALE_SEC 贴太近）

_bucket = BUCKET_DEFAULT   # 本进程的风控桶；由 set_identity() 设定
_held_token = None      # 本进程持有/借用的 token；None = 没持有
_held_dir = None        # 持有时的落点（心跳线程要用 —— 它拿不到调用方的 explicit）
_inherited = False      # True = 从上层进程借来的（只借用，不释放）
_beat_thread = None


def run_path(explicit=None, bucket=None):
    b = _bucket_of(bucket)
    name = _RUN_NAME if b == BUCKET_DEFAULT else "run.%s.lock" % b
    return os.path.join(out_dir(explicit), name)


def _new_token():
    # pid + 随机后缀：光用 pid 会在 pid 复用（关机重启后）时误判成「自己的租约」
    return "%d-%s" % (os.getpid(), uuid.uuid4().hex[:8])


def _read_run(explicit=None):
    try:
        with open(run_path(explicit), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _write_run(d, explicit=None):
    try:
        p = run_path(explicit)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def run_holder(explicit=None, stale_sec=RUN_STALE_SEC, now=None):
    """有**活着的**批量进程在跑吗？有就返回它的信息（dict），没有返回 None。

    过期租约一律当「没有」—— 所以本函数**绝不会**因为残留文件把用户卡死。
    """
    d = _read_run(explicit)
    if not d:
        return None
    now = now if now is not None else time.time()
    try:
        if now - float(d.get("beat") or 0) < float(stale_sec):
            return d
    except Exception:
        pass
    return None


def _multi_allowed():
    return (os.environ.get("KS_ALLOW_MULTI") or "").strip() not in ("", "0", "false", "False")


def _beat(explicit=None):
    """刷新心跳。只在自己真的持有、且盘上还是自己那个 token 时才写。"""
    global _held_token
    try:
        if not _held_token:
            return
        # 后台线程调进来时拿不到调用方的 explicit，用持有时的落点补上
        # （否则会把心跳刷到默认锚点、而真正的租约在别处悄悄过期）
        if explicit is None:
            explicit = _held_dir
        d = _read_run(explicit)
        if not d or str(d.get("token") or "") != _held_token:
            # 我们的心跳曾经超时、租约已被别人接管 → 停止冒充持有者，不再覆盖
            _held_token = None
            return
        d["beat"] = time.time()
        _write_run(d, explicit)
    except Exception:
        pass


def _start_beating():
    """后台守护线程刷心跳。

    为什么必须另起线程而不是「在每个检查点顺手刷一下」：下载阶段
    （几百 MB、几十分钟）里主线程一直闷在 `subprocess.run` 上，
    没有一个自然检查点，租约会在跑得最久的时候过期。
    """
    global _beat_thread
    if _beat_thread is not None:
        return

    def _loop():
        while True:
            time.sleep(_RUN_BEAT_SEC)
            _beat(None)          # 落点由 `_held_dir` 补

    t = threading.Thread(target=_loop, name="ks_guard_beat", daemon=True)
    _beat_thread = t
    t.start()


def current_token():
    """本进程手里的租约 token（给上层进程转发给子进程用）。"""
    return _held_token


def acquire_run(mode="batch", explicit=None, stale_sec=RUN_STALE_SEC):
    """申请「批量独占」。返回 `(是否放行, 占用者信息)`。

    **任何异常一律放行** —— 守卫是保护，不是依赖；它坏掉时下载照跑。

    放行的情况：
      1. `KS_GUARD_OFF=1` 或 `KS_ALLOW_MULTI=1`（显式开口）；
      2. 自己已持有（幂等，重入只是刷一下心跳）；
      3. 环境里有 `KS_RUN_TOKEN` —— 说明自己是上层进程用 `subprocess` 拉起来的
         子进程（`kuaishou_headless.py --resume` 就是），租约归上层，子进程借用；
      4. 盘上没有活着的持有者（含租约已过期、文件损坏）。
    否则拒绝，并把占用者的 pid/mode/开始时间交回去，好让日志说清「谁在跑」。
    """
    global _held_token, _held_dir, _inherited
    try:
        if _disabled() or _multi_allowed():
            return True, None
        if _held_token:                                   # ② 幂等
            _beat(explicit)
            return True, None
        if (os.environ.get("KS_RUN_TOKEN") or "").strip():  # ③ 子进程借用
            _inherited = True
            return True, None
        holder = run_holder(explicit, stale_sec=stale_sec)  # ④ 有人活着 → 拒绝
        if holder:
            return False, holder
        tok = _new_token()
        now = time.time()
        if not _write_run({"token": tok, "pid": os.getpid(), "mode": str(mode),
                           "started_at": now, "beat": now}, explicit):
            return True, None                             # 写不进去 = 守卫失效 → 放行
        _held_token = tok
        _held_dir = explicit
        _start_beating()
        atexit.register(release_run)
        return True, None
    except Exception:
        return True, None


def release_run(explicit=None):
    """释放租约。只删「还是自己那份」的文件；借来的（`_inherited`）不动。"""
    global _held_token, _held_dir
    try:
        if _inherited or not _held_token:
            return
        if explicit is None:
            explicit = _held_dir
        d = _read_run(explicit)
        if d and str(d.get("token") or "") == _held_token:
            try:
                os.remove(run_path(explicit))
            except Exception:
                pass
        _held_token = None
        _held_dir = None
    except Exception:
        pass


def conflict_message(holder=None, explicit=None):
    """已经有一个批量任务在跑时的说明。要同时给出**原因**和**出路**。"""
    who = ""
    if isinstance(holder, dict):
        try:
            mins = int((time.time() - float(holder.get("started_at") or 0)) // 60)
            who = "（PID %s 的「%s」，已跑 %d 分钟）" % (holder.get("pid"),
                                                    holder.get("mode") or "batch", mins)
        except Exception:
            who = ""
    msg = ("[guard] 桶 %s 已经有一个快手批量任务在跑%s —— "
           "同一个桶同一时刻只跑一个。\n"
           "[guard] 原因不是保守，是必要：预算是「读-判断-写」，同一桶的两个进程会\n"
           "[guard] **各领一份**（你看到的是 400，实际打出去 800）；而且两路并行打接口\n"
           "[guard] 正是 2026-09-25 撞出 status_208005 的那个形状。\n"
           "[guard] 单条模式不受这条限制，现在就能用。\n"
           "[guard] 想再多开一路，唯一有意义的做法是**换一个桶**（= 换台设备）：\n"
           "[guard] 同一台设备上再登几个号没用 —— 桶按 did（设备）算，那是同一个桶。\n"
           "[guard] 确实要让同一桶并行（你清楚自己在承担什么）：设 KS_ALLOW_MULTI=1。"
           % (_bucket, who))
    return msg


def run_holder_line(explicit=None):
    """一行租约状态，进预算行，让用户知道是不是有别的窗口在跑。"""
    # 自己上层进程的租约不算「别人在跑」——否则 resume 的子进程会把自己的
    # 父进程报成竞争对手，日志自相矛盾。
    if _inherited or (os.environ.get("KS_RUN_TOKEN") or "").strip():
        return ""
    h = run_holder(explicit)
    if not h:
        return ""
    try:
        mins = int((time.time() - float(h.get("started_at") or 0)) // 60)
    except Exception:
        mins = 0
    return " | 另一个批量任务在跑（PID %s，%d 分钟）" % (h.get("pid"), mins)
