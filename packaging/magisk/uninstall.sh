#!/system/bin/sh

MODDIR="${0%/*}"
FLS_A="/data/adb/fls-a.sh"

if [ -x "$FLS_A" ]; then
    sh "$FLS_A" stop >/dev/null 2>&1 || true
fi

# Runtime, project data and logs intentionally remain in /data/fls for reinstall/upgrade reuse.
if [ -f "$FLS_A" ] && cmp -s "$MODDIR/fls-a.sh" "$FLS_A" 2>/dev/null; then
    rm -f "$FLS_A"
fi

SERVICE_D="/data/adb/service.d/fls-a.sh"
if [ -f "$SERVICE_D" ] && cmp -s "$MODDIR/fls-a.sh" "$SERVICE_D" 2>/dev/null; then
    rm -f "$SERVICE_D"
fi
