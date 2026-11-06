# 长上影阳线选股工作台

每个交易日收盘后自动扫描主板全市场，筛出「收阳 + 上影线 ≥ 实体 2 倍 + 无下影 + 前 10 日无涨停」的个股，
并跟踪每一批标的 T+1 ~ T+10 的收益。整个流程跑在 GitHub 上，不需要开电脑。

## 自动更新

- 每个工作日 **15:40（北京时间）** 由 GitHub Actions 自动运行
- 也可以在仓库的 **Actions → 每日自动更新 → Run workflow** 手动补跑
- 非交易日运行没有副作用：结果仍是最近一个交易日，批次不会重复
- 抓取失败会以红色 ✗ 结束并邮件通知，页面保持上一版，不会被坏数据覆盖

## 筛选条件

| # | 条件 |
|---|---|
| 1 | 当日收阳线（收盘 > 开盘） |
| 2 | 上影线 ≥ 实体 2 倍 |
| 3 | 无下影线 |
| 4 | 剔除科创板、创业板、北交所、ST |
| 5 | 前 10 个交易日无涨停收盘 |
| 6 | 附加观察：分时某两分钟成交额 ≥ 市值 0.5% |

## 访问口令

口令以 SHA-256 哈希校验，页面里不存明文。修改方法：

```bash
# 1. 生成新口令的哈希
python3 -c "import hashlib;print(hashlib.sha256('你的新口令'.encode()).hexdigest())"

# 2. 替换 build/template.html 里的 GATE_HASH 常量，然后推送
```

> 说明：这是前端的遮挡式口令，不是加密。页面 HTML 内含完整明细，懂技术的人查看源码即可绕过。
> 它的作用是挡住随手打开链接的人，以及不让搜索引擎收录（已加 `noindex`）。

## 目录结构

```
build/screen.py       抓东财行情 → 筛选 → 写 snap/full
build/track.py        收益跟踪，生成 track.json
build/build.py        注入模板生成 index.html
build/backfill.py     按日期回补历史图表数据
build/full/<日期>.json   完整明细（含日K与分时，保留最近 60 个交易日）
build/snap/<日期>.json   轻量名单（长期保留，收益跟踪用）
build/track.json         收益台账
.github/workflows/daily.yml   定时任务
```

## 本地手动补跑

```bash
python3 build/screen.py 2026-11-05     # 指定日期重抓
python3 build/backfill.py 2026-11-05   # 仅补图表数据（分时接口只支持最近 5 个交易日）
python3 build/build.py                 # 重新生成页面
```
