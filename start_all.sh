#!/bin/bash
# 一键拉起整条跌倒检测链路：go2rtc（原生，负责拉小米摄像头）→ 跌倒告警桥接器。
#
# 顺序很重要：先 go2rtc 再 frigate。go2rtc 没起时 frigate 会一直
# "Connection timed out"，且重启后 latest.jpg 会返回 "No frames received" 占位图。
#
# 日志落盘到 logs/ ，桥接器每条日志带 HH:MM:SS 时间戳，便于事后按分钟排查。
set -u
cd "$(dirname "$0")"
mkdir -p logs

# 本机有全局代理，会拦截 127.0.0.1 的请求，必须绕过
export NO_PROXY=127.0.0.1,localhost
export no_proxy=127.0.0.1,localhost
export VISION_EVERY="${VISION_EVERY:-15}"

if pgrep -f "go2rtc -c" > /dev/null; then
    echo "go2rtc   : 已在运行 (pid $(pgrep -f 'go2rtc -c' | head -1))"
else
    nohup ./start_go2rtc.sh >> logs/go2rtc.log 2>&1 &
    echo "go2rtc   : 已启动 (pid $!)"
fi

sleep 6

# 摄像头走 DHCP，IP 会漂移（.100/.104/.106）；漂移后 go2rtc 会一直 UDP 超时。
# 用小米云上报的 localip 自动对齐配置，再起桥接器（IP 变了脚本会重启 go2rtc，等它稳一秒）。
./sync_camera_ip.sh | sed 's/^/IP 同步  : /'
sleep 3

if pgrep -f fall_alert_bridge.py > /dev/null; then
    echo "桥接器   : 已在运行 (pid $(pgrep -f fall_alert_bridge.py | head -1))"
else
    nohup /usr/bin/python3 -u fall_alert_bridge.py >> logs/fall_alert.log 2>&1 &
    echo "桥接器   : 已启动 (pid $!)  VISION_EVERY=${VISION_EVERY}s"
fi

echo "日志     : logs/go2rtc.log | logs/fall_alert.log"
echo "查看     : tail -f logs/fall_alert.log"
