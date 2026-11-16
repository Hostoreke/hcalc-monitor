# -*- coding: utf-8 -*-
"""
================================================================================
生成静态快照 snapshot.json，供 GitHub Pages 在电脑离线时兜底展示
================================================================================

原理
----
GitHub Pages 只能托管静态文件，跑不了 Python。所以：
  · 电脑在线时，网页直接连 Tailscale Funnel 取实时数据
  · 电脑离线时，网页读取仓库里的 snapshot.json（最后一次生成的快照）

用法
----
    python snapshot.py            # 生成一次
    python snapshot.py --watch    # 每 5 分钟刷新并自动 git 提交推送

安全
----
快照里只有任务名、状态、步数、能量、时间，**不含**输入文件或分子坐标。
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# 复用监控服务的解析逻辑，避免两处逻辑不一致
from monitor import (snapshot as read_snapshot,      # noqa: E402
                     JOBS, latest_structure)

OUT = os.path.join(HERE, "docs", "snapshot.json")


def write_snapshot():
    data = read_snapshot()
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "snapshot",
        "jobs": data,
        # 内嵌各任务最新一帧结构，使电脑离线时网页仍能显示 3D 分子
        "structures": [latest_structure(p) for p in JOBS.values()],
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    n_run = sum(1 for v in data.values() if v["status"] == "运行中")
    n_done = sum(1 for v in data.values() if v["status"] == "已完成")
    print(f"[{payload['generated_at']}] 快照已更新：{len(data)} 个任务"
          f"（运行中 {n_run}，已完成 {n_done}）")
    return payload


def git_push():
    """把快照提交并推送到 GitHub。失败不抛出，只提示。"""
    try:
        subprocess.run(["git", "add", "docs/snapshot.json"], cwd=HERE,
                       check=True, capture_output=True)
        msg = f"chore: update snapshot {datetime.now():%Y-%m-%d %H:%M}"
        r = subprocess.run(["git", "commit", "-m", msg], cwd=HERE,
                           capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            print(f"  提交跳过：{(r.stdout + r.stderr).strip()[:120]}")
        subprocess.run(["git", "push"], cwd=HERE, check=True, capture_output=True)
        print("  已推送到 GitHub")
    except FileNotFoundError:
        print("  未找到 git，跳过推送（仅本地生成快照）")
    except subprocess.CalledProcessError as e:
        print(f"  推送失败：{e}")


if __name__ == "__main__":
    watch = "--watch" in sys.argv
    if not watch:
        write_snapshot()
        sys.exit(0)

    print("每 5 分钟生成快照并推送，Ctrl+C 停止")
    while True:
        try:
            write_snapshot()
            git_push()
        except Exception as e:                          # noqa: BLE001
            print(f"  本轮出错：{e}")
        time.sleep(300)

