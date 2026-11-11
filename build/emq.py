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
_TODAY = {"d": ""}
# 东财 K线/分时/批量是否可用，同样只探一次：被封时每次重试都在空等超时
_HIS = {"tried": False, "ok": True}
_ULIST = {"tried": False, "ok": True}

TX_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            "?param=%s,day,,,%d,%s")
TX_MIN = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=%s"

# 麦蕊智数：按 licence 计费。证书放环境变量 MAIRUI_LICENCE，本地兜底读文件
# （build/.mairui_key 已在 .gitignore 里，绝不入库）
MAIRUI = "https://api.mairuiapi.com"
_LIC = {"v": None}


def licence():
    if _LIC["v"] is None:
        v = (os.environ.get("MAIRUI_LICENCE") or "").strip()
        if not v:
            try:
                with open(BASE + "/build/.mairui_key", encoding="utf-8") as f:
                    v = f.read().strip()
            except Exception:
                v = ""
        _LIC["v"] = v
    return _LIC["v"]

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


def _is_stock(secid):
    """区分个股与指数：1.000001 是上证指数，0.000001 才是平安银行，不能混。"""
    m, c = secid.split(".")
    if m == "1" and c.startswith(("000", "880", "999")):
        return False
    if c.startswith(("399", "899")):
        return False
    return c.startswith(("60", "68", "00", "30", "43", "83", "87", "92"))


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


def _bars11(rows):
    """把 [(日期,开,收,高,低,量,额)] 补成东财 styles 的 11 列。

    麦蕊与腾讯都不直接给涨跌幅/振幅，用前一根收盘现算；换手率需要流通股本，
    这里留 0 —— 调用方（异动模块）只把它当辅助信号，缺了不影响主判定。
    """
    out, prev = [], None
    for dt, o, c, h, l, v, a in rows:
        pc = prev if prev else (o or c)
        chg = (c / pc - 1) * 100 if pc else 0.0
        amp = (h - l) / pc * 100 if pc else 0.0
        out.append("%s,%.2f,%.2f,%.2f,%.2f,%.0f,%.0f,%.2f,%.2f,%.2f,%.2f"
                   % (dt, o, c, h, l, v, a, amp, chg, c - pc, 0.0))
        prev = c
    return out


def _mairui_kline(code, lmt):
    """麦蕊全历史日K → 11 列东财风格字符串。麦蕊只给不复权，涨停核查与画图都够用。"""
    if not licence():
        return []
    raw = _j("%s/hsstock/history/%s/d/%s" % (MAIRUI, code, licence()),
             retry=2, timeout=40)
    if not isinstance(raw, list) or not raw:
        return []
    rows = []
    for r in raw[-lmt:]:
        try:
            rows.append((r["t"], float(r["o"]), float(r["c"]), float(r["h"]),
                         float(r["l"]), float(r.get("v") or 0), float(r.get("a") or 0)))
        except Exception:
            continue
    return _bars11(rows)


def _mairui_trend(code):
    """麦蕊当日 1 分钟K线 → [时间, 收, 均价, 量, 额]（量额是逐分钟的，均价按累计算）。"""
    if not licence():
        return []
    raw = _j("%s/hsstock/today/%s/1/%s" % (MAIRUI, code, licence()),
             retry=2, timeout=25)
    if not isinstance(raw, list) or not raw:
        return []
    out, cv, ca = [], 0.0, 0.0
    for r in raw:
        try:
            t = str(r.get("t") or "")
            price = float(r["c"])
            v = float(r.get("v") or 0)
            a = float(r.get("a") or 0)
        except Exception:
            continue
        cv += v
        ca += a
        avg = (ca / (cv * 100)) if cv else price
        out.append([t[11:16], round(price, 2), round(avg, 3), int(v), round(a, 0)])
    return out


def kline_lines(secid, lmt=120, fqt=0, src="auto"):
    """日K，返回东财风格字符串列表 "日期,开,收,高,低,量,额"，调用方无需改解析。

    取数顺序：麦蕊（用户指定主源）→ 东财 → 腾讯。板块指数(90.BKxxxx)麦蕊与腾讯
    都没有，只能靠东财；东财不可用时返回空，由上层降级。

    加一层当日磁盘缓存：麦蕊每次返回全历史（约 650KB，且没有条数参数），
    早盘筛选与收益跟踪会重复请求同一批票，缓存能把流量砍掉一半以上。
    """
    mkt, code = secid.split(".")

    cache = _cache_path(secid, fqt, src)
    if cache and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                lines = [x for x in f.read().split("\n") if x]
            if len(lines) >= max(25, lmt):     # 缓存里的根数不够就重新取，别拿短序列凑合
                return lines[-lmt:]
        except Exception:
            pass

    # 一次多取一些再切：先筛选用 120 根、收益跟踪要 400 根，
    # 若按调用方的 lmt 缓存，后一次调用会拿到不够用的短序列。
    lines = _kline_fetch(secid, max(lmt, 800), fqt, src)
    if cache and lines:
        try:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass
    return lines


def _cache_path(secid, fqt, src="auto"):
    """当日缓存路径；非交易日不复用（避免用昨天的数据顶今天的）。"""
    if not _TODAY["d"]:
        _TODAY["d"] = time.strftime("%Y-%m-%d")
    return "%s/build/klcache/%s/%s-%d-%s.txt" % (BASE, _TODAY["d"],
                                                  secid.replace(".", "_"), fqt, src)


def _kline_fetch(secid, lmt, fqt, src="auto"):
    mkt, code = secid.split(".")
    # src="tx"：收益跟踪要一次拉上千只票，麦蕊每次返回全历史（约650KB）太费流量
    if src != "tx" and _is_stock(secid) and licence():
        ks = _mairui_kline(code, lmt)
        if ks:
            return ks

    if hist_ok():
        u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
             "&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
             "&klt=101&fqt=%d&beg=0&end=20500101&lmt=%d" % (secid, fqt, lmt))
        ks = ((_j(u, retry=2, referer="https://quote.eastmoney.com/")
               .get("data") or {}).get("klines") or [])
        if ks:
            return ks

    if not code.isdigit():
        return []
    sym = _tx_symbol(secid)
    key = "qfqday" if fqt else "day"
    d = _j(TX_KLINE % (sym, lmt, "qfq" if fqt else ""),
           retry=2, referer="https://gu.qq.com/")
    node = ((d.get("data") or {}).get(sym) or {})
    rows = node.get(key) or node.get("qfqday") or node.get("day") or []
    bars = []
    for r in rows:
        try:
            bars.append((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                         float(r[5] or 0), float(r[2]) * float(r[5] or 0) * 100))
        except Exception:
            continue
    return _bars11(bars)


def trend_rows(secid):
    """当日分时，返回 ([[时间, 收, 均价, 量, 额]...], 昨收)。麦蕊→东财→腾讯。"""
    mkt, code = secid.split(".")
    if _is_stock(secid) and licence():
        rows = _mairui_trend(code)
        if rows:
            return rows, 0

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


_BST = {}
_BOARD_CHG = {}          # 当日板块涨跌幅，供 board_stats 增量追加
_BHIST = {"loaded": False, "d": {}}


def _bhist_path():
    return BASE + "/build/boardhist.json"


def bhist_load():
    """板块指数历史（收盘序列）。落盘后每天只需追加当日涨跌幅，不再拉外部K线。"""
    if not _BHIST["loaded"]:
        try:
            with open(_bhist_path(), encoding="utf-8") as f:
                _BHIST["d"] = (json.load(f) or {}).get("boards") or {}
        except Exception:
            _BHIST["d"] = {}
        _BHIST["loaded"] = True
    return _BHIST["d"]


def bhist_save(series_map=None):
    """把本次算出的序列并入历史文件，滚动保留最近 250 天。"""
    d = bhist_load()
    for k, v in (series_map or {}).items():
        d[k] = v[-250:]
    try:
        os.makedirs(os.path.dirname(_bhist_path()), exist_ok=True)
        with open(_bhist_path(), "w", encoding="utf-8") as f:
            json.dump({"updated": time.strftime("%Y-%m-%d %H:%M"), "boards": d},
                      f, ensure_ascii=False)
    except Exception:
        pass



_MRS = {}


def _mairui_series(code, lmt=120):
    """麦蕊日K → {日期: (开,收,高,低,量)}，给板块合成兜底用。"""
    if code in _MRS:
        return _MRS[code]
    out = {}
    for line in kline_lines(("1." if code.startswith("6") else "0.") + code, lmt, 0):
        p = line.split(",")
        try:
            out[p[0]] = (float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5]))
        except Exception:
            continue
    _MRS[code] = out
    return out


_TXROWS = {}


def _tx_rows(secid, lmt):
    """腾讯日K轻量版（只取收盘/高低/量），用于重建板块指数时省流量。

    板块之间成分高度重叠，一只票常被十几个板块用到，进程内记忆化能砍掉大半请求
    —— 之前没有这层缓存，681 个板块 × 15 只成分股直接把腾讯打到限流。
    """
    key = (secid, lmt)
    if key in _TXROWS:
        return _TXROWS[key]
    sym = _tx_symbol(secid)
    d = _j(TX_KLINE % (sym, lmt, ""), retry=2, referer="https://gu.qq.com/")
    node = ((d.get("data") or {}).get(sym) or {})
    rows = node.get("day") or node.get("qfqday") or []
    out = {}
    for r in rows:
        try:
            out[r[0]] = (float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5] or 0))
        except Exception:
            continue
    _TXROWS[key] = out
    return out


def _bmetrics(series):
    """由收盘序列算板块位置指标。series: [[日期, 收盘], ...]"""
    closes = [c for _, c in series]
    if len(closes) < 25:
        return None
    hi, lo, cur = max(closes), min(closes), closes[-1]
    n = len(closes)
    return {
        "bars": n, "close": round(cur, 2),
        "dd": round((cur / hi - 1) * 100, 1) if hi else 0.0,
        "pos": round((cur - lo) / (hi - lo) * 100, 1) if hi > lo else 50.0,
        "c20": round((cur / closes[-21] - 1) * 100, 1) if n > 21 else 0.0,
        "c60": round((cur / closes[-61] - 1) * 100, 1) if n > 61 else 0.0,
        "vol5v20": 0,
        "s": [[d, c] for d, c in series[-66:]],
    }


def board_stats(code, days=250, top=8):
    """板块指数：优先用落盘历史 + 当日涨跌幅增量推进，缺历史才重建。

    东财板块指数接口不可用；板块历史又不能天天重算（一次要几千个请求，
    把腾讯/东财都打成限流）。所以第一次重建后落盘 build/boardhist.json，
    此后每天只用成分股快照算出当日板块涨跌幅，追加一根K线即可 ——
    稳态下零外部请求。
    """
    if code in _BST:
        return _BST[code]

    today = time.strftime("%Y-%m-%d")
    ser = (bhist_load().get(code) or [])
    if len(ser) >= 60 and ser[-1][0] != today:
        chg = _BOARD_CHG.get(code)
        if chg is not None:
            last = ser[-1][1]
            ser = ser + [[today, round(last * (1 + chg / 100.0), 2)]]
            _BHIST["d"][code] = ser[-250:]
            res = _bmetrics(ser)
            _BST[code] = res
            return res

    mem = (members_map().get("boards") or {}).get(code) or []
    q = {r.get("f12"): r for r in market()}
    pool = [c for c in mem if c in q and (q[c].get("f21") or 0) > 0]
    if len(pool) < 3:
        _BST[code] = None
        return None
    pool.sort(key=lambda c: -(q[c].get("f21") or 0))
    pool = pool[:top]

    with ThreadPoolExecutor(max_workers=6) as ex:
        got = list(ex.map(lambda c: (c, _tx_rows(
            ("1." if c.startswith("6") else "0.") + c, days)), pool))

    if not any(s2 for _, s2 in got):
        # 轻量源整块不可用（腾讯被限流）：改用麦蕊，只取前 4 只、窗口缩到 120 天
        sub = pool[:4]
        with ThreadPoolExecutor(max_workers=4) as ex:
            got = list(ex.map(lambda c: (c, _mairui_series(c, 120)), sub))

    all_dates = sorted({d for _, s2 in got for d in s2})
    if len(all_dates) < 25:
        _BST[code] = None
        return None
    all_dates = all_dates[-days:]

    norm, weights = {}, {}
    for c, s2 in got:
        ds = [d for d in all_dates if d in s2]
        if len(ds) < len(all_dates) * 0.6:      # 停牌太多的票不参与合成
            continue
        base = s2[ds[0]][1]
        if not base:
            continue
        norm[c] = {d: s2[d][1] / base for d in ds}
        weights[c] = q[c].get("f21") or 1

    if not norm:
        _BST[code] = None
        return None

    series = []
    for d in all_dates:
        num, w = 0.0, 0.0
        for c, m in norm.items():
            if d in m:
                num += m[d] * weights[c]
                w += weights[c]
        if w:
            series.append([d, round(num / w * 1000, 2)])
    res = _bmetrics(series)
    if res:
        _BHIST["d"][code] = series[-250:]
    _BST[code] = res
    return res


def em_ok():
    """东财列表接口是否还活着（进程内只探一次）。"""
    if not _CLIST["tried"]:
        d = _j(EM + "clist/get?po=0&np=1&fltt=2&invt=2&fid=f3&fs=m:0+t:6"
                   "&fields=f12&pn=1&pz=5", retry=1, timeout=12,
               referer="https://quote.eastmoney.com/")
        _CLIST["ok"] = bool((d.get("data") or {}).get("diff"))
        _CLIST["tried"] = True
        print("   行情列表源：%s" % ("东财 clist" if _CLIST["ok"] else "新浪全集 + 本地重算"))
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
    """按 secid 批量取快照。ulist 对字段不做限制，缺的字段自动省略。

    一旦整轮取不到数据就置灰，本次进程不再重试 —— 被限流时反复请求只会更糟，
    上层会自动改用成员股快照自算（见 _board_from_members）。
    """
    if not secids or not _ULIST["ok"]:
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
    if not out:
        _ULIST["ok"] = False
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
    """板块行情一律自算。

    东财 ulist 已经不可靠（云端只给回前 100 个板块就被限流，题材雷达直接瘫掉），
    而板块行情完全可以用成分股快照推出来，没必要再赌第三方接口。
    """
    bs = boards()
    if not bs:
        print("   !! 缺少板块代码缓存 build/boards.json，板块列表为空")
        return [], 0
    rows = _board_from_members()
    rows.sort(key=lambda x: -(_num(x.get("f3")) if po == 1 else -_num(x.get("f3"))))
    return rows, len(bs)


def _board_from_members():
    """板块行情自算：用成分股快照按流通市值加权，不依赖任何板块行情接口。

    涨家数/跌家数/领涨股都能算出来；主力净流入(麦蕊与新浪都没有)留 0，
    题材打分里只影响"资金强度"一项，不影响位置与轮动。
    """
    mem = (members_map().get("boards") or {})
    if not mem:
        return []
    q = {r.get("f12"): r for r in market()}
    out = []
    for code, name in boards():
        rs = [q[c] for c in (mem.get(code) or [])
              if c in q and (q[c].get("f2") or 0) > 0]
        if not rs:
            continue
        wsum = sum((r.get("f21") or r.get("f20") or 0) for r in rs) or len(rs)
        chg = sum((_num(r.get("f3")) * ((r.get("f21") or r.get("f20") or 0) or 1))
                  for r in rs) / wsum
        up = sum(1 for r in rs if _num(r.get("f3")) > 0)
        down = sum(1 for r in rs if _num(r.get("f3")) < 0)
        lead = max(rs, key=lambda r: _num(r.get("f3")))
        _BOARD_CHG[code] = round(chg, 2)
        out.append({
            "f12": code, "f14": name, "f3": round(chg, 2),
            "f2": None, "f62": 0,
            "f104": up, "f105": down,
            "f128": lead.get("f14") or "", "f136": round(_num(lead.get("f3")), 2),
            "f6": sum(_num(r.get("f6")) for r in rs),
            "f20": sum(_num(r.get("f20")) for r in rs),
        })
    return out


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
