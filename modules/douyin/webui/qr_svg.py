# -*- coding: utf-8 -*-
"""二维码 → SVG（纯标准库实现，用于「手机下载」页把局域网地址递给手机）

为什么自己写一个，而不是 pip 装 qrcode / segno
------------------------------------------------
工具箱的硬约束是「整个目录拷走即可用，用户不需要 pip install」。二维码库
（qrcode / segno / Pillow）在三个自带 venv 里都没有，为了一个「把网址显示成
图片」的小功能去改运行时依赖清单，收益和代价不成比例。所以这里自己实现一个
最小的编码器：只做字节模式、版本 1–10、纠错等级 L/M —— 覆盖 URL 场景绰绰有余
（v10-M 已能装 213 字节），代码不到 300 行，零依赖。

正确性不是「看起来像二维码」就算数，而是与参考实现（PyPI `qrcode` 8.2）
**逐模块严格比对**过：同一份数据、同一纠错等级下，本文件生成的矩阵与参考实现
（含自动选择的掩码号）完全一致，覆盖全部 10 个版本 × 2 个纠错等级 × 多种长度。
校验脚本见 tests/test_qr_svg.py（需要隔离环境里的 qrcode 作对照，不参与线上运行）。

对外只有一个函数：
    qr_svg(text, ec="M", scale=8, border=2, ...) -> str   # 一段 <svg> 字符串
"""
# ---------- GF(256)：本原多项式 0x11D ----------
_EXP = [0] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _rs_divisor(degree):
    """生成 degree 次的 RS 生成多项式（首一，系数按高次到低次）。"""
    result = [0] * degree
    result[degree - 1] = 1
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    """多项式除法取余 —— 余数即该块的纠错码字。"""
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        if factor != 0:
            for i in range(len(divisor)):
                result[i] ^= _mul(divisor[i], factor)
    return result


# ---------- 纠错分块表（版本 1–10；数值来自 ISO/IEC 18004 表 9） ----------
# 每项 = (块数1, 总码字1, 数据码字1[, 块数2, 总码字2, 数据码字2])
# 纠错码字/块 = 总码字 - 数据码字，两块同为 (总-数据)。
_RS = {
    "L": {
        1: (1, 26, 19), 2: (1, 44, 34), 3: (1, 70, 55), 4: (1, 100, 80),
        5: (1, 134, 108), 6: (2, 86, 68), 7: (2, 98, 78), 8: (2, 121, 97),
        9: (2, 146, 116), 10: (2, 86, 68, 2, 87, 69),
    },
    "M": {
        1: (1, 26, 16), 2: (1, 44, 28), 3: (1, 70, 44), 4: (2, 50, 32),
        5: (2, 67, 43), 6: (4, 43, 27), 7: (4, 49, 31), 8: (2, 60, 38, 2, 61, 39),
        9: (3, 58, 36, 2, 59, 37), 10: (4, 69, 43, 1, 70, 44),
    },
}

# 格式信息里的纠错等级编码（2 bit）：L=01 M=00 Q=11 H=10
_EC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}

# 校正图形中心坐标（版本 1–10）
_ALIGN = {
    1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30),
    6: (6, 34), 7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}

_MAX_VERSION = 10


def _data_codewords(version, ec):
    e = _RS[ec][version]
    if len(e) == 3:
        return e[0] * e[2]
    return e[0] * e[2] + e[3] * e[5]


def _pick_version(nbytes, ec):
    for v in range(1, _MAX_VERSION + 1):
        cnt_bits = 8 if v <= 9 else 16
        if 4 + cnt_bits + 8 * nbytes <= _data_codewords(v, ec) * 8:
            return v
    raise ValueError("内容过长：%d 字节超出 v%d-%s 容量" % (nbytes, _MAX_VERSION, ec))


def _build_codewords(data, version, ec):
    """字节模式：数据位 → 若干数据码字 → 分块 RS 纠错 → 交织成最终码字序列。"""
    cap = _data_codewords(version, ec)
    cnt_bits = 8 if version <= 9 else 16

    bits = [0, 1, 0, 0]                                   # 模式指示符 0100 = 字节
    for i in range(cnt_bits - 1, -1, -1):                 # 字符计数
        bits.append((len(data) >> i) & 1)
    for b in data:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)

    room = cap * 8 - len(bits)
    bits.extend([0] * min(4, room))                       # 终止符
    while len(bits) % 8:                                  # 补齐到字节边界
        bits.append(0)

    cws = []
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        cws.append(v)
    for i in range(cap - len(cws)):                       # 填充码字 0xEC / 0x11 交替
        cws.append(0xEC if i % 2 == 0 else 0x11)

    # 分块
    e = _RS[ec][version]
    if len(e) == 3:
        groups = [(e[0], e[2])]
        n_ec = e[1] - e[2]
    else:
        groups = [(e[0], e[2]), (e[3], e[5])]
        n_ec = e[1] - e[2]
        assert n_ec == e[4] - e[5]

    divisor = _rs_divisor(n_ec)
    blocks = []
    pos = 0
    for count, dlen in groups:
        for _ in range(count):
            chunk = cws[pos:pos + dlen]
            pos += dlen
            blocks.append(chunk)
    assert pos == cap, "分块拼接后应恰好用完所有数据码字"

    eccs = [_rs_remainder(b, divisor) for b in blocks]

    out = []
    max_d = max(len(b) for b in blocks)
    for i in range(max_d):                                # 数据码字按列交织
        for b in blocks:
            if i < len(b):
                out.append(b[i])
    for i in range(n_ec):                                 # 纠错码字按列交织
        for e_ in eccs:
            out.append(e_[i])
    return out


# ---------- 掩码与罚分（与 ISO/IEC 18004 一致） ----------
def _mask_fn(pattern):
    return (
        lambda i, j: (i + j) % 2 == 0,
        lambda i, j: i % 2 == 0,
        lambda i, j: j % 3 == 0,
        lambda i, j: (i + j) % 3 == 0,
        lambda i, j: (i // 2 + j // 3) % 2 == 0,
        lambda i, j: (i * j) % 2 + (i * j) % 3 == 0,
        lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
        lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
    )[pattern]


def _bch_format(ec, mask):
    """15 位格式信息：5 位数据 + 10 位 BCH(15,5)，再异或 0x5412。"""
    v = (_EC_BITS[ec] << 3) | mask
    d = v << 10
    while d.bit_length() > 10:
        d ^= 0x537 << (d.bit_length() - 11)
    return ((v << 10) | d) ^ 0x5412


def _bch_version(version):
    """18 位版本信息（仅 v7+）：6 位数据 + 12 位 BCH(18,6)。"""
    d = version << 12
    while d.bit_length() > 12:
        d ^= 0x1F25 << (d.bit_length() - 13)
    return (version << 12) | d


def _blank(version):
    """功能图形（探测/校正/定位）与保留区；数据区先留 None。"""
    n = version * 4 + 17
    m = [[None] * n for _ in range(n)]
    res = [[False] * n for _ in range(n)]

    # 三个探测图形 + 分隔符（-1..7 的 9×9 邻域，越界裁掉）
    for r0, c0 in ((0, 0), (0, n - 7), (n - 7, 0)):
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if not (0 <= r < n and 0 <= c < n):
                    continue
                res[r][c] = True
                if 0 <= dr <= 6 and 0 <= dc <= 6:
                    # ⚠️ 两个坐标必须都落在 0..6 才算探测图形本体。漏掉范围判断
                    # 会把 -1 那圈分隔符按「第 0 行/列」涂成深色（真踩过）。
                    m[r][c] = ((dr in (0, 6) and 0 <= dc <= 6)
                               or (dc in (0, 6) and 0 <= dr <= 6)
                               or (2 <= dr <= 4 and 2 <= dc <= 4))
                else:
                    # 分隔符（-1 那圈）：必须显式落成浅色，不能留 None。
                    # None 只表示「还没写」= 可写数据区，留着会被数据填进来。
                    m[r][c] = False

    # 校正图形（中心落在已保留区 = 与探测图形重叠，跳过）
    pos = _ALIGN[version]
    for r in pos:
        for c in pos:
            if res[r][c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    m[r + dr][c + dc] = (dr in (-2, 2) or dc in (-2, 2)
                                         or (dr == 0 and dc == 0))
                    res[r + dr][c + dc] = True

    # 定时图形
    for i in range(8, n - 8):
        m[6][i] = i % 2 == 0
        m[i][6] = i % 2 == 0
        res[6][i] = True
        res[i][6] = True

    # 格式信息保留区（含暗模块位）
    for i in range(9):
        res[8][i] = True
        res[i][8] = True
    for i in range(n - 8, n):
        res[8][i] = True
        res[i][8] = True
    res[n - 8][8] = True

    # 版本信息保留区（v7+）
    if version >= 7:
        for i in range(18):
            res[i // 3][i % 3 + n - 11] = True
            res[i % 3 + n - 11][i // 3] = True
    return m, res, n


def _draw_format(m, n, ec, mask, test):
    bits = _bch_format(ec, mask)
    for i in range(15):
        mod = (not test) and ((bits >> i) & 1) == 1
        # 竖向副本
        if i < 6:
            m[i][8] = mod
        elif i < 8:
            m[i + 1][8] = mod
        else:
            m[n - 15 + i][8] = mod
        # 横向副本
        if i < 8:
            m[8][n - i - 1] = mod
        elif i < 9:
            m[8][15 - i] = mod
        else:
            m[8][15 - i - 1] = mod
    m[n - 8][8] = not test          # 暗模块：恒为深色（自检态下为浅，参与罚分）


def _draw_version(m, n, version, test):
    bits = _bch_version(version)
    for i in range(18):
        mod = (not test) and ((bits >> i) & 1) == 1
        m[i // 3][i % 3 + n - 11] = mod
        m[i % 3 + n - 11][i // 3] = mod


def _place_data(m, res, n, codewords, mask):
    """数据按之字形从右下角向上蛇形填充（跳过功能图形）。"""
    fn = _mask_fn(mask)
    total = len(codewords) * 8
    idx = 0
    row = n - 1
    inc = -1
    for col in range(n - 1, 0, -2):
        if col <= 6:
            col -= 1
        while True:
            for c in (col, col - 1):
                if res[row][c]:
                    continue
                dark = False
                if idx < total:
                    byte = codewords[idx >> 3]
                    dark = ((byte >> (7 - (idx & 7))) & 1) == 1
                idx += 1
                m[row][c] = dark != fn(row, c)      # 掩码：异或
            row += inc
            if row < 0 or row >= n:
                row -= inc
                inc = -inc
                break


def _penalty(m, n):
    """四个罚分规则；返回值越小越好。"""
    score = 0

    # 规则 1：行/列上连续同色 ≥5
    container = [0] * (n + 1)
    for line in list(m) + [[m[r][c] for r in range(n)] for c in range(n)]:
        prev = line[0]
        length = 0
        for cell in line:
            if cell == prev:
                length += 1
            else:
                if length >= 5:
                    container[length] += 1
                length = 1
                prev = cell
        if length >= 5:
            container[length] += 1
    for k in range(5, n + 1):
        score += container[k] * (k - 2)

    # 规则 2：2×2 同色块
    for r in range(n - 1):
        for c in range(n - 1):
            v = m[r][c]
            if v == m[r][c + 1] and v == m[r + 1][c] and v == m[r + 1][c + 1]:
                score += 3

    # 规则 3：1:1:3:1:1 比例（两种朝向各 40 分）
    p1 = (True, False, True, True, True, False, True, False, False, False, False)
    p2 = (False, False, False, False, True, False, True, True, True, False, True)
    for line in list(m) + [[m[r][c] for r in range(n)] for c in range(n)]:
        for i in range(n - 10):
            w = tuple(line[i:i + 11])
            if w == p1 or w == p2:
                score += 40

    # 规则 4：深色占比偏离 50%
    dark = sum(1 for row in m for v in row if v)
    score += int(abs(dark * 100.0 / (n * n) - 50) / 5) * 10
    return score


def _matrix(text, ec="M", mask=None):
    """返回模块矩阵（True=深色）。mask=None 时按标准罚分自动选最优。"""
    if ec not in _RS:
        raise ValueError("纠错等级只支持 L / M")
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    version = _pick_version(len(data), ec)
    codewords = _build_codewords(data, version, ec)
    m0, res, n = _blank(version)

    if mask is not None:
        chosen = mask
    else:
        # 选掩码时用「自检态」：格式/版本信息位与暗模块全按浅色算 —— 与参考实现
        # 的口径一致，否则同一份数据会挑出不同的掩码，矩阵对不上。
        best, best_score = 0, None
        for i in range(8):
            cand = [row[:] for row in m0]
            _draw_format(cand, n, ec, i, True)
            if version >= 7:
                _draw_version(cand, n, version, True)
            _place_data(cand, res, n, codewords, i)
            s = _penalty(cand, n)
            if best_score is None or s < best_score:
                best, best_score = i, s
        chosen = best

    m = [row[:] for row in m0]
    _draw_format(m, n, ec, chosen, False)
    if version >= 7:
        _draw_version(m, n, version, False)
    _place_data(m, res, n, codewords, chosen)
    return m, chosen


def matrix(text, ec="M"):
    """对外暴露的矩阵接口（测试用）：返回 (矩阵, 掩码号)。"""
    return _matrix(text, ec)


def qr_svg(text, ec="M", scale=8, border=2, dark="#1B2025", light="#FFFFFF"):
    """把文本编成二维码并渲染为 SVG 字符串（深色模块合并进一条 path，体积小）。"""
    m, _ = _matrix(text, ec)
    n = len(m)
    side = (n + border * 2) * scale
    parts = []
    for r, row in enumerate(m):
        c = 0
        while c < n:
            if not row[c]:
                c += 1
                continue
            start = c
            while c < n and row[c]:
                c += 1
            x = (border + start) * scale
            y = (border + r) * scale
            w = (c - start) * scale
            parts.append("M%d %dh%dv%dh-%dz" % (x, y, w, scale, w))
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
        'viewBox="0 0 %d %d" shape-rendering="crispEdges" role="img" '
        'aria-label="QR">'
        '<rect width="100%%" height="100%%" fill="%s"/>'
        '<path d="%s" fill="%s"/></svg>'
        % (side, side, side, side, light, "".join(parts), dark)
    )


if __name__ == "__main__":       # 手工冒烟：python qr_svg.py <文本> [scale]
    import sys
    argv = sys.argv[1:]
    txt = argv[0] if argv else "http://192.168.1.7:8520"
    sc = int(argv[1]) if len(argv) > 1 else 8
    print(qr_svg(txt, scale=sc))
