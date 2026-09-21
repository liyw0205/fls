#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${FLS_REPO_URL:-https://github.com/liyw0205/fls.git}"
REPO_BRANCH="${FLS_REPO_BRANCH:-main}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${FLS_INSTALL_DIR:-${HOME}/.local/share/fls}"

say() { printf '[FLS] %s\n' "$*"; }
die() { printf '[FLS][ERROR] %s\n' "$*" >&2; exit 1; }

has_program_tree() {
    [ -f "$1/fls-manager.py" ] && [ -d "$1/fls_manager" ] && [ -f "$1/fls.sh" ]
}

TEMP_SOURCE=""
cleanup() {
    if [ -n "$TEMP_SOURCE" ] && [ -d "$TEMP_SOURCE" ]; then
        rm -rf "$TEMP_SOURCE"
    fi
}
trap cleanup EXIT

if ! has_program_tree "$SOURCE_DIR"; then
    command -v git >/dev/null 2>&1 || die "当前目录不是 FLS 源码/发布包，且未找到 Git"
    TEMP_SOURCE="$(mktemp -d)"
    say "拉取 FLS 仓库：$REPO_URL"
    git clone --depth 1 -b "$REPO_BRANCH" "$REPO_URL" "$TEMP_SOURCE/fls"
    SOURCE_DIR="$TEMP_SOURCE/fls"
fi

mkdir -p "$INSTALL_DIR"
say "安装目录：$INSTALL_DIR"

for item in "$SOURCE_DIR"/* "$SOURCE_DIR"/.[!.]*; do
    [ -e "$item" ] || continue
    name="$(basename "$item")"
    case "$name" in
        .git|.github|.venv|data|log|logs|scripts|.tmp|.pytest_cache|__pycache__|tests|tools|packaging|docs|install.sh)
            continue
            ;;
    esac
    [ "$(readlink -f "$item")" = "$(readlink -f "$INSTALL_DIR/$name" 2>/dev/null || true)" ] && continue
    cp -a "$item" "$INSTALL_DIR/"
done

if [ -d "$SOURCE_DIR/scripts" ] && [ ! -e "$INSTALL_DIR/scripts" ]; then
    cp -a "$SOURCE_DIR/scripts" "$INSTALL_DIR/scripts"
fi
mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/log" "$INSTALL_DIR/scripts"

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
else
    die "未找到 python3/python，请先安装 Python 3.10 或更高版本"
fi

python_version="$($PYTHON_BIN -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
{
    major="${python_version%%.*}"; minor="${python_version#*.}"
    [ "$major" -gt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -ge 10 ]; } || die "Python 版本过低：$python_version"
}

VENV_DIR="$INSTALL_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
if [ ! -x "$VENV_PYTHON" ]; then
    say "创建 Python 虚拟环境：$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_PYTHON" -c 'import flask, requests, apscheduler, socks' >/dev/null 2>&1; then
    say "安装 FLS Python 依赖"
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install Flask requests APScheduler PySocks tzdata setproctitle
fi

chmod 755 "$INSTALL_DIR/fls.sh" 2>/dev/null || true
if [ "${FLS_NO_START:-0}" != "1" ]; then
    say "启动 FLS Manager"
    (cd "$INSTALL_DIR" && sh ./fls.sh start)
else
    say "安装完成，未启动面板"
fi
say "安装完成：$INSTALL_DIR"
