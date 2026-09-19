#!/system/bin/sh

FLS_A="/data/adb/fls-a.sh"
command="${1:-status}"

if [ ! -x "$FLS_A" ]; then
    echo "FLS 尚未安装或调用脚本不存在：$FLS_A" >&2
    exit 1
fi

shift
exec sh "$FLS_A" "$command" "$@"
