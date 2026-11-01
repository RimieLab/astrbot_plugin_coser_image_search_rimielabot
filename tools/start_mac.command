#!/bin/bash
# 双击运行：启动鱼鱼找鸡中继（Mac）
cd "$(dirname "$0")" || exit 1
echo "=============================="
echo "  鱼鱼找鸡 · 本机中继"
echo "=============================="
PY=""
for candidate in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "✗ 没找到 Python 3。装一个再试：在终端里运行 \"xcode-select --install\"，或到 python.org 下载。"
  echo
  read -r -p "按回车关闭窗口…" _
  exit 1
fi
echo "使用 Python：$($PY --version 2>&1)"
echo
"$PY" fish_relay_client.py
echo
echo "中继已停止。"
read -r -p "按回车关闭窗口…" _
