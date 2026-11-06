# -*- coding: utf-8 -*-
"""按交易日回补完整图表数据（日K + 分时），生成 build/full/<日期>.json。

用途：工作台要按日期看历史选股名单，历史日期的 K 线与分时必须在本地留存。
本脚本以东财接口按指定日期重新抓取（分时接口 ndays=5，只支持最近 5 个交易日）。

用法：
    python3 build/backfill.py 2026-11-05
"""
import json, os, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = BASE + "/build/snap"
FULL_DIR = BASE + "/build/full"

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
            time.sleep(0.4 * (i + 1))
    return {}


def secid_of(r):
    return r.get("secid") or (("1." if r.get("mkt") == "沪市" else "0.") + r["code"])


def fetch(secid, day):
    """返回 (bars, trend, preClose)，只保留 <= day 的日K 与该日分时。"""
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
         "&klt=101&fqt=0&beg=0&end=20500101&lmt=200" % secid)
    bars = []
    for line in ((get(u).get("data") or {}).get("klines") or []):
        p = line.split(",")
        try:
            bars.append([p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                         float(p[5]), float(p[6])])
        except Exception:
            continue
    bars = [b for b in bars if b[0] <= day]

    u2 = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s"
          "&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
          "&iscr=0&ndays=5" % secid)
    trend = []
    for line in ((get(u2).get("data") or {}).get("trends") or []):
        p = line.split(",")
        try:
            if not p[0].startswith(day):
                continue
            trend.append([p[0][-5:], float(p[2]), float(p[7]), int(float(p[5])),
                          round(float(p[6]), 0)])
        except Exception:
            continue
    pre = bars[-2][4] if len(bars) >= 2 else 0
    return bars, trend, pre


def main():
    day = sys.argv[1] if len(sys.argv) > 1 else ""
    if not day:
        print("用法: python3 build/backfill.py 2026-11-05")
        sys.exit(1)
    sp = "%s/%s.json" % (SNAP_DIR, day)
    if not os.path.exists(sp):
        print("!! 缺少快照", sp)
        sys.exit(1)
    snap = json.load(open(sp, encoding="utf-8"))
    print("回补 %s · 快照 %d 只" % (day, len(snap)))

    out, miss = [], 0

    def work(r):
        bars, trend, pre = fetch(secid_of(r), day)
        if len(bars) < 22 or not trend:
            return None
        lb = bars[-1]
        if lb[0] != day:                       # 当日停牌/无K线
            return None
        limit_days = []
        for i in range(1, len(bars)):
            pv = bars[i - 1][4]
            if pv and (bars[i][4] / pv - 1) * 100 >= 9.8:
                limit_days.append(bars[i][0])
        d = dict(r)
        d.update({
            "date": day,
            "open": round(lb[1], 2), "high": round(lb[2], 2), "low": round(lb[3], 2),
            "price": round(lb[4], 2),
            "preClose": round(pre, 2),
            "limit": limit_days[-6:],
            "secid": secid_of(r),
            "daily": [[b[0], b[1], b[2], b[3], b[4], round(b[5] / 100.0, 2), int(b[6])]
                      for b in bars][-80:],
            "trend": trend,
            "amount": r.get("amount") or sum(t[4] for t in trend),
        })
        return d

    with ThreadPoolExecutor(max_workers=8) as ex:
        for i, res in enumerate(ex.map(work, snap)):
            if res:
                out.append(res)
            else:
                miss += 1
            if (i + 1) % 20 == 0:
                print("  进度", i + 1, "/", len(snap))

    if not out:
        print("!! 未取到任何数据（可能该日已超出分时接口 5 日窗口）")
        sys.exit(1)

    out.sort(key=lambda x: -x.get("shadow", 0))
    os.makedirs(FULL_DIR, exist_ok=True)
    fp = "%s/%s.json" % (FULL_DIR, day)
    json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("写入", fp, "·", len(out), "只；缺图表 %d 只" % miss)


if __name__ == "__main__":
    main()
