# hCALC Monitor

PVDF / 六方氮化硼（h-BN）界面计算的实时监控看板。

## 它做什么

把本机正在运行的 ORCA 量子化学计算进度（优化步数、当前能量、收敛曲线）
实时展示在网页上，供课题组远程查看。

## 两个访问入口

| 入口 | 地址 | 特性 |
|---|---|---|
| 实时看板 | https://hostoreke.tailacb782.ts.net/ | 5 秒刷新，需电脑在线 |
| GitHub Pages | https://hostoreke.github.io/hcalc-monitor/ | 优先取实时数据，取不到则显示最后一次静态快照 |

**快照机制**：`snapshot.py` 定期读取 ORCA 输出并生成 `docs/snapshot.json`，
提交到仓库后即使电脑关机，网页仍能显示最后状态。

## 架构

```
本机 ORCA 进程 ──写 .out──> monitor.py 解析
                                  │
                                  ├─ SSE ──> https://hostoreke.tailacb782.ts.net/
                                  │          （Tailscale Funnel，公网可达）
                                  └─ snapshot.py ──> docs/snapshot.json ──> GitHub Pages
```

## 本地运行

```powershell
# 后端（8000 端口）
& 'D:\AIFlow\.venv\Scripts\python.exe' monitor.py

# 生成静态快照
& 'D:\AIFlow\.venv\Scripts\python.exe' snapshot.py

# 快照自动刷新并推送（每 5 分钟）
& 'D:\AIFlow\.venv\Scripts\python.exe' snapshot.py --watch
```

## 技术选择说明

- **前端不引 CDN**：图表用原生 Canvas 手绘。本项目所在网络对部分 CDN 域名受限，
  引外部库会导致页面白屏。
- **SSE 而非 WebSocket**：数据单向流动，SSE 穿透隧道与代理更省心。
- **静态快照兜底**：GitHub Pages 无法运行 Python，因此用快照保证离线可达。

## 数据安全

仓库只保存任务名、状态、步数、能量数值与时间戳。
**不包含**输入文件、分子坐标、`.chk`/`.gbw` 等计算资产（已在 `.gitignore` 中排除）。

## 相关项目

PVDF α/β 构象能差的第一性原理计算：
无限长链外推值 **13.80 kJ/mol 每重复单元**。
h-BN 界面结合能（B12N12 碎片）ΔΔE = **−11.67 kJ/mol**。
