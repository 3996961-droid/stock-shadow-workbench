# -*- coding: utf-8 -*-
"""每日更新总入口：筛选 -> 收益跟踪 -> CSV -> 生成页面。

用法：
    python3 build/daily.py

每个交易日收盘后（15:05 之后）运行一次。非交易日运行不会有副作用：
筛选结果仍是最近一个交易日，快照按日期覆盖，批次不会重复。
"""
import os, subprocess, sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable or "python3"


def run(name, script, allow_fail=False):
    print("\n=== %s ===" % name, flush=True)
    r = subprocess.run([PY, os.path.join(BASE, "build", script)],
                       cwd=BASE, capture_output=True, text=True)
    out = (r.stdout or "").strip()
    if out:
        print(out[-1500:])
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if err:
            print(err[-800:])
        if allow_fail:
            print("!! %s 未完成（退出码 %d），继续后续步骤" % (name, r.returncode))
            return False
        print("!! %s 失败（退出码 %d），已中止" % (name, r.returncode))
        sys.exit(r.returncode)
    return True


run("1/4 全市场筛选", "screen.py")
run("2/4 收益跟踪", "track.py")
run("3/4 导出 CSV", "gen_csv.py", allow_fail=True)
run("4/4 生成页面", "build.py")
print("\n完成：index.html 已更新")
