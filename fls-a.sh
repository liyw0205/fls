#!/system/bin/sh
#
# fls-a.sh
# KernelSU / Magisk / adb shell root 环境调用 Termux 中的 FLS
#
# 用法：
#   sh fls-a.sh start
#   sh fls-a.sh restart
#   sh fls-a.sh stop
#   sh fls-a.sh status
#   sh fls-a.sh log
#   sh fls-a.sh update
#   sh fls-a.sh clone
#   sh fls-a.sh bstart
#   sh fls-a.sh rstart
#

TERMUX_PACKAGE="${TERMUX_PACKAGE:-com.termux}"

TERMUX_APP_DIR="/data/data/$TERMUX_PACKAGE"
TERMUX_HOME="$TERMUX_APP_DIR/files/home"
TERMUX_PREFIX="$TERMUX_APP_DIR/files/usr"

FLS_BASE_DIR="${FLS_BASE_DIR:-$TERMUX_HOME/fls}"
FLS_SH="${FLS_SH:-$FLS_BASE_DIR/fls.sh}"

FLS_REPO_URL="${FLS_REPO_URL:-https://github.com/liyw0205/fls.git}"
FLS_REPO_BRANCH="${FLS_REPO_BRANCH:-main}"

WAIT_SECONDS="${WAIT_SECONDS:-60}"
ACTION_TIMEOUT="${ACTION_TIMEOUT:-600}"

FLS_A_LOG="${FLS_A_LOG:-/data/adb/fls-a.log}"
LOCK_DIR="${LOCK_DIR:-/data/adb/fls-a.lock}"

BOOT_DEFAULT_CMD="${BOOT_DEFAULT_CMD:-start}"
QUIET="${QUIET:-0}"

SERVICE_D_DIR="${SERVICE_D_DIR:-/data/adb/service.d}"
SERVICE_D_FILE="${SERVICE_D_FILE:-$SERVICE_D_DIR/fls-a.sh}"

now_time() {
    date "+%Y-%m-%d %H:%M:%S" 2>/dev/null || echo "unknown-time"
}

log_line() {
    mkdir -p "$(dirname "$FLS_A_LOG")" 2>/dev/null
    echo "[$(now_time)] $*" >> "$FLS_A_LOG" 2>/dev/null

    if [ "$QUIET" != "1" ]; then
        echo "$*"
    fi
}

say() {
    log_line "[FLS-A] $*"
}

err() {
    log_line "[FLS-A][ERROR] $*"
}

is_root() {
    [ "$(id -u 2>/dev/null)" = "0" ]
}

is_boot_script_path() {
    case "$0" in
        /data/adb/service.d/*|/data/adb/post-fs-data.d/*)
            return 0
            ;;
    esac

    return 1
}

cleanup_lock() {
    rmdir "$LOCK_DIR" 2>/dev/null
}

acquire_lock() {
    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$$" > "$LOCK_DIR/pid" 2>/dev/null
        trap cleanup_lock EXIT INT TERM
        return 0
    fi

    old_pid=""
    if [ -f "$LOCK_DIR/pid" ]; then
        old_pid="$(cat "$LOCK_DIR/pid" 2>/dev/null)"
    fi

    if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
        err "已有 fls-a.sh 正在运行，PID: $old_pid"
        exit 0
    fi

    err "检测到残留锁，尝试清理"
    rm -rf "$LOCK_DIR" 2>/dev/null

    if mkdir "$LOCK_DIR" 2>/dev/null; then
        echo "$$" > "$LOCK_DIR/pid" 2>/dev/null
        trap cleanup_lock EXIT INT TERM
        return 0
    fi

    err "无法创建锁目录：$LOCK_DIR"
    exit 1
}

wait_termux_ready() {
    i=0

    while [ "$i" -lt "$WAIT_SECONDS" ]; do
        if [ -d "$TERMUX_PREFIX" ] && [ -x "$TERMUX_PREFIX/bin/sh" ]; then
            return 0
        fi

        i=$((i + 1))
        sleep 1
    done

    return 1
}

setup_termux_env() {
    export TERMUX_PACKAGE
    export TERMUX_APP_DIR
    export TERMUX_PREFIX

    export PREFIX="$TERMUX_PREFIX"
    export HOME="$TERMUX_HOME"
    export TMPDIR="$TERMUX_PREFIX/tmp"

    export FLS_BASE_DIR
    export FLS_REPO_URL
    export FLS_REPO_BRANCH

    if [ -x "$TERMUX_PREFIX/bin/bash" ]; then
        export SHELL="$TERMUX_PREFIX/bin/bash"
    else
        export SHELL="$TERMUX_PREFIX/bin/sh"
    fi

    export PATH="$TERMUX_PREFIX/bin:$TERMUX_PREFIX/bin/applets:/system/bin:/system/xbin:/vendor/bin:/product/bin:/apex/com.android.runtime/bin:/apex/com.android.art/bin:$PATH"

    if [ -n "${LD_LIBRARY_PATH:-}" ]; then
        export LD_LIBRARY_PATH="$TERMUX_PREFIX/lib:$LD_LIBRARY_PATH"
    else
        export LD_LIBRARY_PATH="$TERMUX_PREFIX/lib"
    fi

    export LANG="${LANG:-C.UTF-8}"
    export LC_ALL="${LC_ALL:-C.UTF-8}"

    if [ -f "$TERMUX_PREFIX/etc/tls/cert.pem" ]; then
        export SSL_CERT_FILE="$TERMUX_PREFIX/etc/tls/cert.pem"
        export CURL_CA_BUNDLE="$TERMUX_PREFIX/etc/tls/cert.pem"
    fi

    unset PYTHONHOME
    unset PYTHONPATH
    unset LD_PRELOAD

    mkdir -p "$TMPDIR" 2>/dev/null
    chmod 700 "$TMPDIR" 2>/dev/null

    restorecon -R "$TERMUX_APP_DIR" 2>/dev/null
}

check_termux_files() {
    if [ ! -d "$TERMUX_APP_DIR" ]; then
        err "未找到 Termux 目录：$TERMUX_APP_DIR"
        err "请确认 Termux 包名是否为：$TERMUX_PACKAGE"
        exit 1
    fi

    if [ ! -d "$TERMUX_PREFIX" ]; then
        err "未找到 Termux PREFIX：$TERMUX_PREFIX"
        exit 1
    fi

    if [ ! -x "$TERMUX_PREFIX/bin/sh" ]; then
        err "Termux sh 不可执行：$TERMUX_PREFIX/bin/sh"
        exit 1
    fi
}

termux_shell() {
    if [ -x "$TERMUX_PREFIX/bin/bash" ]; then
        echo "$TERMUX_PREFIX/bin/bash"
    else
        echo "$TERMUX_PREFIX/bin/sh"
    fi
}

kill_child() {
    pid="$1"

    if [ -z "$pid" ]; then
        return 0
    fi

    kill "$pid" 2>/dev/null || true
    sleep 2

    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
}

run_termux_cmd() {
    cmd_text="$1"
    shell_bin="$(termux_shell)"

    "$shell_bin" -lc "$cmd_text"
}

ensure_termux_git() {
    if [ -x "$TERMUX_PREFIX/bin/git" ]; then
        return 0
    fi

    if run_termux_cmd "command -v git >/dev/null 2>&1"; then
        return 0
    fi

    say "Termux 内未找到 git，尝试 pkg 安装..."

    if [ ! -x "$TERMUX_PREFIX/bin/pkg" ]; then
        err "Termux 内未找到 pkg，无法自动安装 git"
        return 1
    fi

    run_termux_cmd "pkg update -y || true; pkg install -y git"

    if run_termux_cmd "command -v git >/dev/null 2>&1"; then
        say "Termux git 安装成功"
        return 0
    fi

    err "Termux git 安装失败"
    return 1
}

repo_ready() {
    if [ -f "$FLS_BASE_DIR/fls-manager.py" ] && [ -d "$FLS_BASE_DIR/fls_manager" ] && [ -f "$FLS_BASE_DIR/fls.sh" ]; then
        return 0
    fi

    return 1
}

clone_fls_repo() {
    ensure_termux_git || exit 1

    say "准备在 Termux 中拉取 FLS 仓库"
    say "仓库：$FLS_REPO_URL"
    say "分支：$FLS_REPO_BRANCH"
    say "目录：$FLS_BASE_DIR"

    parent_dir="$(dirname "$FLS_BASE_DIR")"
    run_termux_cmd "mkdir -p '$parent_dir'"

    if [ ! -e "$FLS_BASE_DIR" ]; then
        run_termux_cmd "git clone --depth 1 -b '$FLS_REPO_BRANCH' '$FLS_REPO_URL' '$FLS_BASE_DIR'"
    else
        tmp_dir="${FLS_BASE_DIR}.clone.$$"

        run_termux_cmd "rm -rf '$tmp_dir'"

        run_termux_cmd "git clone --depth 1 -b '$FLS_REPO_BRANCH' '$FLS_REPO_URL' '$tmp_dir'" || {
            run_termux_cmd "rm -rf '$tmp_dir'"
            err "git clone 失败"
            exit 1
        }

        run_termux_cmd "mkdir -p '$FLS_BASE_DIR'"

        if run_termux_cmd "command -v tar >/dev/null 2>&1"; then
            run_termux_cmd "cd '$tmp_dir' && tar cf - . | (cd '$FLS_BASE_DIR' && tar xf -)"
        else
            run_termux_cmd "cp -R '$tmp_dir'/. '$FLS_BASE_DIR'/"
        fi

        run_termux_cmd "rm -rf '$tmp_dir'"
    fi

    if repo_ready; then
        say "FLS 仓库已就绪"
        return 0
    fi

    err "仓库拉取完成，但文件不完整"
    err "缺少 fls.sh / fls-manager.py / fls_manager"
    exit 1
}

ensure_fls_files() {
    if repo_ready; then
        return 0
    fi

    say "未发现完整 FLS 程序，开始自动 git clone"
    clone_fls_repo

    if [ ! -f "$FLS_SH" ]; then
        err "git clone 后仍未找到 FLS 启动脚本：$FLS_SH"
        exit 1
    fi

    chmod 755 "$FLS_SH" 2>/dev/null
}

run_fls_no_timeout() {
    cmd="$1"
    shift

    shell_bin="$(termux_shell)"

    cd "$FLS_BASE_DIR" 2>/dev/null || {
        err "无法进入 FLS 工作目录：$FLS_BASE_DIR"
        exit 1
    }

    say "执行无超时命令：$shell_bin $FLS_SH $cmd $*"

    "$shell_bin" "$FLS_SH" "$cmd" "$@"
    code="$?"

    say "fls.sh $cmd 已退出，退出码：$code"
    exit "$code"
}

run_fls_with_timeout() {
    cmd="$1"
    shift

    shell_bin="$(termux_shell)"

    cd "$FLS_BASE_DIR" 2>/dev/null || {
        err "无法进入 FLS 工作目录：$FLS_BASE_DIR"
        exit 1
    }

    say "执行：$shell_bin $FLS_SH $cmd $*"
    say "控制脚本超时：${ACTION_TIMEOUT}s"

    if [ "$ACTION_TIMEOUT" = "0" ]; then
        "$shell_bin" "$FLS_SH" "$cmd" "$@" >> "$FLS_A_LOG" 2>&1
        code="$?"
        say "fls.sh $cmd 已退出，退出码：$code"
        exit "$code"
    fi

    "$shell_bin" "$FLS_SH" "$cmd" "$@" >> "$FLS_A_LOG" 2>&1 &
    child_pid="$!"

    elapsed=0

    while kill -0 "$child_pid" 2>/dev/null; do
        if [ "$elapsed" -ge "$ACTION_TIMEOUT" ]; then
            err "fls.sh $cmd 执行超过 ${ACTION_TIMEOUT}s，结束控制脚本 PID: $child_pid"
            kill_child "$child_pid"
            exit 124
        fi

        elapsed=$((elapsed + 1))
        sleep 1
    done

    wait "$child_pid" 2>/dev/null
    code="$?"

    say "fls.sh $cmd 已退出，退出码：$code"
    exit "$code"
}

bstart_fls_a() {
    if ! is_root; then
        err "bstart 需要 root 权限"
        err "请执行：su -c 'sh fls-a.sh bstart'"
        exit 1
    fi

    mkdir -p "$SERVICE_D_DIR" 2>/dev/null || {
        err "无法创建目录：$SERVICE_D_DIR"
        exit 1
    }

    src="$0"

    if [ -f "$src" ]; then
        cp "$src" "$SERVICE_D_FILE" 2>/dev/null || {
            err "复制自启脚本失败：$src -> $SERVICE_D_FILE"
            exit 1
        }
    else
        err "无法找到当前脚本文件：$src"
        err "请把 fls-a.sh 保存为文件后再执行 bstart"
        exit 1
    fi

    chmod 755 "$SERVICE_D_FILE" 2>/dev/null || true

    say "已生成 KernelSU / Magisk 自启脚本：$SERVICE_D_FILE"
    say "无参数开机默认执行：$BOOT_DEFAULT_CMD"

    run_fls_with_timeout restart "$@"
}

rstart_fls_a() {
    if ! is_root; then
        err "rstart 需要 root 权限"
        err "请执行：su -c 'sh fls-a.sh rstart'"
        exit 1
    fi

    rm -f "$SERVICE_D_FILE" 2>/dev/null || true

    say "已移除 KernelSU / Magisk 自启脚本：$SERVICE_D_FILE"

    run_fls_with_timeout restart "$@"
}

usage() {
    cat <<EOF
FLS-A

命令：
  start [-p 端口] [-t Token]   启动
  stop                         停止
  restart [-p 端口] [-t Token] 重启
  status                       状态
  log                          日志
  update                       git pull 更新
  clone                        强制重新 clone
  bstart                       生成 service.d 自启并重启
  rstart                       移除 service.d 自启并重启
  ensure-repo                  检查/拉取程序

示例：
  sh fls-a.sh start
  sh fls-a.sh restart -p 5701 -t 123456
  su -c 'sh fls-a.sh bstart'
  su -c 'sh /data/adb/service.d/fls-a.sh rstart'
EOF
}

if is_boot_script_path; then
    QUIET="${QUIET:-1}"
fi

if ! is_root; then
    err "建议使用 root 执行，例如：su -c 'sh fls-a.sh start'"
fi

if [ "$#" -eq 0 ]; then
    if is_boot_script_path; then
        set -- "$BOOT_DEFAULT_CMD"
    else
        usage
        exit 0
    fi
fi

case "$1" in
    -h|--help|help)
        usage
        exit 0
        ;;
esac

cmd="$1"
shift

acquire_lock

say "===================================================="
say "fls-a.sh 启动"
say "PID: $$"
say "命令: $cmd $*"

say "等待 Termux 环境可用..."
if ! wait_termux_ready; then
    err "等待超时，Termux 环境不可用：$TERMUX_PREFIX"
    exit 1
fi

setup_termux_env
check_termux_files

say "Termux PREFIX: $TERMUX_PREFIX"
say "Termux HOME: $TERMUX_HOME"
say "FLS 工作目录: $FLS_BASE_DIR"
say "FLS 启动脚本: $FLS_SH"

ensure_fls_files

case "$cmd" in
    log|logs)
        run_fls_no_timeout "$cmd" "$@"
        ;;
    bstart|boot-start|enable-autostart)
        bstart_fls_a "$@"
        ;;
    rstart|remove-start|disable-autostart)
        rstart_fls_a "$@"
        ;;
    start|stop|restart|status|update|upgrade|pull|clone|ensure-repo|install)
        run_fls_with_timeout "$cmd" "$@"
        ;;
    *)
        run_fls_with_timeout "$cmd" "$@"
        ;;
esac