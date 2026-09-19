#!/system/bin/sh
# FLS Termux entry point.
#
# This wrapper owns the Termux-side proot runtime. It can be called from a
# Termux shell or from an Android root shell (for example, /data/adb).

set -u

TERMUX_PACKAGE="${TERMUX_PACKAGE:-com.termux}"
IN_TERMUX=0

case "${PREFIX:-}" in
    *"/com.termux/"*|*"/com.termux/files/usr"*) IN_TERMUX=1 ;;
esac
if [ -n "${TERMUX_VERSION:-}" ]; then
    IN_TERMUX=1
fi
case "${HOME:-}" in
    /data/data/com.termux*|/data/user/*/com.termux*) IN_TERMUX=1 ;;
esac

if [ "$IN_TERMUX" = "1" ]; then
    TERMUX_HOME="${TERMUX_HOME:-${HOME:-/data/data/$TERMUX_PACKAGE/files/home}}"
    TERMUX_PREFIX="${TERMUX_PREFIX:-${PREFIX:-/data/data/$TERMUX_PACKAGE/files/usr}}"
else
    TERMUX_HOME="${TERMUX_HOME:-/data/data/$TERMUX_PACKAGE/files/home}"
    TERMUX_PREFIX="${TERMUX_PREFIX:-/data/data/$TERMUX_PACKAGE/files/usr}"
fi

SCRIPT_PATH="$0"
case "$SCRIPT_PATH" in
    /*) ;;
    *) SCRIPT_PATH="$(pwd 2>/dev/null)/$SCRIPT_PATH" ;;
esac

FLS_BASE_DIR="${FLS_BASE_DIR:-$TERMUX_HOME/fls}"
FLS_RUNTIME_DIR="${FLS_RUNTIME_DIR:-$TERMUX_HOME/.fls-runtime}"
FLS_PROFILE_FILE="${FLS_PROFILE_FILE:-$TERMUX_HOME/.fls-profile}"
FLS_PROOT_PROFILE="${FLS_PROOT_PROFILE:-}"
FLS_REPO_URL="${FLS_REPO_URL:-https://github.com/liyw0205/fls.git}"
FLS_REPO_BRANCH="${FLS_REPO_BRANCH:-main}"
FLS_RELEASE_REPO="${FLS_RELEASE_REPO:-liyw0205/fls}"
FLS_RUNTIME_TAG="${FLS_RUNTIME_TAG:-proot-runtime}"
FLS_T_LOG="${FLS_T_LOG:-$TERMUX_HOME/.fls-t.log}"

export TERMUX_PACKAGE TERMUX_HOME TERMUX_PREFIX
export HOME="$TERMUX_HOME"
export PREFIX="$TERMUX_PREFIX"
export TMPDIR="${TMPDIR:-$TERMUX_PREFIX/tmp}"
export PATH="$TERMUX_PREFIX/bin:$TERMUX_PREFIX/bin/applets:${PATH:-/system/bin:/system/xbin}"
if [ -d "$TERMUX_PREFIX/lib" ]; then
    export LD_LIBRARY_PATH="$TERMUX_PREFIX/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
fi
if [ -f "$TERMUX_PREFIX/etc/tls/cert.pem" ]; then
    export SSL_CERT_FILE="$TERMUX_PREFIX/etc/tls/cert.pem"
    export CURL_CA_BUNDLE="$TERMUX_PREFIX/etc/tls/cert.pem"
fi
mkdir -p "$TMPDIR" 2>/dev/null || true

say() {
    printf '%s\n' "[FLS-T] $*"
}

err() {
    printf '%s\n' "[FLS-T][ERROR] $*" >&2
}

log_line() {
    mkdir -p "$(dirname "$FLS_T_LOG")" 2>/dev/null || true
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo unknown-time)" "$*" >> "$FLS_T_LOG" 2>/dev/null || true
}

require_termux() {
    if [ ! -x "$TERMUX_PREFIX/bin/sh" ]; then
        err "未找到 Termux shell：$TERMUX_PREFIX/bin/sh"
        err "可通过 TERMUX_PACKAGE、TERMUX_HOME、TERMUX_PREFIX 指定 Termux 路径"
        exit 1
    fi
}

find_command() {
    name="$1"
    if command -v "$name" >/dev/null 2>&1; then
        command -v "$name"
        return 0
    fi
    if [ -x "$TERMUX_PREFIX/bin/$name" ]; then
        printf '%s\n' "$TERMUX_PREFIX/bin/$name"
        return 0
    fi
    return 1
}

download_file() {
    url="$1"
    output="$2"
    if curl_bin="$(find_command curl 2>/dev/null)"; then
        "$curl_bin" -fL --retry 3 --connect-timeout 20 "$url" -o "$output"
        return $?
    fi
    if wget_bin="$(find_command wget 2>/dev/null)"; then
        "$wget_bin" -O "$output" "$url"
        return $?
    fi
    err "Termux 中未找到 curl 或 wget"
    return 1
}

tar_extract() {
    archive="$1"
    target="$2"
    tar_bin="$(find_command tar 2>/dev/null)" || return 1
    "$tar_bin" -xzf "$archive" -C "$target"
}

detect_arch() {
    machine=""
    if [ "$IN_TERMUX" != "1" ] && command -v getprop >/dev/null 2>&1; then
        machine="$(getprop ro.product.cpu.abi 2>/dev/null || true)"
    fi
    [ -n "$machine" ] || machine="$(uname -m 2>/dev/null || true)"
    case "$machine" in
        arm64-v8a|aarch64|arm64) printf '%s\n' arm64 ;;
        armeabi-v7a|armv7*|arm) printf '%s\n' armv7 ;;
        *) return 1 ;;
    esac
}

read_profile() {
    if [ -n "$FLS_PROOT_PROFILE" ]; then
        :
    elif [ -f "$FLS_PROFILE_FILE" ]; then
        FLS_PROOT_PROFILE="$(tr -cd '[:alnum:]_' < "$FLS_PROFILE_FILE" 2>/dev/null)"
    fi
    FLS_PROOT_PROFILE="${FLS_PROOT_PROFILE:-python}"
    case "$FLS_PROOT_PROFILE" in
        python|all) return 0 ;;
        *) err "运行时类型只能是 python 或 all：$FLS_PROOT_PROFILE"; return 1 ;;
    esac
}

runtime_ready() {
    arch="$1"
    [ -x "$FLS_RUNTIME_DIR/bin/proot" ] || return 1
    [ -x "$FLS_RUNTIME_DIR/rootfs/opt/fls-venv/bin/python" ] || return 1
    [ "$(cat "$FLS_RUNTIME_DIR/.profile" 2>/dev/null)" = "$FLS_PROOT_PROFILE" ] || return 1
    [ "$(cat "$FLS_RUNTIME_DIR/.arch" 2>/dev/null)" = "$arch" ] || return 1
    return 0
}

ensure_runtime() {
    read_profile || exit 1
    arch="$(detect_arch 2>/dev/null)" || {
        err "不支持的 Android 架构：$(uname -m 2>/dev/null || echo unknown)"
        exit 1
    }
    if runtime_ready "$arch"; then
        return 0
    fi

    asset="fls-proot-${FLS_PROOT_PROFILE}-${arch}.tar.gz"
    url="https://github.com/$FLS_RELEASE_REPO/releases/download/$FLS_RUNTIME_TAG/$asset"
    mkdir -p "$TERMUX_HOME" "$FLS_RUNTIME_DIR" 2>/dev/null || true
    archive="$TERMUX_HOME/.${asset}.$$"
    temp_dir="$TERMUX_HOME/.fls-runtime.$$"
    rm -rf "$temp_dir" "$archive" 2>/dev/null || true
    say "下载 Termux proot 运行时：$url"
    download_file "$url" "$archive" || {
        rm -f "$archive"
        err "运行时下载失败"
        exit 1
    }
    mkdir -p "$temp_dir"
    tar_extract "$archive" "$temp_dir" || {
        rm -f "$archive"
        rm -rf "$temp_dir"
        err "运行时解压失败"
        exit 1
    }
    if [ ! -x "$temp_dir/bin/proot" ] || [ ! -x "$temp_dir/rootfs/opt/fls-venv/bin/python" ]; then
        rm -f "$archive"
        rm -rf "$temp_dir"
        err "运行时压缩包内容不完整"
        exit 1
    fi

    old_dir="$FLS_RUNTIME_DIR.old.$$"
    rm -rf "$old_dir" 2>/dev/null || true
    if [ -e "$FLS_RUNTIME_DIR" ]; then
        mv "$FLS_RUNTIME_DIR" "$old_dir" || {
            rm -f "$archive"
            rm -rf "$temp_dir"
            err "无法替换旧运行时：$FLS_RUNTIME_DIR"
            exit 1
        }
    fi
    if ! mv "$temp_dir" "$FLS_RUNTIME_DIR"; then
        [ -e "$old_dir" ] && mv "$old_dir" "$FLS_RUNTIME_DIR" 2>/dev/null || true
        rm -f "$archive"
        err "无法安装运行时：$FLS_RUNTIME_DIR"
        exit 1
    fi
    rm -rf "$old_dir" "$archive" 2>/dev/null || true
    mkdir -p "$FLS_RUNTIME_DIR/tmp" 2>/dev/null || true
    chmod 755 "$FLS_RUNTIME_DIR/bin/proot" "$FLS_RUNTIME_DIR/libexec/proot/loader" 2>/dev/null || true
    say "运行时已安装到：$FLS_RUNTIME_DIR"
}

repo_ready() {
    [ -f "$FLS_BASE_DIR/fls-manager.py" ] &&
        [ -d "$FLS_BASE_DIR/fls_manager" ] &&
        [ -f "$FLS_BASE_DIR/fls.sh" ]
}

ensure_git() {
    if find_command git >/dev/null 2>&1; then
        return 0
    fi
    err "Termux 中未找到 git。请先执行：pkg install git，或使用可访问的仓库压缩包下载"
    return 1
}

preserve_project_data() {
    source_dir="$1"
    target_dir="$2"
    preserve_dir="$3"
    mkdir -p "$preserve_dir" "$target_dir" 2>/dev/null || return 1
    for item in data log scripts; do
        if [ -e "$target_dir/$item" ]; then
            mv "$target_dir/$item" "$preserve_dir/$item" 2>/dev/null || return 1
        fi
    done
    copy_status=0
    cp -R "$source_dir/." "$target_dir/" 2>/dev/null || copy_status=1
    for item in data log scripts; do
        if [ -e "$preserve_dir/$item" ]; then
            rm -rf "$target_dir/$item" 2>/dev/null || true
            mv "$preserve_dir/$item" "$target_dir/$item" 2>/dev/null || copy_status=1
        fi
    done
    return "$copy_status"
}

clone_from_archive() {
    target="$1"
    curl_url="${FLS_REPO_URL%.git}/archive/refs/heads/$FLS_REPO_BRANCH.tar.gz"
    archive="$TERMUX_HOME/.fls-source.$$.tar.gz"
    temp_dir="$TERMUX_HOME/.fls-source.$$"
    rm -rf "$temp_dir" "$archive" 2>/dev/null || true
    download_file "$curl_url" "$archive" || return 1
    mkdir -p "$temp_dir"
    tar_extract "$archive" "$temp_dir" || {
        rm -rf "$temp_dir" "$archive"
        return 1
    }
    source_dir="$(find "$temp_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -n 1)"
    [ -n "$source_dir" ] || {
        rm -rf "$temp_dir" "$archive"
        return 1
    }
    preserve="${target}.preserve.$$"
    preserve_project_data "$source_dir" "$target" "$preserve"
    code="$?"
    rm -rf "$temp_dir" "$archive" "$preserve" 2>/dev/null || true
    return "$code"
}

clone_repo() {
    target="$FLS_BASE_DIR"
    temp_dir="${target}.clone.$$"
    preserve="${target}.preserve.$$"
    parent_dir="$(dirname "$target")"
    mkdir -p "$parent_dir" 2>/dev/null || return 1
    rm -rf "$temp_dir" "$preserve" 2>/dev/null || true

    if git_bin="$(find_command git 2>/dev/null)"; then
        say "从 $FLS_REPO_URL 拉取 FLS 到临时目录"
        "$git_bin" clone --depth 1 -b "$FLS_REPO_BRANCH" "$FLS_REPO_URL" "$temp_dir" || {
            rm -rf "$temp_dir"
            return 1
        }
        preserve_project_data "$temp_dir" "$target" "$preserve"
        code="$?"
        rm -rf "$temp_dir" "$preserve" 2>/dev/null || true
        return "$code"
    fi

    say "Termux 未安装 git，尝试下载仓库压缩包"
    clone_from_archive "$target"
}

ensure_repo() {
    if repo_ready; then
        return 0
    fi
    say "未发现完整 FLS 项目，开始拉取：$FLS_BASE_DIR"
    clone_repo || {
        err "FLS 项目拉取失败：$FLS_BASE_DIR"
        exit 1
    }
    repo_ready || {
        err "FLS 项目拉取完成但文件不完整"
        exit 1
    }
}

update_repo() {
    if [ -d "$FLS_BASE_DIR/.git" ] && git_bin="$(find_command git 2>/dev/null)"; then
        say "更新 FLS 仓库：$FLS_BASE_DIR"
        "$git_bin" -C "$FLS_BASE_DIR" fetch --all --prune || exit 1
        "$git_bin" -C "$FLS_BASE_DIR" checkout "$FLS_REPO_BRANCH" >/dev/null 2>&1 || true
        "$git_bin" -C "$FLS_BASE_DIR" pull --ff-only origin "$FLS_REPO_BRANCH" || exit 1
        say "FLS 仓库更新完成"
        return 0
    fi
    clone_repo || {
        err "FLS 仓库更新失败"
        exit 1
    }
    say "FLS 项目更新完成"
}

run_in_proot() {
    command_name="$1"
    shift
    ensure_runtime
    ensure_repo
    mkdir -p "$FLS_BASE_DIR/data" "$FLS_BASE_DIR/log" "$FLS_BASE_DIR/scripts" "$FLS_RUNTIME_DIR/tmp" 2>/dev/null || true

    project_dir="$FLS_BASE_DIR"
    FLS_ROOTFS="$FLS_RUNTIME_DIR/rootfs"
    FLS_PROOT_BIN="$FLS_RUNTIME_DIR/bin/proot"
    export FLS_BASE_DIR=/opt/fls
    export FLS_PYTHON=/opt/fls-venv/bin/python
    export PROOT_LOADER="$FLS_RUNTIME_DIR/libexec/proot/loader"
    export PROOT_TMP_DIR="$FLS_RUNTIME_DIR/tmp"
    export LANG="${LANG:-C.UTF-8}"
    export LC_ALL="${LC_ALL:-C.UTF-8}"
    export TERMUX_VERSION="${TERMUX_VERSION:-fls-wrapper}"

    log_line "执行：$command_name $*"
    if [ -d /sdcard ]; then
        exec "$FLS_PROOT_BIN" --root-id --link2symlink \
            -r "$FLS_ROOTFS" -b /dev -b /proc -b /sys \
            -b "$TERMUX_HOME:$TERMUX_HOME" \
            -b "$FLS_RUNTIME_DIR:$FLS_RUNTIME_DIR" \
            -b "$project_dir:/opt/fls" -b /sdcard:/sdcard \
            -w /opt/fls /bin/sh /opt/fls/fls.sh "$command_name" "$@"
    fi
    exec "$FLS_PROOT_BIN" --root-id --link2symlink \
        -r "$FLS_ROOTFS" -b /dev -b /proc -b /sys \
        -b "$TERMUX_HOME:$TERMUX_HOME" \
        -b "$FLS_RUNTIME_DIR:$FLS_RUNTIME_DIR" \
        -b "$project_dir:/opt/fls" -w /opt/fls \
        /bin/sh /opt/fls/fls.sh "$command_name" "$@"
}

boot_start() {
    ensure_repo
    boot_dir="$TERMUX_HOME/.termux/boot"
    boot_file="$boot_dir/fls-start.sh"
    mkdir -p "$boot_dir" 2>/dev/null || exit 1
    cat > "$boot_file" <<EOF
#!$TERMUX_PREFIX/bin/sh
exec "$TERMUX_PREFIX/bin/sh" "$SCRIPT_PATH" start
EOF
    chmod 755 "$boot_file" 2>/dev/null || true
    say "已生成 Termux:Boot 自启脚本：$boot_file"
    run_in_proot restart "$@"
}

boot_remove() {
    rm -f "$TERMUX_HOME/.termux/boot/fls-start.sh" 2>/dev/null || true
    say "已移除 Termux:Boot 自启脚本"
    run_in_proot restart "$@"
}

usage() {
    cat <<EOF
FLS-T（Termux proot 入口）

用法：
  sh fls-t.sh start [选项]
  sh fls-t.sh stop
  sh fls-t.sh restart [选项]
  sh fls-t.sh status
  sh fls-t.sh log
  sh fls-t.sh update
  sh fls-t.sh clone
  sh fls-t.sh ensure-repo
  sh fls-t.sh bstart
  sh fls-t.sh rstart

环境变量：
  FLS_PROOT_PROFILE=python|all   运行时类型，默认读取 ~/.fls-profile
  TERMUX_PACKAGE                 Termux 包名，默认 com.termux
  TERMUX_HOME / TERMUX_PREFIX    外部 Android 调用时覆盖路径
EOF
}

if [ "$#" -gt 0 ]; then
    case "$1" in
    -h|--help|help)
        usage
        exit 0
        ;;
    esac
fi

require_termux

if [ "$#" -eq 0 ]; then
    set -- status
fi

command_name="$1"
shift
case "$command_name" in
    ensure-repo|install)
        ensure_repo
        ;;
    clone)
        clone_repo || exit 1
        ;;
    update|upgrade|pull)
        ensure_repo
        update_repo
        ;;
    bstart|boot-start|enable-autostart)
        boot_start "$@"
        ;;
    rstart|remove-start|disable-autostart)
        boot_remove "$@"
        ;;
    start|stop|restart|status|log|logs)
        run_in_proot "$command_name" "$@"
        ;;
    *)
        run_in_proot "$command_name" "$@"
        ;;
esac
