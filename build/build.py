# -*- coding: utf-8 -*-
"""把每日明细注入模板，生成工作台页面。

数据来源：
    build/data.json          最新交易日的完整明细（含日K/分时）
    build/full/<日期>.json   各交易日完整明细（screen.py 每日落盘、backfill.py 回补）
    build/snap/<日期>.json   各交易日轻量快照（不含图表）
    build/track.json         收益跟踪批次

设计要点：
    图表数据很重（单日 641 只约 8MB），不可能把所有交易日都塞进一个页面。
    所以主页面内嵌「最近若干个交易日」的完整明细（按总量上限自动决定，至少含最新一日），
    在这些日期之间用顶部下拉切换即可直接看到分时与日K，无需跳页；
    更早的交易日只内嵌轻量名单，并单独生成它的完整页面。

输出：
    长上影选股工作台.html            主入口（内嵌最近 N 个交易日的完整图表）
    历史/长上影_YYYYMMDD.html        内嵌窗口之外的交易日，各自一页
"""
import json, io, os, glob, shutil

# 仓库根目录，云端与本地同一套逻辑
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FULL_DIR = BASE + "/build/full"
SNAP_DIR = BASE + "/build/snap"
THEME_DIR = BASE + "/build/theme"
HIST_OUT = BASE + "/history"
MAIN_OUT = BASE + "/index.html"          # 站点入口，Pages 与本地双击都用它

CAP = 16 * 1024 * 1024          # 主页内嵌图表数据总量上限（字节）
KEEP_FULL = 60                  # 完整图表明细保留最近 N 个交易日（控制仓库体积；snap 全量保留）

tpl = io.open(BASE + "/build/template.html", encoding="utf-8").read()

# 当期明细：本地有 data.json 就用它；云端仓库里不存这个中间文件，直接取完整明细的最新一日
snap_files = sorted(glob.glob(FULL_DIR + "/*.json"), reverse=True)
if os.path.exists(BASE + "/build/data.json"):
    data = json.load(io.open(BASE + "/build/data.json", encoding="utf-8"))
    latest = data[0].get("date") or ""
elif snap_files:
    latest = os.path.basename(snap_files[0])[:-5]
    data = json.load(io.open(snap_files[0], encoding="utf-8"))
else:
    raise SystemExit("!! build/full 为空，请先运行 build/screen.py")

track_path = BASE + "/build/track.json"
track = (json.load(io.open(track_path, encoding="utf-8"))
         if os.path.exists(track_path)
         else {"maxDays": 10, "updated": latest, "latest": latest, "cohorts": []})

# 完整明细：以 data.json 为最新日的准
full = {}
for fp in sorted(glob.glob(FULL_DIR + "/*.json")):
    day = os.path.basename(fp)[:-5]
    try:
        full[day] = json.load(io.open(fp, encoding="utf-8"))
    except Exception as e:
        print("!! 跳过", fp, e)
full[latest] = data

# 轻量快照：缺 snap 的用完整明细裁一份
snap = {}
for fp in sorted(glob.glob(SNAP_DIR + "/*.json")):
    day = os.path.basename(fp)[:-5]
    try:
        snap[day] = json.load(io.open(fp, encoding="utf-8"))
    except Exception:
        pass
HEAVY = {"daily", "trend", "limit", "secid", "preClose"}
for day, rows in full.items():
    if day not in snap:
        snap[day] = [{k: v for k, v in r.items() if k not in HEAVY} for r in rows]

# 体积守护：只保留最近 KEEP_FULL 个交易日的完整图表明细，更早的降级为轻量名单
full_days = sorted(full.keys(), reverse=True)
pruned = full_days[KEEP_FULL:]
for d in pruned:
    full.pop(d, None)

dates = sorted(set(full) | set(snap), reverse=True)

# 题材雷达：取最新一份扫描结果（题材走实时行情，日期可能与选股日不同步）
theme_days = sorted(os.path.basename(f)[:-5] for f in glob.glob(THEME_DIR + "/*.json"))
theme = {}
for day in theme_days:
    try:
        theme = json.load(io.open("%s/%s.json" % (THEME_DIR, day), encoding="utf-8"))
    except Exception as e:
        print("!! 跳过题材数据", day, e)
if theme:
    print("题材雷达：%s · 主推 %d 个 / 观察 %d 个"
          % (theme.get("date"), len([x for x in theme.get("themes", []) if x.get("tier") == "main"]),
             theme.get("counts", {}).get("watch", 0)))


def dumps(o):
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def byte_len(o):
    return len(dumps(o).encode("utf-8"))


# 决定哪些交易日把完整图表内嵌到主页（最新的必须有，其余按上限往前铺）
inline, budget = [], 0
for d in dates:
    if d not in full:
        continue
    size = byte_len(full[d])
    if d == latest or budget + size <= CAP:
        inline.append(d)
        budget += size
inline_set = set(inline)
outside = [d for d in dates if d not in inline_set and d in full]


def hist_name(day):
    return "长上影_%s.html" % day.replace("-", "")


def pages_for(day):
    """以 day 这一页为视角，其它页面各自的相对路径（内嵌窗口内的日期都指向主页）。"""
    in_hist = (day not in inline_set)
    out = {}
    for d in dates:
        if d in inline_set:
            out[d] = ("../" if in_hist else "") + os.path.basename(MAIN_OUT)
        elif d in full:          # 有完整明细才会生成独立页，避免指向不存在的文件
            out[d] = ("" if in_hist else "history/") + hist_name(d)
        else:
            out[d] = ""          # 已轮转出窗口：前端显示“该交易日没有图表数据”
    return out


def build_page(day, inline_days, out_path):
    rows = full.get(day)
    if not rows:
        return None
    others = {d: snap[d] for d in dates if d not in inline_set and d in snap}
    meta = {
        "date": day,
        "latest": latest,
        "dates": dates,
        "inline": inline_days,
        "pages": pages_for(day),
        "n": len(rows),
        "core": len([x for x in rows if x.get("tier") == "core"]),
        "hit": len([x for x in rows if x.get("amt2Pct", 0) >= 0.5 - 1e-9]),
        "cohorts": len(track.get("cohorts") or []),
        "main": day == latest,
    }
    allmap = {d: full[d] for d in inline_days if d in full and d != day}   # 当日就是 DATA，不重复塞
    out = (tpl.replace("__DATA_JSON__", dumps(rows))
              .replace("__ALL_JSON__", dumps(allmap))
              .replace("__HIST_JSON__", dumps(others))
              .replace("__THEME_JSON__", dumps(theme))
              .replace("__TRACK_JSON__", dumps(track))
              .replace("__META_JSON__", json.dumps(meta, ensure_ascii=False))
              .replace("__META_DATE__", day))
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    io.open(out_path, "w", encoding="utf-8").write(out)
    return out_path, len(out.encode("utf-8"))


total = 0
lines = []

# 主页面：内嵌窗口内的全部交易日
r = build_page(latest, inline, MAIN_OUT)
if r:
    path, size = r
    total += size
    lines.append("  ★ %-22s %6.2f MB  %d 只（核心 %d）· 内含 %d 个交易日图表"
                 % (os.path.relpath(path, BASE), size / 1048576, len(full[latest]),
                    len([x for x in full[latest] if x.get("tier") == "core"]), len(inline)))

# 窗口之外的交易日：各自一页（只内嵌自己那一天）
for d in outside:
    p = os.path.join(HIST_OUT, hist_name(d))
    r = build_page(d, [d], p)
    if not r:
        continue
    path, size = r
    total += size
    lines.append("    %-22s %6.2f MB  %d 只（核心 %d）· 窗口外，独立页"
                 % (os.path.relpath(path, BASE), size / 1048576, len(full[d]),
                    len([x for x in full[d] if x.get("tier") == "core"])))

# 清理不再生成的旧页面，免得留下过时模板
keep = {hist_name(d) for d in outside}
removed = 0
for fp in glob.glob(HIST_OUT + "/长上影_*.html"):
    if os.path.basename(fp) not in keep:
        os.remove(fp)
        removed += 1

print("交易日 %d 个：%s" % (len(dates), "、".join(dates)))
print("主页内嵌图表：%s（%.2f MB）" % ("、".join(inline), budget / 1048576))
print("\n".join(lines))
if removed:
    print("清理过期历史页 %d 个" % removed)

# 同步删除磁盘上已轮转出窗口的完整明细，避免仓库随交易日无限膨胀
gone = 0
for d in pruned:
    fp = "%s/%s.json" % (FULL_DIR, d)
    if os.path.exists(fp):
        os.remove(fp)
        gone += 1
if gone:
    print("轮转：超出 %d 个交易日窗口的完整明细，已清理 %d 个" % (KEEP_FULL, gone))

print("合计 %.2f MB · 主入口 %s" % (total / 1048576, os.path.basename(MAIN_OUT)))
