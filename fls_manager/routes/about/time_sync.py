from datetime import timezone

from flask import request

from . import bp
from .helpers import (
    timezone_from_offset,
    fetch_network_utc_time,
    parse_custom_time_with_offset,
    render_time_sync_result,
)
from ...config import (
    get_timezone_offset_hours,
    set_panel_time_calibration,
    reset_panel_time_calibration,
)
from ...scheduler import reload_scheduler


@bp.route("/about/time-sync", methods=["POST"])
def about_time_sync():
    mode = request.form.get("mode", "").strip()
    custom_time = request.form.get("custom_time", "").strip()
    utc_offset = request.form.get("utc_offset", "8").strip()

    try:
        if mode == "beijing":
            offset = 8
            tz = timezone_from_offset(offset)
            network_utc = fetch_network_utc_time()
            virtual_now = network_utc.astimezone(tz)

            result = set_panel_time_calibration(
                offset_hours=offset,
                virtual_now=virtual_now,
            )

            reload_scheduler()

            return render_time_sync_result(
                "北京时间校准完成",
                True,
                "已自动校准为北京时间，未修改系统时间",
                (
                    f"网络 UTC：{network_utc.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"北京时间：{virtual_now.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        if mode == "utc_offset":
            offset = int(utc_offset)
            offset = max(-23, min(23, offset))
            tz = timezone_from_offset(offset)

            network_utc = fetch_network_utc_time()
            virtual_now = network_utc.astimezone(tz)

            result = set_panel_time_calibration(
                offset_hours=offset,
                virtual_now=virtual_now,
            )

            reload_scheduler()

            return render_time_sync_result(
                f"UTC{offset:+d} 时间校准完成",
                True,
                f"已按 UTC{offset:+d} 校准面板虚拟时间，未修改系统时间",
                (
                    f"网络 UTC：{network_utc.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"UTC{offset:+d}：{virtual_now.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        if mode == "custom":
            offset = int(utc_offset)
            offset = max(-23, min(23, offset))

            virtual_now = parse_custom_time_with_offset(custom_time, offset)

            result = set_panel_time_calibration(
                offset_hours=offset,
                virtual_now=virtual_now,
            )

            reload_scheduler()

            return render_time_sync_result(
                "自定义时间校准完成",
                True,
                f"已按 UTC{offset:+d} 应用自定义面板时间，未修改系统时间",
                (
                    f"输入时间：{custom_time}；"
                    f"UTC{offset:+d}：{virtual_now.strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"换算 UTC：{virtual_now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}；"
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        if mode == "reset":
            offset = get_timezone_offset_hours()

            result = reset_panel_time_calibration(offset)

            reload_scheduler()

            return render_time_sync_result(
                "面板时间偏移已重置",
                True,
                "已清除面板虚拟时间偏移，仅保留当前 UTC 时区设置",
                (
                    f"面板时区：{result.get('timezone_text')}；"
                    f"面板时间偏移秒数：{result.get('panel_time_offset_seconds')}"
                ),
            )

        return render_time_sync_result(
            "时间校准失败",
            False,
            "未知的时间校准方式",
        ), 400

    except Exception as e:
        return render_time_sync_result(
            "时间校准失败",
            False,
            str(e),
            "提示：当前方案不修改系统时间。请检查网络或输入格式。",
        ), 400
