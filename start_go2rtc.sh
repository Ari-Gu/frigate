#!/bin/bash
# 以原生方式在 Mac 上启动 go2rtc（与小米摄像头处于同一局域网 192.168.1.x，
# CS2 P2P 才能直连；Docker Desktop for Mac 的 VM NAT 会阻断 P2P 的 UDP 回包）。
# frigate 通过 http://192.168.1.100:8554 拉流。
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/go2rtc_native"
exec ./go2rtc -c "$SCRIPT_DIR/go2rtc.yaml"
