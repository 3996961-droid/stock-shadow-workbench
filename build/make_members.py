# -*- coding: utf-8 -*-
"""用 slist（个股→所属板块）反查，重建"板块→成分股"映射。

东财列表接口被封后，b:BKxxxx（板块成分）这条路断了；但
/api/qt/slist/get?spt=3&secid=市场.代码 仍能取到单只股票所属的全部板块。
把主板三千多只票各问一次，反向聚合，就得到完整的板块成分表。

产出：
    build/members.json  {boards: {BKxxxx: [股票代码...]}, names: {BKxxxx: 板块名}}
    build/boards.json   {boards: [{code, name}...]}   ← 板块代码全集

日常不必每天跑：成分股变动以周计。缺文件时题材模块自动降级，不报错。
用法：python3 build/make_members.py
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import emq

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")

# slist 会把行业、地域、指数成分股一起返回，而题材雷达只要概念板块（原 m:90+t:3）。
# 行业板块没有接口可查，用两条线索剔：个股 f100（东财二级行业名）+ 申万一级行业名。
SW1 = set("""煤炭 石油石化 基础化工 钢铁 有色金属 建筑材料 建筑装饰 电力设备 机械设备
国防军工 汽车 计算机 传媒 通信 银行 非银金融 商贸零售 社会服务 家用电器 食品饮料
纺织服饰 轻工制造 医药生物 农林牧渔 公用事业 交通运输 房地产 电子 综合 环保 美容护理""".split())


def is_industry(name, ind_names):
    """行业板块判定：二级（与个股 f100 同名）、三级（带 Ⅱ/Ⅲ 角标）、一级（申万名单）。"""
    if name in ind_names or name in SW1:
        return True
    return name.endswith(("Ⅱ", "Ⅲ"))


def is_region(name):
    """地域板块：东财统一叫「XX板块」。"""
    return name.endswith("板块")


def boards_of(secid):
    u = ("https://push2.eastmoney.com/api/qt/slist/get?spt=3&fltt=2&invt=2"
         "&fields=f12,f13,f14,f3&secid=%s&pn=1&pz=50&po=1&np=1" % secid)
    d = emq._j(u, retry=2, referer="https://quote.eastmoney.com/")
    return (d.get("data") or {}).get("diff") or []


def main():
    rows = emq.market()
    codes = [r["f12"] for r in rows
             if r.get("f12", "").startswith(MAIN_PREFIX) and (r.get("f2") or 0) > 0]
    print("主板 %d 只，反查所属板块 ..." % len(codes))

    def one(code):
        sid = ("1." if code.startswith("6") else "0.") + code
        try:
            return code, boards_of(sid)
        except Exception:
            return code, []

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=16) as ex:
        got = list(ex.map(one, codes))
    print("   反查完成 %.1fs" % (time.time() - t0))

    mem, names = {}, {}
    for code, bs in got:
        for b in bs:
            bc = b.get("f12") or ""
            if not bc.startswith("BK"):
                continue
            mem.setdefault(bc, []).append(code)
            if b.get("f14"):
                names[bc] = b["f14"]

    ok = {k: v for k, v in mem.items() if len(v) >= 3}      # 只有 1-2 只票的碎板块丢掉
    print("   板块 %d 个 · 成分关系 %d 条"
          % (len(ok), sum(len(v) for v in ok.values())))

    # 剔掉行业/地域，只给题材雷达留概念板块
    ind_names = {r.get("f100") for r in emq.enrich(list(rows), "f100")
                 if r.get("f100")}
    print("   行业板块名 %d 个" % len(ind_names))
    concept = {k: v for k, v in ok.items()
               if not is_industry(names.get(k, ""), ind_names) and not is_region(names.get(k, ""))}
    print("   剔除行业/地域后，概念板块 %d 个" % len(concept))
    ok = concept

    with open(BASE + "/build/members.json", "w", encoding="utf-8") as f:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M"),
                   "boards": ok, "names": names}, f, ensure_ascii=False)

    bl = [{"code": k, "name": names.get(k, k)} for k in ok]
    bl.sort(key=lambda x: x["code"])
    with open(BASE + "/build/boards.json", "w", encoding="utf-8") as f:
        json.dump({"updated": time.strftime("%Y-%m-%d %H:%M"),
                   "boards": bl}, f, ensure_ascii=False)
    print("   已写入 build/members.json · build/boards.json")


if __name__ == "__main__":
    main()
