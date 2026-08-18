#!/usr/bin/env python3
"""品牌图标资产生成器(issue #41)。

从单一几何源生成全套 Web/Favicon 资产:
  - icon-master.svg   可编辑矢量主文件(512 视野,渐变完整版)
  - favicon.svg       小尺寸优化版(64 视野,平涂,26/36px 标记与 SVG favicon 共用)
  - icon-{16,32,48,64,180,192,512,1024}.png(16/32 为像素级光学修正版,PIL 手绘)
  - apple-touch-icon.png(180,满铺不透明底)
  - favicon.ico(16+32+48)

用法:python3 generate.py [输出目录,默认 ../static/assets/brand]
"""
import sys
import os
import io
import cairosvg
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# 主文件几何(512 画布)。所有资产共用这一份坐标。
# ---------------------------------------------------------------------------
BODY = dict(x0=36, y0=36, x1=476, y1=476, r=76)          # 亮蓝圆角方形主体
ENC = dict(x0=62, y0=104, x1=296, y1=408, r=44)          # 深钴蓝围合区
NOTCH = dict(x_in=286, y_top=234, y_bot=278)              # 围合区右墙缺口(网关)
NODE_R = 23
NODES = [(114, 157), (114, 256), (114, 355), (428, 256)]  # 三内部节点 + 外部出口
MERGE = (245, 256)                                        # 三路汇聚点(网关前)
W_BRANCH, W_MAIN = 12, 20                                 # 支线 / 汇聚主线宽
CYAN = "#38CEF7"                                          # 门户冷青光沿

BLUE_A, BLUE_B = "#0259F7", "#1384FE"                     # 主体渐变(协调 #1677FF)
COBALT_A, COBALT_B = "#0232A9", "#00186B"                 # 围合区渐变(更深钴蓝)


def _enclosure_path():
    """围合区轮廓:圆角矩形 + 右墙缺口(显式 path,兼容所有渲染器)。"""
    x0, y0, x1, y1, r = ENC["x0"], ENC["y0"], ENC["x1"], ENC["y1"], ENC["r"]
    gi, yt, yb = NOTCH["x_in"], NOTCH["y_top"], NOTCH["y_bot"]
    return (
        f"M {x0 + r},{y0} H {x1 - r} A {r},{r} 0 0 1 {x1},{y0 + r} "
        f"V {yt} H {gi} V {yb} H {x1} V {y1 - r} "
        f"A {r},{r} 0 0 1 {x1 - r},{y1} H {x0 + r} "
        f"A {r},{r} 0 0 1 {x0},{y1 - r} V {y0 + r} "
        f"A {r},{r} 0 0 1 {x0 + r},{y0} Z"
    )


def master_svg(fullbleed=False):
    """矢量主文件。fullbleed=True 时主体满铺画布(apple-touch-icon 用)。"""
    if fullbleed:
        # 内容整体放大顶格:主体撑满 512 画布,内部元素按同比例平移缩放
        s = 512 / (BODY["x1"] - BODY["x0"])
        body = '<rect width="512" height="512" fill="url(#bg)"/>'
        g = f'<g transform="translate({-BODY["x0"] * s:.4f},{-BODY["y0"] * s:.4f}) scale({s:.6f})">'
        inner_enc = f'<path d="{_enclosure_path()}" fill="url(#eg)"/>'
    else:
        body = (
            f'<rect x="{BODY["x0"]}" y="{BODY["y0"]}" width="{BODY["x1"]-BODY["x0"]}" '
            f'height="{BODY["y1"]-BODY["y0"]}" rx="{BODY["r"]}" fill="url(#bg)"/>'
        )
        g = "<g>"
        inner_enc = f'<path d="{_enclosure_path()}" fill="url(#eg)"/>'
    nodes = "".join(
        f'<circle cx="{x}" cy="{y}" r="{NODE_R}" fill="url(#ng)"/>' for x, y in NODES
    )
    paths = (
        f'<path d="M {NODES[0][0]},{NODES[0][1]} L {MERGE[0]},{MERGE[1]}" '
        f'stroke="#fff" stroke-width="{W_BRANCH}" stroke-linecap="round" fill="none"/>'
        f'<path d="M {NODES[1][0]},{NODES[1][1]} L {MERGE[0]},{MERGE[1]}" '
        f'stroke="#fff" stroke-width="{W_BRANCH}" stroke-linecap="round" fill="none"/>'
        f'<path d="M {NODES[2][0]},{NODES[2][1]} L {MERGE[0]},{MERGE[1]}" '
        f'stroke="#fff" stroke-width="{W_BRANCH}" stroke-linecap="round" fill="none"/>'
        f'<path d="M {MERGE[0]},{MERGE[1]} L {NODES[3][0]},{NODES[3][1]}" '
        f'stroke="#fff" stroke-width="{W_MAIN}" stroke-linecap="round" fill="none"/>'
    )
    # 门户冷青光沿:仅缺口上下两个切面,横 向短线,无竖向栏杆
    x_out = ENC["x1"] + 5
    portal = (
        f'<path d="M {NOTCH["x_in"]-2},{NOTCH["y_top"]} H {x_out}" stroke="{CYAN}" '
        f'stroke-width="9" stroke-linecap="round" opacity="0.22" fill="none"/>'
        f'<path d="M {NOTCH["x_in"]-2},{NOTCH["y_bot"]} H {x_out}" stroke="{CYAN}" '
        f'stroke-width="9" stroke-linecap="round" opacity="0.22" fill="none"/>'
        f'<path d="M {NOTCH["x_in"]},{NOTCH["y_top"]} H {ENC["x1"]+2}" stroke="{CYAN}" '
        f'stroke-width="4.5" stroke-linecap="round" opacity="0.95" fill="none"/>'
        f'<path d="M {NOTCH["x_in"]},{NOTCH["y_bot"]} H {ENC["x1"]+2}" stroke="{CYAN}" '
        f'stroke-width="4.5" stroke-linecap="round" opacity="0.95" fill="none"/>'
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs>
<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="{BLUE_A}"/><stop offset="1" stop-color="{BLUE_B}"/>
</linearGradient>
<linearGradient id="eg" x1="1" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="{COBALT_A}"/><stop offset="1" stop-color="{COBALT_B}"/>
</linearGradient>
<radialGradient id="ng" cx="0.5" cy="0.5" r="0.62" fx="0.34" fy="0.34">
<stop offset="0" stop-color="#ffffff"/><stop offset="0.55" stop-color="#f6f9fe"/>
<stop offset="1" stop-color="#d7e5fa"/>
</radialGradient>
</defs>
{body}
{g}
{inner_enc}
{portal}
{paths}
{nodes}
</g>
</svg>'''


# ---------------------------------------------------------------------------
# 小尺寸优化版(64 视野,平涂):favicon.svg + 侧边栏/登录页内联标记共用。
# 相对主文件:去渐变、支线与节点加粗、缺口开大,保证 26/36px 下轮廓清楚。
# ---------------------------------------------------------------------------
S = dict(
    body=(4.5, 4.5, 59.5, 59.5, 9.5, "#0b60f5"),
    enc=(7.8, 13.0, 37.0, 51.0, 5.5, "#041d66"),
    notch=(35.3, 29.0, 35.0),
    nodes=[(14.3, 19.5), (14.3, 32.0), (14.3, 44.5), (50.0, 32.0, 4.4)],
    node_r=3.6,
    merge=(30.4, 32.0),
    w_branch=3.1,
    w_main=4.6,
)


def small_svg():
    b = S["body"]
    e = S["enc"]
    gi, yt, yb = S["notch"]
    x0, y0, x1, y1, r, _ = e
    enc_path = (
        f"M {x0+r},{y0} H {x1-r} A {r},{r} 0 0 1 {x1},{y0+r} "
        f"V {yt} H {gi} V {yb} H {x1} V {y1-r} "
        f"A {r},{r} 0 0 1 {x1-r},{y1} H {x0+r} "
        f"A {r},{r} 0 0 1 {x0},{y1-r} V {y0+r} "
        f"A {r},{r} 0 0 1 {x0+r},{y0} Z"
    )
    n = S["nodes"]
    m = S["merge"]
    paths = "".join(
        f'<path d="M {x},{y} L {m[0]},{m[1]}" stroke="#fff" stroke-width="{S["w_branch"]}" stroke-linecap="round" fill="none"/>'
        for x, y in n[:3]
    ) + (
        f'<path d="M {m[0]},{m[1]} L {n[3][0]},{n[3][1]}" stroke="#fff" '
        f'stroke-width="{S["w_main"]}" stroke-linecap="round" fill="none"/>'
    )
    circles = "".join(
        f'<circle cx="{n[0]}" cy="{n[1]}" r="{n[2] if len(n) > 2 else S["node_r"]}" fill="#fff"/>'
        for n in S["nodes"]
    )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect x="{b[0]}" y="{b[1]}" width="{b[2]-b[0]}" height="{b[3]-b[1]}" rx="{b[4]}" fill="{b[5]}"/>'
        f'<path d="{enc_path}" fill="{e[5]}"/>'
        + paths + circles + "</svg>"
    )


# ---------------------------------------------------------------------------
# 16/32px 光学修正版:直接在目标像素网格上手工定位,8x 超采样后降采样。
# ---------------------------------------------------------------------------
SS = 8


def _flat_icon(size, cfg):
    """cfg: body/enc/notch/nodes/merge/widths,坐标单位=目标像素。"""
    im = Image.new("RGBA", (size * SS, size * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    def box(t):
        return [v * SS for v in t]

    bx = cfg["body"]
    d.rounded_rectangle(box([bx[0], bx[1], bx[2], bx[3]]), radius=bx[4] * SS, fill=bx[5])
    ex = cfg["enc"]
    d.rounded_rectangle(box([ex[0], ex[1], ex[2], ex[3]]), radius=ex[4] * SS, fill=ex[5])
    # 缺口:从围合区右沿向内切开,透出主体蓝
    gi, yt, yb, xo = cfg["notch"]
    d.rectangle(box([gi, yt, xo, yb]), fill=bx[5])
    # 白色路径(圆头)
    for a, b in cfg["branches"]:
        d.line(box(a) + box(b), fill="#fff", width=int(cfg["w_branch"] * SS))
        for p in (a, b):
            d.ellipse(
                box([p[0] - cfg["w_branch"] / 2, p[1] - cfg["w_branch"] / 2,
                     p[0] + cfg["w_branch"] / 2, p[1] + cfg["w_branch"] / 2]),
                fill="#fff",
            )
    a, b = cfg["main"]
    d.line(box(a) + box(b), fill="#fff", width=int(cfg["w_main"] * SS))
    d.ellipse(
        box([a[0] - cfg["w_main"] / 2, a[1] - cfg["w_main"] / 2,
             a[0] + cfg["w_main"] / 2, a[1] + cfg["w_main"] / 2]), fill="#fff")
    # 节点(条目可为 (x, y) 或 (x, y, r))
    for n in cfg["nodes"]:
        x, y = n[0], n[1]
        r = n[2] if len(n) > 2 else cfg["node_r"]
        d.ellipse(box([x - r, y - r, x + r, y + r]), fill="#fff")
    return im.resize((size, size), Image.LANCZOS)


CFG16 = dict(
    body=(0.5, 0.5, 15.5, 15.5, 3.0, "#0b60f5"),
    enc=(2.0, 2.5, 8.8, 13.5, 1.6, "#041d66"),
    notch=(8.0, 6.6, 9.4, 9.4),           # 浅缺口:16px 下仅作边缘暗示
    # 16px 光学修正:支线省略(避免糊成竖条),三节点拉开间距成独立白点,
    # 外部节点加大半径形成可辨认的端点凸起
    nodes=[(4.4, 4.7), (4.4, 8.0), (4.4, 11.3), (12.4, 8.0, 1.7)],
    node_r=1.25,
    merge=(4.4, 8.0),
    branches=[],
    w_branch=1.9,
    main=[(4.4, 8.0), (11.2, 8.0)],
    w_main=1.9,
)

CFG32 = dict(
    body=(1.0, 1.0, 31.0, 31.0, 6.0, "#0b60f5"),
    enc=(3.8, 6.2, 18.4, 25.8, 3.0, "#041d66"),
    notch=(16.6, 13.6, 18.4, 19.2),
    nodes=[(7.1, 9.8), (7.1, 16.0), (7.1, 22.2)],
    node_r=1.8,
    merge=(13.6, 16.0),
    branches=[[(7.1, 9.8), (13.6, 16.0)], [(7.1, 16.0), (13.6, 16.0)], [(7.1, 22.2), (13.6, 16.0)]],
    w_branch=2.2,
    main=[(13.6, 16.0), (24.4, 16.0)],
    w_main=3.4,
)


def render_svg(svg, w, h):
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=w, output_height=h)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def write_ico(path, images):
    """手工组装 ICO:每个尺寸使用各自独立渲染的图(PIL 自带 save 会从单图降采样,
    破坏 16/32px 光学修正版)。ICO 目录项 + 内嵌 PNG,现代浏览器全支持。"""
    import struct
    blobs = []
    for im in images:
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        blobs.append(buf.getvalue())
    header = struct.pack("<HHH", 0, 1, len(blobs))
    entries, offset = b"", 6 + 16 * len(blobs)
    for blob in blobs:
        im_size = Image.open(io.BytesIO(blob)).size
        entries += struct.pack(
            "<BBBBHHII", im_size[0] % 256, im_size[1] % 256, 0, 0, 1, 32,
            len(blob), offset)
        offset += len(blob)
    with open(path, "wb") as f:
        f.write(header + entries + b"".join(blobs))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(__file__), "..", "static", "assets", "brand")
    out = os.path.abspath(out)
    os.makedirs(out, exist_ok=True)
    static_root = os.path.dirname(os.path.dirname(out))  # .../static

    w = lambda name, data: open(os.path.join(out, name), "wb").write(data)

    # 矢量
    with open(os.path.join(os.path.dirname(__file__), "icon-master.svg"), "w") as f:
        f.write(master_svg())
    w("favicon.svg", small_svg().encode())

    # 大尺寸 PNG(矢量渲染;仅 16/32 需光学修正,48+ 自动渲染)
    master = master_svg()
    for size in (48, 64, 180, 192, 512, 1024):
        render_svg(master, size, size).save(os.path.join(out, f"icon-{size}.png"))
    # apple-touch-icon:满铺不透明(iOS 会自行圆角)
    render_svg(master_svg(fullbleed=True), 180, 180).save(
        os.path.join(out, "apple-touch-icon.png"))

    # 16/32 光学修正版
    _flat_icon(16, CFG16).save(os.path.join(out, "icon-16.png"))
    _flat_icon(32, CFG32).save(os.path.join(out, "icon-32.png"))

    # favicon.ico:16(光学)+32(光学)+48(矢量),各自独立成帧
    write_ico(os.path.join(static_root, "favicon.ico"), [
        Image.open(os.path.join(out, f"icon-{s}.png")) for s in (16, 32, 48)])
    print("written to", out)


if __name__ == "__main__":
    main()
