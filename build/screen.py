# -*- coding: utf-8 -*-
"""全市场筛选：长上影阳线（上影 >= 2 倍实体），剔除科创/创业/北交/ST，前 10 个交易日无涨停。

产出：
    build/snap/<日期>.json   轻量快照（只含选股指标 + 市值 + 放量指标，用于收益跟踪）
    build/data.json          当期明细（含 80 根日K + 分时，用于工作台图表）

用法：
    python3 build/screen.py            # 自动取最近一个交易日
    python3 build/screen.py 2026-11-05 # 指定日期（回补用）
"""
import json, os, sys, time, glob, urllib.request
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emq                                     # 统一取数层（东财列表被封时的重建方案）

# 仓库根目录（build/ 的上一级），云端与本地相同逻辑，避免任何绝对路径
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = BASE + "/build/snap"
FULL_DIR = BASE + "/build/full"
DATA = BASE + "/build/data.json"

CN = timezone(timedelta(hours=8))          # 交易所时间，与运行机器所在时区无关

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

# 只保留主板（沪 600/601/603/605，深 000/001/002/003）
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")

LIST_URL = ("https://push2.eastmoney.com/api/qt/clist/get?po=0&np=1&fltt=2&invt=2&fid=f3"
            "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            "&fields=f12,f13,f14,f2,f3,f6,f8,f15,f16,f17,f18,f20,f21&pn=%d&pz=100")


def get(url, retry=4, quiet=False):
    last = None
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            if i < retry - 1:
                time.sleep(0.8 * (i + 1))
    if not quiet:
        print("   !! 请求失败：%s …（%s）" % (url[:80], type(last).__name__))
    return {}


def all_quotes():
    """全市场快照，返回 (列表, 总数)。

    取数交给 build/emq.py 统一处理：东财 clist 可用就用它，被封时自动改走
    新浪全集 + ulist 批量重建。是否可用由调用方的完整性校验兜底。
    """
    out, total = emq.diff_all(
        "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
        "f12,f13,f14,f2,f3,f6,f8,f15,f16,f17,f18,f20,f21", po=0)
    print("   分页完成：%d 只（接口报告 total=%s）" % (len(out), total))
    return out, total


def pick_candidates(quotes):
    cand = []
    for q in quotes:
        code = q.get("f12") or ""
        name = q.get("f14") or ""
        if not code.startswith(MAIN_PREFIX):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        c, o, h, l = q.get("f2"), q.get("f17"), q.get("f15"), q.get("f16")
        pre, amt = q.get("f18"), q.get("f6")
        if not all(isinstance(x, (int, float)) for x in (c, o, h, l, pre)):
            continue
        if c <= 0 or o <= 0 or h <= 0 or l <= 0 or pre <= 0 or not amt:
            continue
        body, shadow = c - o, h - c
        if body <= 0 or shadow <= 0:                     # 必须收阳且留有上影
            continue
        if shadow < 2 * body - 1e-9:                       # 上影 >= 2 倍实体
            continue
        cand.append({
            "code": code, "name": name, "mkt": "沪市" if q.get("f13") == 1 else "深市",
            "price": round(c, 2), "chg": round((c / pre - 1) * 100, 2),
            "open": o, "high": h, "low": l,
            "shadow": round(shadow / c * 100, 2),
            "body": round(body / o * 100, 2),
            "ratio": round(shadow / body, 2) if body else 0,
            "turn": round(q.get("f8") or 0, 2),
            "mcap": q.get("f20") or 0, "fmcap": q.get("f21") or 0,
            "amount": amt,
        })
    return cand


def secid(r):
    return ("1." if r["mkt"] == "沪市" else "0.") + r["code"]


def fetch_detail(r):
    sid = secid(r)
    # 日K：多取一些，覆盖 10 日涨停核查 + 80 根绘图。取数走 emq（麦蕊/东财/腾讯）
    bars = []
    for line in emq.kline_lines(sid, 120, 0):
        p = line.split(",")
        try:
            bars.append([p[0], float(p[1]), float(p[3]), float(p[4]), float(p[2]),
                         float(p[5]), float(p[6])])
        except Exception:
            continue

    # 分时：麦蕊 1 分钟K线优先，缺字段时由 emq 内部回退
    trend, pre = emq.trend_rows(sid)
    return bars, trend, pre


def enrich(r, bars, trend):
    """用日K核对前 10 个交易日是否涨停，并补涨幅、下影线、放量指标。"""
    if len(bars) < 22:
        return None
    limit_days = []
    for i in range(1, len(bars)):
        prev = bars[i - 1][4]
        if prev and (bars[i][4] / prev - 1) * 100 >= 9.8:
            limit_days.append(bars[i][0])
    last10 = [b[0] for b in bars[-11:-1]]
    lu10 = [d for d in limit_days if d in last10]
    if lu10:                                   # 前 10 个交易日有涨停收盘 -> 剔除
        return None

    lb = bars[-1]
    low_abs = round(max(0.0, min(lb[1], lb[4]) - lb[3]), 3)
    low_pct = round(low_abs / lb[3] * 100, 3) if lb[3] else 0.0
    c = lb[4]
    chg5 = round((c / bars[-6][4] - 1) * 100, 2) if len(bars) >= 6 else 0.0
    chg20 = round((c / bars[-21][4] - 1) * 100, 2) if len(bars) >= 21 else 0.0

    # 峰值连续两分钟成交额（跨午休不算连续）
    amts = [t[4] or 0 for t in trend]
    best2, at2, best1, at1 = 0, -1, 0, -1
    for i, a in enumerate(amts):
        if a > best1:
            best1, at1 = a, i
        if i + 1 < len(amts) and trend[i][0][-5:] != "11:30":
            s2 = a + amts[i + 1]
            if s2 > best2:
                best2, at2 = s2, i
    mc = r["mcap"] or 0
    r.update({
        "open": round(lb[1], 2), "high": round(lb[2], 2), "low": round(lb[3], 2),
        "lowAbs": low_abs, "lowPct": low_pct,
        "chg5": chg5, "chg20": chg20,
        "amt2": best2, "amt2Pct": round(best2 / mc * 100, 3) if mc else 0.0,
        "amt2Win": (trend[at2][0][-5:] + "-" + trend[at2 + 1][0][-5:])
        if (at2 >= 0 and at2 + 1 < len(trend)) else "",
        "amt1": best1, "amt1Pct": round(best1 / mc * 100, 3) if mc else 0.0,
        "lu10": [], "limit": limit_days[-6:],
        "tier": "core" if r["shadow"] >= 2 else "weak",
        "date": lb[0],
        "preClose": round(bars[-2][4], 2),
        "daily": [[b[0], b[1], b[2], b[3], b[4], round(b[5] / 100.0, 2), int(b[6])]
                  for b in bars][-80:],
        "trend": [[t[0][-5:], t[1], t[2], t[3], int(t[4])] for t in trend],
        "secid": secid(r),
        "amount": r["amount"] or sum(t[4] for t in trend),
    })
    return r


HEAVY = {"daily", "trend", "limit", "secid", "preClose"}


def main():
    rng = sys.argv[1] if len(sys.argv) > 1 else "latest"
    print("拉取全市场快照 ...")
    quotes, total = all_quotes()

    # 完整性闸门：接口限流时会只返回第一页，必须拦住，绝不能用残数据覆盖历史
    if total and total < 3000:
        print("!! 接口报告的全市场总数异常（total=%d，A股全市场通常 5000+），"
              "多为限流或降级返回，已中止，未写入任何数据" % total)
        sys.exit(3)
    if total and len(quotes) < total * 0.95:
        print("!! 快照不完整：只取到 %d/%d 只（%.1f%%），已中止，未写入任何数据"
              % (len(quotes), total, len(quotes) / total * 100))
        sys.exit(3)

    cand = pick_candidates(quotes)
    print("全市场 %d 只 · 形态初筛（阳线 + 上影>=2倍实体 + 主板非ST）%d 只" % (len(quotes), len(cand)))

    print("逐只核对前 10 个交易日涨停、抓日K与分时 ...")
    res, drop = [], 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(fetch_detail, r): r for r in cand}
        for i, f in enumerate(futs):
            r = futs[f]
            try:
                bars, trend, pre = f.result()
            except Exception:
                bars, trend, pre = [], [], 0
            e = enrich(r, bars, trend) if (bars and trend) else None
            if e:
                res.append(e)
            else:
                drop += 1
            if (i + 1) % 20 == 0:
                print("  进度", i + 1, "/", len(cand))

    dates = [x["date"] for x in res]
    if not dates:
        print("!! 没有任何标的，可能今天不是交易日或尚未收盘，已中止")
        sys.exit(1)
    day = dates[-1] if rng == "latest" else rng
    res = [x for x in res if x["date"] == day]
    if not res:
        print("!! %s 无符合条件的标的" % day)
        sys.exit(1)

    # 盘中保护：当日尚未收盘时不写盘，避免把盘中价当成收盘信号
    now = datetime.now(CN)
    if day == now.strftime("%Y-%m-%d") and now.hour * 60 + now.minute < 15 * 60 + 5:
        print("!! %s 尚未收盘（北京时间 %s），已中止，未改动任何数据" % (day, now.strftime("%H:%M")))
        sys.exit(2)

    res.sort(key=lambda x: -x["shadow"])

    # 数量骤降保护：与上一份快照对比，异常下跌说明抓取或接口出了问题
    prev = [f for f in sorted(glob.glob(SNAP_DIR + "/*.json"))
            if os.path.basename(f)[:-5] != day]
    if prev:
        pfp = prev[-1]
        pday = os.path.basename(pfp)[:-5]
        try:
            pn = len(json.load(open(pfp, encoding="utf-8")))
        except Exception:
            pn = 0
        if pn and len(res) < pn * 0.3:
            print("!! %s 只筛出 %d 只，而上一交易日 %s 有 %d 只，降幅异常，"
                  "已中止，未写入任何数据" % (day, len(res), pday, pn))
            sys.exit(4)

    os.makedirs(SNAP_DIR, exist_ok=True)

    # 轻量快照（供收益跟踪累计）
    light = [{k: v for k, v in x.items() if k not in HEAVY} for x in res]
    sp = "%s/%s.json" % (SNAP_DIR, day)
    json.dump(light, open(sp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("写入快照", sp, "·", len(light), "只")

    # 当期明细（含图表数据）
    json.dump(res, open(DATA, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    core = len([x for x in res if x["tier"] == "core"])
    hit = len([x for x in res if x["amt2Pct"] >= 0.5 - 1e-9])
    print("写入明细 %s · %d 只（核心 %d / 弱形态 %d；2分钟额>=市值0.5%% 的 %d 只）"
          % (DATA, len(res), core, len(res) - core, hit))

    # 完整明细按交易日留档：历史页面按日期查看名单与图表都从这里取
    os.makedirs(FULL_DIR, exist_ok=True)
    fp = "%s/%s.json" % (FULL_DIR, day)
    json.dump(res, open(fp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("写入完整存档", fp)
    print("日期", day)


if __name__ == "__main__":
    main()
