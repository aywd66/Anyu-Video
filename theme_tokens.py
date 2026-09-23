# -*- coding: utf-8 -*-
"""theme_tokens.py — 桌面端（tkinter）设计令牌。自动生成 —— 请勿手改。

由 tools/gen_tokens.py 从 docs/design-tokens.json 生成。
改令牌 → 编辑 design-tokens.json，再跑 python tools/gen_tokens.py --write
源指纹 → 174474e748e8

为什么 tkinter 端也要走生成：在此之前，toolbox.py 里的颜色是手抄的一份，
注释写着「与 WebUI tokens.css 同源」，但没有任何机制保证它们真的一致。
两份手抄的颜色迟早会分叉，而且不会有人发现。
"""

# 颜色
BG             = '#FAF9F6'   # 窗口底色
CARD           = '#FDFDFC'   # 按钮/卡片白
SUNKEN         = '#F2F1ED'   # 凹陷区（日志窗格等）
PANEL2         = '#F5F4EF'   # 次级底色
LINE           = '#DFDED9'   # 装饰分隔线
LINE_MID       = '#C9C7C1'   # 强调边线（装饰性）：按钮/分段容器/卡片 hover
LINE_STRONG    = '#92918B'   # 控件轮廓：输入框/下拉/开关（须达 WCAG 1.4.11 的 3:1）
HEADING        = '#1B2025'   # 标题文字
TEXT           = '#4E5359'   # 正文文字
MUTED          = '#6B6E73'   # 辅助文字
PRIMARY        = '#006E6F'   # 品牌主色
PRIMARY_HOVER  = '#005E5F'   # 主色 hover
PRIMARY_DARK   = '#004546'   # 主色深调（用于浅底上的文字）
PRIMARY_SOFT   = '#CFF6F5'   # 主色浅底（选中态）
PRIMARY_LINE   = '#A1D6D6'   # 主色描边
OK             = '#26753E'   # 成功
OK_SOFT        = '#D7F4DC'   # 成功浅底
WARN           = '#9E5A00'   # 警示
WARN_SOFT      = '#FFE9C8'   # 警示浅底
DANGER         = '#B53532'   # 错误
DANGER_SOFT    = '#FFE7E2'   # 错误浅底
INFO           = '#3B6AA6'   # 信息
INFO_SOFT      = '#DCEFFF'   # 信息浅底

# 字体（tkinter 只认字体名，不认 font-family 列表，取第一顺位）
FONT_UI = 'Microsoft YaHei'
FONT_READ = 'Consolas'

# 字号
FS_TITLE = 16
FS_SUB = 9
FS_BODY = 10
FS_LABEL = 9

# 间距 / 圆角（tkinter 用不上圆角，间距用于 padx/pady）
SP_1 = 4
SP_2 = 8
SP_3 = 12
SP_4 = 16
SP_6 = 32

# 动效（tkinter 不用缓动曲线，保留时长口径统一）
DUR_FAST_MS = 120


def as_dict():
    """给需要整份取用的场合（例如批量 setattr）。"""
    return {k: v for k, v in globals().items()
            if k.isupper() and not k.startswith('_')}
