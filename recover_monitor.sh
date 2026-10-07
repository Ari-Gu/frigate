#!/bin/bash
# 监控链路自动恢复：画面冻结（frigate 报错占位图 / 取不到帧）时依次重启 go2rtc → frigate 容器。
#
# 为什么需要它（2026-09-23 22:36 事故）：
#   Mac 空闲休眠 12 分钟（pmset sleep=10），醒来后：
#     · go2rtc 的小米 CS2/P2P 通道已断（22:49:01 报 udp i/o timeout）
#     · frigate 的采集 ffmpeg 断掉后**不会自己重连**，画面永远冻结在报错占位图上
#   结果：跌倒检测静默失效 20 分钟，期间只有一条"画面中断"告警（冷却 30 分钟，等于只响一次）。
#   手工恢复必须同时重启 go2rtc + frigate 两个组件（只重启 go2rtc 无效，实测），
#   因此固化成脚本，由 fall_alert_bridge.py 在检测到画面冻结时自动调用。
#
# 用法：./recover_monitor.sh          探测 → 必要时重启（退出码 0=画面正常 1=仍异常）
#       ./recover_monitor.sh --check  只探测不重启（退出码 0=正常 1=异常）
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
CAM="${CAM:-xiaomi_ptz_2k}"
FRIGATE="${FRIGATE_URL:-http://127.0.0.1:5001}"
ERROR_MD5="${FRIGATE_ERROR_FRAME_MD5:-c7b3c8f612faa361ff1a6441b9c4f1fc}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# 判据与 bridge 的 probe_frame_health 一致：报错占位图指纹 + 帧大小
# （不要用 go2rtc 的 /api/frame.jpeg：它依赖 ffmpeg，本机没装，健康时也返回 500）
probe() {
    local f sum size
    f="$(mktemp)"
    if ! curl -s --noproxy '*' -m 10 -o "$f" "$FRIGATE/api/$CAM/latest.jpg"; then
        rm -f "$f"; echo "取帧失败"; return 1
    fi
    sum="$(md5 -q "$f" 2>/dev/null)"
    size="$(wc -c < "$f" | tr -d ' ')"
    rm -f "$f"
    if [ "$sum" = "$ERROR_MD5" ]; then echo "frigate 报错占位图"; return 1; fi
    if [ "${size:-0}" -lt 2000 ]; then echo "帧过小(${size}B)"; return 1; fi
    echo "正常(${size}B)"; return 0
}

DETAIL="$(probe)" && { log "画面 ${DETAIL}，无需恢复"; exit 0; }
log "画面异常: $DETAIL"

if [ "${1:-}" = "--check" ]; then exit 1; fi

# --- 步骤 1/2：重启 go2rtc（launchd KeepAlive 会自动拉起，没托管则手动拉起）---
log "步骤 1/2：重启 go2rtc"
PID="$(pgrep -f 'go2rtc -c' | head -1)"
[ -n "$PID" ] && kill "$PID" 2>/dev/null
sleep 5
if ! pgrep -f "go2rtc -c" > /dev/null; then
    mkdir -p "$DIR/logs"
    nohup "$DIR/start_go2rtc.sh" >> "$DIR/logs/go2rtc.log" 2>&1 &
fi
sleep 12

DETAIL="$(probe)" && { log "恢复成功（重启 go2rtc）：画面 $DETAIL"; exit 0; }
log "go2rtc 重启后仍异常: $DETAIL"

# --- 步骤 2/2：重启 frigate 容器（采集 ffmpeg 断掉后不会自愈，必须重启容器）---
log "步骤 2/2：重启 frigate 容器"
docker restart frigate > /dev/null 2>&1
sleep 45

DETAIL="$(probe)" && { log "恢复成功（重启 go2rtc + frigate）：画面 $DETAIL"; exit 0; }
log "自动恢复失败，画面仍异常: ${DETAIL}（请人工检查摄像头电源与网络）"
exit 1
