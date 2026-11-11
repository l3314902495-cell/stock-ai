#!/usr/bin/env bash
# 每周优化会话开始时运行：切到长期研究分支 optimize/research，并合并 main 的最新改动。
# 用法：bash scripts/start_weekly.sh
set -euo pipefail
BR=optimize/research
git fetch origin main
if git ls-remote --exit-code --heads origin "$BR" >/dev/null 2>&1; then
  git fetch origin "$BR"
  git checkout -B "$BR" "origin/$BR"
  git merge --no-edit origin/main || { echo "合并 main 冲突，请手动解决后再继续"; exit 1; }
else
  git checkout -B "$BR" origin/main
fi
git config user.name  >/dev/null 2>&1 || git config user.name  "stock-ai"
git config user.email >/dev/null 2>&1 || git config user.email "noreply@anthropic.com"
echo "当前分支：$(git rev-parse --abbrev-ref HEAD)  最新提交：$(git log --oneline -1)"
stockai bt board || true
