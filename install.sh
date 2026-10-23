#!/usr/bin/env bash
# 将本仓库安装（软链接）为本地技能，支持 Claude Code 与 CodeBuddy 两套技能目录。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="campus-mail-task-notify"

link_skill() {
    local root="$1"
    local target="${root}/${NAME}"
    if [ ! -d "$root" ]; then
        return 0
    fi
    if [ -e "$target" ] && [ "$(readlink -f "$target")" = "$SRC" ]; then
        echo "已安装：${target}"
        return 0
    fi
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        echo "跳过：${target} 是实体目录，请先手动移除或备份"
        return 0
    fi
    ln -sfn "$SRC" "$target"
    echo "已安装：${target} -> ${SRC}"
}

link_skill "${HOME}/.claude/skills"
link_skill "${HOME}/.codebuddy/skills"

echo
echo "下一步："
echo "  python3 ${SRC}/scripts/task_notifier.py init"
echo "  编辑 ~/.config/campus_mail/credentials.txt 与 config.json 后执行 test-mail 验证"
