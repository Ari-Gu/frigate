# 跌倒检测链路抢修报告 — 2026-09-21

## 结论

链路已恢复：**摄像头 → go2rtc → frigate → 跌倒判定 → 企业微信告警** 全通。
用户接手时摄像头已断流一整天（00:09 起无帧），实际是**三处独立故障叠加**，其中一处
会让跌倒检测"进程活着但完全不工作"。

## 修复前状态

| 组件 | 状态 |
|---|---|
| go2rtc (pid 602) | 活着，但每 10s 报 `read udp i/o timeout` |
| frigate (:5001) | 容器活着，`camera_fps = 0.0` |
| 桥接器 (pid 598) | 活着，但每 3s 抛 `too many values to unpack` |
| 摄像头 | 配置指向 `192.168.1.100`，该地址已失效 |

## 三处故障与修复

### 1. 摄像头 IP 漂移（断流根因）
配置里写死 `192.168.1.100`，摄像头实际漂到了 **`192.168.1.100`**。

关键点：**别再扫网段猜 IP**。本网络关是 TP-Link TL-7DR3610 易展版，易展组网会对下游设备
做 ARP 代答——`.100/.102/.103/.104/.107/.110` 六个 IP 全部应同一个 MAC，ping 通但没有
任何端口，纯代理应答（今天先按 `.104` 排查，白费一轮）。同理，不带 `--noproxy` 的
`curl http://192.168.1.100/` 返回的 502 是**本机代理的错误页**，不是设备响应。

权威来源是小米云（go2rtc 已登录该账号）：

```bash
curl -s --noproxy '*' "http://127.0.0.1:1984/api/xiaomi?id=52122802&region=cn"
# → ip: 192.168.1.100, mac: 94:F8:27:6F:0A:4A   （did 1038891701 未变）
```

改回正确地址后 go2rtc 立即建流，`camera_fps` 恢复到 5.0。

**新增自愈能力** `sync_camera_ip.sh`：查云端 IP → 比对配置 → 不一致就改写 `go2rtc.yaml`
并重启 go2rtc。已接入 `start_all.sh`；另有 `com.user.camip.plist` 每 5 分钟兜底。

### 2. 桥接器主循环崩溃（致命，画面恢复后才暴露）
```
[ERROR] 主循环异常: too many values to unpack (expected 2)   ← 每 3 秒一条
```
`geometric_fall()` 在 9-20 改成返回三元组 `(is_fallen, reason, near)`，但三处调用点没同步改。
异常被 `except Exception` 包住，所以**进程一直"正常运行"，而几何判定、视觉判定、告警全部没有执行**。
之前没暴露，是因为断流时没有 person 事件，那行代码走不到。

已修复主循环与 selftest 三处调用，并把"近失"标记打进了 `[GEO]` 日志。回归用例（躺平框/
直立框/空框/模型输出解析）全部通过，重启后 ERROR 归零。

> 教训：改函数返回值必须全局搜调用点；`except` 兜底会把功能性崩溃伪装成正常进程。

### 3. 小米云 STS 走错区域节点（重连慢，非致命）
`sts.api.io.mi.com` 本机默认 DNS 解析到**新加坡**节点，直连 12s 超时，导致 go2rtc 重启后
要反复重试约 1 分钟才能建流（日志刷 `TLS handshake timeout`）。国内节点实测仅 0.3~0.4s。
只影响重连速度，不影响已建立的 P2P 长连接，**列为可选优化**（需 sudo 改 /etc/hosts）。

## 当前状态（21:55 体检）

| 项 | 值 |
|---|---|
| go2rtc | pid 13216，流 `xiaomi_ptz` 拉自 192.168.1.100 |
| frigate | 0.17.1，:5001，`camera_fps 5.1`、`detection_fps 7.2` |
| 实时帧 | `latest.jpg` 195,838 字节（真实画面；占位图约 51KB） |
| 桥接器 | pid 13881，几何 + 视觉双通道，ERROR 0 |
| 视觉实测 | `agnes-3.0-flash` 11~24s，判 `FALLEN: no`（人靠沙发，正确） |
| 告警 | 21:18 流中断告警已送达企业微信（errcode 0） |

## 需要你做的两件事

1. **注册 IP 自动同步任务**（会话内 launchctl 权限不足，只能你手动执行一次）：
   ```bash
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.user.camip.plist
   ```
2. **根治 IP 漂移**：在 TP-Link 路由器给摄像头 MAC `94:F8:27:6F:0A:4A` 绑定 DHCP 静态租约
   （建议 Mac 也一起绑定）。

## 可选优化

- `/etc/hosts` 增加 `124.251.58.151 sts.api.io.mi.com`，重连时间从 ~60s 降到几秒。
- 真实跌倒场景仍未实地验证（需有人在镜头前躺下）；逻辑、模型、webhook 均已自测通过。
