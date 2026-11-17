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
# 大碎片两项在断电续算后写入 resume_*.out，故指向该文件；
# 续算尚未开始时页面显示「未开始」，不会报错。
JOBS = {
    "hBN 大碎片·β": os.path.join(PVDF, "hbn_big", "resume_beta.out"),
    "hBN 大碎片·α": os.path.join(PVDF, "hbn_big", "resume_alpha.out"),
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


# ---------------------------------------------------------------- 三维结构
def parse_trj_frames(trj_path):
    """
    解析 ORCA 的 *_trj.xyz 轨迹文件（标准多帧 XYZ）。

    ORCA 做几何优化时会把每一步的结构写进这个文件，帧注释里还带当前能量，
    因此可以同时拿到「结构」和「结构对应的能量」，用于网页上的结构回放。
    """
    frames = []
    if not os.path.exists(trj_path):
        return frames
    with open(trj_path, encoding="utf-8", errors="ignore") as f:
        while True:
            line = f.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                n = int(line)
            except ValueError:
                continue
            comment = f.readline().strip()
            atoms = []
            for _ in range(n):
                s = f.readline()
                if not s:
                    break
                q = s.split()
                if len(q) < 4:
                    break
                try:
                    atoms.append([q[0], round(float(q[1]), 4),
                                  round(float(q[2]), 4), round(float(q[3]), 4)])
                except ValueError:
                    break
            if len(atoms) == n:
                m = re.search(r"E\s+(-?\d+\.\d+)", comment)
                frames.append({"energy": float(m.group(1)) if m else None,
                               "atoms": atoms})
    return frames


def latest_structure(out_path):
    """取轨迹文件的最后一帧。"""
    base = os.path.splitext(out_path)[0]
    frames = parse_trj_frames(base + "_trj.xyz")
    if not frames:
        return {"ok": False, "reason": "尚无轨迹文件（优化还没开始或已删除）"}
    last = frames[-1]
    return {"ok": True, "frame": len(frames), "frames": len(frames),
            "energy": last["energy"], "atoms": last["atoms"]}


@app.get("/api/structure/{idx}")
def api_structure(idx: int):
    """按任务序号返回最新一帧的原子坐标。"""
    names = list(JOBS)
    if not (0 <= idx < len(names)):
        return {"ok": False, "reason": "任务序号越界"}
    return {"name": names[idx], **latest_structure(JOBS[names[idx]])}


# ---------------------------------------------------------------- 拉伸扫描
# 拉伸扫描有 24 个计算点（2 种链长 × 2 种构象 × 5 个端距 + 4 个自由参考），
# 用卡片一个个列会非常乱，所以单独做一个聚合视图：
# 只关心「每个端距下 α 与 β 的能量差」，直接给出 ΔE(d) 曲线与进度。
STRETCH_DIR = os.path.join(PVDF, "stretch")

# 读取大输出文件很费时，用 (路径, 修改时间) 做缓存，避免每 30 秒重新解析
_energy_cache = {}


def _energy_cached(path):
    """带缓存地取最终能量。文件未变化就不重复读。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime, st.st_size)
    if key in _energy_cache:
        return _energy_cache[key]
    try:
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    m = re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", txt)
    val = float(m[-1]) if m else None
    done = "ORCA TERMINATED NORMALLY" in txt
    _energy_cache[key] = (val, done)
    return val, done


def _end_to_end(xyz_path, nc):
    """从优化后的 xyz 里量首尾骨架碳的距离。"""
    try:
        L = open(xyz_path, encoding="utf-8", errors="ignore").read().splitlines()
        n = int(L[0])
        P = [[float(x) for x in s.split()[1:4]] for s in L[2:2 + n]]
    except (OSError, ValueError, IndexError):
        return None
    if len(P) < nc:
        return None
    a, b = P[0], P[nc - 1]
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


@app.get("/api/stretch")
def api_stretch():
    """聚合拉伸扫描进度与 ΔE(d) 曲线。"""
    import glob
    HA2KJ = 2625.499639
    jobs = sorted(glob.glob(os.path.join(STRETCH_DIR, "stretch_*.inp")))
    total = len(jobs)

    raw = {}          # (n, d or 'free') -> {beta: {...}, alpha: {...}}
    running = []
    completed = 0
    for inp in jobs:
        stem = os.path.basename(inp)[:-4]
        m = re.match(r"stretch_n(\d+)_(beta|alpha)(_d([\d.]+)|_free)$", stem)
        if not m:
            continue
        n = int(m.group(1))
        kind = m.group(2)
        is_free = m.group(3) == "_free"
        d = None if is_free else float(m.group(4))

        # 目标端距直接在输入文件里，不必解析输出
        if d is None:
            d = _end_to_end(os.path.join(STRETCH_DIR, stem + ".xyz"), 2 * n)

        e = _energy_cached(os.path.join(STRETCH_DIR, stem + ".out"))
        energy, done = e if e else (None, False)
        if done:
            completed += 1
        else:
            # 关键：未完成任务的"当前能量"是优化中途的值，可能高出几个哈特里，
            # 若把它画进曲线会得到一个上千 kJ/mol 的假点，把纵轴撑爆，
            # 导致真实数据看起来是一条平线。所以未完成的任务一律不参与曲线。
            running.append(stem)
            energy = None

        key = (n, "free" if is_free else d)
        raw.setdefault(key, {})[kind] = {"energy": energy, "done": done,
                                         "d": d, "d_free": is_free}

    # 组装曲线：每个端距上 α 与 β 都有能量才算一个点
    series = {}
    for (n, key), v in raw.items():
        if key == "free":
            continue
        b, a = v.get("beta"), v.get("alpha")
        if not (b and a and b["energy"] and a["energy"]):
            continue
        de = (b["energy"] - a["energy"]) * HA2KJ
        series.setdefault(str(n), []).append(
            {"d": key, "dE": round(de, 2), "dE_per_unit": round(de / n, 3)})

    d_star = {}
    for n, pts in series.items():
        pts.sort(key=lambda x: x["d"])
        for i in range(len(pts) - 1):
            y0, y1 = pts[i]["dE"], pts[i + 1]["dE"]
            if y0 > 0 >= y1:
                x0, x1 = pts[i]["d"], pts[i + 1]["d"]
                d_star[n] = round(x0 + (0 - y0) * (x1 - x0) / (y1 - y0), 2)
                break

    free_info = {}
    for (n, key), v in raw.items():
        if key != "free":
            continue
        free_info[str(n)] = {k: (round(vv["d"], 2) if vv["d"] else None)
                             for k, vv in v.items()}

    return {"total": total, "completed": completed,
            "running": running, "series": series,
            "d_star": d_star, "free": free_info,
            "updated": time.strftime("%H:%M:%S")}


@app.get("/api/structure/{idx}/frame/{n}")
def api_structure_frame(idx: int, n: int):
    """按任务序号与帧号返回指定帧，用于结构回放。"""
    names = list(JOBS)
    if not (0 <= idx < len(names)):
        return {"ok": False, "reason": "任务序号越界"}
    base = os.path.splitext(JOBS[names[idx]])[0]
    frames = parse_trj_frames(base + "_trj.xyz")
    if not frames:
        return {"ok": False, "reason": "无轨迹"}
    n = max(1, min(n, len(frames)))
    return {"ok": True, "frame": n, "frames": len(frames),
            "energy": frames[n - 1]["energy"], "atoms": frames[n - 1]["atoms"]}


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
# 与 GitHub Pages 共用同一份页面文件，避免两处维护。
DOCS_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "index.html")

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>hCALC Monitor</title>
</head>
<body>页面文件缺失，请检查 docs/index.html</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    """优先返回与 GitHub Pages 共用的页面文件；缺失时给出提示页。"""
    try:
        with open(DOCS_INDEX, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return PAGE


if __name__ == "__main__":
    import uvicorn
    print(f"hCALC Monitor 启动: http://127.0.0.1:{PORT}")
    print(f"监控任务: {list(JOBS)}")
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")