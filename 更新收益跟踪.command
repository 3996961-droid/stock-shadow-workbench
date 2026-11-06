#!/bin/bash
# 每日一键更新：每个交易日收盘后（15:05 之后）双击本文件即可
# 作用：全市场重新筛选 -> 更新各批次 T+1~T+10 收益 -> 导出 CSV -> 重新生成工作台 -> 自动打开
cd "$(dirname "$0")" || exit 1

echo "======== 每个交易日更新 ========"
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3，请先在终端执行 xcode-select --install"
  read -r -p "按回车退出"
  exit 1
fi

python3 build/daily.py || { echo; echo "更新未完成，请检查网络后重试"; read -r -p "按回车退出"; exit 1; }

echo
echo "完成，正在打开工作台 ..."
open "长上影选股工作台.html"
sleep 1
