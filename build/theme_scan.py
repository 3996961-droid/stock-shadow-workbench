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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emq                                     # 统一取数层（东财列表被封时的重建方案）
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
    "rotWin": 60,        # 轮动观察窗口（交易日）：这段时间内领涨过几次，决定"新不新"
    "rotTop": 15,        # 当日板块涨幅前 N 名算"领涨"
    "rotNew": 10.0,      # 轮动新颖分 ≥ 此值 → 算"这一波不是上一波"
    "sibWin": 20,        # 同主线姊妹板块的观察窗口
    "lowDd": 30.0,       # 距 250 日高点回撤 ≥ 此值(%) → 低位（市场整体深跌时会自动提高）
    "lowPos": 20.0,      # 250 日位置分位 ≤ 此值(%) → 低位
    "hot24": 10,         # 近 24 小时报道 ≥ 此条数 → 有热度（绝对量口径）
    "hotBurst": 2.0,     # 近24h 报道量 / 日均密度 ≥ 此倍数 → 爆发
    "hotMin": 5,         # 走"相对爆发"这条路的，24h 至少要有这么多条，防低基数伪爆发
    "maxThemes": 20,     # 最多深入分析多少个题材
    "leaders": 8,        # 每个题材取前几名龙头
}

# 主线归并：同一主线下的分支互为"姊妹板块"。
# 姊妹板块近期在领涨，说明这条线已经炒过，自己再启动就不算新题材。
LINES = (
    ("光通信", ("光纤", "光通信", "光模块", "CPO", "光芯片", "铜缆高速", "光器件", "光电")),
    ("航天卫星", ("商业航天", "卫星", "航天", "星链", "火箭")),
    ("低空经济", ("通用航空", "低空", "无人机", "飞行汽车", "eVTOL")),
    ("算力硬件", ("算力", "数据中心", "PCB", "液冷", "服务器", "IDC", "交换机", "铜连接")),
    ("半导体", ("芯片", "半导体", "光刻", "晶圆", "封测", "碳化硅", "存储", "HBM",
                "第三代半导体", "集成电路", "先进封装")),
    ("机器人", ("机器人", "减速器", "伺服", "丝杠", "灵巧手", "机器视觉")),
    ("新能源车", ("锂电", "电池", "固态电池", "钠电", "充电桩", "汽车零部件", "整车")),
    ("光伏风电", ("光伏", "风电", "储能", "逆变器", "HJT", "钙钛矿", "绿电")),
    ("医药", ("创新药", "中药", "医疗", "CXO", "疫苗", "减肥药", "生物医药")),
    ("消费", ("白酒", "食品", "旅游", "免税", "零售", "冰雪", "家电", "纺织")),
    ("军工", ("军工", "国防", "导弹", "无人机", "大飞机")),
    ("资源", ("锂矿", "稀土", "铜", "铝", "黄金", "煤", "油气", "磷", "锑", "钨")),
    ("金融地产", ("券商", "银行", "保险", "地产", "建筑", "信托")),
    ("周期化工", ("化工", "化纤", "农药", "化肥", "水泥", "钢铁", "有色")),
)


def theme_line(name):
    """题材归入主线；归不进去的各自成线。"""
    for ln, kws in LINES:
        if any(k in name for k in kws):
            return ln
    return name

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
    out, _ = emq.diff_all("m:90+t:3",
                          "f12,f14,f2,f3,f62,f104,f105,f128,f136", po=1)
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
        "s": [(x[0], float(x[2])) for x in p[-66:]],   # 最近 66 日收盘，供轮动统计
    }


def rotation_stats(stat, min_bars=P["rotWin"] + 1):
    """轮动新颖度：这一波热点跟上一波是不是同一个题材。

    做法是全市场横截面比较——每天把所有题材按涨幅排名，前 rotTop 名算"领涨"。
    某题材在过去 rotWin 天里领涨次数越多、离上次领涨越近，说明市场在反复炒它，
    这次再启动就不算"新"；反过来从未领涨过的，才是真正的轮动切过来。

    返回 {code: {lead, last, g60, g20}}
    """
    # 以最长序列的日期作为基准日历
    ref = max((v["s"] for v in stat.values() if len(v["s"]) >= min_bars),
              key=len, default=[])
    dates = [d for d, _ in ref]
    if not dates:
        return {}
    # 关键：必须排除当日。今天在涨是"正在启动"的证据，不是"炒过了"的证据，
    # 把当日算进领涨次数，所有今天启动的题材都会被判成"刚炒过"，逻辑正好反过来。
    win = dates[-(P["rotWin"] + 1):-1]
    # 每个板块的日涨幅序列
    chg = {}
    for code, v in stat.items():
        m = dict(v["s"])
        arr, prev = {}, None
        for d in win:
            px = m.get(d)
            if px is None:
                continue
            if prev:
                arr[d] = (px / prev - 1) * 100
            prev = px
        if arr:
            chg[code] = arr

    # 逐日排名
    lead, last = {}, {}
    for i, d in enumerate(win):
        day = [(c, a[d]) for c, a in chg.items() if d in a]
        if len(day) < 20:
            continue
        day.sort(key=lambda x: -x[1])
        for c, _v in day[:P["rotTop"]]:
            lead[c] = lead.get(c, 0) + 1
            last[c] = i                       # 记位置，最后换成"距今几天"
    n = len(win)

    out = {}
    for code, v in stat.items():
        g60 = (v["s"][-1][1] / v["s"][0][1] - 1) * 100 if len(v["s"]) >= 2 else 0
        g20 = (v["s"][-1][1] / v["s"][-21][1] - 1) * 100 if len(v["s"]) > 21 else 0
        out[code] = {
            "lead": lead.get(code, 0),
            # win 已排除当日，所以"距今"要再加 1 天，才是从今天往回数
            "last": (n - last[code]) if code in last else None,
            "g60": round(g60, 1), "g20": round(g20, 1),
        }
    return out


def sibling_lead(rot, names):
    """同主线姊妹板块近 sibWin 日的领涨次数合计（不含自己）。

    names: {code: 板块名}，用于把板块归到主线。
    同一条线里只要有分支在领涨，就说明这条线已经被市场挖过了。
    """
    line_of = {c: theme_line(nm) for c, nm in names.items()}
    tot = {c: 0 for c in names}
    for code, r in rot.items():
        if r["last"] is None or r["last"] > P["sibWin"] or not r["lead"]:
            continue
        ln = line_of.get(code)
        for c2 in names:
            if c2 != code and line_of.get(c2) == ln:
                tot[c2] += r["lead"]
    return tot


def score_rot(rot):
    """轮动新颖分（25 分）：这一波热点是不是上一波那批。

    - 60 日内领涨 6 次以上 → 0 分（都被炒烂了）
    - 只有反复领涨的题材才吃"刚炒过"折扣：领涨过 1~2 次的，最近在涨说明是正在启动，
      不该罚；领涨 3 次以上且离上次很近，才是真的在同一个题材上反复做
    - 同主线姊妹板块近期在领涨 → 再打一次折（说明是整条线在动，不是新方向）
    """
    base = max(0.0, 1 - rot["lead"] / 6.0) * 25
    last = rot["last"]
    if last is not None and rot["lead"] >= 3:
        if last < 8:
            base *= 0.2
        elif last < 15:
            base *= 0.5
        elif last < 25:
            base *= 0.75
    sib = rot.get("sib") or 0
    if sib >= 8:
        base *= 0.6
    elif sib >= 4:
        base *= 0.85
    return round(base, 1)


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
    out, _ = emq.diff_all("b:%s" % code,
                          "f12,f14,f2,f3,f6,f8,f9,f20,f21,f62,f115", po=1)
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
    """题材综合分：低位 30 + 轮动新颖 25 + 爆发 25 + 资金 20 + 催化加成 0~10

    催化加成放在 100 分之外：满分体系仍是四项，权威源（新闻联播/韭研）
    只作为"这件事确实有人在说"的加分，不改变基础排序结构。
    """
    low = min(max((t["dd"] * -1 - 10) / 40, 0), 1) * 15 + min(max((35 - t["pos"]) / 35, 0), 1) * 15
    rot = t.get("rotScore") or 0
    bt = t.get("burst") or 0
    burst = min(bt / (P["hotBurst"] * 2), 1) * 15 + min((t.get("h24") or 0) / 25, 1) * 10
    money = min(max(t["chg"], 0) / 5, 1) * 10 + min(max(t.get("netIn") or 0, 0) / 2e8, 1) * 10
    cat = t.get("catBonus") or 0
    return round(low + rot + burst + money + cat, 1)


def score_leader(m, rank_in_board, board_amt):
    """龙头分：涨幅 25 + 成交额占比 25 + 市值适中 20 + 主力 20 + 换手 10"""
    chg = min(max(m["chg"], 0) / 10, 1) * 25
    share = min((m["amt"] / board_amt) / 0.15, 1) * 25 if board_amt else 0
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


# ---------------- 6.5 催化源 ----------------
def load_catalyst(ds):
    """读 catalyst.py 的产出（新闻联播 + 韭研清洗结果）。"""
    fp = BASE + "/build/catalyst/%s.json" % ds
    if not os.path.exists(fp):
        fs = sorted(glob.glob(BASE + "/build/catalyst/20*.json"))
        if not fs:
            return {}
        fp = fs[-1]
    try:
        d = json.load(open(fp, encoding="utf-8"))
    except Exception:
        return {}
    if d.get("date") and d["date"] != ds:
        print("   催化数据来自 %s（今日尚未生成），仅作参考" % d["date"])
    return d


def cat_bonus(ct):
    """催化加成 0~10：权威源背书才给分，纯行情稿不给。"""
    if not ct:
        return 0.0
    b = 0.0
    if ct.get("xwlb"):
        b += 8.0                       # 新闻联播点名，最高权重
    b += min(ct.get("hotlist", 0) * 2.5, 5.0)     # 韭研舆情盘点显式点名
    b += min(ct.get("mention", 0) * 0.8, 3.0)     # 韭研正文提及
    return round(min(b, 10.0), 1)


# ---------------- 主流程 ----------------
def main():
    t0 = time.time()
    now = datetime.now(CN)
    ds = now.strftime("%Y-%m-%d")
    print("题材雷达 · %s" % now.strftime("%Y-%m-%d %H:%M"))

    boards = fetch_boards()
    themes = [b for b in boards if is_theme(b.get("f14") or "")]
    names = {b["f12"]: b["f14"] for b in themes}
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

    # 轮动统计：谁在过去 rotWin 天里反复领涨
    rot = rotation_stats(stat)
    sib = sibling_lead(rot, {c: names.get(c, "") for c in rot})
    print("   轮动统计：%d 个题材有历史，%d 个过去 %d 日从未领涨"
          % (len(rot), len([1 for r in rot.values() if not r["lead"]]), P["rotWin"]))

    # 催化源
    cat = load_catalyst(ds)
    cat_map = {t["code"]: t for t in (cat.get("themes") or [])}
    if cat:
        n_ind = len((cat.get("xwlb") or {}).get("industry") or [])
        n_doc = (cat.get("jiuyan") or {}).get("usable", 0)
        print("   催化源 %s：联播产业 %d 条 · 韭研可用 %d 篇 · 命中题材 %d 个"
              % (cat.get("date"), n_ind, n_doc, len(cat_map)))
    else:
        print("   未找到催化源数据，跳过催化加成")

    # 初筛：低位 或 轮动新颖，且今日没有明显走弱
    cand = []
    for b in themes:
        s = stat.get(b["f12"])
        if not s:
            continue
        r = rot.get(b["f12"]) or {"lead": 0, "last": None, "g60": 0, "g20": 0}
        rr = dict(r)
        rr["sib"] = sib.get(b["f12"], 0)
        rs = score_rot(rr)
        is_low = s["dd"] <= -low_dd and s["pos"] <= P["lowPos"]
        is_new = rs >= P["rotNew"]
        if not (is_low or is_new):
            continue
        c = {
            "code": b["f12"], "name": b["f14"],
            "chg": round(b.get("f3") or 0, 2),
            "netIn": b.get("f62") or 0,
            "up": b.get("f104") or 0, "down": b.get("f105") or 0,
            "leader": b.get("f128") or "",
            "low": is_low, "new": is_new,
            "rotScore": rs, "lead60": r["lead"], "lastLead": r["last"],
            "sibLead": rr["sib"], "line": theme_line(b["f14"]),
        }
        c.update({k: v for k, v in s.items() if k != "s"})
        cand.append(c)
    cand.sort(key=lambda x: (x["chg"], x["dd"]))
    low_cnt = len([x for x in cand if x["low"]])
    new_cnt = len([x for x in cand if x["new"]])
    both = [x for x in cand if x["low"] and x["new"]]
    print("   位置/轮动初筛：%d 个（低位 %d · 轮动新 %d · 两者兼有 %d）"
          % (len(cand), low_cnt, new_cnt, len(both)))

    # 资讯热度：只查今天有动静的，避免把几百个死票都问一遍
    probe = [x for x in cand if x["chg"] > -1.0 and (x["chg"] > 0 or x["netIn"] > 0)]
    probe.sort(key=lambda x: (-(x["chg"]), x["dd"]))
    probe = probe[:170]
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
        # 爆发判定：相对日常 2 倍以上且至少 hotMin 条绝对量，或绝对条数够多。
        # 只要相对倍数不看绝对量，2 条快讯就能凑出 7 倍"爆发"，是低基数伪信号。
        # 当日大跌的题材不计——那种"爆发"多半是利空报道，不是机会。
        burst_ok = c["burst"] >= P["hotBurst"] and c["h24"] >= P["hotMin"]
        abs_ok = c["h24"] >= P["hot24"]
        c["hot"] = (burst_ok or abs_ok) and c["chg"] > -1.0
        ct = cat_map.get(c["code"])
        c["catBonus"] = cat_bonus(ct)
        c["cat"] = ct or None
        c["score"] = score_theme(c)

    # 分组：主推（三项齐全）/ 观察（缺一项）
    main_list = [x for x in cand if x["low"] and x["new"] and x["hot"]]
    main_list.sort(key=lambda x: -x["score"])
    # 主线去重：同一条线最多留 2 个进主推。一条线里既有火箭又有卫星时，
    # 全部塞进主推只会让页面变成同一条线的三份重复结论。
    kept, per_line = [], {}
    for x in main_list:
        ln = x.get("line") or x["name"]
        if per_line.get(ln, 0) >= 2:
            x["demoted"] = "同主线已有更强的题材入选，降为观察"
            continue
        per_line[ln] = per_line.get(ln, 0) + 1
        kept.append(x)
    if len(kept) != len(main_list):
        print("   主线去重：主推 %d → %d" % (len(main_list), len(kept)))
    main_list = kept
    watch = [x for x in cand if x not in main_list]
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
                # f6 是成交额。早先误用 f62（主力净流入）当成交额，会让"成交额占比"
                # 和"主力净流入"两项算同一份数据，龙头榜退化成净流入排行榜。
                "amt": m.get("f6") or 0,
            })
        if not rows:
            t["leaders"] = []
            continue
        board_amt = sum(r["amt"] for r in rows)
        for r in rows:
            r["lead"] = score_leader(r, 0, board_amt)

        # 题材纯度标记：东财板块成分股里常混进一堆"顺带"的票
        # （商业航天板块里塞满 PCB 公司，它们其实是另一条热点），
        # 用韭研舆情点名名单交叉验证，点过名的标 named，页面上单独高亮。
        named = set((t.get("cat") or {}).get("stocks") or [])
        for r in rows:
            r["named"] = bool(named) and r["name"] in named

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

    # 催化证据裁剪：只留结论和证据句，不把整篇正文塞进页面
    for t in deep:
        ct = t.get("cat")
        if ct:
            t["cat"] = {
                "xwlb": ct.get("xwlb", 0), "hotlist": ct.get("hotlist", 0),
                "mention": ct.get("mention", 0), "score": ct.get("score", 0),
                "stocks": (ct.get("stocks") or [])[:15],
                "ev": (ct.get("ev") or [])[:4],
            }

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
            "lead60": x.get("lead60"), "lastLead": x.get("lastLead"),
            "line": x.get("line"), "rotScore": x.get("rotScore"),
            "catBonus": x.get("catBonus", 0),
        } for x in watch[:40]],
        # 催化源概览：新闻联播条目 + 韭研可用文档，页面上做独立区块
        "catalyst": {
            "date": cat.get("date") if cat else None,
            "xwlb": {
                "date": (cat.get("xwlb") or {}).get("date"),
                "total": (cat.get("xwlb") or {}).get("total", 0),
                "available": (cat.get("xwlb") or {}).get("available", False),
                "note": (cat.get("xwlb") or {}).get("note", ""),
                "industry": (cat.get("xwlb") or {}).get("industry") or [],
            } if cat else None,
            "jiuyan": {
                "total": (cat.get("jiuyan") or {}).get("total", 0),
                "usable": (cat.get("jiuyan") or {}).get("usable", 0),
                # 可用优先、分数降序，页面只展示前 20 条（全量统计看 total/usable）
                "docs": [{
                    "title": d["title"], "type": d["type"], "usable": d["usable"],
                    "score": d["score"], "reason": d["reason"], "hotlist": d["hotlist"],
                    "themes": [t["name"] for t in (d.get("themes") or [])[:4]],
                    "quote": d.get("quote", ""), "doc_id": d.get("doc_id", ""),
                } for d in sorted(((cat.get("jiuyan") or {}).get("docs") or []),
                                  key=lambda d: (-(1 if d["usable"] else 0), -d["score"]))[:20]],
            } if cat else None,
            "hotThemes": (cat.get("summary") or {}).get("hotThemes") or [],
        } if cat else None,
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
