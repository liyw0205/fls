#!/system/bin/sh

MODDIR="${0%/*}"
FLS_ROOT="${FLS_ROOT:-/data/fls}"
FLS_RUNTIME_DIR="${FLS_RUNTIME_DIR:-$FLS_ROOT/runtime}"
FLS_PROJECT_DIR="${FLS_PROJECT_DIR:-$FLS_ROOT/project}"
FLS_A="/data/adb/fls-a.sh"
FLS_A_LOG="${FLS_A_LOG:-$FLS_ROOT/fls-a.log}"
RELEASE_REPO="${FLS_RELEASE_REPO:-liyw0205/fls}"
RELEASE_TAG="${FLS_RELEASE_TAG:-latest}"
PROFILE="${FLS_PROOT_PROFILE:-}"

if [ -z "$PROFILE" ] && [ -f "$FLS_ROOT/profile" ]; then
    PROFILE="$(tr -cd '[:alnum:]_' < "$FLS_ROOT/profile" 2>/dev/null)"
fi
PROFILE="${PROFILE:-python}"

if [ -f "$MODDIR/runtime.env" ]; then
    . "$MODDIR/runtime.env"
    RELEASE_REPO="${FLS_RELEASE_REPO:-$RELEASE_REPO}"
    RELEASE_TAG="${FLS_RELEASE_TAG:-$RELEASE_TAG}"
fi

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

sync_project() {
    mkdir -p "$FLS_PROJECT_DIR"
    module_version="$(sed -n 's/^version=//p' "$MODDIR/module.prop" 2>/dev/null | head -n 1)"
    if [ -n "$module_version" ] && [ -f "$FLS_PROJECT_DIR/.fls-module-version" ] &&
        [ "$(cat "$FLS_PROJECT_DIR/.fls-module-version" 2>/dev/null)" = "$module_version" ] &&
        [ -f "$FLS_PROJECT_DIR/fls.sh" ]; then
        return 0
    fi
    if [ -d "$MODDIR/payload/fls" ]; then
        cp -R "$MODDIR/payload/fls/." "$FLS_PROJECT_DIR/" 2>/dev/null || return 1
    fi
    mkdir -p "$FLS_PROJECT_DIR/data" "$FLS_PROJECT_DIR/log" "$FLS_PROJECT_DIR/scripts"
    chmod 755 "$FLS_PROJECT_DIR/fls.sh" "$FLS_PROJECT_DIR/fls-manager.py" 2>/dev/null || true
    [ -n "$module_version" ] && printf '%s\n' "$module_version" > "$FLS_PROJECT_DIR/.fls-module-version"
}

install_runtime() {
    arch="$(detect_arch 2>/dev/null)" || {
        log_line "[FLS-A][ERROR] 不支持的 Android 架构：$(getprop ro.product.cpu.abi 2>/dev/null || uname -m)"
        return 1
    }

    if [ -x "$FLS_RUNTIME_DIR/bin/proot" ] &&
        [ -x "$FLS_RUNTIME_DIR/rootfs/opt/fls-venv/bin/python" ] &&
        [ "$(cat "$FLS_RUNTIME_DIR/.profile" 2>/dev/null)" = "$PROFILE" ] &&
        [ "$(cat "$FLS_RUNTIME_DIR/.arch" 2>/dev/null)" = "$arch" ]; then
        return 0
    fi

    case "$PROFILE" in
        python|all) ;;
        *) log_line "[FLS-A][ERROR] FLS_PROOT_PROFILE 只能是 python 或 all：$PROFILE"; return 1 ;;
    esac

    asset="fls-proot-${PROFILE}-${arch}.tar.gz"
    if [ "$RELEASE_TAG" = "latest" ]; then
        url="https://github.com/$RELEASE_REPO/releases/latest/download/$asset"
    else
        url="https://github.com/$RELEASE_REPO/releases/download/$RELEASE_TAG/$asset"
    fi

    mkdir -p "$FLS_ROOT" "$FLS_RUNTIME_DIR"
    archive="$FLS_ROOT/.${asset}.$$"
    log_line "[FLS-A] 下载运行时：$url"
    download_file "$url" "$archive" || {
        log_line "[FLS-A][ERROR] proot 运行时下载失败"
        rm -f "$archive"
        return 1
    }

    temp_dir="$FLS_ROOT/.runtime.$$"
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

install_bridge() {
    if [ ! -f "$FLS_A" ] || ! cmp -s "$MODDIR/fls-a.sh" "$FLS_A" 2>/dev/null; then
        cp "$MODDIR/fls-a.sh" "$FLS_A" 2>/dev/null || return 1
        chmod 755 "$FLS_A" 2>/dev/null || true
    fi
}

mkdir -p "$FLS_ROOT"
install_bridge || exit 1
sync_project || {
    log_line "[FLS-A][ERROR] 同步 FLS 项目失败"
    exit 1
}

(
    sleep "${FLS_BOOT_DELAY:-15}"
    install_runtime || exit 1
    export FLS_ROOT FLS_RUNTIME_DIR FLS_PROJECT_DIR FLS_LOG="$FLS_A_LOG"
    sh "$FLS_A" start >>"$FLS_A_LOG" 2>&1
) &
