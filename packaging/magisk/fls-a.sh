#!/system/bin/sh
# KernelSU / Magisk entry point for the FLS proot runtime.

FLS_ROOT="${FLS_ROOT:-/data/fls}"
FLS_RUNTIME_DIR="${FLS_RUNTIME_DIR:-$FLS_ROOT/runtime}"
FLS_ROOTFS="${FLS_ROOTFS:-$FLS_RUNTIME_DIR/rootfs}"
FLS_PROJECT_DIR="${FLS_PROJECT_DIR:-$FLS_ROOT/project}"
FLS_PROOT_BIN="${FLS_PROOT_BIN:-$FLS_RUNTIME_DIR/bin/proot}"
FLS_LOG="${FLS_LOG:-$FLS_ROOT/fls-a.log}"
FLS_MODULE_DIR="${FLS_MODULE_DIR:-/data/adb/modules/fls-manager}"
FLS_MODULE_PROP="${FLS_MODULE_PROP:-$FLS_MODULE_DIR/module.prop}"

log_line() {
    mkdir -p "$(dirname "$FLS_LOG")" 2>/dev/null
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown-time)" "$*" >> "$FLS_LOG" 2>/dev/null
    printf '%s\n' "$*"
}

err() {
    log_line "[FLS-A][ERROR] $*" >&2
}

require_runtime() {
    if [ ! -x "$FLS_PROOT_BIN" ]; then
        err "未找到 proot 运行时：$FLS_PROOT_BIN"
        err "请先安装模块并等待首次启动，或检查 /data/fls/runtime 是否完整"
        exit 1
    fi

    if [ ! -x "$FLS_ROOTFS/usr/bin/python3" ] || [ ! -x "$FLS_ROOTFS/opt/fls-venv/bin/python" ]; then
        err "运行时 Python 环境不完整：$FLS_ROOTFS"
        exit 1
    fi

    if [ ! -f "$FLS_PROJECT_DIR/fls.sh" ] || [ ! -f "$FLS_PROJECT_DIR/fls-manager.py" ]; then
        err "未找到 FLS 项目：$FLS_PROJECT_DIR"
        exit 1
    fi
}

run_in_proot() {
    require_runtime
    mkdir -p "$FLS_PROJECT_DIR/data" "$FLS_PROJECT_DIR/log" 2>/dev/null || true
    command="$1"
    shift

    export HOME=/root
    export PATH=/opt/fls-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    export FLS_BASE_DIR=/opt/fls
    export FLS_PYTHON=/opt/fls-venv/bin/python
    export FLS_MODULE_DIR FLS_MODULE_PROP
    export LD_LIBRARY_PATH="$FLS_RUNTIME_DIR/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export PROOT_LOADER="$FLS_RUNTIME_DIR/libexec/proot/loader"
    if [ -f "$FLS_RUNTIME_DIR/libexec/proot/loader32" ]; then
        export PROOT_LOADER_32="$FLS_RUNTIME_DIR/libexec/proot/loader32"
    else
        unset PROOT_LOADER_32
    fi
    export PROOT_TMP_DIR="$FLS_ROOT/tmp"
    export LANG="${LANG:-C.UTF-8}"
    export LC_ALL="${LC_ALL:-C.UTF-8}"

    module_parent="$(dirname "$FLS_MODULE_DIR")"
    module_bind_args=""
    if [ -d "$module_parent" ]; then
        mkdir -p "$FLS_ROOTFS$module_parent" 2>/dev/null || true
        module_bind_args="-b $module_parent:$module_parent"
    fi

    # Keep the daemon alive after fls.sh returns; stop/restart operate through the shared PID file.
    if [ -f /etc/resolv.conf ] && [ -d /sdcard ]; then
        "$FLS_PROOT_BIN" --root-id --link2symlink \
            -r "$FLS_ROOTFS" -b /dev -b /proc -b /sys \
            -b "$FLS_ROOT:$FLS_ROOT" -b "$FLS_PROJECT_DIR:/opt/fls" \
            $module_bind_args \
            -b /etc/resolv.conf:/etc/resolv.conf \
            -b /sdcard:/sdcard -w /opt/fls \
            /bin/sh /opt/fls/fls.sh "$command" "$@" 2>&1
    elif [ -f /etc/resolv.conf ]; then
        "$FLS_PROOT_BIN" --root-id --link2symlink \
            -r "$FLS_ROOTFS" -b /dev -b /proc -b /sys \
            -b "$FLS_ROOT:$FLS_ROOT" -b "$FLS_PROJECT_DIR:/opt/fls" \
            $module_bind_args \
            -b /etc/resolv.conf:/etc/resolv.conf \
            -w /opt/fls /bin/sh /opt/fls/fls.sh "$command" "$@" 2>&1
    elif [ -d /sdcard ]; then
        "$FLS_PROOT_BIN" --root-id --link2symlink \
            -r "$FLS_ROOTFS" -b /dev -b /proc -b /sys \
            -b "$FLS_ROOT:$FLS_ROOT" -b "$FLS_PROJECT_DIR:/opt/fls" \
            $module_bind_args \
            -b /sdcard:/sdcard \
            -w /opt/fls /bin/sh /opt/fls/fls.sh "$command" "$@" 2>&1
    else
        "$FLS_PROOT_BIN" --root-id --link2symlink \
            -r "$FLS_ROOTFS" -b /dev -b /proc -b /sys \
            -b "$FLS_ROOT:$FLS_ROOT" -b "$FLS_PROJECT_DIR:/opt/fls" \
            $module_bind_args \
            -w /opt/fls \
            /bin/sh /opt/fls/fls.sh "$command" "$@" 2>&1
    fi
}

if [ "$#" -eq 0 ]; then
    set -- status
fi

case "$1" in
    -h|--help|help)
        cat <<'EOF'
FLS proot module entry point

用法：
  sh /data/adb/fls-a.sh start
  sh /data/adb/fls-a.sh stop
  sh /data/adb/fls-a.sh restart
  sh /data/adb/fls-a.sh status
  sh /data/adb/fls-a.sh log
  sh /data/adb/fls-a.sh update
EOF
        exit 0
        ;;
esac

command="$1"
shift
run_in_proot "$command" "$@"
