# -*- coding: utf-8 -*-
"""催化源清洗：新闻联播 + 韭研公社 → 可用性判定 → 题材归因

为什么单独一个模块：
    行情接口只能告诉你"哪个板块涨了"，告诉不了"为什么涨、有没有权威背书"。
    这里补两个偏可信的源：
      1. 新闻联播：最高层级的政策/产业信号，上了就是全国动员级别，缺点是绝大多数
         日子跟股市无关（政治/民生/国际为主），所以必须做"可用性判定"，不能硬凑。
      2. 韭研公社：产业侧的舆情盘点与个股逻辑，时效比新闻联播快，但噪音大
         （免责声明、行情播报、纯公司简介），同样要清洗和判可用。

清洗链：
    原始文本 → 去 HTML/标记 → 去免责声明与资料来源尾巴 → 去行情播报套话
             → 长度与信号词判定 → 题材归因 → 只留可引用证据句

可用性判定（usable）三项同时满足：
    len_ok   正文 ≥ 180 字（太短说明只是标题党或摘要残缺）
    signal   产业信号词命中 ≥ 2（订单/中标/产能/投产/涨价/技术突破/量产/招标…）
    theme    至少命中 1 个已知题材

题材归因两种口径：
    hotlist  舆情盘点类文档（"①算力-…②商业航天-…"）里显式点名的热点，权重最高
    mention  正文提及板块核心词，只算弱提及

产出：build/catalyst/<日期>.json
用法：
    python3 build/catalyst.py            # 当日
    python3 build/catalyst.py 2026-11-07 # 指定日期
"""
import json, os, re, sys, time, glob, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = BASE + "/build/catalyst"
HIST_DIR = BASE + "/build/catalyst/hist"

CN = timezone(timedelta(hours=8))
UT = "fa5fd1943c7b386f172d6893dbfba10b"
JY_ENDPOINT = os.environ.get("JY_MCP_URL", "https://mcp.jiuyangongshe.com/")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}

P = {
    "minLen": 180,       # 正文最短字数
    "minSignal": 2,      # 产业信号词最少命中数
    "maxDocs": 40,       # 每日最多处理多少篇韭研文档
    "quoteLen": 120,     # 证据句截断长度
    "xwlbBack": 5,       # 新闻联播往前回溯天数
}

# 产业信号词：出现说明文本里有"可交易的事实"，而不只是情绪
SIGNAL_WORDS = (
    "订单", "中标", "签约", "合同", "产能", "投产", "扩产", "量产", "开工", "竣工",
    "涨价", "提价", "降价", "涨价函", "招标", "获批", "批文", "核准", "备案",
    "技术突破", "突破", "首发", "首台", "首套", "国产替代", "自主可控", "专利",
    "营收", "净利", "毛利", "同比增长", "环比增长", "扭亏", "预增",
    "出货", "装机", "渗透率", "市占率", "交付", "下线", "并网", "试产",
    "试点", "示范", "规划", "补贴", "政策", "标准", "立项", "融资",
    "收购", "重组", "控股", "参股", "增资", "回购", "减持",
)

# 行情播报套话：密集出现说明是盘面复述，不是产业信息
NOISE_WORDS = ("拉升", "涨停", "走强", "震荡", "冲高", "翻红", "跌停", "异动",
               "封板", "炸板", "连板", "高开", "低开", "尾盘", "盘中")

# 新闻联播分类词表
POLITIC_WORDS = ("习近平", "李强", "赵乐际", "王沪宁", "蔡奇", "丁薛祥", "韩正",
                 "五中全会", "二十一大", "全会", "中央", "国务院", "全国人大",
                 "政协", "宣讲团", "代表选举", "港澳台", "贺电", "会见", "会谈",
                 "签署命令", "主席令", "致电", "指示", "批示", "调研", "考察")
LIVING_WORDS = ("天气", "雨雪", "降温", "秋粮", "春运", "暑运", "物价", "就业",
                "医保", "教育", "养老", "消费", "旅游", "景区", "航班", "高铁")
WORLD_WORDS = ("国际联播快讯", "伊朗", "美国", "美军", "俄罗斯", "乌克兰", "以色列",
               "加沙", "欧盟", "联合国", "北约", "日本", "韩国", "印度", "巴勒斯坦")
# 产业词：命中则判为产业新闻
INDUSTRY_WORDS = ("技术", "突破", "研发", "制造", "装备", "芯片", "半导体", "集成电",
                  "算力", "数据中心", "人工智能", "机器人", "卫星", "航天", "航空",
                  "光", "通信", "5G", "6G", "新能源", "光伏", "风电", "核电", "储能",
                  "电池", "锂", "稀土", "新材料", "化工", "钢铁", "有色", "汽车",
                  "船舶", "电网", "特高压", "油气", "煤", "种业", "农机", "医药",
                  "疫苗", "创新药", "基建", "铁路", "公路", "港口", "水利", "工程",
                  "投产", "开工", "建成", "下线", "首飞", "试航", "产量", "产能",
                  "进出口", "外贸", "制造业", "工业", "数字化", "智能", "绿色", "低碳")


# ---------------- 通用 ----------------
def jget(url, retry=3, timeout=25, headers=None):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=headers or HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception:
            if i < retry - 1:
                time.sleep(0.5 * (i + 1))
    return {}


def hget(url, retry=3, timeout=25):
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i < retry - 1:
                time.sleep(0.5 * (i + 1))
    return ""


def fetch_boards():
    """题材板块清单，用于把文本归因到板块。"""
    out, page = [], 1
    while page <= 10:
        u = ("https://push2.eastmoney.com/api/qt/clist/get?po=1&np=1&fltt=2&invt=2&fid=f3"
             "&fs=m:90+t:3&fields=f12,f14&pn=%d&pz=100" % page)
        d = (jget(u).get("data") or {})
        diff = d.get("diff") or []
        if not diff:
            break
        out.extend(diff)
        if len(out) >= (d.get("total") or 0):
            break
        page += 1
    return [(b["f12"], b["f14"]) for b in out]


# ---------------- 1. 新闻联播 ----------------
def xwlb(date):
    """抓某一日的新闻联播条目。返回 (实际日期, 条目列表)。

    央视网日归档页是静态 HTML，条目标题直接写在 alt="[视频]…" 里。
    当日 19:00 播出，所以盘后跑任务时当天往往还是 404，需要往前回溯。
    """
    for k in range(P["xwlbBack"] + 1):
        d = date - timedelta(days=k)
        u = "https://tv.cctv.com/lm/xwlb/day/%s.shtml" % d.strftime("%Y%m%d")
        h = hget(u, retry=1, timeout=20)
        if not h or len(h) < 3000:
            continue
        titles, seen = [], set()
        for t in re.findall(r'alt="\[视频\]([^"]+)"', h):
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                titles.append(t)
        if not titles:
            continue
        items = []
        for t in titles:
            kind = "产业" if any(w in t for w in INDUSTRY_WORDS) else \
                   "国际" if any(w in t for w in WORLD_WORDS) else \
                   "政治" if any(w in t for w in POLITIC_WORDS) else \
                   "民生" if any(w in t for w in LIVING_WORDS) else "其他"
            items.append({"title": t, "kind": kind,
                          "kw": [w for w in INDUSTRY_WORDS if w in t]})
        return d.strftime("%Y-%m-%d"), items
    return None, []


# ---------------- 2. 韭研公社 ----------------
class JY(object):
    """韭研 MCP（JSON-RPC over HTTP / SSE）精简客户端。"""

    def __init__(self):
        self.key = self._load_key()
        self.session = None
        self._id = 0
        self.ok = bool(self.key)
        self.err = "" if self.ok else "缺少 JY_API_KEY"

    @staticmethod
    def _load_key():
        k = os.environ.get("JY_API_KEY")
        if k and k.strip():
            return k.strip()
        # 本地回退：读 skill 目录下的密钥文件
        for p in (os.path.expanduser("~/.workbuddy/skills/jiuyan-gongshe/scripts/.jy_api_key"),
                  BASE + "/.jy_api_key"):
            if os.path.exists(p):
                try:
                    return open(p, encoding="utf-8").read().strip()
                except Exception:
                    pass
        return ""

    def _nid(self):
        self._id += 1
        return self._id

    def _post(self, payload, reply=True):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Authorization": "Bearer " + self.key}
        if self.session:
            h["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(JY_ENDPOINT, data=json.dumps(payload).encode("utf-8"),
                                     headers=h, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            with resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self.err = "HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:200])
            return None
        except Exception as e:
            self.err = "网络错误: %s" % e
            return None
        if not reply:
            return None
        if "text/event-stream" in ctype:
            res = None
            for block in raw.split("\n\n"):
                lines = [l[5:].lstrip() for l in block.split("\n") if l.startswith("data:")]
                if not lines:
                    continue
                try:
                    msg = json.loads("\n".join(lines))
                except Exception:
                    continue
                if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                    res = msg
            return res
        raw = raw.strip()
        try:
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def init(self):
        self._post({"jsonrpc": "2.0", "id": self._nid(), "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "catalyst", "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, reply=False)

    def search(self, date_from, date_to, doc_types=None, top_k=30, query=None):
        if not self.ok:
            return []
        self.init()
        args = {"date_from": date_from, "date_to": date_to, "top_k": top_k}
        if doc_types:
            args["doc_type"] = doc_types
        if query:
            args["query"] = query
        r = self._post({"jsonrpc": "2.0", "id": self._nid(), "method": "tools/call",
                        "params": {"name": "search_documents", "arguments": args}})
        if not r or r.get("error"):
            self.err = json.dumps((r or {}).get("error") or {}, ensure_ascii=False)[:200]
            return []
        cont = ((r.get("result") or {}).get("content") or [])
        text = "\n".join(c.get("text", "") for c in cont if c.get("type") == "text")
        return parse_docs(text)


def parse_docs(text):
    """解析 MCP 返回的可读文本。

    格式：
        [N] 标题
        类型: 点评 | 日期: 2026-11-09 | 股票: SZ000530,...
        摘要: ....
        doc_id: xxx
    注意：正文含 U+2028 行分隔符，只能按 \n 切分，不能用 splitlines()。
    """
    docs = []
    if not text:
        return docs
    blocks = re.split(r"\n\[(\d+)\]\s", "\n" + text.replace("\u2028", "\n"))
    # blocks: ['', '1', '标题\n类型…', '2', ...]
    for i in range(1, len(blocks) - 1, 2):
        body = blocks[i + 1]
        lines = body.split("\n")
        title = (lines[0] or "").strip()
        meta = next((l for l in lines[1:4] if l.startswith("类型")), "")
        dtype = ""
        ddate = ""
        stocks = []
        m = re.search(r"类型:\s*([^|]+)", meta)
        if m:
            dtype = m.group(1).strip()
        m = re.search(r"日期:\s*(\d{4}-\d{2}-\d{2})", meta)
        if m:
            ddate = m.group(1)
        m = re.search(r"股票:\s*([A-Z0-9,\s]+)", meta)
        if m:
            stocks = [s.strip() for s in m.group(1).split(",") if s.strip()]
        ab = body.find("摘要:")
        db = body.rfind("doc_id:")
        summary = body[ab + 3:db].strip() if ab >= 0 and db > ab else \
                  (body[ab + 3:].strip() if ab >= 0 else "")
        did = body[db + 7:].strip().split("\n")[0].strip() if db >= 0 else ""
        docs.append({"title": title, "type": dtype, "date": ddate,
                     "stocks": stocks[:30], "summary": summary, "doc_id": did})
    return docs


# ---------------- 3. 清洗 ----------------
def clean(text):
    """去掉标记、免责声明、资料来源、行情套话。"""
    if not text:
        return ""
    t = re.sub(r"<[^>]+>", " ", text)
    t = t.replace("\u2028", "\n").replace("\u00a0", " ")
    t = re.sub(r"[#*`>]+", " ", t)                      # markdown 标记
    t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", " ", t)   # emoji
    # 砍掉免责声明及其后的所有内容
    t = re.split(r"(?:免责声明|风险提示|资料来源|数据来源|分析师声明|"
                 r"本报告仅供|本订阅号|法律声明|版权声明)[:：]?", t)[0]
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\(?\b[036][0-9]{5}\b\)?", " ", t)      # 股票代码
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def noise_ratio(text):
    if not text:
        return 1.0
    hits = sum(text.count(w) for w in NOISE_WORDS)
    return hits / max(len(text) / 100.0, 1.0)


def judge(doc, boards):
    """可用性判定：返回 (usable, score, reasons, themes)。"""
    raw = doc.get("summary") or ""
    txt = clean(raw)
    reasons, bad = [], []
    if len(txt) < P["minLen"]:
        bad.append("正文仅 %d 字，信息量不足" % len(txt))
    sig = [w for w in SIGNAL_WORDS if w in txt]
    if len(sig) < P["minSignal"]:
        bad.append("产业信号词仅 %d 个" % len(sig))
    nr = noise_ratio(txt)
    if nr > 3.0 and len(sig) < 4:
        bad.append("行情播报套话密集，实质信息少")
    # 纯宏观清单：有 CPI/PPI/汇率 但几乎没有产业词
    macro = any(w in txt for w in ("CPI", "PPI", "利率", "汇率", "外储", "社融", "MLF", "LPR"))
    if macro and len(sig) < 3:
        bad.append("以宏观数据为主，缺少产业抓手")
    themes = map_themes(txt, boards)
    if not themes:
        bad.append("未命中任何题材")
    usable = not bad
    score = min(100, len(sig) * 8 + len(themes) * 14 + (15 if doc.get("hotlist") else 0))
    return usable, score, (bad or ["清洗后信息可用"]), themes


# 统计口径/风格口径的板块，不是产业题材。
# 归因阶段必须剔掉：一篇稿子里出现"知识产权"四个字，不该把结论算到"知识产权"板块上。
SKIP_BOARD = {
    "知识产权", "机构重仓", "基金重仓", "QFII重仓", "社保重仓", "券商重仓",
    "昨日涨停", "昨日连板", "昨日首板", "昨日触板", "昨日涨停_含一字",
    "昨日连板_含一字", "昨日打二板以上表现", "历史新高", "历史新低",
    "破净股", "百元股", "低价股", "高价股", "次新股", "壳资源", "ST股",
    "融资融券", "转融券标的", "MSCI中国", "富时罗素", "标普道琼斯",
    "大盘股", "中盘股", "小盘股", "超大盘", "中字头", "行业龙头", "题材股",
    "反转股", "趋势股", "成长风格", "价值风格", "举牌", "股权转让",
    "高送转", "预盈预增", "预亏预减", "业绩预增", "业绩预减",
}


def map_themes(text, boards, limit=8):
    """把文本归因到题材板块：板块名去后缀后作为核心词做匹配。"""
    hits = []
    for code, name in boards:
        if name in SKIP_BOARD:
            continue          # 统计口径的板块不是产业题材，归因过去只会污染结论
        core = re.sub(r"(概念|板块|指数|行业)$", "", name).strip()
        if len(core) < 2:
            continue
        n = text.count(core)
        if n:
            hits.append((code, name, n))
    hits.sort(key=lambda x: -x[2])
    return [{"code": c, "name": n, "n": k} for c, n, k in hits[:limit]]


def quotes(text, word, limit=2, span=None):
    """摘出包含关键词的句子，作为可引用证据。"""
    out = []
    for s in re.split(r"[。；\n]", text):
        s = s.strip()
        if word in s and len(s) >= 8:
            out.append(s[:span or P["quoteLen"]])
            if len(out) >= limit:
                break
    return out


# ---------------- 4. 舆情热点清单 ----------------
def parse_hotlist(text):
    """解析"①算力-…②商业航天-…"或"1、XX：…"式的热点盘点。

    这是韭研最有价值的文档类型：作者已经把当日热点整理成清单，
    直接拿来就能和行情板块对齐，比正文关键词匹配准确得多。
    """
    out = []
    if not text:
        return out
    # 形态一：①②③ 圈号 + 名称-描述
    for m in re.finditer(r"[①②③④⑤⑥⑦⑧⑨⑩]\s*([^-\n：:（(]{2,18})\s*[-－]\s*([^\n]{6,200})", text):
        desc = m.group(2).strip()
        out.append({"name": m.group(1).strip(), "desc": desc,
                    "stocks": pick_stocks(desc), "form": "circ"})
    # 形态二：热度盘点式的 "名称-描述"（无圈号但成段列举）
    if not out:
        for m in re.finditer(r"^\s*(\d{1,2})[、.．]\s*([^\n：:]{2,18})[：:]\s*([^\n]{20,300})", text, re.M):
            desc = m.group(3).strip()
            out.append({"name": m.group(2).strip(), "desc": desc,
                        "stocks": pick_stocks(desc), "form": "num"})
    # 去重
    seen, uniq = set(), []
    for h in out:
        if h["name"] in seen:
            continue
        seen.add(h["name"])
        uniq.append(h)
    return uniq[:12]


def pick_stocks(desc):
    """从描述里挖出被点名的个股名单。

    韭研的舆情盘点几乎都写成"…（顺灏股份、润贝航科、西部材料等）"，
    这个名单比东财板块成分股干净得多——板块里常混进一堆顺带的票
    （比如"商业航天"里塞满了 PCB 公司），用点名名单能校正龙头身份。
    """
    out = []
    for seg in re.findall(r"[（(]([^（()）]{6,200})[)）]", desc):
        for s in re.split(r"[、,，\s]+", seg):
            s = re.sub(r"等$", "", s.strip())
            if 2 <= len(s) <= 5 and re.fullmatch(r"[\u4e00-\u9fa5A-Za-z*]{2,5}", s) \
                    and s not in ("关注", "受益", "龙头", "相关", "概念"):
                out.append(s)
    return out[:15]


def is_hotlist_doc(title, text):
    keys = ("舆情热度", "今日汇总", "热点", "共振", "复盘", "涨停分析", "异动")
    if any(k in (title or "") for k in keys):
        return True
    return len(re.findall(r"[①②③④⑤⑥⑦⑧⑨⑩]", text or "")) >= 3


# ---------------- 5. 主流程 ----------------
def main():
    t0 = time.time()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    now = datetime.now(CN)
    if arg:
        try:
            day = datetime.strptime(arg, "%Y-%m-%d")
        except Exception:
            sys.exit("日期格式应为 YYYY-MM-DD")
    else:
        day = now.replace(tzinfo=None)
    ds = day.strftime("%Y-%m-%d")
    print("催化源清洗 · %s" % ds)

    boards = fetch_boards()
    print("   题材板块 %d 个（用于归因）" % len(boards))

    # --- 新闻联播 ---
    xd, items = xwlb(day)
    ind = [x for x in items if x["kind"] == "产业"]
    print("   新闻联播 %s：%d 条，其中产业类 %d 条" % (xd or "未获取到", len(items), len(ind)))
    xwlb_out = {
        "date": xd, "total": len(items), "industry": ind,
        "available": len(ind) > 0,
        "note": ("产业条目 %d 条，可作为政策/技术背书" % len(ind)) if ind else
                ("当日条目以%s为主，无产业信号" %
                 ("、".join(sorted({x["kind"] for x in items})) or "其他"))
                if items else "未取到当日条目（19:00 前抓取会回溯到上一日）",
    }

    # --- 韭研公社 ---
    jy = JY()
    docs = []
    if not jy.ok:
        print("   韭研：%s，跳过" % jy.err)
    else:
        # 点评类优先（舆情盘点、盘前盘后都在这里），再补研报与纪要
        for dt in (["remark"], ["research", "meeting"]):
            got = jy.search(ds, ds, doc_types=dt, top_k=P["maxDocs"])
            docs.extend(got)
        # 去重
        seen, uniq = set(), []
        for d in docs:
            k = d.get("doc_id") or d.get("title")
            if k in seen:
                continue
            seen.add(k)
            uniq.append(d)
        docs = uniq
        print("   韭研：当日 %d 篇（点评/研报/纪要）%s" % (len(docs), jy.err or ""))

    jy_out, theme_hit = [], {}
    for d in docs:
        txt = clean(d.get("summary") or "")
        hl = is_hotlist_doc(d.get("title"), txt)
        d["hotlist"] = hl
        usable, score, reasons, themes = judge(d, boards)
        hot = parse_hotlist(txt) if hl else []
        rec = {
            "title": d.get("title"), "type": d.get("type"), "date": d.get("date"),
            "doc_id": d.get("doc_id"), "len": len(txt), "usable": usable,
            "score": score, "reason": "；".join(reasons), "hotlist": hl,
            "themes": themes,
            "hot": [{"name": h["name"], "desc": h["desc"][:100],
                     "stocks": h.get("stocks") or []} for h in hot],
            "quote": txt[:P["quoteLen"]] if usable else "",
        }
        jy_out.append(rec)

        if not usable:
            continue
        # 题材归因：热点清单权重 3，正文提及权重 1
        for h in hot:
            for t in map_themes(h["name"] + " " + h["desc"], boards, limit=3):
                e = theme_hit.setdefault(t["code"], {
                    "code": t["code"], "name": t["name"], "hotlist": 0, "mention": 0,
                    "ev": [], "stocks": []})
                e["hotlist"] += 1
                for s in (h.get("stocks") or []):
                    if s not in e["stocks"]:
                        e["stocks"].append(s)
                if len(e["ev"]) < 4:
                    e["ev"].append({"src": "韭研", "title": d.get("title"),
                                    "kind": "热点点名", "text": h["desc"][:P["quoteLen"]],
                                    "doc_id": d.get("doc_id")})
        for t in themes:
            e = theme_hit.setdefault(t["code"], {
                "code": t["code"], "name": t["name"], "hotlist": 0, "mention": 0,
                "ev": [], "stocks": []})
            e["mention"] += 1
            if len(e["ev"]) < 4:
                q = quotes(txt, re.sub(r"(概念|板块|指数|行业)$", "", t["name"]), 1)
                if q:
                    e["ev"].append({"src": "韭研", "title": d.get("title"),
                                    "kind": "正文提及", "text": q[0],
                                    "doc_id": d.get("doc_id")})

    # 新闻联播归因（权重最高，单独标记）
    for it in ind:
        for t in map_themes(it["title"], boards, limit=3):
            e = theme_hit.setdefault(t["code"], {
                "code": t["code"], "name": t["name"], "hotlist": 0, "mention": 0,
                "ev": [], "stocks": []})
            e["xwlb"] = e.get("xwlb", 0) + 1
            if len(e["ev"]) < 5:
                e["ev"].append({"src": "新闻联播", "title": it["title"],
                                "kind": "官方报道", "text": it["title"], "doc_id": ""})

    # 汇总每个题材的催化强度
    for e in theme_hit.values():
        e.setdefault("stocks", [])
        e["score"] = min(100, e.get("xwlb", 0) * 40 + e["hotlist"] * 25 + e["mention"] * 6)
    ranked = sorted(theme_hit.values(), key=lambda x: -x["score"])

    us = [d for d in jy_out if d["usable"]]
    out = {
        "date": ds,
        "generated": now.strftime("%Y-%m-%d %H:%M"),
        "params": P,
        "xwlb": xwlb_out,
        "jiuyan": {"total": len(jy_out), "usable": len(us), "docs": jy_out,
                   "error": jy.err if not jy.ok else ""},
        "themes": ranked,
        "summary": {
            "xwlbIndustry": len(ind),
            "jyUsable": len(us),
            "hotThemes": [{"name": x["name"], "score": x["score"],
                           "hotlist": x["hotlist"], "xwlb": x.get("xwlb", 0)}
                          for x in ranked[:10]],
        },
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    fp = "%s/%s.json" % (OUT_DIR, ds)
    json.dump(out, open(fp, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("   可用文档 %d / %d，命中题材 %d 个" % (len(us), len(jy_out), len(ranked)))
    print("   催化最强的题材：")
    for x in ranked[:6]:
        print("     %-12s 强度%-4d 点名%d 提及%d 联播%d" %
              (x["name"], x["score"], x["hotlist"], x["mention"], x.get("xwlb", 0)))
    print("写入 %s（%.1f KB）· 用时 %.1fs" % (fp, os.path.getsize(fp) / 1024, time.time() - t0))

    # 轮转：只留最近 60 份
    olds = sorted(os.path.basename(f) for f in glob.glob(OUT_DIR + "/[0-9]*.json"))[:-60]
    for fn in olds:
        os.remove(OUT_DIR + "/" + fn)


if __name__ == "__main__":
    main()
