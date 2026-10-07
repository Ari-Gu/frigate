#!/bin/bash
# 自动纠正 go2rtc.yaml 里的小米摄像头局域网 IP。
#
# 背景：摄像头走 DHCP，IP 会在 .100 / .104 / .106 之间漂移。一旦漂移，
# go2rtc 会持续报 "read udp [::]:xxxxx: i/o timeout"（P2P 打洞打到了空地址），
# frigate camera_fps 归零、latest.jpg 变成 "No frames" 占位图，跌倒告警失效。
#
# 原理：小米云里存着该设备最近上报的 localip，go2rtc 已用 xiaomi token 登录，
# 直接问 go2rtc 的 /api/xiaomi 接口即可拿到权威 IP（比 ping 扫段靠谱：
# 本网 TP-Link 易展组网会对离线设备做 ARP 代答，ping 通不代表设备在）。
#
# 用法：./sync_camera_ip.sh          查一次，IP 变了就改配置并重启 go2rtc
#       ./sync_camera_ip.sh --show   只打印当前配置 IP 与云端 IP，不改任何东西
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
CFG="$DIR/go2rtc.yaml"
USER_ID=52122802
REGION=cn
API="http://127.0.0.1:1984/api/xiaomi?id=${USER_ID}&region=${REGION}"
SHOW_ONLY=0
[ "${1:-}" = "--show" ] && SHOW_ONLY=1

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

cfg_ip() { grep -o 'xiaomi://[0-9]*:__REDACTED__@[0-9.]*' "$CFG" | head -1 | sed 's/.*@//'; }

# 单次尝试：STS 走海外节点时直连可能 12s+ 超时，一次失败不代表拿不到 IP，
# 所以外层做 3 次短超时重试，比一次 25s 长等待成功率更高（总耗时上限相近）。
cloud_ip_once() {
    curl -s --noproxy '*' -m 12 "$API" | /usr/bin/python3 -c '
import json, re, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
for s in d.get("sources", []):
    m = re.search(r"ip:\s*([0-9.]+)", s.get("info", ""))
    if m:
        print(m.group(1))
        break
' 2>/dev/null
}

cloud_ip() {
    local ip
    for i in 1 2 3; do
        ip="$(cloud_ip_once)"
        [ -n "$ip" ] && { echo "$ip"; return 0; }
        [ "$i" -lt 3 ] && sleep 2
    done
    return 1
}

CUR_IP="$(cfg_ip)"
NEW_IP="$(cloud_ip)"

if [ -z "$NEW_IP" ]; then
    # go2rtc 没运行 或 小米云 STS 超时（sts.api.io.mi.com 直连偶发 6s+ / TLS 握手超时）
    if [ "$SHOW_ONLY" = "1" ]; then
        log "配置 IP=${CUR_IP}；云端 IP 取不到（go2rtc 未运行或 STS 超时）"
        exit 2
    fi
    if ! pgrep -f "go2rtc -c" > /dev/null; then
        log "go2rtc 未运行 -> 先启动，再同步"
        mkdir -p "$DIR/logs"
        nohup "$DIR/start_go2rtc.sh" >> "$DIR/logs/go2rtc.log" 2>&1 &
        sleep 10
        NEW_IP="$(cloud_ip)"
    fi
    if [ -z "$NEW_IP" ]; then
        log "云端仍取不到 IP（STS 超时），保持现状，稍后重试"
        exit 2
    fi
fi

log "配置 IP=$CUR_IP  云端 IP=$NEW_IP"
[ "$SHOW_ONLY" = "1" ] && exit 0
[ "$CUR_IP" = "$NEW_IP" ] && log "无需改动" && exit 0

cp "$CFG" "$CFG.bak.$(date +%Y%m%d%H%M%S)"
/usr/bin/python3 - "$CFG" "$NEW_IP" <<'PY'
import re, sys
cfg, ip = sys.argv[1], sys.argv[2]
s = open(cfg).read()
new, n = re.subn(r'(xiaomi://\d+:__REDACTED__@)[0-9.]+', lambda m: m.group(1) + ip, s)
if n == 0:
    sys.exit('未匹配到 xiaomi:// 源，配置结构可能已变')
open(cfg, 'w').write(new)
PY
log "已把 go2rtc.yaml 的摄像头地址更新为 $NEW_IP，重启 go2rtc"

PID="$(pgrep -f 'go2rtc -c' | head -1)"
[ -n "$PID" ] && kill "$PID" 2>/dev/null
sleep 3
# launchd KeepAlive 会自动拉起；若没被托管则手动拉起
if ! pgrep -f "go2rtc -c" > /dev/null; then
    mkdir -p "$DIR/logs"
    nohup "$DIR/start_go2rtc.sh" >> "$DIR/logs/go2rtc.log" 2>&1 &
fi
sleep 12
if pgrep -f "go2rtc -c" > /dev/null; then
    log "go2rtc 已重启（摄像头 $NEW_IP）"
else
    log "警告：go2rtc 未起来，请手动执行 start_go2rtc.sh"
    exit 1
fi
