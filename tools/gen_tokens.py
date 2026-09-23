# -*- coding: utf-8 -*-
"""生成设计令牌产物 —— 让 Web 工作台与桌面启动器共用同一份设计语言。

用法
----
    python tools/gen_tokens.py --write    重新生成两个产物
    python tools/gen_tokens.py --check    只校验产物是否与源文件漂移（退出码 1 = 有漂移）

为什么要有这个脚本
------------------
改这个项目的人（包括未来的我）很容易犯同一个错：在 Web 端的 tokens.css 里
改了主色，忘了 tkinter 端还有一份手抄的 `C_PRIMARY`。两边开始慢慢分叉，
半年后没人说得清哪个才是"对"的色。

所以把令牌抽成一份 JSON，两端产物都从它生成：
    docs/design-tokens.json          ← 唯一权威，人改这一个文件
        ├── assets/css/tokens.css    ← Web 端（自动生成）
        └── theme_tokens.py          ← 桌面端（自动生成）

产物的头两行写着"自动生成"和源文件指纹；--check 会重算一遍比对，
任何手改产物都会被抓出来。

间距与圆角为什么和设计稿不完全一样
----------------------------------
设计稿是空手画的，可以任选刻度；这套 CSS 已经有 1600 行在用现有的
`--sp-*` 与 `--r-*`，贸然平移刻度会让整站布局位移。
所以这里保留既有刻度（本来就是 4 的倍数，符合 4pt 基准），
只把「颜色 / 字体 / 圆角 / 阴影 / 动效」这些**换肤该换的东西**换成 V2 取值。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "docs", "design-tokens.json")
OUT_CSS = os.path.join(ROOT, "modules", "douyin", "webui", "assets", "css", "tokens.css")
OUT_PY = os.path.join(ROOT, "theme_tokens.py")

BANNER = "自动生成 —— 请勿手改"


# ============================================================================
# 别名层：把 V2 的新名字映射到两端既有的历史名字，避免全站重命名
# ============================================================================
def css_aliases(hexmap: dict) -> list[tuple[str, str]]:
    """(别名, 值表达式) —— 值可能是 hex，也可能是 var(--新名)。"""
    return [
        # ---- 套件前缀（方案C 时代留下的 --bb-*，被大量既有 CSS 直接引用）----
        ("--bb-bg", "var(--paper)"),
        ("--bb-card", "var(--surface)"),
        ("--bb-input-bg", "var(--paper-2)"),
        ("--bb-border", "var(--line)"),
        ("--bb-border-soft", "var(--surface-sunken)"),
        ("--bb-hover-teal", "var(--brand-line)"),
        ("--bb-primary", "var(--brand)"),
        ("--bb-primary-dark", "var(--brand-hover)"),
        ("--bb-primary-light", "var(--brand-soft)"),
        ("--bb-primary-grad", "linear-gradient(135deg,var(--brand),#2E8B8C)"),
        ("--bb-accent", "#B4632A"),
        ("--bb-accent-dark", "#8F4C1D"),
        ("--bb-accent-soft", "#FBE9DA"),
        ("--bb-success", "var(--ok)"),
        ("--bb-success-soft", "var(--ok-soft)"),
        ("--bb-danger", "var(--danger)"),
        ("--bb-danger-soft", "var(--danger-soft)"),
        ("--bb-danger-border", "#F0C4BE"),
        ("--bb-warning", "var(--warn)"),
        ("--bb-warning-soft", "var(--warn-soft)"),
        ("--bb-text", "var(--ink-2)"),
        ("--bb-text-strong", "var(--ink)"),
        ("--bb-heading", "var(--ink)"),
        ("--bb-text-secondary", "var(--ink-3)"),
        ("--bb-faint", "var(--ink-3)"),
        ("--bb-invert", "#FFFFFF"),
        ("--bb-font-sans", "var(--font-ui)"),
        ("--bb-font-num", "var(--font-read)"),
        ("--bb-font-mono", "var(--font-read)"),
        ("--bb-r-card", "var(--r-card)"),
        ("--bb-r-ctl", "var(--r-input)"),
        ("--bb-r-sm", "var(--r-small)"),
        ("--bb-r-xs", "6px"),
        ("--bb-r-pill", "var(--r-pill)"),
        ("--bb-shadow-card", "var(--sh-2)"),
        ("--bb-shadow-card-hover", "var(--sh-3)"),
        ("--bb-shadow-bar", "var(--sh-1)"),
        ("--bb-shadow-primary", "0 2px 8px rgba(0,110,111,.20)"),
        ("--bb-shadow-primary-hover", "0 4px 14px rgba(0,110,111,.28)"),
        ("--bb-shadow-focus", "0 0 0 3px var(--brand-soft)"),
        ("--bb-topbar-h", "60px"),
        ("--bb-content-max", "var(--content-max)"),
        ("--bb-z-nav", "var(--z-sticky)"),
        ("--bb-z-modal", "var(--z-pop)"),
        ("--bb-z-toast", "var(--z-toast)"),
        ("--bb-t", "var(--dur)"),
        # ---- 更早一层的历史别名（DYSide 时代），仍然有引用 ----
        ("--bg", "var(--paper)"),
        ("--bg-soft", "var(--paper-2)"),
        ("--panel", "var(--surface)"),
        ("--panel-2", "var(--surface-sunken)"),
        ("--line-soft", "var(--surface-sunken)"),
        ("--cyan", "var(--brand)"),
        ("--cyan-dim", "var(--brand-soft)"),
        ("--green", "var(--ok)"),
        ("--green-soft", "var(--ok-soft)"),
        ("--red", "var(--danger)"),
        ("--red-soft", "var(--danger-soft)"),
        ("--amber", "var(--warn)"),
        ("--amber-soft", "var(--warn-soft)"),
        ("--text", "var(--ink-2)"),
        ("--muted", "var(--ink-3)"),
        ("--faint", "var(--ink-3)"),
        ("--invert", "#FFFFFF"),
        ("--font-sans", "var(--font-ui)"),
        ("--font-mono", "var(--font-read)"),
        ("--glow-pink", "0 0 0 3px rgba(124,58,237,.14)"),
        ("--glow-cyan", "0 0 0 3px var(--brand-soft)"),
        ("--t-fast", "var(--dur-fast) var(--ease)"),
        ("--t-med", "var(--dur-slow) var(--ease)"),
    ]


# tkinter 端常量：键名保持与 toolbox.py 里的 C_* 属性一致，减少改动面
PY_MAP = [
    ("BG", "paper", "窗口底色"),
    ("CARD", "surface", "按钮/卡片白"),
    ("SUNKEN", "surface-sunken", "凹陷区（日志窗格等）"),
    ("PANEL2", "paper-2", "次级底色"),
    ("LINE", "line", "装饰分隔线"),
    ("LINE_MID", "line-mid", "强调边线（装饰性）：按钮/分段容器/卡片 hover"),
    ("LINE_STRONG", "line-strong", "控件轮廓：输入框/下拉/开关（须达 WCAG 1.4.11 的 3:1）"),
    ("HEADING", "ink", "标题文字"),
    ("TEXT", "ink-2", "正文文字"),
    ("MUTED", "ink-3", "辅助文字"),
    ("PRIMARY", "brand", "品牌主色"),
    ("PRIMARY_HOVER", "brand-hover", "主色 hover"),
    ("PRIMARY_DARK", "brand-ink", "主色深调（用于浅底上的文字）"),
    ("PRIMARY_SOFT", "brand-soft", "主色浅底（选中态）"),
    ("PRIMARY_LINE", "brand-line", "主色描边"),
    ("OK", "ok", "成功"),
    ("OK_SOFT", "ok-soft", "成功浅底"),
    ("WARN", "warn", "警示"),
    ("WARN_SOFT", "warn-soft", "警示浅底"),
    ("DANGER", "danger", "错误"),
    ("DANGER_SOFT", "danger-soft", "错误浅底"),
    ("INFO", "info", "信息"),
    ("INFO_SOFT", "info-soft", "信息浅底"),
]


# ============================================================================
# 生成
# ============================================================================
def build_css(doc: dict, fingerprint: str) -> str:
    col = doc["color"]
    sp = doc["space"]          # 保留既有刻度（4 的倍数，符合 4pt 基准）
    md = doc["motion"]
    z = doc["z"]

    def c(k):
        return col[k]["hex"]

    L = []
    A = L.append
    A("/* " + "=" * 74)
    A(" * tokens.css — 设计令牌（全站唯一取色/取尺寸来源）")
    A(" * " + "-" * 72)
    A(" * ⚠️ 本文件由 tools/gen_tokens.py 从 docs/design-tokens.json 生成，%s。" % BANNER)
    A(" *    改令牌 → 编辑 design-tokens.json，再跑：")
    A(" *        python tools/gen_tokens.py --write")
    A(" *    查漂移 → python tools/gen_tokens.py --check（已接入工具箱自检）")
    A(" *    源指纹 → %s" % fingerprint)
    A(" * " + "-" * 72)
    A(" * 注意：间距与圆角保留既有刻度。既有 CSS 已有 1600+ 行在用这套变量，")
    A(" * 平移刻度会让整站布局位移 —— 而现有的 4/8/12/16/24/32/48/64 本来就是")
    A(" * 4pt 基准的倍数，所以无需改动。换肤换的是颜色、字体、圆角、阴影、动效。")
    A(" * " + "=" * 74 + " */")
    A(":root{")
    A("  /* ---------------- 中性色（底微暖 hue 95 / 字微冷 hue 250，冷暖对撞比纯灰更有品质） ---------------- */")
    A("  --paper:%s;" % c("paper"))
    A("  --paper-2:%s;" % c("paper-2"))
    A("  --surface:%s;" % c("surface"))
    A("  --surface-sunken:%s;" % c("surface-sunken"))
    A("  --line:%s;      /* 装饰分隔线，不承担识别责任 */" % c("line"))
    A("  --line-mid:%s;   /* 强调边线（装饰性）：按钮、分段容器、卡片 hover */" % c("line-mid"))
    A("  --line-strong:%s; /* 控件轮廓：输入框/下拉/开关，WCAG 1.4.11 要求 3:1 */" % c("line-strong"))
    A("  --ink:%s;" % c("ink"))
    A("  --ink-2:%s;" % c("ink-2"))
    A("  --ink-3:%s;" % c("ink-3"))
    A("")
    A("  /* ---------------- 品牌色（青 hue 195；刻意避开方案C 的 178，防止「新方案看着还是老方案」） ---------------- */")
    A("  --brand:%s;" % c("brand"))
    A("  --brand-hover:%s;" % c("brand-hover"))
    A("  --brand-ink:%s;   /* 浅底上的品牌色文字 */" % c("brand-ink"))
    A("  --brand-soft:%s;" % c("brand-soft"))
    A("  --brand-line:%s;" % c("brand-line"))
    A("")
    A("  /* ---------------- 语义色（色相互相拉开 ≥40°，浅底之间不会混淆） ---------------- */")
    A("  --ok:%s;      --ok-soft:%s;" % (c("ok"), c("ok-soft")))
    A("  --warn:%s;    --warn-soft:%s;" % (c("warn"), c("warn-soft")))
    A("  --danger:%s;  --danger-soft:%s;" % (c("danger"), c("danger-soft")))
    A("  --info:%s;    --info-soft:%s;" % (c("info"), c("info-soft")))
    A("")
    A("  /* ---------------- 平台标识色（不属于设计系统，随平台而变） ---------------- */")
    A("  --pink:#7C3AED;")
    A("  --pink-soft:rgba(124,58,237,.08);")
    A("")
    A("  /* ---------------- 字体：全系统栈，零外链 ---------------- */")
    A("  --font-ui:%s;" % doc["font"]["ui"])
    A("  --font-read:%s;" % doc["font"]["read"])
    A("")
    A("  /* ---------------- 字号阶梯 ---------------- */")
    A("  --fs-display:26px;")
    A("  --fs-h1:22px;")
    A("  --fs-h2:15px;")
    A("  --fs-body:14px;")
    A("  --fs-small:12.5px;")
    A("  --fs-label:11px;")
    A("")
    A("  /* ---------------- 间距（4pt 基准，保留既有刻度） ---------------- */")
    for k in ("1", "2", "3", "4", "5", "6", "7", "8"):
        A("  --sp-%s:%s;" % (k, sp[k]))
    A("")
    A("  /* ---------------- 圆角（比方案C 更克制：14→12 / 10→8） ---------------- */")
    A("  --r-card:12px;")
    A("  --r-input:8px;")
    A("  --r-small:6px;")
    A("  --r-pill:999px;")
    A("")
    A("  /* ---------------- 阴影（克制：看得清就是过重） ---------------- */")
    for k in ("1", "2", "3"):
        A("  --sh-%s:%s;" % (k, doc["shadow"][k]))
    A("")
    A("  /* ---------------- 动效（指数缓动，无回弹） ---------------- */")
    A("  --ease:%s;" % md["ease"])
    A("  --ease-out:%s;" % md["ease_out"])
    A("  --dur-fast:%s;" % md["dur_fast"])
    A("  --dur:%s;" % md["dur"])
    A("  --dur-slow:%s;" % md["dur_slow"])
    A("")
    A("  /* ---------------- 布局 ---------------- */")
    A("  --topbar-h:60px;")
    A("  --sidebar-w:232px;")
    A("  --content-max:1200px;")
    A("")
    A("  /* ---------------- z 轴语义（不用魔法数字） ---------------- */")
    A("  --z-sticky:%s;" % z["sticky"])
    A("  --z-pop:%s;" % z["pop"])
    A("  --z-toast:%s;" % z["toast"])
    A("")
    A("  /* ---------------- 兼容别名（映射到上方新名，勿删） ---------------- */")
    A("  /* 既有 CSS / JS 内联样式仍在引用这些历史名字。")
    A("     把它们指向新令牌，改一处即全站换肤，无需全站重命名。 */")
    for name, val in css_aliases(col):
        A("  %s:%s;" % (name, val))
    A("}")
    A("")
    return "\n".join(L)


def build_py(doc: dict, fingerprint: str) -> str:
    col = doc["color"]
    sp = doc["space"]
    md = doc["motion"]
    L = []
    A = L.append
    A("# -*- coding: utf-8 -*-")
    A('"""theme_tokens.py — 桌面端（tkinter）设计令牌。%s。' % BANNER)
    A("")
    A("由 tools/gen_tokens.py 从 docs/design-tokens.json 生成。")
    A("改令牌 → 编辑 design-tokens.json，再跑 python tools/gen_tokens.py --write")
    A("源指纹 → %s" % fingerprint)
    A("")
    A("为什么 tkinter 端也要走生成：在此之前，toolbox.py 里的颜色是手抄的一份，")
    A("注释写着「与 WebUI tokens.css 同源」，但没有任何机制保证它们真的一致。")
    A("两份手抄的颜色迟早会分叉，而且不会有人发现。")
    A('"""')
    A("")
    A("# 颜色")
    for key, tok, why in PY_MAP:
        A("%-14s = '%s'   # %s" % (key, col[tok]["hex"], why))
    A("")
    A("# 字体（tkinter 只认字体名，不认 font-family 列表，取第一顺位）")
    A("FONT_UI = 'Microsoft YaHei'")
    A("FONT_READ = 'Consolas'")
    A("")
    A("# 字号")
    A("FS_TITLE = 16")
    A("FS_SUB = 9")
    A("FS_BODY = 10")
    A("FS_LABEL = 9")
    A("")
    A("# 间距 / 圆角（tkinter 用不上圆角，间距用于 padx/pady）")
    A("SP_1 = %s" % sp["1"].replace("px", ""))
    A("SP_2 = %s" % sp["2"].replace("px", ""))
    A("SP_3 = %s" % sp["3"].replace("px", ""))
    A("SP_4 = %s" % sp["4"].replace("px", ""))
    A("SP_6 = %s" % sp["6"].replace("px", ""))
    A("")
    A("# 动效（tkinter 不用缓动曲线，保留时长口径统一）")
    A("DUR_FAST_MS = %d" % int(md["dur_fast"].replace("ms", "")))
    A("")
    A("")
    A("def as_dict():")
    A('    """给需要整份取用的场合（例如批量 setattr）。"""')
    A("    return {k: v for k, v in globals().items()")
    A("            if k.isupper() and not k.startswith('_')}")
    A("")
    return "\n".join(L)


# ============================================================================
# 入口
# ============================================================================
def main(argv):
    mode = argv[1] if len(argv) > 1 else "--check"
    if mode not in ("--write", "--check"):
        print(__doc__)
        return 2

    if not os.path.isfile(SRC):
        print("✘ 找不到令牌源：%s" % SRC)
        return 2
    raw = io.open(SRC, "rb").read()
    doc = json.loads(raw.decode("utf-8"))
    fp = hashlib.sha256(raw).hexdigest()[:12]

    targets = [(OUT_CSS, build_css(doc, fp)), (OUT_PY, build_py(doc, fp))]

    if mode == "--write":
        for path, content in targets:
            d = os.path.dirname(path)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            io.open(path, "w", encoding="utf-8", newline="\n").write(content)
            print("✔ 已生成 %s  (%d 字节)" % (os.path.relpath(path, ROOT), len(content.encode("utf-8"))))
        c = sum(1 for x in doc["contrast"] if not x["pass"])
        print("  对比度门禁 %d 组，不达标 %d 组" % (len(doc["contrast"]), c))
        return 0

    drift = []
    for path, content in targets:
        rel = os.path.relpath(path, ROOT)
        if not os.path.isfile(path):
            drift.append("%s 不存在" % rel)
            continue
        got = io.open(path, encoding="utf-8").read()
        if got != content:
            gl, wl = got.splitlines(), content.splitlines()
            first = next((i for i in range(max(len(gl), len(wl)))
                          if (gl[i] if i < len(gl) else None) != (wl[i] if i < len(wl) else None)), 0)
            drift.append("%s 与令牌源不一致（第 %d 行起）" % (rel, first + 1))
    if drift:
        print("✘ 令牌产物已漂移：")
        for d in drift:
            print("   -", d)
        print("  修复：python tools/gen_tokens.py --write")
        return 1
    print("✔ 令牌产物与 docs/design-tokens.json 一致（指纹 %s）" % fp)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
