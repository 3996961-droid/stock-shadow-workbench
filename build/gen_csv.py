# -*- coding: utf-8 -*-
"""根据当期明细导出选股明细 CSV（含两分钟放量指标）"""
import json, csv, io, os, glob

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_p = BASE + "/build/data.json"
if not os.path.exists(_p):                 # 云端无中间文件时取完整明细最新一日
    _f = sorted(glob.glob(BASE + "/build/full/*.json"), reverse=True)
    if not _f:
        raise SystemExit("!! build/full 为空，无法导出 CSV")
    _p = _f[0]
data = json.load(open(_p, encoding="utf-8"))


def amt2_peak(x):
    """峰值两分钟累计成交额（元）及其窗口；跨午休不算连续两分钟"""
    T, mc = x["trend"], x["mcap"]
    best, bi, b1 = 0.0, -1, 0.0
    for i in range(len(T)):
        a = T[i][4] or 0
        if a > b1:
            b1 = a
        if i + 1 >= len(T) or T[i][0] == "11:30":
            continue
        s2 = a + (T[i + 1][4] or 0)
        if s2 > best:
            best, bi = s2, i
    win = f'{T[bi][0]}-{T[bi + 1][0]}' if bi >= 0 else ""
    pct = best / mc * 100 if mc else 0.0
    return best, win, pct, b1


for x in data:
    a2, win, pct, a1 = amt2_peak(x)
    x["amt2"], x["amt2Win"], x["amt2Pct"], x["amt1"] = a2, win, pct, a1
    x["amt1Pct"] = a1 / x["mcap"] * 100 if x["mcap"] else 0.0

data.sort(key=lambda x: -x["amt2Pct"])

HEAD = ["序号", "代码", "名称", "市场", "现价", "涨跌幅%", "开盘", "最高", "最低",
        "上影线%", "实体%", "上影/实体倍数", "换手率%",
        "总市值(亿)", "成交额(亿)",
        "2分钟峰值额(亿)", "2分钟额占市值%", "峰值窗口",
        "单分钟峰值额(亿)", "单分钟占市值%", "达标≥0.5%", "形态分层"]

DAY = data[0].get("date") or "unknown"
path = BASE + "/选股明细_%s.csv" % DAY.replace("-", "")
with io.open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(HEAD)
    for i, x in enumerate(data, 1):
        b = x["daily"][-1]
        w.writerow([
            i, x["code"], x["name"], x["mkt"], f'{x["price"]:.2f}', f'{x["chg"]:.2f}',
            f"{b[1]:.2f}", f"{b[2]:.2f}", f"{b[3]:.2f}",
            f'{x["shadow"]:.2f}', f'{x["body"]:.2f}', f'{x["ratio"]:.2f}',
            f'{x["turn"]:.2f}', f'{x["mcap"]/1e8:.1f}', f'{x["amount"]/1e8:.2f}',
            f'{x["amt2"]/1e8:.2f}', f'{x["amt2Pct"]:.2f}', x["amt2Win"],
            f'{x["amt1"]/1e8:.2f}', f'{x["amt1Pct"]:.2f}',
            "是" if x["amt2Pct"] >= 0.5 - 1e-9 else "否",
            "核心（长上影）" if x["tier"] == "core" else "弱形态",
        ])

hit = [x for x in data if x["amt2Pct"] >= 0.5 - 1e-9]
print("导出", len(data), "行；两分钟额≥市值0.5% 共", len(hit), "只：",
      "、".join(f'{x["name"]}({x["code"]}) {x["amt2Pct"]:.2f}%' for x in hit))
