# -*- coding: utf-8 -*-
"""题材雷达：低位新题材扫描 → 信息爆发检测 → 龙头识别 → 财务排雷

逻辑链（四道门）：
    1. 概念板块全量 → 剔除统计型标签，只留产业题材
    2. 每个题材拉日K → 算 250 日位置分位、距高点回撤、成立时长
    3. 低位门：回撤够深 + 位置够低
       新题门：板块成立时间短（K线根数少）
       爆发门：近 24 小时资讯量突增
    4. 门内题材拉成分股（仅主板）→ 龙头打分 → 财务排雷

设计取舍：
    - 只算主板（600/601/603/605/000/001/002/003），与长上影选股口径一致
    - "爆发"用 近24h 报道量 / 日均发文密度，避免长期热门题材被误判为爆发
    - 财务排雷分两档：硬雷剔除、软雷标黄（用户选"适中"）

产出：build/theme/<日期>.json
用法：
    python3 build/theme_scan.py
"""
import json, os, re, sys, time, glob, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEME_DIR = BASE + "/build/theme"

CN = timezone(timedelta(hours=8))
UT = "fa5fd1943c7b386f172d6893dbfba10b"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

# ---------------- 可调参数（会原样写进 JSON，页面上展示，便于对照） ----------------
P = {
    "newBars": 800,      # 板块 K 线不足这么多根 → 视为"新题材"（约 3.3 年）
    "lowDd": 30.0,       # 距 250 日高点回撤 ≥ 此值(%) → 低位（市场整体深跌时会自动提高）
    "lowPos": 20.0,      # 250 日位置分位 ≤ 此值(%) → 低位
    "hot24": 8,          # 近 24 小时报道 ≥ 此条数 → 有热度（绝对量口径）
    "hotBurst": 2.0,     # 近24h 报道量 / 日均密度 ≥ 此倍数 → 爆发
    "maxThemes": 20,     # 最多深入分析多少个题材
    "leaders": 8,        # 每个题材取前几名龙头
}

MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")

# 统计型板块（财报、风格、持仓、技术形态类），不是产业题材，全部剔除
SKIP_WORDS = (
    "昨日", "涨停", "连板", "ST", "摘帽", "破净", "新高", "新低",
    "一季报", "中报", "三季报", "年报", "季报", "预增", "预减", "预亏",
    "扭亏", "首亏", "续亏", "业绩",
    "风格", "成长", "价值股", "市盈率", "市净率", "题材股", "趋势股", "反转股",
    "重仓", "新进", "增仓", "减仓", "持股", "股权集中", "股权分散",
    "金股", "密集调研", "增持计划", "连续亏损", "分红", "送转",
    "社保", "QFII", "私募", "基金", "保险", "券商", "信托", "外资", "机构",
    "百元股", "低价股", "高价股", "次新股", "壳资源", "MSCI", "富时", "标普",
    "融资融券", "转债", "标的",
    "大盘股", "中盘股", "小盘股", "超大盘", "行业龙头", "龙头股", "中字头",
    "通信技术", "显示技术",     # 东财新拆的宽泛行业板块，名字太泛，热度易误匹配
)


def jget(url, retry=3, timeout=25):
    """GET -> dict，失败返回 {}。"""
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            if i < retry - 1:
                time.sleep(0.6 * (i + 1))
    return {}


def jget_text(url, retry=3, timeout=25):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i < retry - 1:
                time.sleep(0.6 * (i + 1))
    return ""


# ---------------- 1. 概念板块 ----------------
def fetch_boards():
    out, page = [], 1
    while page <= 10:
        u = ("https://push2.eastmoney.com/api/qt/clist/get?po=1&np=1&fltt=2&invt=2&fid=f3"
             "&fs=m:90+t:3&fields=f12,f14,f2,f3,f62,f104,f105,f128,f136&pn=%d&pz=100" % page)
        d = (jget(u).get("data") or {})
        diff = d.get("diff") or []
        if not diff:
            break
        out.extend(diff)
        if len(out) >= (d.get("total") or 0):
            break
        page += 1
        time.sleep(0.1)
    print("   概念板块 %d 个" % len(out))
    return out


def is_theme(name):
    return not any(w in name for w in SKIP_WORDS)


# ---------------- 2. 板块K线：位置与成立时长 ----------------
def board_stat(code):
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=90.%s&ut=%s"
         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
         "&klt=101&fqt=0&beg=0&end=20500101" % (code, UT))
    d = (jget(u).get("data") or {})
    kl = d.get("klines") or []
    if len(kl) < 20:
        return None
    p = [x.split(",") for x in kl]
    close = float(p[-1][2])
    n = len(p)
    w = p[-250:] if n >= 250 else p
    hi = max(float(x[3]) for x in w)
    lo = min(float(x[4]) for x in w)
    dd = (close / hi - 1) * 100 if hi else 0          # 距近250日高点回撤
    pos = (close - lo) / (hi - lo) * 100 if hi > lo else 50.0   # 250日位置分位
    c20 = (close / float(p[-21][2]) - 1) * 100 if n > 21 else 0.0
    c60 = (close / float(p[-61][2]) - 1) * 100 if n > 61 else 0.0
    amt5 = sum(float(x[6]) for x in p[-5:]) / 5 if n >= 5 else 0
    amt20 = sum(float(x[6]) for x in p[-20:]) / 20 if n >= 20 else 0
    return {
        "bars": n, "close": round(close, 2),
        "dd": round(dd, 1), "pos": round(pos, 1),
        "c20": round(c20, 1), "c60": round(c60, 1),
        "vol5v20": round(amt5 / amt20, 2) if amt20 else 0,
    }


# ---------------- 3. 资讯热度 ----------------
def news_heat(kw, size=60):
    """返回近24h/近7日条数、日均密度、爆发倍数、样例标题。

    必须用 searchScope="title"：用默认全文搜索会把「XX概念下跌」这类通用行情稿
    全部命中，导致关键词污染（"卫星导航"抓回"托育服务下跌"）。限定标题后命中率 100%。
    """
    param = {"uid": "", "keyword": kw, "type": ["cmsArticleWebOld"],
             "client": "web", "clientType": "web", "clientVersion": "curr",
             "param": {"cmsArticleWebOld": {"searchScope": "title", "sort": "time",
                                            "pageIndex": 1, "pageSize": size,
                                            "preTag": "", "postTag": ""}}}
    u = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
         + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    raw = jget_text(u)
    m = re.search(r"^\s*\w+\((.*)\)\s*$", raw.strip(), re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(1))
    except Exception:
        return None
    arts = ((d.get("result") or {}).get("cmsArticleWebOld") or [])
    core = kw.replace("概念", "").replace("板块", "").strip()
    now = datetime.now(CN).replace(tzinfo=None)
    times, titles = [], []
    for a in arts:
        title = re.sub(r"<[^>]+>", "", a.get("title") or "")
        body = re.sub(r"<[^>]+>", "", a.get("content") or "")
        # 本地强校验：搜索会把「3D玻璃」拆成「3D」「玻璃」分别匹配，
        # 也会把「通信技术」匹配到公司全称，必须在标题里真正出现才算
        if core not in title and core not in body:
            continue
        try:
            t = datetime.strptime((a.get("date") or "")[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        times.append(t)
        titles.append({"t": title, "d": (a.get("date") or "")[:16],
                       "u": a.get("url") or ""})
    if not times:
        return {"h24": 0, "h7": 0, "density": 0, "burst": 0, "titles": [], "total": 0}

    h24 = sum(1 for t in times if t >= now - timedelta(days=1))
    h7 = sum(1 for t in times if t >= now - timedelta(days=7))
    span = max((max(times) - min(times)).total_seconds() / 86400.0, 0.2)
    density = len(times) / span                       # 日均发文密度
    # 样本太少时不用密度外推，否则小分母会把倍数放大得离谱
    burst = h24 if len(times) < 6 else h24 / max(density, 0.5)
    return {"h24": h24, "h7": h7, "density": round(density, 1),
            "burst": round(burst, 2), "titles": titles[:6], "total": len(times)}


# ---------------- 4. 成分股 ----------------
def fetch_members(code):
    out, page = [], 1
    while page <= 6:
        u = ("https://push2.eastmoney.com/api/qt/clist/get?po=1&np=1&fltt=2&invt=2&fid=f3"
             "&fs=b:%s&fields=f12,f14,f2,f3,f8,f9,f20,f21,f62,f115&pn=%d&pz=100" % (code, page))
        d = (jget(u).get("data") or {})
        diff = d.get("diff") or []
        if not diff:
            break
        out.extend(diff)
        if len(out) >= (d.get("total") or 0):
            break
        page += 1
    return out


# ---------------- 4.5 龙头图表（供页面点击查看 K 线 / 分时） ----------------
def stock_charts(code, bars=80):
    """补抓单只股票的日K与当日分时，字段结构与 screen.py 完全一致，
    这样前端可以直接复用同一套绘图函数。"""
    sid = ("1." if code.startswith("6") else "0.") + code
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
         "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
         "&klt=101&fqt=0&beg=0&end=20500101&lmt=120" % sid)
    daily = []
    for line in ((jget(u).get("data") or {}).get("klines") or []):
        p = line.split(",")
        try:
            # [日期, 开, 高, 低, 收, 量, 额]
            daily.append([p[0], round(float(p[1]), 2), round(float(p[3]), 2),
                          round(float(p[4]), 2), round(float(p[2]), 2),
                          round(float(p[5]), 1), round(float(p[6]), 0)])
        except Exception:
            continue
    if len(daily) < 25:
        return None

    u2 = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s"
          "&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
          "&iscr=0&ndays=1" % sid)
    td = (jget(u2).get("data") or {})
    trend = []
    for line in (td.get("trends") or []):
        p = line.split(",")
        try:
            # [时间(HH:MM), 价格, 均价, 量, 额]
            trend.append([p[0][-5:], round(float(p[2]), 2), round(float(p[7]), 2),
                          int(float(p[5])), round(float(p[6]), 0)])
        except Exception:
            continue
    pre = td.get("preClose")
    return {"daily": daily[-bars:], "trend": trend,
            "preClose": round(float(pre), 2) if pre else None,
            "secid": sid, "mkt": "沪市" if code.startswith("6") else "深市"}


# ---------------- 5. 财务与排雷 ----------------
def dcget(report, filt, size=1, sort=None):
    u = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=%s&columns=ALL"
         "&filter=%s&pageNumber=1&pageSize=%d&source=WEB&client=WEB"
         % (report, urllib.parse.quote(filt), size))
    if sort:
        u += "&sortColumns=%s&sortTypes=-1" % sort
    return ((jget(u).get("result") or {}).get("data") or [])


def secucode(code):
    return code + (".SH" if code.startswith(("6",)) else ".SZ")


def finance(code):
    """主要财务指标 + 业绩预告 + 减持，做硬雷/软雷判定。"""
    sc = secucode(code)
    fin = dcget("RPT_F10_FINANCE_MAINFINADATA", '(SECUCODE="%s")' % sc, 2, "REPORT_DATE")
    hard, soft = [], []
    out = {"gross": None, "roe": None, "debt": None, "revG": None,
           "npG": None, "netProfit": None, "cash": None, "period": ""}
    if fin:
        r = fin[0]
        def f(k):
            v = r.get(k)
            return float(v) if isinstance(v, (int, float)) else None
        out = {
            "gross": round(f("XSMLL") or 0, 1),
            "roe": round(f("ROEJQ") or 0, 1),
            "debt": round(f("ZCFZL") or 0, 1),
            "revG": round(f("TOTALOPERATEREVETZ") or 0, 1),
            "npG": round(f("PARENTNETPROFITTZ") or 0, 1),
            "netProfit": f("PARENTNETPROFIT"),
            "cash": f("NETCASH_OPERATE_PK"),
            "period": r.get("REPORT_DATE_NAME") or "",
        }
        if out["netProfit"] is not None and out["netProfit"] < 0:
            hard.append("最新报告期亏损")
        if out["debt"] and out["debt"] > 70:
            soft.append("资产负债率 %.0f%%" % out["debt"])
        if out["cash"] is not None and out["cash"] < 0:
            soft.append("经营现金流为负")
        if out["npG"] is not None and out["npG"] < -50:
            soft.append("净利润同比 %.0f%%" % out["npG"])
        if out["revG"] is not None and out["revG"] < -30:
            soft.append("营收同比 %.0f%%" % out["revG"])

    # 业绩预告：预亏 / 首亏 / 续亏 视为硬雷
    fc = dcget("RPT_PUBLIC_OP_NEWPREDICT", '(SECURITY_CODE="%s")' % code, 1, "NOTICE_DATE")
    if fc:
        typ = (fc[0].get("PREDICT_TYPE") or "") + (fc[0].get("PREDICT_FINANCE") or "")
        if any(k in typ for k in ("预亏", "首亏", "续亏", "增亏")):
            hard.append("业绩预告" + ("预亏" if "预亏" in typ else typ[:4]))
        out["forecast"] = typ[:16]
        out["fcDate"] = (fc[0].get("NOTICE_DATE") or "")[:10]

    # 大股东减持：近 180 天
    sh = dcget("RPT_SHARE_HOLDER_INCREASE", '(SECURITY_CODE="%s")' % code, 5, "NOTICE_DATE")
    cut = []
    now = datetime.now(CN).replace(tzinfo=None)
    for r in sh:
        nd = (r.get("NOTICE_DATE") or "")[:10]
        try:
            if (now - datetime.strptime(nd, "%Y-%m-%d")).days > 200:
                continue
        except Exception:
            continue
        num = r.get("CHANGE_NUM")
        if isinstance(num, (int, float)) and num < 0:
            cut.append("%s 减持" % nd)
    if cut:
        soft.append("近期减持 " + cut[0])

    out["hard"] = hard
    out["soft"] = soft
    return out


# ---------------- 6. 打分 ----------------
def score_theme(t):
    """题材综合分：低位 30 + 新颖 20 + 爆发 30 + 资金 20"""
    low = min(max((t["dd"] * -1 - 10) / 40, 0), 1) * 15 + min(max((35 - t["pos"]) / 35, 0), 1) * 15
    new = min(max((P["newBars"] - t["bars"]) / P["newBars"], 0), 1) * 20
    bt = t.get("burst") or 0
    burst = min(bt / (P["hotBurst"] * 2), 1) * 20 + min((t.get("h24") or 0) / 20, 1) * 10
    money = min(max(t["chg"], 0) / 5, 1) * 10 + min(max(t.get("netIn") or 0, 0) / 2e8, 1) * 10
    return round(low + new + burst + money, 1)


def score_leader(m, rank_in_board, board_amt):
    """龙头分：涨幅 25 + 成交额占比 25 + 市值适中 20 + 主力 20 + 换手 10"""
    chg = min(max(m["chg"], 0) / 10, 1) * 25
    share = min((m["amt"] / board_amt) / 0.25, 1) * 25 if board_amt else 0
    mc = m["mcap"] or 0
    # 100亿~800亿最舒服，太小易操纵、太大弹性差
    if 1e10 <= mc <= 8e10:
        cap = 20
    elif mc < 1e10:
        cap = 20 * max(mc / 1e10, 0.2)
    else:
        cap = 20 * max(1 - (mc - 8e10) / 1.2e11, 0.2)
    net = min(max(m.get("netIn") or 0, 0) / 5e7, 1) * 20
    turn = min(m["turn"] / 15, 1) * 10
    return round(chg + share + cap + net + turn, 1)


def score_core(f):
    """核心竞争力：毛利率 25 + ROE 25 + 营收增速 25 + 盈利质量 25"""
    g = min(max((f.get("gross") or 0) / 50, 0), 1) * 25
    r = min(max((f.get("roe") or 0) / 20, 0), 1) * 25
    v = min(max((f.get("revG") or 0) / 30, 0), 1) * 25
    q = 0
    if f.get("cash") is not None and f.get("netProfit"):
        q = min(max(f["cash"] / f["netProfit"], 0), 1) * 25
    return round(g + r + v + q, 1)


# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    now = datetime.now(CN)
    print("题材雷达 · %s" % now.strftime("%Y-%m-%d %H:%M"))

    boards = fetch_boards()
    themes = [b for b in boards if is_theme(b.get("f14") or "")]
    print("   剔除统计型标签后，产业题材 %d 个" % len(themes))

    # 拉位置数据
    print("   拉取 %d 个题材的位置数据 ..." % len(themes))
    stat = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for b, s in zip(themes, ex.map(lambda x: board_stat(x["f12"]), themes)):
            if s:
                stat[b["f12"]] = s
    print("   成功 %d 个" % len(stat))

    # 低位阈值自适应：市场整体深跌时"回撤 30%"不再稀缺，按全体题材中位数上浮
    dds = sorted(s["dd"] for s in stat.values())
    med_dd = dds[len(dds) // 2] if dds else 0
    low_dd = max(P["lowDd"], abs(med_dd) * 1.15)
    print("   题材回撤中位数 %.1f%% → 本轮低位阈值取 %.1f%%" % (med_dd, low_dd))

    # 三道门
    cand = []
    for b in themes:
        s = stat.get(b["f12"])
        if not s:
            continue
        is_low = s["dd"] <= -low_dd and s["pos"] <= P["lowPos"]
        is_new = s["bars"] < P["newBars"]
        if not (is_low or is_new):
            continue
        cand.append({
            "code": b["f12"], "name": b["f14"],
            "chg": round(b.get("f3") or 0, 2),
            "netIn": b.get("f62") or 0,
            "up": b.get("f104") or 0, "down": b.get("f105") or 0,
            "leader": b.get("f128") or "",
            "low": is_low, "new": is_new,
            **s,
        })
    cand.sort(key=lambda x: (x["chg"], x["dd"]))
    low_cnt = len([x for x in cand if x["low"]])
    new_cnt = len([x for x in cand if x["new"]])
    both = [x for x in cand if x["low"] and x["new"]]
    print("   位置/成立时长初筛：%d 个（低位 %d · 新题材 %d · 两者兼有 %d）"
          % (len(cand), low_cnt, new_cnt, len(both)))

    # 资讯热度：候选全部查，避免观察池因缺数据被误判
    probe = cand
    print("   查询 %d 个题材的资讯热度 ..." % len(probe))
    with ThreadPoolExecutor(max_workers=8) as ex:
        heats = list(ex.map(lambda x: news_heat(x["name"]), probe))
    for c, h in zip(probe, heats):
        if h:
            c.update({"h24": h["h24"], "h7": h["h7"], "density": h["density"],
                      "burst": h["burst"], "titles": h["titles"]})
        else:
            c.update({"h24": 0, "h7": 0, "density": 0, "burst": 0, "titles": []})

    for c in cand:
        c.setdefault("h24", 0)
        c.setdefault("burst", 0)
        c.setdefault("titles", [])
        # 爆发判定：相对日常 2 倍以上且至少有 2 条，或绝对条数够多。
        # 当日大跌的题材不计——那种"爆发"多半是利空报道，不是机会。
        burst_ok = c["burst"] >= P["hotBurst"] and c["h24"] >= 2
        abs_ok = c["h24"] >= P["hot24"]
        c["hot"] = (burst_ok or abs_ok) and c["chg"] > -1.0
        c["score"] = score_theme(c)

    # 分组：主推（三项齐全）/ 观察（缺一项）
    main_list = [x for x in cand if x["low"] and x["new"] and x["hot"]]
    watch = [x for x in cand if x not in main_list]
    main_list.sort(key=lambda x: -x["score"])
    watch.sort(key=lambda x: -x["score"])
    print("   主推 %d 个 · 观察 %d 个" % (len(main_list), len(watch)))

    # 主推全深挖，观察补足到上限
    deep = main_list + watch[:max(0, P["maxThemes"] - len(main_list))]
    for t in deep:
        t["tier"] = "main" if t in main_list else "watch"
    print("   深挖 %d 个题材的龙头与财务 ..." % len(deep))

    for t in deep:
        members = fetch_members(t["code"])
        rows = []
        for m in members:
            code = m.get("f12") or ""
            name = m.get("f14") or ""
            if not code.startswith(MAIN_PREFIX):
                continue
            if "ST" in name.upper() or "退" in name:
                continue
            price, chg = m.get("f2"), m.get("f3")
            if not isinstance(price, (int, float)) or price <= 0:
                continue
            rows.append({
                "code": code, "name": name,
                "price": round(price, 2),
                "chg": round(chg or 0, 2),
                "turn": round(m.get("f8") or 0, 2),
                "pe": round(m.get("f115") or 0, 1),
                "mcap": m.get("f20") or 0,
                "fmcap": m.get("f21") or 0,
                "netIn": m.get("f62") or 0,
                "amt": abs(m.get("f62") or 0) or 1,
            })
        if not rows:
            t["leaders"] = []
            continue
        board_amt = sum(r["amt"] for r in rows)
        for r in rows:
            r["lead"] = score_leader(r, 0, board_amt)

        top = sorted(rows, key=lambda r: -r["lead"])[:P["leaders"]]
        with ThreadPoolExecutor(max_workers=6) as ex:
            fins = list(ex.map(lambda r: finance(r["code"]), top))
        leads = []
        for r, f in zip(top, fins):
            r["fin"] = f
            r["core"] = score_core(f)
            r["risk"] = {"hard": f.get("hard") or [], "soft": f.get("soft") or []}
            leads.append(r)
        # 硬雷剔除，软雷保留并标黄
        clean = [x for x in leads if not x["risk"]["hard"]]
        t["removed"] = [{"code": x["code"], "name": x["name"], "why": x["risk"]["hard"]}
                        for x in leads if x["risk"]["hard"]]
        t["leaders"] = clean
        time.sleep(0.1)

    # 只给主推题材的龙头补抓图表：页面只在这些卡片上展示个股明细，
    # 每只约 12KB（80 根日K + 当日分时），主推通常只有个位数题材，体积可控。
    tg, seen = [], set()
    for t in deep:
        if t.get("tier") != "main":
            continue
        for L in (t.get("leaders") or []):
            if L["code"] not in seen:
                seen.add(L["code"])
                tg.append(L)
    if tg:
        print("   为主推的 %d 只龙头补抓日K与分时 ..." % len(tg))
        with ThreadPoolExecutor(max_workers=8) as ex:
            charts = list(ex.map(lambda L: stock_charts(L["code"]), tg))
        ok = 0
        for L, ch in zip(tg, charts):
            if ch:
                L.update(ch)
                ok += 1
        print("   图表抓取成功 %d / %d" % (ok, len(tg)))

    out = {
        "date": now.strftime("%Y-%m-%d"),
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "params": {**P, "lowDdEff": round(low_dd, 1), "medDd": round(med_dd, 1)},
        "counts": {"boards": len(boards), "themes": len(themes),
                   "main": len(main_list), "watch": len(watch)},
        "themes": deep,
        # 观察池概览：不深挖，只给指标，看到全貌
        "watchBrief": [{
            "code": x["code"], "name": x["name"], "chg": x["chg"],
            "dd": x["dd"], "pos": x["pos"], "c20": x["c20"], "c60": x["c60"],
            "bars": x["bars"], "h24": x["h24"], "burst": x["burst"],
            "score": x["score"], "low": x["low"], "new": x["new"], "hot": x["hot"],
        } for x in watch[:40]],
    }
    os.makedirs(THEME_DIR, exist_ok=True)
    fp = "%s/%s.json" % (THEME_DIR, now.strftime("%Y-%m-%d"))
    json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("写入 %s（%.1f KB）· 用时 %.1fs" % (fp, os.path.getsize(fp) / 1024, time.time() - t0))

    # 只保留最近 60 个交易日，一份约 100KB，控制仓库体积
    olds = sorted(os.path.basename(f) for f in glob.glob(THEME_DIR + "/*.json"))[:-60]
    for fn in olds:
        os.remove(THEME_DIR + "/" + fn)
    if olds:
        print("   题材历史轮转：清理 %d 个较早交易日" % len(olds))


if __name__ == "__main__":
    main()
