#!/usr/bin/env bash
# 本地自检：语法、配置 schema、测试套件。不需要安装 AstrBot，也不需要网络。
#
#   bash tests/run_tests.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> 1/4 检查 Python 语法"
python3 -m py_compile main.py core/*.py
echo "    OK"

echo "==> 2/4 校验 _conf_schema.json"
python3 - <<'PY'
import json

with open("_conf_schema.json", encoding="utf-8") as f:
    data = json.load(f)
required = {
    "enabled",
    "engines",
    "timeout_sec",
    "max_accounts",
    "strict_consensus",
    "proxy",
    "cooldown_sec",
    "per_user_day",
    "global_day",
    "cache_days",
    "cache_max",
    "send_progress",
    "send_on_miss",
    "found_image",
    "public_upload",
    "upload_ttl",
    "max_image_mb",
    "artwork_min_similarity",
    "lens_render",
    "browser_exe",
    "browser_headless",
    "browser_no_sandbox",
    "browser_profile",
}
missing = required - set(data)
assert not missing, f"缺少配置项: {sorted(missing)}"
for key, spec in data.items():
    assert "type" in spec and "default" in spec, f"{key} 缺少 type/default"
print("    OK，字段:", ", ".join(data))
PY

echo "==> 3/4 校验 metadata.yaml 基本字段"
python3 - <<'PY'
from pathlib import Path

text = Path("metadata.yaml").read_text(encoding="utf-8")
for field in ("name:", "display_name:", "version:", "repo:", "astrbot_version:"):
    assert field in text, f"metadata.yaml 缺少 {field}"
print("    OK")
PY

echo "==> 4/4 运行测试套件"
python3 -m unittest discover -s tests

echo
echo "全部检查通过 ✅"
