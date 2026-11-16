# -*- coding: utf-8 -*-
"""
================================================================================
hCALC Monitor —— 计算任务实时监控服务
================================================================================

用途
----
把正在运行的 ORCA 计算进度实时推到浏览器：几何优化步数、当前能量、
SCF 收敛曲线、任务状态。为后续做成完整网站打基础（一期原型）。

设计要点
--------
1. **不依赖外部 CDN**：前端图表用原生 Canvas 手绘。本机网络对部分域名受限，
   若引 CDN 会导致页面空白。
2. **SSE 而不是 WebSocket**：数据流单向（服务器→浏览器），SSE 穿透隧道和
   代理都更省心，浏览器原生 EventSource 即可。
3. **只读文件**：服务只解析 ORCA 输出，不写、不启动任务，避免与计算争抢资源。

启动
----
    python monitor.py
    浏览器打开 http://127.0.0.1:8000
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

PORT = int(os.environ.get("MONITOR_PORT", "8000"))

PVDF = r"D:\Calculation\PVDF"

# 监控的任务清单：标签 -> 输出文件路径
JOBS = {
    "hBN 大碎片·β": os.path.join(PVDF, "hbn_big", "opt_beta_on_hBN.out"),
    "hBN 大碎片·α": os.path.join(PVDF, "hbn_big", "opt_alpha_on_hBN.out"),
    "hBN 小碎片·β": os.path.join(PVDF, "hbn", "opt_beta_on_hBN.out"),
    "hBN 小碎片·α": os.path.join(PVDF, "hbn", "opt_alpha_on_hBN.out"),
}

app = FastAPI(title="hCALC Monitor")

# ---------------------------------------------------------------- 跨域
# GitHub Pages 上的前端（hostoreke.github.io）要跨域读取本服务的 JSON，
# 没有 CORS 头浏览器会直接拦截。此处放开只读接口的跨域。
from fastapi.middleware.cors import CORSMiddleware      # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------- 访问控制
# Funnel 会把服务暴露到公网，任何人拿到网址都能看。这里强制 HTTP Basic 认证。
# 账号密码可通过环境变量覆盖；未覆盖时用默认值。
import base64                                          # noqa: E402
from fastapi import Request                            # noqa: E402
from fastapi.responses import Response                 # noqa: E402

AUTH_USER = os.environ.get("MONITOR_USER", "hostoreke")
AUTH_PASS = os.environ.get("MONITOR_PASS", "hcalc-2026-pvdf")
# 免密开关：设 MONITOR_NO_AUTH=1 即关闭认证（公网任何人可直接访问，慎用）
AUTH_ENABLED = os.environ.get("MONITOR_NO_AUTH", "0") != "1"


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """默认要求 Basic 认证；设置 MONITOR_NO_AUTH=1 可关闭。"""
    if not AUTH_ENABLED:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, pwd = decoded.split(":", 1)
            if user == AUTH_USER and pwd == AUTH_PASS:
                return await call_next(request)
        except (ValueError, UnicodeDecodeError):
            pass
    return Response(
        content="401 Unauthorized",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="hCALC Monitor"'},
    )


# ---------------------------------------------------------------- 解析
def parse_job(path):
    """解析 ORCA 输出：步数、能量轨迹、SCF 曲线、状态。"""
    info = {
        "exists": os.path.exists(path),
        "steps": 0,
        "energy": None,
        "trace": [],
        "status": "未知",
        "size_kb": 0,
        "updated": None,
    }
    if not info["exists"]:
        info["status"] = "未开始"
        return info

    try:
        info["size_kb"] = round(os.path.getsize(path) / 1024, 1)
        info["updated"] = datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%H:%M:%S")
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        info["status"] = "读取失败"
        return info

    info["steps"] = len(re.findall(r"GEOMETRY OPTIMIZATION CYCLE", txt))

    # 每个优化步的最终能量
    energies = [float(x) for x in
                re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", txt)]
    if energies:
        info["energy"] = energies[-1]
        info["trace"] = [round(e, 6) for e in energies]

    if "ORCA TERMINATED NORMALLY" in txt:
        info["status"] = "已完成"
    elif "error termination" in txt.lower() or "aborting the run" in txt:
        info["status"] = "失败"
    else:
        info["status"] = "运行中"
    return info


def snapshot():
    return {name: parse_job(p) for name, p in JOBS.items()}


# ---------------------------------------------------------------- SSE
async def event_stream():
    while True:
        data = snapshot()
        yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        await asyncio.sleep(5)


@app.get("/api/jobs")
def api_jobs():
    return snapshot()


@app.get("/api/stream")
async def api_stream():
    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------- 前端
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hCALC Monitor · hostoreke</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--dim:#94a3b8;--accent:#38bdf8;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--fg);font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;padding:24px}
  h1{font-size:20px;font-weight:600;margin-bottom:4px}
  .sub{color:var(--dim);font-size:13px;margin-bottom:20px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
  .card{background:var(--card);border-radius:12px;padding:16px;border:1px solid #334155}
  .title{font-size:14px;font-weight:600;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
  .badge{font-size:11px;padding:2px 8px;border-radius:99px;font-weight:500}
  .b-run{background:#164e63;color:var(--accent)}
  .b-ok{background:#14532d;color:var(--ok)}
  .b-bad{background:#7f1d1d;color:var(--bad)}
  .b-idle{background:#334155;color:var(--dim)}
  .row{display:flex;justify-content:space-between;font-size:12.5px;color:var(--dim);margin:4px 0}
  .row b{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}
  canvas{width:100%;height:110px;margin-top:10px;display:block}
  .foot{margin-top:20px;color:var(--dim);font-size:12px}
</style>
</head>
<body>
<h1>hCALC Monitor</h1>
<div class="sub">PVDF / h-BN 界面计算实时监控　·　每 5 秒刷新　·　<span id="ts"></span></div>
<div class="grid" id="grid"></div>
<div class="foot">数据来源：ORCA 输出文件实时解析　·　本页不写文件、不起任务，只读监控</div>

<script>
const grid = document.getElementById('grid');
const cards = {};

function statusClass(s){
  if(s==='运行中') return 'b-run';
  if(s==='已完成') return 'b-ok';
  if(s==='失败')   return 'b-bad';
  return 'b-idle';
}

function makeCard(name){
  const el = document.createElement('div');
  el.className = 'card';
  el.innerHTML = `
    <div class="title"><span>${name}</span><span class="badge b-idle">—</span></div>
    <div class="row"><span>优化步数</span><b class="v-steps">—</b></div>
    <div class="row"><span>当前能量 (Ha)</span><b class="v-energy">—</b></div>
    <div class="row"><span>输出文件</span><b class="v-size">—</b></div>
    <div class="row"><span>最后更新</span><b class="v-upd">—</b></div>
    <canvas></canvas>`;
  grid.appendChild(el);
  return {
    badge: el.querySelector('.badge'),
    steps: el.querySelector('.v-steps'),
    energy: el.querySelector('.v-energy'),
    size: el.querySelector('.v-size'),
    upd: el.querySelector('.v-upd'),
    canvas: el.querySelector('canvas')
  };
}

function draw(canvas, trace){
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  canvas.width = w*dpr; canvas.height = h*dpr;
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);

  ctx.strokeStyle = '#334155'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(0,h-1); ctx.lineTo(w,h-1); ctx.stroke();

  if(!trace || trace.length < 2){
    ctx.fillStyle = '#64748b'; ctx.font = '11px sans-serif';
    ctx.fillText('等待能量数据…', 8, h/2);
    return;
  }
  const lo = Math.min(...trace), hi = Math.max(...trace);
  const pad = (hi-lo)*0.15 || 0.001;
  const y0 = lo-pad, y1 = hi+pad;
  const X = i => (i/(trace.length-1))*(w-8)+4;
  const Y = v => h-6-((v-y0)/(y1-y0))*(h-12);

  ctx.strokeStyle = '#38bdf8'; ctx.lineWidth = 1.8;
  ctx.beginPath();
  trace.forEach((v,i)=> i? ctx.lineTo(X(i),Y(v)) : ctx.moveTo(X(i),Y(v)));
  ctx.stroke();

  // 末点高亮
  const lx = X(trace.length-1), ly = Y(trace[trace.length-1]);
  ctx.fillStyle = '#38bdf8';
  ctx.beginPath(); ctx.arc(lx, ly, 3, 0, Math.PI*2); ctx.fill();
}

const es = new EventSource('/api/stream');
es.onmessage = (ev) => {
  const data = JSON.parse(ev.data);
  document.getElementById('ts').textContent = new Date().toLocaleTimeString('zh-CN');
  Object.entries(data).forEach(([name, j]) => {
    const c = cards[name] || (cards[name] = makeCard(name));
    c.badge.textContent = j.status;
    c.badge.className = 'badge ' + statusClass(j.status);
    c.steps.textContent = j.steps;
    c.energy.textContent = j.energy === null ? '—' : j.energy.toFixed(6);
    c.size.textContent = j.size_kb + ' KB';
    c.upd.textContent = j.updated || '—';
    draw(c.canvas, j.trace);
  });
};
es.onerror = () => { document.getElementById('ts').textContent = '连接中断，重试中…'; };
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


if __name__ == "__main__":
    import uvicorn
    print(f"hCALC Monitor 启动: http://127.0.0.1:{PORT}")
    print(f"监控任务: {list(JOBS)}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
