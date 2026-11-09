# -*- coding: utf-8 -*-
"""低位异动：个股低位启动 → 消息催化归因 → 催化持续性评估

为什么单独一个模块：
    长上影选股看的是"K线的形状"，题材雷达看的是"板块的位置"。两者都漏掉一类机会：
    某只票在低位突然放量涨停，但所属板块整体没动 —— 这种"个股先于板块"的异动，
    往往对应一条还没扩散的消息。等板块跟涨，位置就高了。

四步判定链：
    1. 异动门  当日涨幅 ≥ 3% 且（涨停 或 5日均额/20日均额 ≥ 1.5）
    2. 低位门  距 250 日高点回撤 ≥ 25%，或 250 日位置分位 ≤ 35%
    3. 背离门  个股涨幅 − 所属最强产业板块涨幅 ≥ 3 个百分点
               （板块也在涨就不算"独立异动"，那种归题材雷达管）
    4. 归因    个股新闻 + 板块新闻 + 韭研点名 + 政策池 + 新闻联播，五路证据

三条归因纪律（都是踩过坑之后加的）：
    时效硬闸门   超过 N 天的一律不进证据池。第一版没做，把 173 天前的旧闻
                 （"西部材料回应向 SpaceX 供货传闻"）当成了今天的催化。
    机构名在标题  机构名只认标题不认正文。正文里的"XX研究院""国家"太容易误伤，
                 第一版把"机器人ETF获净申购"判成了国家级政策。
    政策要动词    只有机构名 + 印发/发布/出台/批复… 才认政策级别，
                 否则降档 —— 否则"上海市某公司发布新品"会被当成上海市政策。

催化能维持多久（持久性）：
    不是拍脑袋给天数，而是按 类别 × 级别 × 新鲜度 × 可验证性 × 后续节点：
      类别基础窗口：政策规划 5、订单业绩 4、技术突破 3、资本运作 2、行情情绪 1、研报 1
      级别加成：国家级 ×1.6，部委级 ×1.4，省级 ×1.2
      新鲜度衰减：当日 1.0、1日 0.85、2-3日 0.6、>3日 0.35
      可验证（含具体数字）+1 天；后续节点（11月13日/明年Q1）+2 天
    ≥7 天"强"，4-6"中"，2-3"弱"，≤1"脉冲"。

产出：build/anomaly/<日期>.json
用法：
    python3 build/anomaly.py            # 当日
    python3 build/anomaly.py 2026-11-09 # 指定日期
"""
import json, os, re, sys, glob, time, urllib.request, urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = BASE + "/build/anomaly"
# 只保留主板（沪 600/601/603/605，深 000/001/002/003），与 screen.py 完全一致
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
CN = timezone(timedelta(hours=8))
UT = "fa5fd1943c7b386f172d6893dbfba10b"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "*/*",
}

P = {
    "scanTop": 400,        # 涨幅榜最多取多少只做初筛
    "minChg": 3.0,         # 异动：当日涨幅下限
    "volMin": 1.5,         # 异动：5日均额 / 20日均额 下限（未涨停时必须满足）
    "lowDd": 25.0,         # 低位：距 250 日高点回撤 ≥ 此值(%)
    "lowPos": 35.0,        # 低位：250 日位置分位 ≤ 此值(%)
    "minBars": 120,        # 上市至少这么多交易日（新股不算低位）
    "minMcap": 20e8,       # 总市值下限
    "devMin": 3.0,         # 背离：个股涨幅 − 最强板块涨幅 ≥ 此值(百分点)
    # 首日门：只抓"今天才起涨"的，前几天已经涨过的属于鱼身，不是低位异动
    "firstBars": 5,        # 回看多少个交易日
    "firstMaxDay": 3.0,    # 回看窗口内单日涨幅上限（达到即算已经异动过）
    "firstMaxCum": 8.0,    # 回看窗口内累计涨幅上限（防连续小阳线堆出趋势）
    "maxOut": 30,          # 最多输出多少只
    "newsDays": 10,        # 个股新闻回溯上限（天）
    "sectorDays": 7,       # 板块新闻回溯上限
    "policyDays": 7,       # 政策池回溯上限
    "xwlbDays": 5,         # 新闻联播回溯天数
}

# 统计型板块：涨跌幅不代表产业景气，不能用来算"最强板块"
STAT_BOARD = (
    "昨日", "今日", "最近", "连板", "多板", "首板", "反包", "涨停", "跌停",
    "高振幅", "热股", "新高", "新低", "百元股", "低价股", "小盘", "大盘",
    "中盘", "微盘", "绩优", "亏损", "预盈", "预亏", "ST股", "次新", "破净",
    "高送转", "融资融券", "深股通", "沪股通", "MSCI", "富时", "标普",
    "机构重仓", "基金重仓", "QFII", "社保", "券商", "私募", "证金", "汇金",
    "央国企改革", "题材股", "科技风格", "红利", "价值", "成长", "超跌",
    "反弹", "填权", "举牌", "股权转让", "注册制", "AB股", "GDR", "B股", "转债",
)

# 板块关键词扩展：东财板块名太窄，很多重磅新闻标题里根本不含板块全名。
# 例：11-09 的《"星云星座计划"发布 我国太空算力网加快部署》——标题里没有"商业航天"
# 四个字，只搜板块名会整条漏掉。这里给常见板块补同义/相关词。
KW_EXT = {
    "商业航天": ("星舰", "太空算力", "探月", "登月", "航天发射", "火箭"),
    "卫星互联网": ("低轨卫星", "星链", "千帆星座", "星座组网", "卫星通信"),
    "卫星导航": ("北斗", "导航系统"),
    "人形机器人": ("具身智能", "宇树", "灵巧手", "人形机器人"),
    "机器人概念": ("具身智能", "人形机器人"),
    "数据中心": ("算力", "智算中心", "液冷"),
    "算力概念": ("算力", "智算", "AI服务器"),
    "PCB": ("印制电路板", "覆铜板"),
    "固态电池": ("半固态电池", "固态电池"),
    "可控核聚变": ("核聚变", "托卡马克", "高温超导"),
    "稀土永磁": ("稀土", "钕铁硼"),
    "存储芯片": ("HBM", "DRAM", "存储芯片"),
    "低空经济": ("eVTOL", "飞行汽车", "低空"),
    "光通信": ("光模块", "光纤", "CPO"),
    "大飞机": ("C919", "国产大飞机"),
    "军工": ("军工", "国防"),
    "创新药": ("创新药", "临床"),
}

# ETF/媒体稿：它们描述"指数涨了多少"，不是产业催化，一律压到最低窗口
MEDIA_WORDS = ("ETF", "净申购", "标的指数", "指数涨", "基金", "联接", "盘中获")

# ---------------- 政府背书级别 ----------------
# 顺序即优先级。机构名必须在标题里出现才认（正文太容易误伤）。
GOV_LEVELS = (
    ("国家级", 100, (
        "新闻联播", "央视新闻", "央视快评", "人民日报", "新华社", "国务院", "中共中央",
        "中央政治局", "国家主席", "工信部", "工业和信息化部", "国家发展改革委", "发改委",
        "国防科工局", "国家航天局", "中国科学院", "中科院", "财政部", "科技部",
        "国家数据局", "国资委", "证监会", "中央军委", "载人航天工程办公室",
        "国家能源局", "国家网信办", "中央网信办", "全国人大", "国家医保局",
        "国家药监局", "国家林草局", "国家原子能机构",
    )),
    ("部委级", 80, (
        "商务部", "交通运输部", "自然资源部", "生态环境部", "水利部", "农业农村部",
        "住房城乡建设部", "应急管理部", "民航局", "市场监管总局", "海关总署",
        "国家统计局", "国家知识产权局", "国家铁路局", "气象局", "烟草局",
    )),
    ("省级", 60, (
        "上海市", "北京市", "广东省", "江苏省", "浙江省", "山东省", "四川省",
        "湖北省", "湖南省", "河南省", "河北省", "安徽省", "福建省", "江西省",
        "陕西省", "辽宁省", "吉林省", "黑龙江省", "山西省", "甘肃省", "青海省",
        "云南省", "贵州省", "海南省", "广西", "内蒙古", "西藏", "宁夏", "新疆",
        "深圳市", "重庆市", "天津市", "省政府", "省经信委", "省工信厅",
        "省发改委", "省科技厅", "市经信委", "市工信局",
    )),
    ("市级", 40, ("市政府", "区政府", "县政府", "高新区", "经开区", "自贸区", "管委会")),
    ("行业组织", 30, ("GSMA", "协会", "联盟", "学会", "联合会")),
)

# 政策动词：收紧到"真的在发文件/定规划"。支持/推动/加快这类泛动词已剔除，
# 否则任何带"支持"二字的公司新闻都会被判成政府背书。
POLICY_VERB = (
    "印发", "下发", "发布", "出台", "批复", "部署", "规划", "方案", "意见", "措施",
    "条例", "办法", "试点", "审议通过", "批准立项", "立项", "挂牌", "揭牌",
    "谋划", "核准", "批复建设",
)

# 事件类别 → (基础窗口天数, 关键词)
# "落地""启动"原先放在政策规划里，把「海天瑞声：已经落地多个具身智能项目」这种
# 公司动态判成了政策（5 天）。这两个词太泛，归到订单/资本运作才准。
EVENT_CATS = (
    ("政策规划", 5, ("行动方案", "发展规划", "规划", "指导意见", "实施意见", "若干措施",
                     "条例", "管理办法", "试点", "产业基金", "专项", "扶持", "补贴",
                     "示范", "政策", "部署")),
    ("订单业绩", 4, ("中标", "签约", "订单", "合同", "框架协议", "营收", "净利", "预增",
                     "业绩", "扭亏", "毛利率", "交付", "供货", "产能", "扩产", "满产",
                     "点亮", "量产线", "落地")),
    ("技术突破", 3, ("突破", "成功", "首次", "首台", "首套", "量产", "投产", "下线",
                     "入轨", "发射", "试飞", "点火", "并网", "验收", "国产化", "替代",
                     "搭载", "在轨")),
    ("资本运作", 2, ("定增", "增发", "回购", "增持", "并购", "重组", "收购", "上市",
                     "IPO", "聆讯", "股权激励", "可转债", "分拆", "启动")),
    ("行情情绪", 1, ("龙虎榜", "涨停", "涨超", "跌超", "异动", "封板", "连板", "走强",
                     "拉升", "冲高", "盘中", "收盘", "早盘", "午评", "复盘", "高开",
                     "直线", "领涨", "封单")),
    ("研报软文", 1, ("研报", "评级", "推荐", "看好", "ETF", "基金", "净申购", "观点",
                     "解读", "展望", "策略", "调研")),
)

# 结果类标题：它们描述"已经涨了"，不解释"为什么涨"，做原因排序时要压下去
RESULT_WORDS = ("涨停", "走强", "拉升", "异动", "封板", "冲高", "涨超", "高开",
                "直线", "连板", "盘中", "早盘", "午评", "收盘", "复盘", "领涨", "封单")

NOISE_WORDS = ("免责声明", "风险提示", "不构成投资建议", "据此操作", "扫码", "点击查看")

# 后续节点：只认明确、近期的时间点。"20XX年"是远期规划，不算节点。
NEXT_PAT = re.compile(
    r"((?:1[0-2]|0?[1-9])\s*月\s*\d{1,2}\s*[日号]"
    r"|(?:1[0-2]|0?[1-9])\s*月[上中下]旬"
    r"|明年\s*(?:上|中|下)?半年|明年\s*Q[1-4]|下个?月)")


# ---------------- 基础请求 ----------------
def jget(url, retry=3, timeout=25):
    for _ in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))
        except Exception:
            time.sleep(0.4)
    return {}


def jget_text(url, retry=3, timeout=25):
    for _ in range(retry):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception:
            time.sleep(0.4)
    return ""


def hget(url, retry=2, timeout=20):
    for _ in range(retry):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
            return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception:
            time.sleep(0.4)
    return ""


# ---------------- 1. 行情：异动候选 ----------------
def quote_top(maxn=P["scanTop"], min_chg=P["minChg"]):
    """涨幅榜降序取前 N 只，涨幅低于门槛即停（降序，后面更小）。"""
    out, page = [], 1
    while page <= 6 and len(out) < maxn:
        u = ("https://push2.eastmoney.com/api/qt/clist/get?po=1&np=1&fltt=2&invt=2&fid=f3"
             "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
             "&fields=f12,f13,f14,f2,f3,f6,f8,f10,f20,f21,f62,f100,f115"
             "&pn=%d&pz=100" % page)
        d = (jget(u).get("data") or {})
        df = d.get("diff") or []
        if not df:
            break
        stop = False
        for x in df:
            chg = x.get("f3")
            if not isinstance(chg, (int, float)):
                continue
            if chg < min_chg:
                stop = True
                break
            out.append(x)
        if stop:
            break
        page += 1
    return out[:maxn]


def stock_kline(code, market, bars=280):
    u = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%d.%s&ut=%s"
         "&fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
         "&klt=101&fqt=1&beg=0&end=20500101&lmt=%d" % (market, code, UT, bars))
    ks = ((jget(u).get("data") or {}).get("klines") or [])
    rows = []
    for x in ks:
        p = x.split(",")
        if len(p) < 11:
            continue
        try:
            rows.append({"d": p[0], "o": float(p[1]), "c": float(p[2]), "h": float(p[3]),
                         "l": float(p[4]), "amt": float(p[6]), "chg": float(p[8]),
                         "turn": float(p[10])})
        except Exception:
            continue
    return rows


def low_metrics(rows):
    if len(rows) < 30:
        return None
    w = rows[-250:]
    hi = max(r["h"] for r in w)
    lo = min(r["l"] for r in w)
    cs = [r["c"] for r in w]
    cur = cs[-1]
    dd = (cur / hi - 1) * 100 if hi else 0
    pos = (cur - lo) / (hi - lo) * 100 if hi > lo else 50
    c20 = (cur / cs[-21] - 1) * 100 if len(cs) > 21 else 0
    c60 = (cur / cs[-61] - 1) * 100 if len(cs) > 61 else 0
    a5 = sum(r["amt"] for r in rows[-5:]) / 5.0
    a20 = sum(r["amt"] for r in rows[-20:]) / 20.0

    # 首日门用：回看窗口内（不含今天）的逐日涨幅与累计涨幅
    n = P["firstBars"]
    seq, mx = [], 0.0
    if len(cs) >= n + 2:
        for i in range(2, n + 2):          # i=2 → 昨天, …, i=n+1 → 窗口最左一天
            seq.append(round((cs[-i] / cs[-i - 1] - 1) * 100, 1))
        mx = max(seq)
    cum = 1.0
    for v in seq:
        cum *= (1 + v / 100.0)
    cum = (cum - 1) * 100

    # 当日成交额 / 前 20 日均额（不含当日）—— 区分「缩量抢跑」与「放量进场」的关键。
    # 西部材料 10-28 涨 7.90% 只有 8.0 亿、量比 0.82，就是资金在没人注意时先买；
    # 而放量 2 倍以上的涨停是资金高调进场，通常意味着消息已经扩散。
    prev20 = [r["amt"] for r in rows[-21:-1]]
    a_mean = sum(prev20) / len(prev20) if prev20 else 0
    amt_ratio = round(rows[-1]["amt"] / a_mean, 2) if a_mean else 0

    # 近 20 日（不含今天）已有过几次涨停。首日门只看前 5 日，挡不住
    # "涨一波→放量跌停洗盘→横三周→再涨停"这种二波启动（泰尔股份 10-20/21 两连板
    # 之后今天再度涨停，前 5 日安静，照样过门）。这个计数就是给这类票盖个戳。
    ups20 = 0
    for i in range(max(1, len(cs) - 21), len(cs) - 1):
        if cs[i - 1] and (cs[i] / cs[i - 1] - 1) * 100 >= 9.5:
            ups20 += 1

    return {"dd": round(dd, 1), "pos": round(pos, 1), "c20": round(c20, 1),
            "c60": round(c60, 1), "volRatio": round(a5 / a20, 2) if a20 else 0,
            "bars": len(rows), "high250": round(hi, 2), "low250": round(lo, 2),
            "prevMax": round(mx, 1), "prevCum": round(cum, 1),
            "prevSeq": seq, "prevN": len(seq), "amtRatio": amt_ratio,
            "ups20": ups20}


def stock_boards(code, market):
    u = ("https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2"
         "&fields=f12,f13,f14,f3&secid=%d.%s&pn=1&pz=40&po=1&np=1" % (market, code))
    df = (jget(u).get("data") or {}).get("diff") or []
    out = []
    for x in df:
        if not isinstance(x, dict):
            continue
        nm, chg = x.get("f14") or "", x.get("f3")
        if not isinstance(chg, (int, float)):
            continue
        out.append({"code": x.get("f12"), "name": nm, "chg": chg})
    return out


# 央媒白名单：只留真正的中央级媒体。三大证券报不能进 ——
# "欧洲多国超市制冷设备遭网络攻击"（证券时报转载）被当成红旗连锁的国家级背书，太荒唐。
MEDIA_GOV = ("央视新闻", "央视网", "央视财经", "新华社", "人民日报", "经济日报",
             "中国政府网", "央广网", "中国新闻网", "求是")
# 央媒稿件还要有"国家级"信号词，才认成国家级（否则只是普通报道）
MEDIA_SIGNAL = ("我国", "国家", "全国", "国务院", "中科院", "中国科学院",
                "发布", "印发", "启动", "部署", "获批", "首个", "全球首")

# 国家重大工程：航天类事件多半没有"工信部/中科院"这类机构名，
# 但"载人航天""运载火箭""探月"本身就代表国家意志，必须单独识别。
PROJ_STRONG = ("载人航天", "运载火箭", "探月工程", "登月", "空间站", "国家航天局",
               "嫦娥", "北斗组网", "长征十号", "长征八号")
PROJ_WEAK = ("组网任务", "入轨", "发射成功", "星座计划", "卫星互联网", "首飞", "试飞")
CN_WORDS = ("我国", "中国", "国家", "国内首")
# 海外映射不是"政府背书"，要单独标，分数也低一档
OVERSEA = ("SpaceX", "马斯克", "星舰", "星链", "NASA", "蓝色起源", "亚马逊")

# 地域/行政板块：它们的涨跌跟产业无关，且会把地方新闻甚至领导人活动配进来
REGION = ("北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
          "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
          "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
          "青海", "宁夏", "新疆", "内蒙古", "深圳", "雄安", "长三角", "粤港澳",
          "长江三角", "成渝", "京津冀")

# 板块新闻缓存：同一板块只抓一次，全局共享（去重后通常 30-40 个板块）
_SECTOR_NEWS = {}


def is_stat_board(name):
    return any(w in name for w in STAT_BOARD)


def is_region_board(nm):
    for r in REGION:
        if nm == r or (nm.startswith(r) and len(nm) <= len(r) + 2):
            return True
    return False


def industry_boards(real):
    """可用于消息面关联的产业板块：排除统计型、地域型、行政型。

    地域板块必须排除：东方新能因为所属"北京板块"，新闻联播
    "习近平参加北京市区人大代表换届选举投票"被当成它的政策背书。
    """
    out = []
    for b in real:
        nm = b["name"]
        if is_stat_board(nm) or is_region_board(nm):
            continue
        if nm.endswith("特区") or nm.endswith("新区") or "大开发" in nm:
            continue
        if len(nm) < 2:
            continue
        out.append(b)
    return out


# 宽泛行业板块：申万一级那一层，任何一条行业新闻都能"命中"，
# 对解释个股异动毫无信息量。长江通信因为属于"通信"，一条工信部行业标准
# 征求意见稿被当成了它的国家级政策背书 —— 必须排除。
WIDE_SECTOR = (
    "通信", "电子", "计算机", "机械设备", "化工", "基础化工", "有色金属", "医药生物",
    "医药", "汽车", "电力设备", "食品饮料", "银行", "非银金融", "房地产", "建筑装饰",
    "公用事业", "交通运输", "商贸零售", "纺织服饰", "纺织", "轻工制造", "家用电器",
    "农林牧渔", "钢铁", "煤炭", "石油石化", "环保", "国防军工", "社会服务", "传媒",
    "建筑材料", "综合", "养老金", "个人养老金", "社保", "保险", "券商",
    "塑料", "橡胶", "包装材料", "造纸", "印刷", "木业家具", "工艺商品", "文教休闲",
    "珠宝首饰", "钟表珠宝", "饰品", "商业百货", "贸易", "物流", "港口", "航空机场",
    "银行", "多元金融", "工程建设", "装修装饰", "水泥建材", "玻璃玻纤", "陶瓷",
    # 主题型但过宽：任何行业都能蹭。保利联合(民爆)靠"一带一路"吃到中汽协的
    # 汽车出口数据，成了它涨停的理由 —— 这种板块只能当背景，不能当归因依据。
    "一带一路", "国企改革", "央企改革", "混改", "证金持股", "参股银行", "参股券商",
    "创投", "独角兽", "举牌", "股权转让", "并购重组", "壳资源", "预盈预增", "高送转",
)


def is_wide_sector(nm):
    return nm in WIDE_SECTOR


def pick_boards(real, ind, n=4):
    """归因候选板块：按行业归属对齐、排除宽泛行业、名称互不包含。

    名称去重是必须的：西部材料同时挂着"小金属"和"其他小金属"，两个都占位，
    "商业航天"（真正驱动它涨停的板块）就被挤出名额了。
    """
    cands = [b for b in real if not is_wide_sector(b["name"])] or list(real)
    cands.sort(key=board_rank(ind))
    out, names = [], []
    for b in cands:
        nm = b["name"]
        if any(nm in x or x in nm for x in names):
            continue
        names.append(nm)
        out.append(b)
        if len(out) >= n:
            break
    return out


def board_rank(ind):
    """板块排序：行业归属 > 非宽泛 > 涨幅。

    保利联合是民爆股，却因为挂着"养老金"概念，被商务部一条养老金服务出口的
    新闻解释了涨幅。先按"所属行业"对齐，再把宽泛行业排到后面。
    """
    def r(b):
        nm = b["name"]
        same = 0 if (ind and (nm == ind or nm in ind or ind in nm)) else 1
        return (1 if is_wide_sector(nm) else 0, same, -b["chg"])
    return r


def board_kw(b):
    """板块 + 扩展同义词，用于匹配不含板块全名的重磅新闻。"""
    core = re.sub(r"(概念|板块|指数|行业)$", "", b["name"]).strip()
    kws = [core] + [k for k in KW_EXT.get(b["name"], ()) if len(k) >= 2]
    return [k for k in kws if len(k) >= 2]


def sector_news(name, today):
    if name in _SECTOR_NEWS:
        return _SECTOR_NEWS[name]
    out, seen = [], set()
    for kw in ([name] + list(KW_EXT.get(name, ()))[:3]):
        for a in search_news(kw, size=30, max_days=P["sectorDays"], today=today):
            if a["t"] in seen:
                continue
            seen.add(a["t"])
            out.append(a)
    # 排序键：先"是不是结果稿"（描述涨跌的排后），再政府级别高的排前。
    # 不做这一步，40 条上限会把真正的政策稿挤掉。
    out.sort(key=lambda a: (is_result_only(a["t"]),
                            -gov_level(a["t"], a.get("body") or "", a.get("media") or "")[1]))
    out = out[:40]
    _SECTOR_NEWS[name] = out
    return out


# ---------------- 2. 新闻 ----------------
def search_news(kw, size=60, max_days=10, today=None):
    """东财新闻搜索（限标题）。max_days 是硬闸门，过期一律丢。"""
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
        return []
    try:
        d = json.loads(m.group(1))
    except Exception:
        return []
    arts = ((d.get("result") or {}).get("cmsArticleWebOld") or [])
    core = re.sub(r"(概念|板块|指数|行业)$", "", kw).strip()
    today = today or datetime.now(CN).replace(tzinfo=None)
    out = []
    for a in arts:
        title = re.sub(r"<[^>]+>", "", a.get("title") or "")
        body = re.sub(r"<[^>]+>", "", a.get("content") or "")
        if core not in title and core not in body:
            continue
        if any(w in title for w in NOISE_WORDS):
            continue
        day = (a.get("date") or "")[:16]
        if date_gap(day, today) > max_days:      # 时效硬闸门
            continue
        out.append({"t": title, "d": day, "u": a.get("url") or "",
                    "media": (a.get("mediaName") or "")[:12], "body": body[:300]})
    return out


def xwlb_days(days=P["xwlbDays"]):
    """近 N 日新闻联播条目，累积返回。"""
    now = datetime.now(CN).replace(tzinfo=None)
    out, seen = [], set()
    for k in range(days):
        d = now - timedelta(days=k)
        h = hget("https://tv.cctv.com/lm/xwlb/day/%s.shtml" % d.strftime("%Y%m%d"), retry=1)
        if not h or len(h) < 3000:
            continue
        for t in re.findall(r'alt="\[视频\]([^"]+)"', h):
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                out.append({"d": d.strftime("%Y-%m-%d"), "t": t})
    return out


def policy_pool(days=P["policyDays"]):
    """当日政策池：标题里同时有政府机构 + 政策动词。"""
    today = datetime.now(CN).replace(tzinfo=None)
    pool, seen = [], set()
    for kw in ("行动方案", "发展规划", "实施意见", "指导意见", "若干措施", "产业基金"):
        for a in search_news(kw, size=30, max_days=days, today=today):
            if a["t"] in seen:
                continue
            lvl, sc = gov_level(a["t"])
            if sc < 60 or not any(v in a["t"] for v in POLICY_VERB):
                continue
            seen.add(a["t"])
            pool.append({"t": a["t"], "d": a["d"], "u": a["u"], "lvl": lvl, "sc": sc})
    pool.sort(key=lambda x: (-x["sc"], x["d"]))
    return pool[:20]


def catalyst_pool(ds):
    """读当日 catalytic 结果里韭研热点清单点名的个股 —— 最精准的个股级关联。"""
    p = "%s/build/catalyst/%s.json" % (BASE, ds)
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return []
    out = []
    for doc in ((d.get("jiuyan") or {}).get("docs") or []):
        for h in (doc.get("hot") or []):
            if not (h.get("stocks") or []):
                continue
            out.append({"name": h.get("name") or "", "desc": h.get("desc") or "",
                        "stocks": h.get("stocks") or [], "title": doc.get("title") or "",
                        "d": (doc.get("d") or d.get("date") or "")[:10]})
    return out


# ---------------- 3. 归因 ----------------
def gov_level(title, body="", media=""):
    """返回 (级别名, 分数)。三级判定，从严到宽：

    1. 机构名 + 政策动词 → 真政策文件（100/80/60/40）
    2. 机构名但无政策动作 → 降档（避免"上海市某公司发布新品"算上海市政策）
    3. 无机构名 → 按 海外映射 / 国家重大工程 / 央媒 兜底
       （航天事件常常没有部委名，但"载人航天""运载火箭"本身就是国家意志）
    """
    t = title
    for name, sc, words in GOV_LEVELS:
        if any(w in t for w in words):
            if any(v in t for v in POLICY_VERB):
                return name, sc
            return name, max(20, sc - 30)
    src = media if media in MEDIA_GOV else ""
    if not src:
        for m in MEDIA_GOV:
            if m in (body or "")[:150]:
                src = m
                break
    if any(w in t for w in OVERSEA):
        return "海外映射", 45
    if any(w in t for w in PROJ_STRONG):
        return "国家级工程", 75
    if any(w in t for w in CN_WORDS) and any(w in t for w in PROJ_WEAK):
        return "国家级工程", 70
    if src and any(w in t for w in MEDIA_SIGNAL):
        return "国家级媒体", 65
    return "", 0


def event_cat(title, body=""):
    """只看标题。正文里随便一句"规划/方案"就会把 ETF 软文判成政策规划，
    这是第一版实测的污染源（"人形机器人表现活跃 融资客布局名单"被判政策规划 6 天）。"""
    for name, base, words in EVENT_CATS:
        if any(w in title for w in words):
            return name, base
    return "其他", 1


# 程序性公告：变更保荐代表人、开立募集资金专户、董事会决议、权益分派……
# 这些标题里有"公告""决议"，会被当成"资本运作"当成上涨理由，实际全是噪音。
# 首日异动的票消息面本来就稀薄，不挡掉这些，主因栏就只剩一堆套话。
NOISE = (
    "变更保荐代表人", "保荐代表人", "保荐机构", "持续督导",
    "开立募集资金专户", "募集资金三方监管", "闲置募集资金", "募集资金存放",
    "董事会决议", "监事会决议", "股东大会决议", "临时股东大会", "会议决议",
    "权益分派", "股权登记日", "除权除息", "利润分配实施",
    "机构风向标", "持仓机构", "重仓基金", "基金持仓",
    "龙虎榜", "融资余额", "融资融券余额", "大宗交易", "折溢价",
    "投资者关系活动", "调研活动", "网上说明会",
    "公司章程", "章程修正", "内部审计", "审计机构",
    "第三季度报告", "半年度报告", "年度报告", "业绩说明会", "季度报告",
    "股票交易异常波动公告", "澄清公告",
)


def is_noise(title):
    return any(w in title for w in NOISE)


# 利空：净利润下降 73% 不能被写成"为什么涨"。原来的规则只要命中"业绩"就给 4 分权重，
# 金杯汽车的主因栏直接变成了"第三季度归母净利润同比下降 73.5%"。
NEG = (
    "下降", "下滑", "减少", "亏损", "预亏", "首亏", "续亏", "由盈转亏", "负增长",
    "减持", "清仓", "质押", "冻结", "诉讼", "仲裁", "处罚", "罚款", "警示函",
    "立案", "问询", "监管函", "终止", "失败", "违约", "退市", "风险提示",
    "商誉减值", "计提", "坏账", "下调", "降价", "价格下调", "产能过剩",
)


def is_bad_news(title):
    return any(w in title for w in NEG)


def is_media(title):
    return any(w in title for w in MEDIA_WORDS)


def date_gap(day_str, today):
    try:
        d = datetime.strptime(day_str[:10], "%Y-%m-%d")
    except Exception:
        return 99
    return (today - d).days


def freshness(days):
    if days <= 0:
        return 1.0
    if days == 1:
        return 0.85
    if days <= 3:
        return 0.6
    return 0.35


def has_number(text):
    return bool(re.search(r"\d+(\.\d+)?\s*(亿|万|颗|项|%|元|倍|台|吨|Tbps|GW|MW)", text))


def next_node(title, today):
    m = NEXT_PAT.search(title)
    if not m:
        return ""
    s = m.group(1)
    dm = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})", s)
    if dm:
        try:
            dd = datetime(today.year, int(dm.group(1)), int(dm.group(2)))
            if dd.date() <= today.date():        # 已经过去的日子不算"后续"
                return ""
        except Exception:
            return ""
    return s


def is_result_only(title):
    """标题只是在描述"它涨了"，没有解释涨的原因。"""
    if not any(w in title for w in RESULT_WORDS):
        return False
    substantive = ("印发", "发布", "出台", "中标", "签约", "订单", "定增", "收购",
                   "投产", "量产", "突破", "发射", "入轨", "获批", "规划", "方案")
    return not any(w in title for w in substantive)


def make_ev(t, d, u, body, today, src, relate, tag="", media=""):
    lvl, gsc = gov_level(t, body, media)
    cat, base = event_cat(t, body)
    # ETF/媒体稿：不解释产业原因，直接压到 1 天，且不参与"政府背书"
    if is_media(t) and not any(v in t for v in POLICY_VERB):
        cat, base, lvl, gsc = "研报软文", 1, "", 0
    if gsc >= 90:
        base = max(base, 5)     # 真政策文件，关键词再偏也至少给 5 天
    gap = date_gap(d, today)
    days = base
    if lvl in ("国家级", "国家级媒体", "国家级工程"):
        days *= 1.6
    elif lvl == "海外映射":
        days *= 1.3
    elif lvl == "部委级":
        days *= 1.4
    elif lvl == "省级":
        days *= 1.2
    if has_number(t + " " + body):
        days += 1
    nxt = next_node(t, today)
    if nxt:
        days += 2
    days = max(1.0, days * freshness(gap))
    return {"src": src, "relate": relate, "tag": tag, "d": d, "t": t, "u": u,
            "lvl": lvl, "gsc": gsc, "cat": cat, "gap": gap,
            "days": round(days, 1), "next": nxt, "result": is_result_only(t),
            "neg": is_bad_news(t)}


def pick_reason(evs):
    """挑一条最能解释"为什么涨"的：政府级别 + 事件实质 + 个股相关 − 结果类 − 陈旧。

    板块级证据的类别权重打 6 折 —— 这个模块抓的本来就是"个股背离板块"的异动，
    板块整体没涨就说明市场没把它当板块行情，此时"板块级政策"对个股的解释力天然更弱。
    不折的话，长江通信的净利 +6381% 会被一条跟它无关的航天政策压掉。
    """
    if not evs:
        return None
    cat_w = {"政策规划": 5, "订单业绩": 4, "技术突破": 3.5, "资本运作": 2,
             "研报软文": 0.5, "行情情绪": 0.2, "其他": 0.5}
    def key(e):
        w = cat_w.get(e["cat"], 0)
        if e["relate"] != "个股":
            w *= 0.6
        v = w * 2
        if e["gsc"]:
            v += e["gsc"] / 22.0
        if e["relate"] == "个股":
            v += 3.5
        if e.get("tag") == "点名":
            v += 4.5
        if e["result"]:
            v -= 6
        if e.get("neg"):
            v -= 10      # 利空不能当"为什么涨"：金杯汽车主因一度是"净利同比下降 73.5%"
        v -= e["gap"] * 0.4
        return v
    return sorted(evs, key=key, reverse=True)[0]


def verdict(days, gov_lvl, gov_sc, nexts):
    lvl = "强" if days >= 7 else "中" if days >= 4 else "弱" if days >= 2 else "脉冲"
    tag = lvl
    if gov_lvl == "海外映射":
        tag += " · 海外映射"          # 不是政府背书，是"事件源头在海外"，单独标
    elif gov_sc >= 90:
        tag += " · %s政策" % gov_lvl
    elif gov_sc >= 60:
        # 有机构名但标题里没有政策动词，只算"被提及"，不敢写成背书
        tag += " · %s提及" % gov_lvl
    if nexts:
        tag += " · 有后续"
    return lvl, tag


# ---------------- 4. 单只分析 ----------------
def screen_one(x, today):
    """量化筛选：异动 + 低位 + 背离。不做消息归因（归因放第二轮，好复用板块新闻缓存）。"""
    code, market, name = x["f12"], int(x.get("f13") or 0), x.get("f14") or ""
    # 与 screen.py 同一口径：只留主板。创业板/科创板是 20cm，混进来涨停与背离都不可比
    if not code.startswith(MAIN_PREFIX):
        return None
    if "ST" in name or "退" in name or name[:1] in ("N", "C"):
        return None
    rows = stock_kline(code, market)
    if len(rows) < P["minBars"]:
        return None
    m = low_metrics(rows)
    if not m:
        return None
    chg = float(x.get("f3") or 0)
    mcap = x.get("f20") or 0
    if mcap < P["minMcap"]:
        return None

    if not ((m["dd"] <= -P["lowDd"]) or (m["pos"] <= P["lowPos"])):
        return None

    # 首日门：今天是这波异动的第一天。前 N 日只要有一天涨到 3%（即我们自己的异动阈值），
    # 说明主升已经开始了，今天只是延续，属于鱼身不是低位异动；连续小阳线堆出的趋势也排除。
    if m["prevN"] and (m["prevMax"] >= P["firstMaxDay"] or m["prevCum"] > P["firstMaxCum"]):
        return None

    limit_up = chg >= 9.8
    if not (limit_up or m["volRatio"] >= P["volMin"]):
        return None

    bds = stock_boards(code, market)
    real = industry_boards([b for b in bds if not is_stat_board(b["name"])])
    if not real:
        return None
    best = max(real, key=lambda b: b["chg"])
    excess = chg - best["chg"]
    if excess < P["devMin"]:
        return None

    return {
        "code": code, "name": name, "mkt": "沪" if market == 1 else "深",
        "close": x.get("f2"), "chg": round(chg, 2),
        "amt": x.get("f6") or 0, "turn": x.get("f8") or 0,
        "volRatio": m["volRatio"], "mcap": mcap, "fmcap": x.get("f21") or 0,
        "pe": x.get("f115") or 0,
        "netIn": x.get("f62") or 0, "ind": x.get("f100") or "",
        "dd": m["dd"], "pos": m["pos"], "c20": m["c20"], "c60": m["c60"],
        "prevMax": m["prevMax"], "prevCum": m["prevCum"], "prevSeq": m["prevSeq"],
        "amtRatio": m["amtRatio"], "ups20": m["ups20"],
        "bars": m["bars"], "limitUp": limit_up,
        "board": {"name": best["name"], "chg": round(best["chg"], 2)},
        "excess": round(excess, 1),
        "sig": (["首日"] + (["涨停"] if limit_up else []) +
                (["放量%.1f倍" % m["volRatio"]] if m["volRatio"] >= P["volMin"] else []) +
                ["背离 %+.1f" % excess]),
        "_real": real,
    }


def attribute(it, today, cat_pool):
    """消息归因 + 持续性评估。五路证据按可信度排序，个股类优先展示。"""
    real = it.pop("_real")
    name, limit_up, excess = it["name"], it["limitUp"], it["excess"]
    evs = []

    # 1) 韭研舆情盘点显式点名（带个股名单，最精准）
    for h in cat_pool:
        if name in h["stocks"]:
            evs.append(make_ev("%s：%s" % (h["name"] or "热点", h["desc"]),
                               h["d"], "", "", today, "韭研", "个股", "点名"))

    # 2) 个股新闻（最直接）。程序性公告先剔掉：首日异动的票消息面稀薄，
    #    不挡的话主因会变成"变更保荐代表人""闲置募集资金补充流动资金"这类套话。
    own_news = [a for a in search_news(name, max_days=P["newsDays"], today=today)
                if not is_noise(a["t"])]
    for a in own_news:
        evs.append(make_ev(a["t"], a["d"], a["u"], a["body"], today, "个股", "个股",
                           media=a.get("media") or ""))

    # 3) 概念板块优先于行业分类，只保留真抓到新闻的板块（纳米银这类搜不到东西就别占名额）
    picks = [b for b in pick_boards(real, it.get("ind") or "") if sector_news(b["name"], today)]
    for b in picks[:3]:
        for a in sector_news(b["name"], today)[:20]:
            evs.append(make_ev(a["t"], a["d"], a["u"], a["body"], today, "板块",
                               b["name"], b["name"], a.get("media") or ""))

    # 4) 政策池 —— 只允许通过「个股当日最强板块」进来。
    #    东财一只票常挂十几个概念（苏州科达、飞亚达、长江通信都挂着"商业航天"），
    #    按全部板块匹配会让同一份《快舟规划》同时变成三只不相干的票的上涨理由。
    driver = [it["board"]]
    for p in POLICY:
        for b in driver:
            if any(k in p["t"] for k in board_kw(b)):
                evs.append(make_ev(p["t"], p["d"], p["u"], "", today, "政策",
                                   b["name"], b["name"]))
                break

    # 5) 新闻联播
    for xw in XWLB:
        for b in driver:
            if any(k in xw["t"] for k in board_kw(b)):
                evs.append(make_ev(xw["t"], xw["d"], "", "", today, "新闻联播",
                                   b["name"], b["name"]))
                break

    # 去重，个股/点名类占前三位，板块类补三位
    uniq, seen = [], set()
    for e in sorted(evs, key=lambda e: (-(e.get("tag") == "点名"), e["result"],
                                        -e["gsc"], -e["days"])):
        k = e["t"][:30]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    own = [e for e in uniq if e["relate"] == "个股"][:3]
    rest = [e for e in uniq if e["relate"] != "个股"][:3]
    evs = own + rest

    nexts = sorted({e["next"] for e in evs if e["next"]})
    reason = pick_reason(evs)
    named = any(e.get("tag") == "点名" for e in evs)

    # 判级以"主因"为准，不用全池最大值 —— 否则一条不相关的国家级新闻
    # 就能把整只票的持续性拉高（长江通信被工信部一条行业标准抬成"国家级政策"）。
    base_days = reason["days"] if reason else 1.0
    multi = len({e["src"] for e in evs if e["days"] >= base_days * 0.8})
    top_days = round(base_days * (1.2 if multi >= 2 else 1.0), 1)

    # 政府级别必须与主因同源：力星股份涨的是"人形机器人"，不能拿商业航天的
    # 海外映射当它的背书。只从「个股级 + 主因所在板块」里取。
    rel = reason["relate"] if reason else ""
    gov_pool = [e for e in evs if e["relate"] == "个股" or e["relate"] == rel] or evs
    gov_ev = max(gov_pool, key=lambda e: e["gsc"]) if gov_pool else None
    gov_sc = gov_ev["gsc"] if gov_ev else 0
    gov_lvl = gov_ev["lvl"] if gov_ev else ""
    lvl, tag = verdict(top_days, gov_lvl, gov_sc, nexts)

    # ---- 风险 ----
    risks = []
    txt = " ".join(e["t"] for e in evs)
    if re.search(r"(净利|利润).{0,8}(下降|减少|下滑|亏损|预亏)", txt):
        risks.append("业绩下滑")
    if (it.get("pe") or 0) > 100:
        risks.append("PE>100")
    if limit_up and (it.get("turn") or 0) < 3:
        risks.append("缩量封板")
    if not evs:
        risks.append("查无消息" if not own_news else "仅程序性公告")
    elif all(e["result"] or e["cat"] in ("行情情绪", "研报软文") for e in evs):
        risks.append("只有行情稿")
    own_real = [e for e in evs if e["relate"] == "个股" and e["cat"] != "行情情绪"]
    if evs and not own_real:
        risks.append("个股无独立催化")
    if any(is_bad_news(e["t"]) for e in evs):
        risks.append("消息面偏空")

    # 主因是板块级证据 → 持续性封顶 3 天。
    # 一只票往往挂十几个概念板块，不设这道闸，任何一只"沾边"的票都能拿到
    # 当天最重磅新闻的 8 天窗口（纺织股蹭算力、微透气膜蹭商业航天都是这么来的）。
    if reason and reason["relate"] != "个股":
        top_days = min(top_days, 3.0)
        lvl, tag = verdict(top_days, gov_lvl, gov_sc, nexts)

    # ---- 资金抢跑识别 ----
    # 抢跑的定义是"股价先动、但资金是悄悄买"：当日成交额没有放大（甚至缩量），
    # 主力却净流入，而且找不出能解释的消息。放量 1.6 倍以上的涨停是高调进场，
    # 通常意味着消息已经扩散，那属于题材雷达管的范围。
    # 西部材料 10-28 正是这个形态：涨 7.90%、只成交 8.0 亿、量比 0.82，当天零消息。
    ar = it.get("amtRatio") or 0
    net = it.get("netIn") or 0
    amt = it.get("amt") or 0
    # "消息空白"只看个股级的正向证据：板块新闻不算这只票的催化，利空也不算上涨理由。
    # 泰尔股份有一条"智平方具身智能试点"（机器人概念板块级），但它自己零消息，
    # 缩量涨停 + 主力净流入 0.97 亿 —— 这正是抢跑。
    # 安泰集团唯一的个股消息是"前三季度净亏损 1.20 亿"，利空挡不住缩量买入，更是抢跑。
    blank = not [e for e in evs if e["relate"] == "个股" and not e["result"]
                 and not e.get("neg") and e["cat"] not in ("行情情绪", "研报软文")]
    thin = amt < 0.8e8          # 成交额不足 8000 万：少量资金就能拉板，不代表有信息
    if ar and ar <= 0.85:
        if thin:
            sneak = "缩量封板"
        elif net > 0 and blank:
            sneak = "资金抢跑"
        else:
            sneak = "缩量上涨" if net > 0 else "缩量封板"
    elif ar <= 1.25 and blank and net > 0 and not thin:
        sneak = "疑似抢跑"
    elif ar >= 1.6 and not blank:
        sneak = "放量进场"
    elif ar >= 1.6 and blank:
        sneak = "放量无消息"
    else:
        sneak = ""
    it["sneak"] = sneak
    it["sneakTxt"] = "当日额 %.2f×前20均 · 主力%s%.2f亿" % (
        ar, "+" if net >= 0 else "", net / 1e8)

    it["named"] = named
    it["why"] = {
        "lvl": lvl, "tag": tag, "days": round(top_days, 1),
        "gov": gov_sc, "govLvl": gov_lvl,
        "reason": reason["t"] if reason else "",
        "cat": reason["cat"] if reason else "",
        "ev": evs, "next": nexts,
    }
    it["risk"] = risks
    it["score"] = round(min(100, top_days * 7 + gov_sc * 0.25 + excess * 0.9
                            + (8 if nexts else 0) + (6 if named else 0)), 1)
    return it



def main():
    t0 = time.time()
    now = datetime.now(CN)
    today = now.replace(tzinfo=None)
    ds = sys.argv[1] if len(sys.argv) > 1 else now.strftime("%Y-%m-%d")
    print("低位异动 · %s %s" % (ds, now.strftime("%H:%M")))

    global XWLB, POLICY
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(xwlb_days)
        f2 = ex.submit(policy_pool)
        XWLB, POLICY = f1.result(), f2.result()
    CAT = catalyst_pool(ds)
    print("   联播 %d 条 · 政策池 %d 条 · 韭研点名热点 %d 组"
          % (len(XWLB), len(POLICY), len(CAT)))

    quotes = quote_top()
    qmain = [x for x in quotes if str(x.get("f12") or "").startswith(MAIN_PREFIX)]
    print("   涨幅榜 ≥%.1f%% 共 %d 只（主板 %d 只）" % (P["minChg"], len(quotes), len(qmain)))

    # 第一轮：纯量化筛选（异动 + 低位 + 首日 + 背离）
    base = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(lambda x: screen_one(x, today), qmain):
            if r:
                base.append(r)
    print("   通过 异动+低位+首日+背离 四重门：%d 只" % len(base))

    # 预抓板块新闻：同一板块只抓一次，避免 66 只股票重复请求
    bn = {}
    for it in base:
        for b in pick_boards(it["_real"], it.get("ind") or ""):
            bn[b["name"]] = 1
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(lambda n: sector_news(n, today), list(bn.keys())))
    print("   预抓 %d 个板块的资讯（含关键词扩展）" % len(bn))

    # 第二轮：消息归因 + 持续性
    items = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(lambda it: attribute(dict(it), today, CAT), base):
            if r:
                items.append(r)
    print("   完成归因：%d 只" % len(items))

    items.sort(key=lambda x: -x["score"])
    items = items[:P["maxOut"]]

    # 补抓 K 线：与题材雷达共用同一套绘图函数，点开卡片就能看图
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from theme_scan import stock_charts
        with ThreadPoolExecutor(max_workers=6) as ex:
            chs = list(ex.map(lambda it: stock_charts(it["code"], bars=90), items))
        for it, c in zip(items, chs):
            if c:
                it["daily"] = c["daily"]
                it["trend"] = c["trend"]
                it["preClose"] = c["preClose"]
        print("   补抓 K 线 %d/%d" % (len([x for x in items if x.get("daily")]), len(items)))
    except Exception as e:
        print("   K 线补抓失败（不影响主数据）：%s" % e)
    for it in items:
        print("   %-8s %-6s %+6.2f%% 超额%+5.1f 距高%6.1f%% 分位%5.1f%% | %s %.1f天%s"
              % (it["name"], it["code"], it["chg"], it["excess"], it["dd"], it["pos"],
                 it["why"]["tag"], it["why"]["days"], " [点名]" if it["named"] else ""))

    out = {"date": ds, "generated": now.strftime("%Y-%m-%d %H:%M"), "params": P,
           "counts": {"scanned": len(qmain), "hits": len(items),
                      "xwlb": len(XWLB), "policy": len(POLICY)},
           "policy": POLICY[:10], "items": items}
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    path = "%s/%s.json" % (OUT_DIR, ds)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    for fn in sorted(glob.glob(OUT_DIR + "/*.json"))[:-60]:
        os.remove(fn)
    print("   输出 %s（%.1f KB，用时 %.1f 秒）"
          % (path, os.path.getsize(path) / 1024.0, time.time() - t0))


XWLB, POLICY = [], []

if __name__ == "__main__":
    main()
