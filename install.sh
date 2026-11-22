#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${FLS_REPO_URL:-https://github.com/liyw0205/fls.git}"
REPO_BRANCH="${FLS_REPO_BRANCH:-main}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${FLS_INSTALL_DIR:-${HOME}/.local/share/fls}"
case "$INSTALL_DIR" in
    /*) ;;
    *) INSTALL_DIR="$PWD/$INSTALL_DIR" ;;
esac

say() { printf '[FLS] %s\n' "$*"; }
die() { printf '[FLS][ERROR] %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
用法：install.sh [--venv PATH]

Linux 默认使用 /root/myenv；可用 FLS_VENV_PATH 或 --venv PATH 覆盖。
容器和 CI 环境沿用安装目录内的 .venv。
EOF
}

is_isolated_runtime() {
    [ "${CI:-}" = "true" ] || [ "${GITHUB_ACTIONS:-}" = "true" ] ||
        [ -f /.dockerenv ] || [ -f /run/.containerenv ]
}

if is_isolated_runtime; then
    DEFAULT_VENV_PATH="$INSTALL_DIR/.venv"
else
    DEFAULT_VENV_PATH="/root/myenv"
fi
if [ -n "${FLS_VENV_PATH:-}" ]; then
    VENV_PATH_SOURCE="explicit"
    DEFAULT_VENV_PATH="$FLS_VENV_PATH"
elif [ -r "$INSTALL_DIR/data/python-venv-path" ]; then
    SAVED_VENV_PATH="$(sed -n '1p' "$INSTALL_DIR/data/python-venv-path")"
    SAVED_VENV_SOURCE="$(sed -n '2p' "$INSTALL_DIR/data/python-venv-path")"
    if [ -n "$SAVED_VENV_PATH" ] && [ "$SAVED_VENV_SOURCE" = "explicit" ]; then
        DEFAULT_VENV_PATH="$SAVED_VENV_PATH"
        VENV_PATH_SOURCE="explicit"
    else
        VENV_PATH_SOURCE="default"
    fi
else
    VENV_PATH_SOURCE="default"
fi
FLS_VENV_PATH="${FLS_VENV_PATH:-$DEFAULT_VENV_PATH}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --venv|-V)
            [ "$#" -ge 2 ] || die "--venv 后面需要填写虚拟环境目录"
            FLS_VENV_PATH="$2"
            VENV_PATH_SOURCE="explicit"
            shift 2
            ;;
        --venv=*)
            FLS_VENV_PATH="${1#--venv=}"
            [ -n "$FLS_VENV_PATH" ] || die "--venv 后面需要填写虚拟环境目录"
            VENV_PATH_SOURCE="explicit"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "未知参数：$1（使用 --help 查看用法）"
            ;;
    esac
done

case "$FLS_VENV_PATH" in
    /*) ;;
    *) FLS_VENV_PATH="$PWD/$FLS_VENV_PATH" ;;
esac

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

if [ -n "${FLS_PYTHON:-}" ]; then
    VENV_PYTHON="$FLS_PYTHON"
    [ -x "$VENV_PYTHON" ] || die "FLS_PYTHON 不可执行：$VENV_PYTHON"
else
    VENV_PYTHON="$FLS_VENV_PATH/bin/python"
    if [ ! -x "$VENV_PYTHON" ]; then
        say "创建 Python 虚拟环境：$FLS_VENV_PATH"
        "$PYTHON_BIN" -m venv "$FLS_VENV_PATH" || die "无法创建 Python 环境：$FLS_VENV_PATH"
    fi
fi

runtime_version="$($VENV_PYTHON -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
runtime_major="${runtime_version%%.*}"
runtime_minor="${runtime_version#*.}"
[ "$runtime_major" -gt 3 ] || { [ "$runtime_major" -eq 3 ] && [ "$runtime_minor" -ge 10 ]; } || die "运行环境 Python 版本过低：$runtime_version"

venv_path_file="$INSTALL_DIR/data/python-venv-path"
venv_path_tmp="$venv_path_file.tmp.$$"
printf '%s\n%s\n' "$FLS_VENV_PATH" "$VENV_PATH_SOURCE" > "$venv_path_tmp" || die "无法保存 Python 环境路径"
mv -f "$venv_path_tmp" "$venv_path_file" || die "无法保存 Python 环境路径"

if ! "$VENV_PYTHON" -c 'import flask, requests, apscheduler, socks, qrcode' >/dev/null 2>&1; then
    say "安装 FLS Python 依赖"
    "$VENV_PYTHON" -m pip install --upgrade pip
    "$VENV_PYTHON" -m pip install Flask requests APScheduler PySocks tzdata setproctitle qrcode
fi

chmod 755 "$INSTALL_DIR/fls.sh" 2>/dev/null || true
if [ "${FLS_NO_START:-0}" != "1" ]; then
    say "启动 FLS Manager"
    (cd "$INSTALL_DIR" && FLS_PYTHON="$VENV_PYTHON" sh ./fls.sh start)
else
    say "安装完成，未启动面板"
fi
say "安装完成：$INSTALL_DIR"
