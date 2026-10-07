#!/bin/bash
# 把跌倒告警链路重新挂回 launchd（fallalert + logclean）
# ---------------------------------------------------------------
# 为什么需要手动跑一次：WorkBuddy 的受限执行环境里 `launchctl bootstrap`
# 恒返回 "Bootstrap failed: 5: Input/output error"（连最小探针 plist 也一样），
# 所以 2026-09-30 改完 plist 后无法自动重新加载。在**本机 Terminal** 里跑本脚本即可。
#
# 用法：  bash /Users/mac/project/frigate/launchd_reload.sh
#
# 本脚本会：
#   1) 停掉可能由 nohup 手动拉起的桥进程（避免双进程 → 同一次跌倒推两条告警）
#   2) 安装最新 plist 到 ~/Library/LaunchAgents
#   3) bootout 旧实例 → bootstrap 新实例（fallalert 带 KeepAlive，会自动常驻）
#   4) 若 bootstrap 仍失败，退化为 nohup 直接拉起桥，保证监控不中断
set -u
U=$(id -u)
cd "$(dirname "$0")" || exit 1

echo "== 1) 停止手动实例 =="
if pgrep -f "fall_alert_bridge.py" > /dev/null; then
    pkill -f "fall_alert_bridge.py"
    echo "   已停止 pid $(pgrep -f 'fall_alert_bridge.py' | tr '\n' ' ')"
    sleep 2
else
    echo "   无运行中的桥进程"
fi

echo "== 2) 安装 plist =="
cp com.user.fallalert.plist com.user.logclean.plist ~/Library/LaunchAgents/ || exit 1
ls -la ~/Library/LaunchAgents/com.user.fallalert.plist ~/Library/LaunchAgents/com.user.logclean.plist

echo "== 3) 重新加载 =="
launchctl bootout gui/$U/com.user.fallalert 2>/dev/null
launchctl bootout gui/$U/com.user.logclean 2>/dev/null
sleep 1
OK=0
if launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.user.fallalert.plist; then
    echo "   fallalert 已加载（KeepAlive 常驻）"
    OK=1
else
    echo "   ⚠️ fallalert 加载失败"
fi
if launchctl bootstrap gui/$U ~/Library/LaunchAgents/com.user.logclean.plist; then
    echo "   logclean 已加载（每小时滚动+清理日志）"
else
    echo "   ⚠️ logclean 加载失败（不影响功能：桥自身每小时也会做同样的维护）"
fi

sleep 5
if [ "$OK" = "1" ] && pgrep -f "fall_alert_bridge.py" > /dev/null; then
    echo "== 完成：桥由 launchd 托管运行 =="
else
    echo "== 降级：nohup 直接拉起桥（无 KeepAlive，重启后请再跑一次本脚本）=="
    NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
    VISION_ENABLED=1 VISION_EVERY=15 \
    LOG_DIR="$PWD/logs" LOG_RETENTION_DAYS=7 LOG_MIRROR_STDOUT=1 \
    nohup /usr/bin/python3 -u "$PWD/fall_alert_bridge.py" \
        >> "$PWD/logs/fall_alert_bridge.console.log" 2>&1 &
    sleep 5
fi

pgrep -fl fall_alert_bridge
echo "== 查看日志： tail -f $PWD/logs/fall_alert_bridge.latest.log =="
