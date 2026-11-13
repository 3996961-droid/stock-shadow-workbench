# -*- coding: utf-8 -*-
"""生成盘中选股的「全市场过滤器」——让前端能独立复现选股逻辑

盘中要在浏览器里实时筛长上影，需要两样东西：
    1. 代码全集（主板、非 ST）        → 快照里拿不到全集，得预先生成
    2. 前 10 个交易日有没有涨停        → 依赖历史日线，盘中算不出来

这两样每天只需生成一次，产出 build/live/universe.json，随页面内嵌。
前端拿到它 + 实时快照，就能独立完成「收阳 / 上影≥2倍实体 / 前10日无涨停」
的完整判定，不需要后端参与。

为什么用 Tushare 而不是逐只拉 K 线：
    daily 接口按 trade_date 一次返回全市场（5573 行 / 0.54 秒），
    10 个交易日只要 10 个请求。逐只拉是 3000 次请求，之前把接口打到限流过。

产出格式（尽量小，随页面内嵌）：
    {"base": "2026-11-12",          # 这批评审截至哪个交易日
     "gen":  "2026-11-13 10:30",    # 生成时间
     "days": 10,
     "codes": "600000,600004,...",  # 主板非 ST 全集，逗号分隔
     "limit": "0f3c..."}           # 十六进制位图，第 i 位=1 表示该股前10日有涨停

用法：
    python3 build/gen_universe.py            # 截至最近一个已收盘交易日
    python3 build/gen_universe.py 2026-11-12 # 指定基准日
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emq

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = BASE + "/build/live"
OUT = OUT_DIR + "/universe.json"

CN = timezone(timedelta(hours=8))

# 只保留主板（沪 600/601/603/605，深 000/001/002/003）
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
LIMIT_PCT = 9.8          # 主板涨停判定（收盘涨幅 ≥ 9.8%）
DAYS = 10


def now_cn():
    return datetime.now(CN)


def trade_days(end_yyyymmdd, n):
    """截至 end 往前取 n 个已收盘交易日（升序）。"""
    start = (datetime.strptime(end_yyyymmdd, "%Y%m%d") - timedelta(days=40)).strftime("%Y%m%d")
    cal = emq.ts_api("trade_cal",
                     {"exchange": "SSE", "start_date": start,
                      "end_date": end_yyyymmdd, "is_open": "1"},
                     "cal_date")
    ds = sorted(x["cal_date"] for x in cal if x.get("cal_date"))
    return ds[-n:] if len(ds) >= n else ds


def main():
    base = sys.argv[1].replace("-", "") if len(sys.argv) > 1 else None
    if not base:
        # 默认取「最近一个已收盘的交易日」：盘中运行时要退到昨天
        d = now_cn()
        if d.hour * 100 + d.minute < 1505:
            d = d - timedelta(days=1)
        base = d.strftime("%Y%m%d")
        # 往前挪到真实交易日
        while True:
            cal = emq.ts_api("trade_cal",
                             {"exchange": "SSE", "start_date": base, "end_date": base,
                              "is_open": "1"}, "cal_date")
            if cal:
                break
            base = (datetime.strptime(base, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    print("基准交易日", base)

    # ---- 1. 主板非 ST 全集 ----
    sb = emq.ts_api("stock_basic", {"exchange": "", "list_status": "L"},
                    "ts_code,symbol,name,market")
    if not sb:
        raise SystemExit("!! stock_basic 取不到，检查 TUSHARE_TOKEN")
    codes = []
    for x in sb:
        sym = x.get("symbol") or ""
        name = x.get("name") or ""
        if x.get("market") != "主板":
            continue
        if not sym.startswith(MAIN_PREFIX):
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        codes.append(sym)
    codes.sort()
    print("主板非 ST：%d 只" % len(codes))
    keep = set(codes)

    # ---- 2. 最近 DAYS 个交易日的全市场涨幅 ----
    days = trade_days(base, DAYS)
    print("评审区间 %s → %s（%d 天）" % (days[0], days[-1], len(days)))
    limit = set()
    for i, day in enumerate(days):
        rows = emq.ts_api("daily", {"trade_date": day}, "ts_code,close,pre_close,pct_chg")
        if not rows:
            print("   !! %s 无数据，跳过" % day)
            continue
        n = 0
        for r in rows:
            sym = (r.get("ts_code") or "").split(".")[0]
            if sym not in keep:
                continue
            pct = r.get("pct_chg")
            if pct is None:
                try:
                    pct = (float(r["close"]) / float(r["pre_close"]) - 1) * 100
                except Exception:
                    continue
            if float(pct) >= LIMIT_PCT:
                limit.add(sym)
                n += 1
        print("   %s → %d 只涨停（主板）" % (day, n))

    # ---- 3. 位图编码 ----
    bits = bytearray((len(codes) + 7) // 8)
    for i, c in enumerate(codes):
        if c in limit:
            bits[i >> 3] |= 1 << (i & 7)

    payload = {
        "base": "%s-%s-%s" % (base[:4], base[4:6], base[6:]),
        "gen": now_cn().strftime("%Y-%m-%d %H:%M"),
        "days": len(days),
        "codes": ",".join(codes),
        "limit": bits.hex(),
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    json.dump(payload, open(OUT, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    size = os.path.getsize(OUT)
    print("写入 %s · %.1f KB · 其中前 %d 日涨停 %d 只" % (OUT, size / 1024, DAYS, len(limit)))

    # ---- 4. 注入模板，生成盯盘页 ----
    tpl_path = BASE + "/build/live_template.html"
    if os.path.exists(tpl_path):
        tpl = open(tpl_path, encoding="utf-8").read()
        if "__UNIVERSE__" not in tpl:
            print("   !! 模板缺少 __UNIVERSE__ 占位，跳过生成")
        else:
            page = tpl.replace("__UNIVERSE__", json.dumps(payload, ensure_ascii=False))
            out_html = BASE + "/live.html"
            open(out_html, "w", encoding="utf-8").write(page)
            print("写入 %s · %.1f KB" % (out_html, len(page.encode()) / 1024))


if __name__ == "__main__":
    main()
