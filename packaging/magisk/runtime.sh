#!/system/bin/sh

FLS_ROOT="${FLS_ROOT:-/data/fls}"
FLS_RUNTIME_DIR="${FLS_RUNTIME_DIR:-$FLS_ROOT/runtime}"
FLS_PROJECT_DIR="${FLS_PROJECT_DIR:-$FLS_ROOT/project}"
FLS_A_LOG="${FLS_A_LOG:-$FLS_ROOT/fls-a.log}"
FLS_RELEASE_REPO="${FLS_RELEASE_REPO:-liyw0205/fls}"
FLS_RUNTIME_TAG="${FLS_RUNTIME_TAG:-proot-runtime}"
FLS_PROOT_PROFILE="${FLS_PROOT_PROFILE:-}"

if [ -z "$FLS_PROOT_PROFILE" ] && [ -f "$FLS_ROOT/profile" ]; then
    FLS_PROOT_PROFILE="$(tr -cd '[:alnum:]_' < "$FLS_ROOT/profile" 2>/dev/null)"
fi
FLS_PROOT_PROFILE="${FLS_PROOT_PROFILE:-python}"

log_line() {
    mkdir -p "$(dirname "$FLS_A_LOG")" 2>/dev/null
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown-time)" "$*" >> "$FLS_A_LOG" 2>/dev/null
}

download_file() {
    url="$1"
    output="$2"
    if command -v curl >/dev/null 2>&1; then
        curl -fL --retry 3 --connect-timeout 15 "$url" -o "$output"
        return $?
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -O "$output" "$url"
        return $?
    fi
    if command -v busybox >/dev/null 2>&1 && busybox wget --help >/dev/null 2>&1; then
        busybox wget -O "$output" "$url"
        return $?
    fi
    log_line "[FLS-A][ERROR] 找不到 curl/wget，无法下载 proot 运行时"
    return 1
}

detect_arch() {
    machine="$(getprop ro.product.cpu.abi 2>/dev/null || uname -m 2>/dev/null)"
    case "$machine" in
        arm64-v8a|aarch64|arm64) echo arm64 ;;
        armeabi-v7a|armv7*|arm) echo armv7 ;;
        *) return 1 ;;
    esac
}

runtime_ready() {
    [ -x "$FLS_RUNTIME_DIR/bin/proot" ] &&
        [ -x "$FLS_RUNTIME_DIR/rootfs/opt/fls-venv/bin/python" ] &&
        [ "$(cat "$FLS_RUNTIME_DIR/.profile" 2>/dev/null)" = "$FLS_PROOT_PROFILE" ] &&
        [ "$(cat "$FLS_RUNTIME_DIR/.arch" 2>/dev/null)" = "$1" ]
}

fls_install_runtime() {
    arch="$(detect_arch 2>/dev/null)" || {
        log_line "[FLS-A][ERROR] 不支持的 Android 架构：$(getprop ro.product.cpu.abi 2>/dev/null || uname -m)"
        return 1
    }
    if runtime_ready "$arch"; then
        return 0
    fi
    case "$FLS_PROOT_PROFILE" in
        python|all) ;;
        *) log_line "[FLS-A][ERROR] FLS_PROOT_PROFILE 只能是 python 或 all：$FLS_PROOT_PROFILE"; return 1 ;;
    esac

    asset="fls-proot-${FLS_PROOT_PROFILE}-${arch}.tar.gz"
    url="https://github.com/$FLS_RELEASE_REPO/releases/download/$FLS_RUNTIME_TAG/$asset"
    mkdir -p "$FLS_ROOT"
    archive="$FLS_ROOT/.${asset}.$$"
    temp_dir="$FLS_ROOT/.runtime.$$"
    log_line "[FLS-A] 下载运行时：$url"
    download_file "$url" "$archive" || {
        log_line "[FLS-A][ERROR] proot 运行时下载失败"
        rm -f "$archive"
        return 1
    }
    rm -rf "$temp_dir"
    mkdir -p "$temp_dir"
    tar -xzf "$archive" -C "$temp_dir" || {
        log_line "[FLS-A][ERROR] 运行时压缩包解压失败"
        rm -f "$archive"
        rm -rf "$temp_dir"
        return 1
    }
    rm -rf "$FLS_RUNTIME_DIR"
    mv "$temp_dir" "$FLS_RUNTIME_DIR"
    rm -f "$archive"
    chmod 755 "$FLS_RUNTIME_DIR/bin/proot" "$FLS_RUNTIME_DIR/libexec/proot/loader" 2>/dev/null || true
    log_line "[FLS-A] proot 运行时安装完成：$FLS_RUNTIME_DIR"
}

fls_sync_project() {
    source_dir="$1"
    module_prop="$2"
    [ -d "$source_dir" ] || return 1
    module_version="$(sed -n 's/^version=//p' "$module_prop" 2>/dev/null | head -n 1)"
    mkdir -p "$FLS_PROJECT_DIR" "$FLS_PROJECT_DIR/data" "$FLS_PROJECT_DIR/log" "$FLS_PROJECT_DIR/scripts"
    if [ -n "$module_version" ] &&
        [ "$(cat "$FLS_PROJECT_DIR/.fls-module-version" 2>/dev/null)" = "$module_version" ] &&
        [ -f "$FLS_PROJECT_DIR/fls.sh" ]; then
        return 0
    fi
    cp -R "$source_dir/." "$FLS_PROJECT_DIR/" 2>/dev/null || return 1
    chmod 755 "$FLS_PROJECT_DIR/fls.sh" "$FLS_PROJECT_DIR/fls-manager.py" 2>/dev/null || true
    [ -n "$module_version" ] && printf '%s\n' "$module_version" > "$FLS_PROJECT_DIR/.fls-module-version"
}

fls_install_bridge() {
    module_dir="$1"
    target="$2"
    [ -f "$module_dir/fls-a.sh" ] || return 1
    if [ ! -f "$target" ] || ! cmp -s "$module_dir/fls-a.sh" "$target" 2>/dev/null; then
        cp "$module_dir/fls-a.sh" "$target" 2>/dev/null || return 1
        chmod 755 "$target" 2>/dev/null || true
    fi
}
