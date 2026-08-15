#!/usr/bin/env python3
"""生成 3 个用量总览高保真原型(自包含单文件,内联 portal.css 风格 + 仿真数据 + SVG 图表)"""
import math, random, webbrowser
from pathlib import Path

random.seed(42)
OUT = Path(__file__).parent

# ---------------- 仿真数据 ----------------
HOURS = list(range(24))
REQS = [2,1,0,0,0,3,8,26,58,92,120,140,96,110,150,170,120,86,60,44,30,18,9,4]      # 请求/时
TOK_IN = [r * random.randint(900, 1600) for r in REQS]
TOK_CACHE = [int(t * random.uniform(.25, .55)) for t in TOK_IN]
TOK_OUT = [r * random.randint(300, 700) for r in REQS]
TFT = [random.randint(380, 2200) for _ in HOURS]                                     # 平均TFT ms
TOT_REQ = sum(REQS); TOT_IN = sum(TOK_IN); TOT_OUT = sum(TOK_OUT); TOT_CACHE = sum(TOK_CACHE)
AVG_TFT = sum(TFT) // 24; AVG_LAT = AVG_TFT + 900

MODELS = [("deepseek-v4-flash-0731", 912), ("qwen3.6-35b-fp8", 486), ("claude-opus-5（别名）", 203)]
KEYS = [("pi-local", 873), ("justink", 401), ("home-only", 327)]
FAILS = 14

ROWS = [
    ("08-15 14:32:05", "pi-local", "…Rg2Q", "deepseek-v4-flash-0731", "acompletion", 12480, 8192, 1024, 644, 3210, "112.10.88.23", "ok", ""),
    ("08-15 14:31:47", "justink", "…bGvw", "qwen3.6-35b-fp8", "messages", 830, 0, 246, 388, 1140, "192.168.88.12", "ok", ""),
    ("08-15 14:31:02", "pi-local", "…Rg2Q", "deepseek-v4-flash-0731", "acompletion", 44600, 39800, 5200, 1890, 18460, "112.10.88.23", "ok", ""),
    ("08-15 14:30:38", "home-only", "…Iag", "qwen3.6-35b-fp8", "acompletion", 2210, 512, 680, 410, 2205, "203.198.17.66", "ok", ""),
    ("08-15 14:29:55", "pi-local", "…Rg2Q", "claude-opus-5", "messages", 960, 0, 130, 0, 0, "112.10.88.23", "failure", "上游 connect_timeout=5s 超时（站点隧道中断）"),
    ("08-15 14:28:41", "justink", "…bGvw", "deepseek-v4-flash-0731", "acompletion", 1560, 768, 210, 512, 1430, "192.168.88.12", "ok", ""),
    ("08-15 14:27:19", "pi-local", "…Rg2Q", "deepseek-v4-flash-0731", "acompletion", 22180, 16400, 2800, 1210, 9930, "112.10.88.23", "ok", ""),
    ("08-15 14:26:58", "home-only", "…Iag", "qwen3.6-35b-fp8", "acompletion", 640, 0, 88, 350, 890, "203.198.17.66", "ok", ""),
    ("08-15 14:26:12", "justink", "…bGvw", "claude-opus-5", "messages", 1180, 0, 160, 0, 0, "192.168.88.12", "failure", "Anthropic 协议转换：400 model_not_found"),
    ("08-15 14:25:37", "pi-local", "…Rg2Q", "deepseek-v4-flash-0731", "acompletion", 3024, 1024, 410, 480, 2110, "172.18.0.2", "ok", ""),
]

def fmt(n):
    return f"{n:,}"

# ---------------- 共享 CSS(portal.css 令牌子集) ----------------
CSS = """
* { box-sizing: border-box; margin: 0; }
body { font-family: -apple-system, "PingFang SC", "Segoe UI", "Microsoft YaHei", sans-serif;
  background: #f7f8fa; color: #1f1f1f; font-size: 13px; }
.mono, .num, code { font-family: ui-monospace, "SF Mono", Consolas, Menlo, monospace;
  font-variant-numeric: tabular-nums; }
.pf-layout { display: flex; min-height: 100vh; }
.pf-sidebar { width: 208px; background: #fff; border-right: 1px solid #f0f0f0; padding: 16px 12px;
  display: flex; flex-direction: column; position: sticky; top: 0; height: 100vh; }
.pf-logo { display: flex; align-items: center; gap: 9px; padding: 2px 8px 16px; }
.pf-logo-mark { width: 28px; height: 28px; border-radius: 7px; background: linear-gradient(135deg,#1677ff,#4096ff);
  display: flex; align-items: center; justify-content: center; color: #fff; }
.pf-nav { display: flex; flex-direction: column; gap: 2px; }
.pf-nav-item { display: flex; align-items: center; gap: 9px; padding: 8px 10px; border-radius: 6px;
  color: #595959; text-decoration: none; }
.pf-nav-item:hover { background: #f5f7fa; color: #1677ff; }
.pf-nav-item.active { background: #e6f4ff; color: #1677ff; font-weight: 500; }
.pf-sidebar-foot { margin-top: auto; display: flex; align-items: center; gap: 8px; padding: 10px 8px 0;
  border-top: 1px solid #f0f0f0; }
.pf-avatar { width: 26px; height: 26px; border-radius: 50%; background: #1677ff; color: #fff;
  display: flex; align-items: center; justify-content: center; font-size: 12px; }
.pf-main { flex: 1; min-width: 0; }
.pf-topbar { height: 50px; background: #fff; border-bottom: 1px solid #f0f0f0;
  display: flex; align-items: center; justify-content: space-between; padding: 0 22px;
  position: sticky; top: 0; z-index: 5; }
.pf-content { padding: 20px 22px; max-width: 1280px; }
.pf-page-head { margin-bottom: 14px; }
.pf-desc { font-size: 12px; color: #8c8c8c; line-height: 1.6; }
.pf-card { background: #fff; border: 1px solid #f0f0f0; border-radius: 8px; padding: 16px; margin-bottom: 14px; }
.pf-card.flush { padding: 0; overflow: hidden; }
.pf-card-head { display: flex; align-items: center; justify-content: space-between;
  padding: 12px 14px; border-bottom: 1px solid #f5f5f5; }
.pf-card-title { font-size: 13px; font-weight: 600; }
.pf-card-extra { font-size: 12px; color: #8c8c8c; }
.pf-chip { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11.5px;
  border: 1px solid; line-height: 1.7; white-space: nowrap; }
.pf-chip.ok { color: #389e0d; background: #f6ffed; border-color: #b7eb8f; }
.pf-chip.err { color: #cf1322; background: #fff1f0; border-color: #ffa39e; }
.pf-chip.gray { color: #8c8c8c; background: #fafafa; border-color: #e8e8e8; }
.pf-chip.info { color: #0958d9; background: #e6f4ff; border-color: #91caff; }
.pf-key { display: inline-block; padding: 1px 6px; border-radius: 4px; background: #fafafa;
  border: 1px solid #f0f0f0; font-size: 12px; color: #595959; }
.pf-table { width: 100%; border-collapse: collapse; }
.pf-table th { text-align: left; font-weight: 500; color: #8c8c8c; font-size: 12px;
  padding: 9px 12px; border-bottom: 1px solid #f0f0f0; background: #fafbfc; }
.pf-table td { padding: 8px 12px; border-bottom: 1px solid #f7f7f7; }
.pf-table tbody tr:hover { background: #fafafa; }
.pf-table .num { text-align: right; }
.pf-table th.num { text-align: right; }
.pf-link { background: none; border: none; color: #1677ff; cursor: pointer; font-size: 12.5px; padding: 0; }
.pf-btn { border: 1px solid #d9d9d9; background: #fff; border-radius: 6px; padding: 5px 12px;
  font-size: 12.5px; cursor: pointer; color: #1f1f1f; }
.pf-btn.primary { background: #1677ff; border-color: #1677ff; color: #fff; }
.pf-btn.sm { padding: 2px 9px; font-size: 12px; }
.pf-input, .pf-select { border: 1px solid #d9d9d9; border-radius: 6px; padding: 5px 9px;
  font-size: 12.5px; background: #fff; color: #1f1f1f; outline: none; }
.pf-input:focus, .pf-select:focus { border-color: #4096ff; }
.pf-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.pf-stat-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 14px; }
.pf-stat { background: #fff; border: 1px solid #f0f0f0; border-radius: 8px; padding: 13px 15px; }
.pf-stat-label { font-size: 12px; color: #8c8c8c; }
.pf-stat-value { font-size: 21px; font-weight: 600; margin-top: 4px; }
.pf-stat-sub { font-size: 11.5px; color: #bfbfbf; margin-top: 2px; }
.pf-bar-item { display: grid; grid-template-columns: 150px 1fr 70px; gap: 10px; align-items: center;
  padding: 5px 14px; font-size: 12.5px; }
.pf-bar-track { background: #f5f5f5; border-radius: 4px; height: 8px; overflow: hidden; }
.pf-bar-fill { display: block; height: 100%; background: #1677ff; border-radius: 4px; }
.pf-bar-fill.violet { background: #722ed1; }
.pf-ribbon { position: fixed; right: 14px; bottom: 14px; background: #1f1f1f; color: #fff;
  border-radius: 8px; padding: 8px 14px; font-size: 12px; z-index: 99; opacity: .92; }
.legend { display: flex; gap: 14px; font-size: 11.5px; color: #8c8c8c; align-items: center; }
.legend i { display: inline-block; width: 9px; height: 9px; border-radius: 2px; margin-right: 5px; }
"""

def sidebar(active="用量总览"):
    items = [("仪表盘","▦"),("站点与公钥","⛓"),("分组","▤"),("模型与别名","⇄"),("用户 Key","⚿"),
             ("用量总览","▥"),("MCP 管理","▣"),("安全设置","⛨")]
    nav = "".join(f'<a class="pf-nav-item{" active" if t==active else ""}" href="#">{i}<span>{t}</span></a>'
                  for t, i in items)
    return f'''<aside class="pf-sidebar"><div class="pf-logo">
      <div class="pf-logo-mark">⚡</div><div style="font-weight:600">远程模型网关</div></div>
      <nav class="pf-nav">{nav}</nav>
      <div class="pf-sidebar-foot"><div class="pf-avatar">A</div>
      <div><div style="font-size:12.5px">管理员</div><div class="pf-desc">邮箱登录</div></div></div></aside>'''

def shell(title, body, proto):
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 远程模型网关</title><style>{CSS}</style></head>
<body><div class="pf-layout">{sidebar()}
<div class="pf-main"><header class="pf-topbar"><div style="font-weight:600">{title}</div>
<div class="pf-avatar">A</div></header><main class="pf-content">{body}</main></div></div>
<div class="pf-ribbon">📋 {proto}</div></body></html>'''

# ---------------- 图表 SVG 生成 ----------------
W, H, PAD_L, PAD_B, PAD_T = 860, 190, 44, 22, 10
def x(i): return PAD_L + i * (W - PAD_L - 8) / 23
def y_tok(v, mx): return PAD_T + (H - PAD_T - PAD_B) * (1 - v / mx)
def area(series, mx, color, op):
    pts = [(x(i), y_tok(v, mx)) for i, v in enumerate(series)]
    d = "M" + f" L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    d += f" L{x(23):.1f},{H - PAD_B} L{x(0):.1f},{H - PAD_B} Z"
    line = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    return (f'<path d="{d}" fill="{color}" opacity="{op}"/>'
            f'<path d="{line}" fill="none" stroke="{color}" stroke-width="1.6"/>')

def bars(series, mx, color):
    bw = (W - PAD_L - 8) / 23 * 0.45
    return "".join(
        f'<rect x="{x(i) - bw/2:.1f}" y="{y_tok(v, mx):.1f}" width="{bw:.1f}" '
        f'height="{H - PAD_B - y_tok(v, mx):.1f}" rx="1.5" fill="{color}"/>'
        for i, v in enumerate(series))

def xlabels(step=4, baseline=None):
    y0 = H - 6 if baseline is None else baseline
    return "".join(f'<text x="{x(i):.1f}" y="{y0}" font-size="10" fill="#8c8c8c" text-anchor="middle">{h:02d}</text>'
                   for i, h in enumerate(HOURS) if i % step == 0)

# A/B/C 共用的趋势图
mx_in = max(TOK_IN) * 1.05
trend = f'''<svg viewBox="0 0 {W} {H}" style="width:100%">
  {bars(REQS, max(REQS) * 1.15, "#bcd9ff")}
  {area(TOK_IN, mx_in, "#1677ff", "0.14")}
  {area([a + b for a, b in zip(TOK_IN, TOK_OUT)], mx_in, "#722ed1", "0.10")}
  {xlabels()}
  {[f'<text x="6" y="{y_tok(mx_in*0.999, mx_in):.0f}" font-size="10" fill="#8c8c8c">{fmt(int(mx_in//1000))}k</text>'][0]}
</svg>'''

def hbar_rows(data, violet_alt=True):
    mx = data[0][1]
    out = []
    for i, (name, v) in enumerate(data):
        pct = int(v / mx * 100)
        out.append(f'<div class="pf-bar-item"><div>{name}</div>'
                   f'<div class="pf-bar-track"><span class="pf-bar-fill{" violet" if violet_alt and i % 2 else ""}" '
                   f'style="width:{pct}%"></span></div><div class="num" style="text-align:right">{fmt(v)}</div></div>')
    return "".join(out)

def sev_tft(ms): return "#389e0d" if ms < 1000 else ("#d46b08" if ms < 3000 else "#cf1322")
def sev_dur(ms): return "#389e0d" if ms < 5000 else ("#d46b08" if ms < 15000 else "#cf1322")

def token_cell(pin, pcache, pout):
    cache_line = (f'<div><span style="color:#0958d9">▣</span> <span style="color:#0958d9">{fmt(pcache)}</span></div>'
                  if pcache else '<div style="color:#bfbfbf;font-size:11.5px">无缓存命中</div>')
    return (f'<div style="line-height:1.6;white-space:nowrap"><span style="color:#389e0d">↓</span> {fmt(pin)}'
            f'<span style="color:#531dab;margin-left:7px">↑</span> {fmt(pout)}</div>{cache_line}')

def lat_cell(tft, tot):
    if not tot:
        return '<span style="color:#bfbfbf">—</span>'
    tft_s = f"{tft}ms" if tft < 1000 else f"{tft/1000:.1f}s"
    tot_s = f"{tot}ms" if tot < 1000 else f"{tot/1000:.1f}s"
    p1 = tft / tot * 100
    bar = (f'<div style="height:5px;width:110px;border-radius:3px;overflow:hidden;display:flex;margin-bottom:4px">'
           f'<span style="width:{p1:.0f}%;background:{sev_tft(tft)}"></span>'
           f'<span style="width:{100-p1:.0f}%;background:{sev_dur(tot-tft)};opacity:.75"></span></div>')
    return (f'{bar}<div style="line-height:1.5;white-space:nowrap"><span style="color:#8c8c8c">首T</span> '
            f'<b style="color:{sev_tft(tft)}">{tft_s}</b><span style="color:#8c8c8c;margin-left:5px">总</span> '
            f'<b style="color:{sev_dur(tot)}">{tot_s}</b></div>')

def detail_rows():
    out = []
    for (ts, alias, k4, model, ctype, pin, pcache, pout, tft, lat, ip, st, err) in ROWS:
        status = '<span class="pf-chip ok">成功</span>' if st == "ok" else '<span class="pf-chip err">失败</span>'
        ip_txt = ip if not ip.startswith("172.18.") else f'{ip} <span class="pf-desc">（经 nginx）</span>'
        fail_mut = ' style="opacity:.62"' if st == "failure" else ""
        out.append(
            f'<tr{fail_mut}><td class="mono">{ts[6:]}</td>'
            f'<td>{alias}</td>'
            f'<td class="mono">{model}</td><td>{ctype}</td>'
            f'<td>{token_cell(pin, pcache, pout)}</td>'
            f'<td>{lat_cell(tft, lat)}</td>'
            f'<td class="mono" style="white-space:nowrap">{ip_txt}</td>'
            f'<td>{status}</td><td><button class="pf-link">详情</button></td></tr>')
    return "".join(out)

STAT_CARDS = f'''<div class="pf-stat-grid">
  <div class="pf-stat"><div class="pf-stat-label">请求总数</div><div class="pf-stat-value">{fmt(TOT_REQ)}</div><div class="pf-stat-sub">失败 {FAILS} · 成功率 {(1-FAILS/TOT_REQ)*100:.1f}%</div></div>
  <div class="pf-stat"><div class="pf-stat-label">输入 Token</div><div class="pf-stat-value">{TOT_IN/10000:.1f}<span style="font-size:12px;color:#8c8c8c"> 万</span></div><div class="pf-stat-sub">缓存命中 {(TOT_CACHE/TOT_IN)*100:.0f}%</div></div>
  <div class="pf-stat"><div class="pf-stat-label">输出 Token</div><div class="pf-stat-value">{TOT_OUT/10000:.1f}<span style="font-size:12px;color:#8c8c8c"> 万</span></div><div class="pf-stat-sub">平均输出 {TOT_OUT//TOT_REQ}/次</div></div>
  <div class="pf-stat"><div class="pf-stat-label">缓存读取</div><div class="pf-stat-value">{TOT_CACHE/10000:.1f}<span style="font-size:12px;color:#8c8c8c"> 万</span></div><div class="pf-stat-sub">节省推理 {TOT_CACHE/TOT_IN*100:.0f}% 输入</div></div>
  <div class="pf-stat"><div class="pf-stat-label">平均 TFT</div><div class="pf-stat-value">{AVG_TFT}<span style="font-size:12px;color:#8c8c8c"> ms</span></div><div class="pf-stat-sub">首 token 时延</div></div>
  <div class="pf-stat"><div class="pf-stat-label">平均总延迟</div><div class="pf-stat-value">{AVG_LAT/1000:.1f}<span style="font-size:12px;color:#8c8c8c"> s</span></div><div class="pf-stat-sub">生成 {AVG_LAT-AVG_TFT} ms</div></div>
</div>'''

FILTER_BAR = '''<div class="pf-toolbar" style="padding:11px 14px">
  <select class="pf-select"><option>今天</option><option>最近 7 天</option><option>最近 30 天</option></select>
  <select class="pf-select"><option>全部 Key</option><option>pi-local</option><option>justink</option></select>
  <select class="pf-select"><option>全部模型</option><option>deepseek-v4-flash-0731</option></select>
  <select class="pf-select"><option>全部状态</option><option>成功</option><option>失败</option></select>
  <input class="pf-input" style="width:220px" placeholder="搜索 request_id / 模型 / IP…">
  <span style="flex:1"></span>
  <button class="pf-btn sm">‹ 上一页</button><span class="pf-desc">1 / 46</span><button class="pf-btn sm">下一页 ›</button>
</div>'''

DETAIL_HEAD = ("<tr><th>时间 <span style='color:#bfbfbf'>↕</span></th><th>Key <span style='color:#bfbfbf'>↕</span></th>"
               "<th>模型 <span style='color:#bfbfbf'>↕</span></th><th>类型 <span style='color:#bfbfbf'>↕</span></th>"
               "<th>Token <span style='color:#bfbfbf'>↕</span></th><th>延迟 <span style='color:#bfbfbf'>↕</span></th>"
               "<th>IP <span style='color:#bfbfbf'>↕</span></th><th>状态 <span style='color:#bfbfbf'>↕</span></th><th>操作</th></tr>")

# ---------------- 原型 A:仪表盘优先 ----------------
body_a = f'''<div class="pf-page-head"><div class="pf-desc">管理员用量视图：趋势、分布与请求明细一站式（原型 A · 仪表盘优先）。</div></div>
{STAT_CARDS}
<div style="display:grid;grid-template-columns:2fr 1fr;gap:14px">
  <div class="pf-card"><div class="pf-card-head" style="padding:0 0 10px;border:none">
    <div><div class="pf-card-title">请求量与 Token 消耗（按小时）</div>
    <div class="legend" style="margin-top:6px"><span><i style="background:#bcd9ff"></i>请求数</span>
    <span><i style="background:#1677ff"></i>输入 Token</span><span><i style="background:#722ed1"></i>输入+输出</span></div></div>
    <select class="pf-select" style="font-size:12px"><option>今天</option><option>最近 7 天</option></select></div>
    {trend}</div>
  <div class="pf-card"><div class="pf-card-title" style="margin-bottom:8px">模型请求分布</div>
    {hbar_rows(MODELS)}
    <div class="pf-card-title" style="margin:14px 0 8px">Key 请求占比</div>
    {hbar_rows(KEYS)}</div>
</div>
<div class="pf-card flush"><div class="pf-card-head"><div class="pf-card-title">请求明细（最近）</div>
<div class="pf-card-extra">共 1,601 次 · 失败 14</div></div>
{FILTER_BAR}
<table class="pf-table"><thead>{DETAIL_HEAD}</thead><tbody>{detail_rows()}</tbody></table>
<div class="pf-desc" style="padding:8px 14px">鉴权失败不产生日志行；明细最多保留最近 500 条。</div></div>'''

# ---------------- 原型 B:日志优先(sub2api 风格) ----------------
body_b = f'''<div class="pf-page-head"><div class="pf-desc">以单次请求为核心的排查视图（原型 B · 日志优先，sub2api 风格）。</div></div>
<div class="pf-card flush" style="margin-bottom:14px"><div style="padding:12px 14px;display:flex;gap:24px;flex-wrap:wrap">
  {[f'<div><div class="pf-stat-label">{lb}</div><div style="font-size:17px;font-weight:600;margin-top:2px">{v}</div></div>'
    for lb, v in [("请求", fmt(TOT_REQ)), ("输入", f"{TOT_IN/10000:.1f} 万"), ("缓存读", f"{TOT_CACHE/10000:.1f} 万"),
                  ("输出", f"{TOT_OUT/10000:.1f} 万"), ("平均 TFT", f"{AVG_TFT} ms"), ("失败", "14")]] and
   "".join(f'<div><div class="pf-stat-label">{lb}</div><div style="font-size:17px;font-weight:600;margin-top:2px">{v}</div></div>'
    for lb, v in [("请求", fmt(TOT_REQ)), ("输入", f"{TOT_IN/10000:.1f} 万"), ("缓存读", f"{TOT_CACHE/10000:.1f} 万"),
                  ("输出", f"{TOT_OUT/10000:.1f} 万"), ("平均 TFT", f"{AVG_TFT} ms"), ("失败", "14")])}
</div></div>
<div class="pf-card flush">
<div class="pf-card-head"><div class="pf-card-title">请求日志</div>
<div class="pf-card-extra">匹配 1,601 / 1,601 条</div></div>
{FILTER_BAR}
<table class="pf-table"><thead>{DETAIL_HEAD}</thead><tbody>{detail_rows()}</tbody></table>
<div style="display:flex;justify-content:space-between;align-items:center;padding:10px 14px">
  <div class="pf-desc">失败行置灰；「详情」查看 request_id / session / 耗时分解。</div>
  <div><button class="pf-btn sm">‹ 上一页</button> <span class="pf-desc">1 / 46</span> <button class="pf-btn sm">下一页 ›</button></div>
</div></div>
<div class="pf-card"><div class="pf-card-title" style="margin-bottom:10px">详情抽屉示意（点「详情」从右侧滑出）</div>
<table class="pf-table"><tbody>
<tr><td style="color:#8c8c8c;width:110px">Request ID</td><td class="mono">chatcmpl-9cbfb59333ee6aae</td></tr>
<tr><td style="color:#8c8c8c">Session</td><td class="mono">4f1357ce-2a72-42a8-8e87-30d2c51c6df6</td></tr>
<tr><td style="color:#8c8c8c">客户端 IP</td><td class="mono">112.10.88.23</td></tr>
<tr><td style="color:#8c8c8c">耗时分解</td><td>
  <div style="display:flex;align-items:center;gap:8px;margin:4px 0">
    <div style="height:10px;border-radius:5px;overflow:hidden;display:flex;width:320px;background:#f5f5f5">
      <span style="width:20%;background:#faad14"></span><span style="width:80%;background:#1677ff"></span></div>
    <span class="pf-desc">排队/TFT 644ms · 生成 2,566ms · 总 3,210ms</span></div></td></tr>
<tr><td style="color:#8c8c8c">Token</td><td>输入 12,480（缓存命中 8,192）· 输出 1,024</td></tr>
</tbody></table></div>'''

# ---------------- 原型 C:双 Tab 混合 ----------------
TFT_H, TFT_T, TFT_B = 150, 10, 30
def y_tft(v):
    return TFT_T + (TFT_H - TFT_T - TFT_B) * (1 - v / (max(TFT) * 1.1))
tft_bars = "".join(
    f'<rect x="{x(i) - (W - PAD_L - 8) / 23 * 0.3:.1f}" y="{y_tft(v):.1f}" width="{(W - PAD_L - 8) / 23 * 0.6:.1f}" '
    f'height="{TFT_H - TFT_B - y_tft(v):.1f}" rx="1.5" fill="{"#faad14" if v > 1500 else "#ffd591"}"/>'
    for i, v in enumerate(TFT))
tft_svg = f'<svg viewBox="0 0 {W} {TFT_H}" style="width:100%">{tft_bars}{xlabels(baseline=TFT_H - 8)}</svg>'
body_c = f'''<div class="pf-page-head"><div class="pf-desc">趋势与明细分 Tab（原型 C · 修订版）。时间按 Asia/Shanghai (+08) 显示；Token 与延迟为聚合展示；点列头排序。</div></div>
<div class="pf-card" style="padding:0 14px">
<div class="pf-toolbar" style="border-bottom:1px solid #f0f0f0;padding:0">
  <button class="pf-btn" style="border:none;border-bottom:2px solid #1677ff;border-radius:0;background:none;color:#1677ff;font-weight:500">趋势总览</button>
  <button class="pf-btn" style="border:none;border-radius:0;background:none;color:#595959">请求明细</button>
  <span style="flex:1"></span>
  <button class="pf-btn sm" title="只刷新数据,不刷新页面">↻ 刷新</button>
  <select class="pf-select" style="margin:8px 0"><option>今天</option><option>最近 7 天</option></select>
</div>
<div style="padding:14px 2px">
{STAT_CARDS.replace('margin-bottom: 14px', 'margin-bottom: 0')}
<div style="display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-top:14px">
  <div><div class="pf-card"><div class="pf-card-title" style="margin-bottom:8px">请求量与 Token（按小时）</div>{trend}</div>
  <div class="pf-card"><div class="pf-card-title" style="margin-bottom:8px">平均 TFT（按小时，&gt;1.5s 标橙红）</div>
  {tft_svg}</div></div>
  <div class="pf-card"><div class="pf-card-title" style="margin-bottom:8px">模型分布</div>{hbar_rows(MODELS)}
  <div class="pf-card-title" style="margin:14px 0 8px">Key 占比</div>{hbar_rows(KEYS)}</div>
</div></div></div>
<div class="pf-card flush" style="margin-top:14px"><div class="pf-card-head">
<div class="pf-card-title">「请求明细」Tab 内容预览（聚合列 + 可排序 + 刷新）</div>
<div class="pf-card-extra">共 1,601 次 · 失败 14 · 第 1/46 页</div></div>
<div class="pf-toolbar" style="padding:11px 14px">
  <select class="pf-select"><option>全部 Key</option><option>pi-local</option><option>justink</option></select>
  <select class="pf-select"><option>全部模型</option><option>deepseek-v4-flash-0731</option></select>
  <select class="pf-select"><option>全部状态</option><option>成功</option><option>失败</option></select>
  <input class="pf-input" style="width:220px" placeholder="搜索 request_id / 模型 / IP…">
  <span style="flex:1"></span>
  <button class="pf-btn sm">‹ 上一页</button><span class="pf-desc">1 / 46</span><button class="pf-btn sm">下一页 ›</button>
</div>
<table class="pf-table"><thead>{DETAIL_HEAD}</thead><tbody>{detail_rows()}</tbody></table>
<div class="pf-desc" style="padding:8px 14px">鉴权失败不产生日志行；172.18.x 为网关(nginx)地址——真实客户端 IP 需上游支持 X-Forwarded-For（已知限制）。</div></div>'''

(OUT / "usage-proto-a.html").write_text(shell("用量总览", body_a, "原型 A · 仪表盘优先 — 供评审，非功能页面"))
(OUT / "usage-proto-b.html").write_text(shell("用量总览", body_b, "原型 B · 日志优先（sub2api 风格）— 供评审，非功能页面"))
(OUT / "usage-proto-c.html").write_text(shell("用量总览", body_c, "原型 C · 双 Tab（趋势 + 明细）— 供评审，非功能页面"))
print("prototypes written:", [p.name for p in OUT.glob("usage-proto-*.html")])
