#!/system/bin/sh

MODDIR="${0%/*}"
FLS_ROOT="${FLS_ROOT:-/data/fls}"
FLS_RUNTIME_DIR="${FLS_RUNTIME_DIR:-$FLS_ROOT/runtime}"
FLS_PROJECT_DIR="${FLS_PROJECT_DIR:-$FLS_ROOT/project}"
FLS_A="/data/adb/fls-a.sh"
FLS_T="/data/adb/fls-t.sh"
FLS_A_LOG="${FLS_A_LOG:-$FLS_ROOT/fls-a.log}"

if [ -f "$MODDIR/runtime.env" ]; then
    . "$MODDIR/runtime.env"
fi
. "$MODDIR/runtime.sh" || exit 1

mkdir -p "$FLS_ROOT"
fls_install_bridge "$MODDIR" "$FLS_A" || exit 1
fls_install_termux_script "$MODDIR" "$FLS_T" || exit 1
fls_sync_project "$MODDIR/fls" "$MODDIR/module.prop" || {
    log_line "[FLS-A][ERROR] 同步 FLS 项目失败"
    exit 1
}

(
    sleep "${FLS_BOOT_DELAY:-15}"
    fls_install_runtime || exit 1
    export FLS_ROOT FLS_RUNTIME_DIR FLS_PROJECT_DIR FLS_LOG="$FLS_A_LOG"
    sh "$FLS_A" start >>"$FLS_A_LOG" 2>&1
) &
