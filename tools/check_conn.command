#!/bin/bash
# 双击运行：只检查能不能连上服务器
cd "$(dirname "$0")" || exit 1
for candidate in python3 /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "✗ 没找到 Python 3。"
  read -r -p "按回车关闭窗口…" _
  exit 1
fi
"$PY" fish_relay_client.py --check
echo
read -r -p "按回车关闭窗口…" _
