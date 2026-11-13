# -*- coding: utf-8 -*-
"""盘中实时选股：异动监控 + 长上影形态筛查 + 候选保留

三步对应：
    第一步  盘中实时监控异动   每轮输出「2分钟爆量」与「快速拉升」
    第二步  14:45 看形态       用当前快照算收阳 / 上影>=2倍实体 / 主板非ST
    第三步  保留合格标的       14:45 起把形态候选写入 build/live/<日期>.json

用法：
    python3 build/live_pick.py              跑一次（默认：不论时段都扫一遍）
    python3 build/live_pick.py --watch      盘中循环，每 120 秒一轮
    python3 build/live_pick.py --final      强制按「定版」处理（不限 14:45）
    python3 build/live_pick.py --no-kline   跳过前 10 日涨停复核（快，但不准）

数据来源：全市场快照走 emq.market()（新浪全集，5578 只约 2.3 秒）；
日 K 只对形态初筛通过的几十只拉，走 emq.kline_lines()，当天有缓存。

为什么日 K 只拉初筛后的：
    全市场 5578 只逐个拉日 K 是上万次请求，之前就这么把接口打到限流过。
    形态初筛后通常只剩几十只，请求量降到两个数量级以下。

注意：盘中拉到的当日 K 线是未完成的一根，所以「前 10 日涨停」复核必须
排除最后一根，只看到昨天为止。
"""
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emq

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE_DIR = BASE + "/build/live"
CN = timezone(timedelta(hours=8))

# 只保留主板（沪 600/601/603/605，深 000/001/002/003）
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")

FINAL_AT = 1445          # 从这里开始算「定版」，把候选落盘
SHADOW_K = 2.0           # 上影 >= 2 倍实体
MIN_AMT = 3000e4         # 当日成交额下限（元），太小的票形态没意义
HOT_PCT = 5.0            # 异动：涨幅门槛
BURST = 0.5              # 异动：2 分钟成交额 / 流通市值 门槛（%）


def now_cn():
    return datetime.now(CN)


def hhmm(dt=None):
    return (dt or now_cn()).strftime("%H:%M")


def secid(code, mkt):
    return ("1." if mkt == 1 else "0.") + code


def snapshot():
    """全市场快照 → 只留主板非 ST，带上算形态要用的字段。"""
    rows = emq.market()
    out = {}
    for r in rows:
        code = str(r.get("f12") or "")
        if not code.startswith(MAIN_PREFIX):
            continue
        name = str(r.get("f14") or "")
        if "ST" in name.upper() or "退" in name:
            continue
        c, o, h, l = r.get("f2"), r.get("f17"), r.get("f15"), r.get("f16")
        pre, amt = r.get("f18"), r.get("f6")
        if not all(isinstance(x, (int, float)) for x in (c, o, h, l, pre)):
            continue
        if c <= 0 or o <= 0 or h <= 0 or pre <= 0 or not amt:
            continue
        out[code] = {
            "code": code, "name": name, "mkt": r.get("f13") or 0,
            "price": c, "open": o, "high": h, "low": l, "pre": pre,
            "chg": round((c / pre - 1) * 100, 2),
            "amount": amt,
            "turn": r.get("f8") or 0,
            "fmcap": r.get("f21") or 0,
        }
    return out


def shape(q):
    """长上影形态判定。返回指标 dict，不符合返回 None。"""
    c, o, h = q["price"], q["open"], q["high"]
    body, shadow = c - o, h - c
    if body <= 0:                       # 未收阳
        return None
    if shadow <= 0:                     # 无上影
        return None
    if shadow < SHADOW_K * body - 1e-9:  # 上影不足 2 倍实体
        return None
    if q["amount"] < MIN_AMT:
        return None
    return {
        "shadow": round(shadow / c * 100, 2),      # 上影占现价 %
        "body": round(body / o * 100, 2),          # 实体占开盘 %
        "ratio": round(shadow / body, 2),
        "lowPct": round(max(0.0, min(c, o) - q["low"]) / q["low"] * 100, 2) if q["low"] else 0,
    }


def limit_check(code, mkt, n=10):
    """前 n 个交易日（不含今天）是否有收盘涨停。返回 (通过的涨停日列表, 是否可用)。"""
    bars = []
    for line in emq.kline_lines(secid(code, mkt), 40, 0):
        p = line.split(",")
        try:
            bars.append((p[0], float(p[4])))     # (日期, 收盘)
        except Exception:
            continue
    if len(bars) < n + 3:
        return [], False                          # 数据不足，不判
    climbs = []
    for i in range(1, len(bars) - 1):             # 最后一根是今天，排除
        prev, cur = bars[i - 1][1], bars[i][1]
        if prev and (cur / prev - 1) * 100 >= 9.8:
            climbs.append(bars[i][0])
    recent = [d for d, _ in bars[-(n + 1):-1]]    # 最近 n 个已收盘交易日
    hit = [d for d in climbs if d in recent]
    return hit, True


def scan(prev=None, do_kline=True):
    """跑一轮。返回 (形态候选, 异动列表, 覆盖数)。"""
    snap = snapshot()

    # ---- 第二步：形态初筛 ----
    cand = []
    for code, q in snap.items():
        s = shape(q)
        if not s:
            continue
        q = dict(q)
        q.update(s)
        cand.append(q)
    cand.sort(key=lambda x: -x["shadow"])

    # ---- 第三步的前置：前 10 日涨停复核 ----
    if do_kline and cand:
        with ThreadPoolExecutor(max_workers=12) as ex:
            res = list(ex.map(lambda x: limit_check(x["code"], x["mkt"]), cand))
        kept = []
        for q, (hit, ok) in zip(cand, res):
            if not ok:                    # 数据不足的票标出来，不丢，让人自己判断
                q["lu10"] = []
                q["lu10ok"] = False
                kept.append(q)
            elif hit:
                continue                  # 前 10 日有涨停 → 剔除
            else:
                q["lu10"] = []
                q["lu10ok"] = True
                kept.append(q)
        cand = kept

    # ---- 第一步：异动 ----
    hot = []
    for code, q in snap.items():
        tags = []
        if q["chg"] >= HOT_PCT:
            tags.append("涨超%.0f%%" % HOT_PCT)
        if prev and code in prev:
            d = q["amount"] - prev[code]["amount"]
            if d > 0 and q["fmcap"] > 0:
                r = d / q["fmcap"] * 100
                if r >= BURST:
                    tags.append("爆量%.2f%%" % r)
                q["_burst"] = round(r, 3)
                q["_dchg"] = round(q["chg"] - prev[code]["chg"], 2)
                if q["_dchg"] >= 1.5:
                    tags.append("拉升%.1f%%" % q["_dchg"])
        if tags:
            q = dict(q)
            q["tags"] = tags
            hot.append(q)
    hot.sort(key=lambda x: -(x.get("_burst") or 0))
    return cand, hot, len(snap), snap


def show(cand, hot, covered, dt, final=False):
    print("=" * 74)
    print("盘中扫描 %s · 覆盖 %d 只 · 形态候选 %d · 异动 %d%s"
          % (dt.strftime("%H:%M:%S"), covered, len(cand), len(hot),
             "  【定版】" if final else ""))
    print("-" * 74)
    if hot:
        print("%-8s %-9s %8s %9s %8s  %s" % ("代码", "名称", "涨跌%", "额(亿)", "流通亿", "异动"))
        for q in hot[:12]:
            print("%-8s %-9s %8.2f %9.2f %8.1f  %s"
                  % (q["code"], q["name"][:8], q["chg"], q["amount"] / 1e8,
                     q["fmcap"] / 1e8, " / ".join(q["tags"])))
        print("-" * 74)
    if cand:
        print("%-8s %-9s %7s %7s %7s %8s %8s"
              % ("代码", "名称", "现价", "涨跌%", "上影%", "上影/实体", "流通亿"))
        for q in cand[:20]:
            flag = "" if q.get("lu10ok", True) else "  (K线不足未核)"
            print("%-8s %-9s %7.2f %7.2f %7.2f %8.1f %8.1f%s"
                  % (q["code"], q["name"][:8], q["price"], q["chg"],
                     q["shadow"], q["ratio"], q["fmcap"] / 1e8, flag))
        if len(cand) > 20:
            print("… 另有 %d 只" % (len(cand) - 20))
    else:
        print("（无形态候选）")
    print("=" * 74)


def save(cand, dt, final):
    os.makedirs(LIVE_DIR, exist_ok=True)
    day = dt.strftime("%Y-%m-%d")
    payload = {
        "date": day,
        "time": dt.strftime("%H:%M:%S"),
        "final": bool(final),
        "count": len(cand),
        "items": [{k: v for k, v in q.items() if not k.startswith("_")} for q in cand],
    }
    p1 = "%s/%s.json" % (LIVE_DIR, day)
    json.dump(payload, open(p1, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump(payload, open("%s/latest.json" % LIVE_DIR, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    print("写入", p1, "·", len(cand), "只")


def main():
    args = set(sys.argv[1:])
    watch = "--watch" in args
    force_final = "--final" in args
    do_kline = "--no-kline" not in args

    if not watch:
        dt = now_cn()
        cand, hot, cov, _ = scan(None, do_kline)
        final = force_final or (dt.hour * 100 + dt.minute >= FINAL_AT)
        show(cand, hot, cov, dt, final)
        if final and cand:
            save(cand, dt, True)
        elif final:
            print("定版时点但无候选，不写盘")
        return

    print("进入盘中循环（每 120 秒一轮，Ctrl+C 退出）")
    prev, last_save = None, ""
    while True:
        dt = now_cn()
        hm = dt.hour * 100 + dt.minute
        if hm > 1500:
            print("已过收盘，退出")
            break
        try:
            cand, hot, cov, snap = scan(prev, do_kline)
            final = force_final or hm >= FINAL_AT
            show(cand, hot, cov, dt, final)
            if final and cand:
                tag = dt.strftime("%H:%M")
                if tag != last_save:      # 每分钟最多写一次，避免刷盘
                    save(cand, dt, True)
                    last_save = tag
            prev = {c: {"amount": q["amount"], "chg": q["chg"]}
                    for c, q in snap.items()}     # 供下一轮做差分
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("本轮失败：%s" % e)
        time.sleep(120)


if __name__ == "__main__":
    main()
