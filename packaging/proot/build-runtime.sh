#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
用法：
  build-runtime.sh --arch arm64|armv7 --platform linux/arm64|linux/arm/v7 \
    --termux-arch aarch64|arm --profile python|all --output FILE
EOF
}

ARCH=""
PLATFORM=""
TERMUX_ARCH=""
PROFILE=""
OUTPUT=""
IMAGE="${FLS_RUNTIME_IMAGE:-debian:bookworm-slim}"
TERMUX_REPO="${TERMUX_REPO:-https://packages.termux.dev/apt/termux-main}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --arch) ARCH="$2"; shift 2 ;;
        --platform) PLATFORM="$2"; shift 2 ;;
        --termux-arch) TERMUX_ARCH="$2"; shift 2 ;;
        --profile) PROFILE="$2"; shift 2 ;;
        --output) OUTPUT="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "未知参数：$1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "$ARCH" ] || [ -z "$PLATFORM" ] || [ -z "$TERMUX_ARCH" ] || [ -z "$PROFILE" ] || [ -z "$OUTPUT" ]; then
    usage >&2
    exit 2
fi

case "$ARCH:$TERMUX_ARCH:$PROFILE" in
    arm64:aarch64:python|arm64:aarch64:all|armv7:arm:python|armv7:arm:all) ;;
    *) echo "架构或运行时配置无效：$ARCH/$TERMUX_ARCH/$PROFILE" >&2; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || { echo "需要 Docker" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "需要 curl" >&2; exit 1; }
command -v gzip >/dev/null 2>&1 || { echo "需要 gzip" >&2; exit 1; }
command -v dpkg-deb >/dev/null 2>&1 || { echo "需要 dpkg-deb" >&2; exit 1; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/rootfs" "$work/runtime/bin" "$work/runtime/lib" "$work/runtime/libexec/proot"

if [ "$PROFILE" = "all" ]; then
    extra_packages="bash git curl wget unzip zip xz-utils tar procps iproute2 dnsutils build-essential pkg-config libffi-dev libssl-dev nodejs php-cli ruby perl lua5.4 default-jre-headless"
else
    extra_packages=""
fi

docker run --rm --platform "$PLATFORM" \
    -e FLS_EXTRA_PACKAGES="$extra_packages" \
    -v "$work:/out" \
    "$IMAGE" \
    bash -euxo pipefail -c '
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends \
            ca-certificates tzdata python3 python3-pip python3-venv
        if [ -n "$FLS_EXTRA_PACKAGES" ]; then
            apt-get install -y --no-install-recommends $FLS_EXTRA_PACKAGES
            if [ ! -e /usr/bin/lua ] && [ -x /usr/bin/lua5.4 ]; then
                ln -s /usr/bin/lua5.4 /usr/bin/lua
            fi
        fi
        python3 -m venv /opt/fls-venv
        /opt/fls-venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check \
            Flask requests APScheduler PySocks
        rm -rf /var/lib/apt/lists/* /root/.cache /tmp/*
        # Docker may expose /etc/hostname as a read-only mount; it is harmless in the archive.
        rm -f /etc/hostname 2>/dev/null || true
        # Never archive container pseudo-filesystems or the bind-mounted output
        # directory. They are dynamic mounts, not part of the guest rootfs.
        tar --hard-dereference --numeric-owner --xattrs --acls \
            --exclude=./proc --exclude=./sys --exclude=./dev \
            --exclude=./run --exclude=./out --exclude=./etc/hostname \
            -czf /out/rootfs.tar.gz -C / .
    '

packages=(proot libandroid-shmem libtalloc)
curl -fsSL "$TERMUX_REPO/dists/stable/main/binary-$TERMUX_ARCH/Packages.gz" \
    -o "$work/Packages.gz"
gzip -dc "$work/Packages.gz" > "$work/Packages"
for package in "${packages[@]}"; do
    package_line="$(awk -v p="$package" 'BEGIN{RS="\n\n";FS="\n"} $0 ~ "^Package: "p"\n" {for(i=1;i<=NF;i++) if($i ~ /^Filename:/) {sub(/^Filename: /,"",$i); print $i; exit}}' "$work/Packages")"
    [ -n "$package_line" ] || { echo "Termux 仓库中找不到 $package ($TERMUX_ARCH)" >&2; exit 1; }
    curl -fsSL "$TERMUX_REPO/$package_line" -o "$work/$package.deb"
    dpkg-deb -x "$work/$package.deb" "$work/termux"
done

termux_usr="$work/termux/data/data/com.termux/files/usr"
cp -a "$termux_usr/bin/proot" "$work/runtime/bin/proot"
cp -a "$termux_usr/libexec/proot/loader" "$work/runtime/libexec/proot/loader"
if [ -f "$termux_usr/libexec/proot/loader32" ]; then
    cp -a "$termux_usr/libexec/proot/loader32" "$work/runtime/libexec/proot/loader32"
fi
cp -a "$termux_usr/lib/libandroid-shmem.so" "$work/runtime/lib/libandroid-shmem.so"
cp -a "$termux_usr/lib/libtalloc.so"* "$work/runtime/lib/"
chmod 0755 "$work/runtime/bin/proot" "$work/runtime/libexec/proot/loader" "$work/runtime/libexec/proot/loader32" 2>/dev/null || true

mkdir -p "$work/runtime/rootfs"
tar -xzf "$work/rootfs.tar.gz" -C "$work/runtime/rootfs"
mkdir -p "$work/runtime/rootfs"/{proc,sys,dev,run}
rm -f "$work/runtime/rootfs/etc/resolv.conf"
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$work/runtime/rootfs/etc/resolv.conf"
printf '%s\n' "$PROFILE" > "$work/runtime/.profile"
printf '%s\n' "$ARCH" > "$work/runtime/.arch"

mkdir -p "$(dirname "$OUTPUT")"
# Keep the final archive compatible with Android toybox/busybox tar.
tar --hard-dereference --numeric-owner -czf "$OUTPUT" -C "$work/runtime" \
    bin lib libexec rootfs .profile .arch
printf 'runtime=%s\nprofile=%s\narch=%s\n' "$OUTPUT" "$PROFILE" "$ARCH"
