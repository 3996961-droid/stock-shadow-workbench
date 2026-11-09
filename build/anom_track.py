# -*- coding: utf-8 -*-
"""低位异动：T+1 ~ T+5 个股收益跟踪

口径
    每一批「低位异动」名单以信号日收盘价为成本，向后跟踪 5 个交易日，
    记录每只票 T+1..T+5 的累计涨跌幅，以及窗口内最高/最低（浮盈浮亏极值）。
    等权平均做批次汇总，同时给出上证指数同期涨幅用于算超额。

数据
    输入  build/anomaly/<日期>.json     每天扫描出的异动名单（含信号日收盘价）
    输出  build/anomtrack.json          跟踪台账（页面直接读）

    已满 5 个交易日的批次会封存（不再请求行情）；未满的天天刷新。
    前复权（fqt=1）取价，避免除权造成假跳空；信号价以 K 线里信号日那一根为准。
"""
import json, os, glob, io, time, urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANOM_DIR = BASE + "/build/anomaly"
OUT = BASE + "/build/anomtrack.json"
MAXD = 5
CN = timezone(timedelta(hours=8))
UT = "fa5fd1943c7b386f172d6893dbfba10b"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


def jget(url, retry=3, timeout=20):
    for _ in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(0.4)
    return {}


def mkt_of(code):
    return 1 if code[:1] == "6" else 0


def kline(secid, beg="0", lmt=400):
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&ut=%s"
         "&fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
         "&klt=101&fqt=1&beg=%s&end=20500101&lmt=%d" % (secid, UT, beg, lmt))
    ks = ((jget(u).get("data") or {}).get("klines") or [])
    rows = []
    for x in ks:
        p = x.split(",")
        if len(p) < 11:
            continue
        try:
            rows.append({"d": p[0], "c": float(p[2]), "h": float(p[3]), "l": float(p[4])})
        except Exception:
            continue
    rows.sort(key=lambda r: r["d"])
    return rows


def idx_calendar():
    """上证指数日线：给每个批次算同期基准。"""
    return {r["d"]: r["c"] for r in kline("1.000001", beg="20240101", lmt=800)}


def track_one(it, date, cal):
    rows = kline("%d.%s" % (mkt_of(it["code"]), it["code"]), beg="20240101", lmt=400)
    on = [r for r in rows if r["d"] == date]
    base = on[0]["c"] if on else it.get("close")
    fwd = [r for r in rows if r["d"] > date][:MAXD]
    if not base:
        return None
    rets = [round((r["c"] / base - 1) * 100, 2) for r in fwd]
    why = it.get("why") or {}
    return {
        "code": it.get("code"),
        "name": it.get("name"),
        "base": round(base, 2),
        "last": round(fwd[-1]["c"], 2) if fwd else round(base, 2),
        "days": len(rets),
        "rets": rets,
        "dates": [r["d"] for r in fwd],
        "close": [round(r["c"], 2) for r in fwd],
        "mfe": round((max(r["h"] for r in fwd) / base - 1) * 100, 2) if fwd else 0.0,
        "mae": round((min(r["l"] for r in fwd) / base - 1) * 100, 2) if fwd else 0.0,
        "board": (it.get("board") or {}).get("name") or "",
        "sneak": it.get("sneak") or "",
        "tier": why.get("lvl") or "",
        "exp": why.get("days") or 0,
        "score": it.get("score") or 0,
        "chg0": it.get("chg") or 0,
        "dd": it.get("dd") or 0,
    }


def summarize(items, date, cal):
    """批次汇总：每个 T+k 的等权平均、胜率，以及上证同期。"""
    avg = []
    base_idx = cal.get(date)
    for k in range(MAXD):
        vs = [x["rets"][k] for x in items if len(x["rets"]) > k]
        avg.append({
            "t": k + 1,
            "n": len(vs),
            "ret": round(sum(vs) / len(vs), 2) if vs else None,
            "win": round(len([v for v in vs if v > 0]) / len(vs) * 100, 0) if vs else None,
        })
    # 指数同期：按本批已走到的交易日
    idx = []
    days = max([x["days"] for x in items] or [0])
    for k in range(days):
        d = next((x["dates"][k] for x in items if len(x["dates"]) > k), "")
        if base_idx and d and cal.get(d):
            idx.append(round((cal[d] / base_idx - 1) * 100, 2))
        else:
            idx.append(None)
    return {"avg": avg, "idx": idx}


def main():
    files = sorted(glob.glob(ANOM_DIR + "/*.json"))
    if not files:
        print("!! 没有异动名单，先跑 build/anomaly.py")
        return

    old = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(io.open(OUT, encoding="utf-8"))
            for c in prev.get("cohorts") or []:
                old[c.get("date")] = c
        except Exception:
            old = {}

    cal = idx_calendar()
    cohorts = []
    for fp in files:
        day = os.path.basename(fp)[:-5]
        try:
            data = json.load(io.open(fp, encoding="utf-8"))
        except Exception as e:
            print("!! 跳过", fp, e)
            continue
        items = data.get("items") or []
        prev = old.get(day)
        if prev and (prev.get("days") or 0) >= MAXD:
            cohorts.append(prev)                       # 已封存，直接沿用
            print("  %s  已封存 T+%d（%d 只）" % (day, prev.get("days"), len(prev.get("items") or [])))
            continue

        with ThreadPoolExecutor(max_workers=8) as ex:
            rows = [r for r in ex.map(lambda x: track_one(x, day, cal), items) if r]
        rows.sort(key=lambda x: -((x["rets"][-1] if x["rets"] else -999)))

        days = max([r["days"] for r in rows] or [0])
        summary = summarize(rows, day, cal)
        cohorts.append({
            "date": day, "n": len(rows), "days": days, "sealed": days >= MAXD,
            "idx": summary["idx"], "avg": summary["avg"], "items": rows,
        })
        print("  %s  T+%d / T+%d  %d 只%s" % (day, days, MAXD, len(rows), "  已封存" if days >= MAXD else ""))

    cohorts.sort(key=lambda c: c["date"], reverse=True)
    out = {
        "updated": datetime.now(CN).strftime("%Y-%m-%d %H:%M"),
        "maxDays": MAXD,
        "cohorts": cohorts,
    }
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print("T+1~T+%d 跟踪：%d 个批次，最新 %s T+%d  → %s"
          % (MAXD, len(cohorts), cohorts[0]["date"] if cohorts else "-",
             cohorts[0]["days"] if cohorts else 0, os.path.relpath(OUT, BASE)))


if __name__ == "__main__":
    main()
