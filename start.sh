#!/usr/bin/env bash
# 一键启动（Linux / macOS）
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
fi
./.venv/bin/pip install -q -r requirements.txt
exec ./.venv/bin/python -m sshdesk "$@"
