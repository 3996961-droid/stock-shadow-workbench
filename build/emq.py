# -*- coding: utf-8 -*-
"""统一行情取数层：东财列表接口被风控后的重建方案。

背景（2026-11-11）
------------------
东财 /api/qt/clist/get（"列表"接口）当天起被整段拦断：本机与 GitHub Actions
都取不到，表现为连接重置（RemoteDisconnected）或 HTTPError，而同一域名下的

    /api/qt/stock/get      个股快照        正常
    /api/qt/ulist.np/get   按代码批量取    正常
    /api/qt/slist/get      个股所属板块    正常
    push2his .../kline     日K              正常

全部可用。也就是说"按代码取"没被封，被封的是"按条件筛全市场"。

所以把"列表"拆成 代码全集 + ulist 批量 两步：

    全市场     ← 新浪 Market_Center.getHQNodeData（逐字段与东财一致，已核对）
    概念板块   ← build/boards.json（上一次成功运行时落下的板块代码全集）
    板块成分   ← build/members.json（build/make_members.py 用 slist 反查生成）

设计原则：
1. 永远先试东财 clist，通了就走原路 —— 哪天解除限制自动回退，不留后遗症；
2. 任一环节拿不到只让对应模块降级，绝不让整条流水线崩掉；
3. 对外只暴露 diff_all()，返回值与东财 diff 同构，调用方无需关心来源。
"""
import json
import os
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BOARDS = BASE + "/build/boards.json"
MEMBERS = BASE + "/build/members.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

EM = "https://push2.eastmoney.com/api/qt/"
SINA = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.")

# 东财 clist 是否可用，进程内只探一次
_CLIST = {"tried": False, "ok": True}
_MARKET = {"rows": None}
# 东财 K线/分时/批量是否可用，同样只探一次：被封时每次重试都在空等超时
_HIS = {"tried": False, "ok": True}
_ULIST = {"tried": False, "ok": True}

TX_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            "?param=%s,day,,,%d,%s")
TX_MIN = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s"

# clist 原生字段（重建时按需裁剪，保证返回结构与东财一致）
CLIST_FIELDS = ("f12,f13,f14,f2,f3,f6,f8,f10,f15,f16,f17,f18,f20,f21,"
                "f62,f100,f104,f105,f115,f128,f136")


def _get(url, retry=3, timeout=30, referer=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    last = None
    for i in range(retry):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                        timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            if i < retry - 1:
                time.sleep(0.5 * (i + 1))
    return ""


def _j(url, **kw):
    t = _get(url, **kw)
    try:
        return json.loads(t) if t else {}
    except Exception:
        return {}


# ---------------- 0. 日K / 分时（封禁时改走腾讯） ----------------
def _tx_symbol(secid):
    m, c = secid.split(".")
    return ("sh" if m == "1" else "sz") + c


def hist_ok():
    """东财历史行情（K线/分时）是否还活着。被封时一次探测就够，别每次空等。"""
    if not _HIS["tried"]:
        u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000"
             "&fields1=f1&fields2=f51,f53&klt=101&fqt=1&beg=0&end=20500101&lmt=3")
        d = _j(u, retry=1, timeout=8, referer="https://quote.eastmoney.com/")
        _HIS["ok"] = bool((d.get("data") or {}).get("klines"))
        _HIS["tried"] = True
        print("   历史行情源：%s" % ("东财 push2his" if _HIS["ok"] else "腾讯 ifzq"))
    return _HIS["ok"]


def kline_lines(secid, lmt=120, fqt=0):
    """日K，返回东财风格字符串列表 "日期,开,收,高,低,量,额"，调用方无需改解析。

    东财被封时走腾讯：其 qfqday 每行是 [日期, 开, 收, 高, 低, 量(手)]，没有成交额，
    用 收盘×量×100 估算（只用于图表展示与放量比，不影响涨停核查）。
    """
    if hist_ok():
        u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
             "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
             "&klt=101&fqt=%d&beg=0&end=20500101&lmt=%d" % (secid, fqt, lmt))
        ks = ((_j(u, retry=2, referer="https://quote.eastmoney.com/")
               .get("data") or {}).get("klines") or [])
        if ks:
            return ks

    sym = _tx_symbol(secid)
    key = "qfqday" if fqt else "day"
    d = _j(TX_KLINE % (sym, lmt, "qfq" if fqt else ""),
           retry=2, referer="https://gu.qq.com/")
    node = ((d.get("data") or {}).get(sym) or {})
    rows = node.get(key) or node.get("qfqday") or node.get("day") or []
    out = []
    for r in rows:
        try:
            dt, o, c, h, l = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])
            v = float(r[5] or 0)
        except Exception:
            continue
        out.append("%s,%.2f,%.2f,%.2f,%.2f,%.0f,%.0f"
                   % (dt, o, c, h, l, v, c * v * 100))
    return out


def trend_rows(secid):
    """当日分时，返回 ([[时间, 收, 均价, 量, 额]...], 昨收)。东财不可用时走腾讯逐分钟累计差。"""
    if hist_ok():
        u = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid=%s"
             "&fields1=f1,f2,f3,f4,f5,f6,f7,f8&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
             "&iscr=0&ndays=1" % secid)
        d = _j(u, retry=2, referer="https://quote.eastmoney.com/").get("data") or {}
        out = []
        for line in (d.get("trends") or []):
            p = line.split(",")
            try:
                out.append([p[0], float(p[2]), float(p[7]), int(float(p[5])),
                            round(float(p[6]), 0)])
            except Exception:
                continue
        if out:
            return out, d.get("preClose") or 0

    sym = _tx_symbol(secid)
    d = _j(TX_MIN % sym, retry=2, referer="https://gu.qq.com/")
    node = ((d.get("data") or {}).get(sym) or {}).get("data") or {}
    raw = node.get("data") or []
    pre = 0
    try:
        pre = float(node.get("prec") or 0)
    except Exception:
        pass
    out, pv, pa = [], 0.0, 0.0
    for line in raw:
        p = line.split()
        if len(p) < 4:
            continue
        try:
            hhmm = p[0]
            price = float(p[1])
            cv = float(p[2])           # 累计量(手)
            ca = float(p[3])           # 累计额(元)
        except Exception:
            continue
        v, a = max(0.0, cv - pv), max(0.0, ca - pa)
        pv, pa = cv, ca
        avg = (ca / (cv * 100)) if cv else price
        out.append(["%s:%s" % (hhmm[:2], hhmm[2:]),
                    round(price, 2), round(avg, 3), int(v), round(a, 0)])
    if not pre:
        pre = round(out[0][1], 2) if out else 0
    return out, pre


def em_ok():
    """东财列表接口是否还活着（进程内只探一次）。"""
    if not _CLIST["tried"]:
        d = _j(EM + "clist/get?po=0&np=1&fltt=2&invt=2&fid=f3&fs=m:0+t:6"
                   "&fields=f12&pn=1&pz=5", retry=1, timeout=12,
               referer="https://quote.eastmoney.com/")
        _CLIST["ok"] = bool((d.get("data") or {}).get("diff"))
        _CLIST["tried"] = True
        print("   行情列表源：%s" % ("东财 clist" if _CLIST["ok"] else "东财 ulist + 新浪全集"))
    return _CLIST["ok"]


# ---------------- 1. 全市场快照（新浪） ----------------
def _num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def _sina_row(x):
    """新浪一条 → 东财 diff 结构。市值单位万科 → 元，价格保持两位小数。"""
    sym = x.get("symbol") or ""
    return {
        "f12": x.get("code") or "",
        "f13": 1 if sym.startswith("sh") else 0,
        "f14": x.get("name") or "",
        "f2": round(_num(x.get("trade")), 2),
        "f3": round(_num(x.get("changepercent")), 2),
        "f6": _num(x.get("amount")),
        "f8": round(_num(x.get("turnoverratio")), 2),
        "f15": round(_num(x.get("high")), 2),
        "f16": round(_num(x.get("low")), 2),
        "f17": round(_num(x.get("open")), 2),
        "f18": round(_num(x.get("settlement")), 2),
        "f20": round(_num(x.get("mktcap")) * 1e4, 0),
        "f21": round(_num(x.get("nmc")) * 1e4, 0),
        "f23": round(_num(x.get("pb")), 2),
        "f115": round(_num(x.get("per")), 2),
    }


def _sina_page(p):
    u = SINA + ("getHQNodeData?page=%d&num=100&sort=symbol&asc=1&node=hs_a" % p)
    d = _j(u, retry=3, referer="https://finance.sina.com.cn/")
    return d if isinstance(d, list) else []


def market():
    """全市场 A 股快照（沪深京），东财 diff 结构，进程内缓存一次。"""
    if _MARKET["rows"] is not None:
        return _MARKET["rows"]
    cnt = _j(SINA + "getHQNodeStockCount?node=hs_a",
             referer="https://finance.sina.com.cn/")
    try:
        total = int(cnt)
    except Exception:
        total = 5400
    pages = total // 100 + 2
    with ThreadPoolExecutor(max_workers=8) as ex:
        got = list(ex.map(_sina_page, range(1, pages + 1)))
    rows, seen = [], set()
    for pg in got:
        for x in pg:
            c = x.get("code") or ""
            if not c or c in seen:
                continue
            seen.add(c)
            rows.append(_sina_row(x))
    print("   全市场快照：%d 只（新浪，接口报告 %d）" % (len(rows), total))
    _MARKET["rows"] = rows
    return rows


# ---------------- 2. 按代码批量（ulist） ----------------
def ulist(secids, fields=CLIST_FIELDS, chunk=100):
    """按 secid 批量取快照。ulist 对字段不做限制，缺的字段自动省略。"""
    if not secids:
        return []
    ids = list(secids)
    # f12 必带：返回顺序不保证与请求一致，靠它回填
    fl = fields if "f12" in fields else "f12," + fields
    parts = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]

    def one(grp):
        u = (EM + "ulist.np/get?fltt=2&invt=2&fields=%s&secids=%s"
             % (fl, ",".join(grp)))
        return (_j(u, referer="https://quote.eastmoney.com/").get("data") or {}).get("diff") or []

    out = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for d in ex.map(one, parts):
            out.extend([x for x in d if isinstance(x, dict)])
    return out


def enrich(rows, fields="f10,f62,f100,f115", chunk=100):
    """给已有行补东财独有字段（量比、主力净流入、行业、PE），原地写入。

    新浪没有 f62/f100/f10，异动归因和题材打分会用到，只能回东财按代码补。
    """
    if not rows:
        return rows
    ids = [("%d.%s" % (r.get("f13", 0), r["f12"])) for r in rows if r.get("f12")]
    data = {x.get("f12"): x for x in ulist(ids, fields + ",f12", chunk)}
    for r in rows:
        extra = data.get(r.get("f12")) or {}
        for k in fields.split(","):
            if k in extra and extra[k] not in (None, "-"):
                r[k] = extra[k]
    return rows


# ---------------- 3. 板块 / 成分股 ----------------
def boards():
    """概念板块代码全集 [(code, name)]。"""
    try:
        with open(BOARDS, encoding="utf-8") as f:
            d = json.load(f)
        return [(b["code"], b["name"]) for b in d.get("boards") or []]
    except Exception:
        return []


def save_boards(rows):
    """把当次成功拿到的板块清单落盘，供列表接口不可用时复用。"""
    bs = [{"code": x.get("f12"), "name": x.get("f14")}
          for x in rows if x.get("f12") and x.get("f14")]
    if len(bs) < 100:                      # 明显不完整就不覆盖
        return
    try:
        with open(BOARDS, "w", encoding="utf-8") as f:
            json.dump({"updated": time.strftime("%Y-%m-%d %H:%M"),
                       "boards": bs}, f, ensure_ascii=False)
    except Exception:
        pass


def members_map():
    try:
        with open(MEMBERS, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _board_diff(fields, po):
    bs = boards()
    if not bs:
        print("   !! 缺少板块代码缓存 build/boards.json，板块列表为空")
        return [], 0
    rows = ulist(["90." + c for c, _ in bs], fields)
    rows.sort(key=lambda x: -(_num(x.get("f3")) if po == 1 else -_num(x.get("f3"))))
    return rows, len(bs)


def _member_diff(fs, fields, po):
    code = fs.split(":", 1)[1].strip()
    codes = ((members_map().get("boards") or {}).get(code) or [])
    if not codes:
        return [], 0
    ids = [("1." if c.startswith("6") else "0.") + c for c in codes]
    rows = ulist(ids, fields)
    rows.sort(key=lambda x: -(_num(x.get("f3")) if po == 1 else -_num(x.get("f3"))))
    return rows, len(ids)


def _clist_diff(fs, fields, po, maxn):
    """东财原生分页（接口可用时走这条，行为与改造前完全一致）。"""
    out, page, total = [], 1, 0
    while page <= 100:
        u = (EM + "clist/get?po=%d&np=1&fltt=2&invt=2&fid=f3&fs=%s&fields=%s"
             "&pn=%d&pz=100" % (po, fs, fields, page))
        d = _j(u, referer="https://quote.eastmoney.com/").get("data") or {}
        diff = d.get("diff") or []
        if not diff:
            break
        total = d.get("total") or total
        out.extend(diff)
        if total and len(out) >= total:
            break
        if maxn and len(out) >= maxn:
            break
        page += 1
        time.sleep(0.12)
    return out, total


def diff_all(fs, fields=CLIST_FIELDS, po=1, maxn=None):
    """替代原来的 clist 分页：返回 (rows, total)，行结构与东财 diff 同构。"""
    if em_ok():
        out, total = _clist_diff(fs, fields, po, maxn)
        if out:
            return (out[:maxn] if maxn else out), total

    if "m:90+t:" in fs:                     # 概念/行业板块
        rows, total = _board_diff(fields, po)
    elif fs.startswith("b:"):               # 板块成分股
        rows, total = _member_diff(fs, fields, po)
    else:                                   # 个股（全市场各类 fs 都归一到这里）
        rows = market()
        rows.sort(key=lambda x: -(_num(x.get("f3")) if po == 1 else -_num(x.get("f3"))))
        total = len(rows)
        if maxn:
            rows = rows[:maxn]
        need = [k for k in fields.split(",") if k in ("f10", "f62", "f100", "f115", "f9")]
        if need:
            enrich(rows, ",".join(need))
    if maxn:
        rows = rows[:maxn]
    return rows, total
