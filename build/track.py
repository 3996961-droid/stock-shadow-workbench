# -*- coding: utf-8 -*-
"""收益跟踪：每个交易日的选股各成一批（cohort），每批只跟踪其 T+1 ~ T+10 的收益。

用法：
    python3 build/track.py

读取 build/snap/*.json（每个交易日一份），抓取每只标的在信号日之后的收盘价，
生成 build/track.json：
    cohorts[]  —— 按日期倒序，每批含 rows（逐只 T+n 逐日与累计收益）+ series（等权净值）
"""
import json, os, glob, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = BASE + "/build/snap"
TRACK = BASE + "/build/track.json"
CACHE = BASE + "/build/px_cache.json"

MAXD = 10

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}


def get(url, retry=3):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            if i == retry - 1:
                return {}
            time.sleep(0.5 * (i + 1))
    return {}


def fetch_closes(sid, beg):
    """返回 [[date, close, high, low], ...]"""
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2&fields2=f51,f52,f53,f54,f55&klt=101&fqt=0&beg=%s&end=20500101"
         % (sid, beg))
    out = []
    for line in ((get(u).get("data") or {}).get("klines") or []):
        p = line.split(",")
        try:
            out.append([p[0], float(p[2]), float(p[3]), float(p[4])])
        except Exception:
            continue
    return out


def daily_rets(bars, base_px):
    out, prev = [], base_px
    for b in bars:
        c = b[1]
        out.append(round((c / prev - 1) * 100, 2) if prev else 0.0)
        prev = c
    return out


def main():
    files = sorted(glob.glob(SNAP_DIR + "/*.json"))
    if not files:
        print("!! build/snap 为空，请先运行 screen.py")
        return
    snaps = []
    for f in files:
        d = os.path.basename(f)[:-5]
        rows = json.load(open(f, encoding="utf-8"))
        if rows:
            snaps.append((d, rows))
    snaps.sort(key=lambda x: x[0])
    print("批次", len(snaps), "个：", ", ".join(d for d, _ in snaps))

    # 去重：同一只票可能出现在多个批次，只抓一次
    need = {}
    for d, rows in snaps:
        for r in rows:
            sid = r.get("secid") or (("1." if r.get("mkt") == "沪市" else "0.") + r["code"])
            need.setdefault(r["code"], (sid, d))
    first_date = min(d for d, _ in snaps)
    print("需抓取标的", len(need), "只，起始", first_date)

    got = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {}
        for code, (sid, d) in need.items():
            futs[ex.submit(fetch_closes, sid, d.replace("-", ""))] = code
        for i, f in enumerate(futs):
            code = futs[f]
            bars = f.result()
            if bars:
                got[code] = bars
            if (i + 1) % 50 == 0:
                print("  进度", i + 1, "/", len(need))

    idx_bars = fetch_closes("1.000001", (first_date).replace("-", ""))
    idx_close = {b[0]: b[1] for b in idx_bars}

    cohorts = []
    for d, rows in snaps:
        cal = [b[0] for b in idx_bars if b[0] > d][:MAXD]
        out_rows = []
        for r in rows:
            bars = [b for b in (got.get(r["code"]) or []) if b[0] > d][:MAXD]
            base_px = r["price"]
            last = bars[-1] if bars else None
            out_rows.append({
                "code": r["code"], "name": r["name"], "cohort": d,
                "base": round(base_px, 2),
                "last": round(last[1], 2) if last else round(base_px, 2),
                "days": len(bars),
                "ret": round((last[1] / base_px - 1) * 100, 2) if (last and base_px) else 0.0,
                "hi": round(max([b[2] for b in bars]) if bars else base_px, 2),
                "lo": round(min([b[3] for b in bars]) if bars else base_px, 2),
                "path": [round((b[1] / base_px - 1) * 100, 2) for b in bars],
                "drets": daily_rets(bars, base_px),
                "pathDates": [b[0] for b in bars],
            })
        days = max([r["days"] for r in out_rows], default=0)
        series = [{"t": 0, "date": d, "nav": 0.0, "idx": 0.0}]
        for t in range(1, days + 1):
            vals = [r["path"][t - 1] for r in out_rows if r["days"] >= t]
            dt = cal[t - 1] if t - 1 < len(cal) else ""
            series.append({
                "t": t, "date": dt,
                "nav": round(sum(vals) / len(vals), 2) if vals else 0.0,
                "idx": round((idx_close.get(dt, 0) / idx_close.get(d, 1) - 1) * 100, 2)
                if idx_close.get(dt) else 0.0,
            })
        cohorts.append({
            "date": d, "n": len(out_rows), "days": days,
            "done": days >= MAXD,
            "calendar": cal,
            "windowEnd": cal[MAXD - 1] if len(cal) >= MAXD else (cal[-1] if cal else ""),
            "nav": series[-1]["nav"], "idxRet": series[-1]["idx"],
            "win": round(len([r for r in out_rows if r["ret"] > 0]) / len(out_rows) * 100, 1)
            if out_rows else 0.0,
            "series": series, "rows": out_rows,
        })

    cohorts.sort(key=lambda c: c["date"], reverse=True)
    out = {
        "maxDays": MAXD,
        "updated": cohorts[0]["date"] if cohorts else "",
        "latest": cohorts[0]["date"] if cohorts else "",
        "nCohort": len(cohorts),
        "cohorts": cohorts,
    }
    json.dump(out, open(TRACK, "w", encoding="utf-8"), ensure_ascii=False,
              separators=(",", ":"))

    for c in cohorts:
        print("  %s  共 %d 只 · 已跟踪 T+%d · 等权 %+.2f%%" %
              (c["date"], c["n"], c["days"], c["nav"]))
    print("写入", TRACK)


if __name__ == "__main__":
    main()
