#!/system/bin/sh

SKIPUNZIP=1
FLS_ROOT="${FLS_ROOT:-/data/fls}"
FLS_PROJECT_DIR="${FLS_PROJECT_DIR:-$FLS_ROOT/project}"
FLS_A_LOG="${FLS_A_LOG:-$FLS_ROOT/fls-a.log}"

print_line() {
    if command -v ui_print >/dev/null 2>&1; then
        ui_print "$1"
    else
        echo "$1"
    fi
}

abort_install() {
    if command -v abort >/dev/null 2>&1; then
        abort "$1"
    fi
    echo "$1" >&2
    exit 1
}

extract_entry() {
    unzip -o "$ZIPFILE" "$1" -d "$MODPATH" >/dev/null 2>&1 ||
        abort_install "FLS 模块文件解压失败：$1"
}

mkdir -p "$MODPATH" "$FLS_ROOT" "$FLS_PROJECT_DIR"
for entry in module.prop runtime.env runtime.sh fls-a.sh fls-t.sh service.sh uninstall.sh action.sh version.json changelog.md; do
    extract_entry "$entry"
done
extract_entry "webroot/*"
extract_entry "fls/*"

chmod 755 "$MODPATH"/*.sh "$MODPATH"/runtime.sh 2>/dev/null || true
. "$MODPATH/runtime.env" 2>/dev/null || true
. "$MODPATH/runtime.sh" || abort_install "FLS 运行时辅助脚本加载失败"

print_line "- 同步 FLS 程序文件（保留 data、log 和用户脚本）"
fls_sync_project "$MODPATH/fls" "$MODPATH/module.prop" ||
    abort_install "FLS 程序文件同步失败"

if ! fls_install_bridge "$MODPATH" "/data/adb/fls-a.sh"; then
    abort_install "FLS Android 调用脚本安装失败"
fi
if ! fls_install_termux_script "$MODPATH" "/data/adb/fls-t.sh"; then
    abort_install "FLS Termux 调用脚本安装失败"
fi

print_line "- 检查 proot 运行时（固定标签：${FLS_RUNTIME_TAG:-proot-runtime}）"
if fls_install_runtime; then
    print_line "- proot 运行时已就绪"
else
    print_line "- proot 暂未下载成功，首次启动 service.sh 时会自动重试"
fi

print_line "- FLS 模块安装完成"
