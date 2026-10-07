# Frigate 跌倒检测告警桥接

基于 **go2rtc + Frigate 0.17 + 本地视觉模型** 的家庭跌倒检测链路。Frigate 负责取流与目标检测，
本项目负责"判定 + 告警"：把画面交给免费视觉模型理解，用几何特征与姿态描述双通道交叉验证，
命中后经飞书机器人推送**文字 + 现场截图**。

## 架构

```
小米摄像头 ──▶ go2rtc (:1984/:8554) ──▶ Frigate (:5001)
                                          │  events / snapshot
                                          ▼
                              fall_alert_bridge.py
                          ┌───────────────┴───────────────┐
                     几何通道                         AI 视觉通道
                  宽高比 + 重心 + 静止            free_vision.py（多模型池）
                  需 FALL_FRAMES=3 次             强信号 1 次 / 弱信号 2 次
                          └───────────────┬───────────────┘
                                          ▼
                                飞书机器人（文字 + 截图）
```

## 快速开始

```bash
cp .ENV.example .ENV      # 填入自己的飞书 webhook / 摄像头地址
cp feishu_app.conf.example feishu_app.conf   # 可选：飞书发图需要应用凭证
python3 fall_alert_bridge.py
```

开机自启：`launchd_reload.sh` 会重载 `com.user.fallalert` 等 5 个 plist。
本仓库的 plist 里的路径是模板，需按本机实际路径改。

## 判定策略

信号分**强 / 弱**两档，用不同的确认次数，兼顾"不漏报"与"不误报"：

| 信号 | 含义 | 确认次数 |
|---|---|---|
| `strong` | 模型判 `FALLEN: yes`，或描述人躺/趴/瘫倒在地 | `GENAI_FRAMES`=1 |
| `weak` | 坐/跪/蹲在地上这类"软升级"，及"疑似贴地"兜底 | `GENAI_WEAK_FRAMES`=2 |
| 几何 | 包围框宽高比 + 重心 + 静止 | `FALL_FRAMES`=3 |

弱信号还有一层**去抖**：两次弱命中相隔必须 ≥ `WEAK_MIN_GAP`（默认 20s）才算独立观测。
否则模型把同一个姿势连续两帧误读成"坐在地上"，就会攒成 2/2 误报。

### 误报防线（都是实测事故驱动的）

| 日期 | 误报描述 | 防线 |
|---|---|---|
| 09-18 | `reclining on a sofa ... not ON THE FLOOR` | 先剥离否定语境再判断贴地 |
| 09-23 | `sitting on a sofa with their legs up on the floor` | 肢体贴地 / 家具豁免 |
| 09-30 | 一人坐沙发 + 一人坐地上（多主体） | 家具豁免不覆盖"姿态词紧贴地板" |
| 10-01 | 单帧把坐沙发误读成坐地上 | 弱信号需累计 2 次确认 |
| 10-07 | `sitting on a small stool on the floor` | 姿态词的**支撑面**若是家具则不升级 |
| 10-07 | 相隔 16s 的两次同帧幻觉凑成 2/2 | `WEAK_MIN_GAP` 去抖 |

## 回归测试

改动 `_interpret_core()` 后**必须**跑完 5 个套件：

```bash
for f in test_*.py; do python3 $f; done
```

- `test_interpret_cases.py` — 16 例真实误报/漏报文案
- `test_weak_signal.py` — 26 项信号分级与去抖门控
- `test_alert_policy.py` — 58 项告警策略与文案
- `test_bridge_changes.py` — 桥接改动与静默/节流
- `test_log_rotation.py` — 28 项日志滚动与归档

## 目录

| 文件 | 作用 |
|---|---|
| `fall_alert_bridge.py` | 主循环：取事件 → 双通道判定 → 告警 |
| `free_vision.py` | 多 Provider 免费视觉模型池，超时自动切换 |
| `log_maintenance.py` | 日志按天滚动、压缩归档、7 天保留 |
| `sync_camera_ip.sh` | 摄像头 IP 变动时自动同步 go2rtc/Frigate |
| `recover_monitor.sh` / `start_all.sh` | 自愈与一键启动 |

## 注意

- 本仓库**不含**任何密钥。`.ENV`、`feishu_app.conf` 已被 `.gitignore` 排除，
  `.ENV.example` / `feishu_app.conf.example` 是模板。
- 摄像头 IP、小米账号、设备 DID、飞书 webhook 均已替换为占位值，部署前需替换。
