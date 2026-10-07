# agnes-ai 视觉模型适配测试报告

- 日期：2026-09-17
- 端点：`https://apihub.agnes-ai.com/v1`（Cloudflare CDN，本机直连可达，1.5s）
- Key：ENV 文件 `AGNES_KEY`（已接入 `free_vision.py` / `config.yaml` / `docker-compose.yaml`）
- 测试图：`/tmp/falltest/{fallen,standing,sitting,empty}.png`（640×360 合成场景，220 image_tokens/张）

## 1. 端点与鉴权

| 检查项 | 结果 |
|---|---|
| `/v1/models` 无 Authorization | **401 Token not provided** → 说明是真实鉴权端点，可用作 key 有效性判据 |
| `/v1/models` 带 Key | 200，返回 12 个 agnes 自研模型 |
| `/v1/chat/completions` 带 Key | 200，图片理解正常 |

> 与 NVIDIA `integrate.api.nvidia.com` 不同，agnes 的 `/v1/models` **不是**公开端点，无需担心"列表 200 但业务 403"的坑。

## 2. 模型清单与可用性（12 个）

| 模型 | 类型 | 实测结论 |
|---|---|---|
| **agnes-3.0-flash** | 多模态对话（非推理） | ✅ **主选**，判定全对，2~12s（中位 8s），输出 ~20 token |
| **agnes-2.5-flash** | 多模态（推理型） | ✅ 备选，5~7s 稳定，空场景不幻觉；reasoning 占 ~368 token |
| agnes-2.5-pro | 多模态 | ❌ 403 `insufficient_user_quota`（余额 $0.000000） |
| agnes-2.5-pro-beta / pro-alpha | 多模态 | ❌ 403 配额为 0 |
| agnes-2.0-flash | 多模态（推理型） | ⚠️ 免费额度限流严（429），max_tokens 小则空输出 |
| agnes-2.5-pro-alpha | 多模态 | ❌ 403 |
| agnes-image-2.0/2.1/2.5-flash | 图像生成 | — 非本场景 |
| agnes-video-2.5 / -2.5-flash / -v2.0 | 视频生成 | — 非本场景 |

## 3. 四场景判定精度

| 场景 | agnes-3.0-flash | agnes-2.5-flash |
|---|---|---|
| 躺卧（fallen） | ✅ FALLEN: yes | ✅ FALLEN: yes |
| 站立（standing） | ✅ FALLEN: no | ✅ FALLEN: no |
| 坐姿（sitting） | ✅ FALLEN: no | ✅ FALLEN: no |
| 空房间（empty） | ⚠️ FALLEN: no，但幻觉"有人坐在地上" | ✅ "画面中没有人" |

**结论：两者 4/4 主判定全对，无误报。** 3.0-flash 在空场景会脑补人形，但输出仍为 `FALLEN: no`，不会触发误告警；追求零幻觉可切 2.5-flash。

## 4. 延迟分布（agnes-3.0-flash，14 次采样）

`2.1 / 2.2 / 3.3 / 3.4 / 3.9 / 4.0 / 5.0 / 5.7 / 6.3 / 8.0 / 8.6 / 11.2 / 12.3 / 19.2 / 31.9 s`

- 中位 ≈ 6s，P75 ≈ 8.6s，P90 ≈ 12.3s，长尾偶发 19~32s
- 因此 `VISION_DEADLINE` 从 12s 放宽到 **18s**：12s 会频繁触发无谓切换（切过去还要再花 5~7s）

## 5. 已落地的代码改动

1. **`free_vision.py`**
   - 新增 `agnes` provider（置于候选池首位），Key 从 ENV 的 `AGNES_KEY` 读取
   - `load_env_file()` 支持 `key=value` 写法（原来只认 `key: value`，导致 `AGNES_KEY=sk-...` 被漏掉）
   - 新增 `MODEL_MAX_TOKENS`：推理型模型给 700（原来 150 会返回空 content）
   - `_call()` 空 content 时回退读 `reasoning_content`
   - `VISION_DEADLINE` 默认 12s → 18s
2. **`fall_alert_bridge.py`**
   - `_interpret()` 标记容错：实测模型出现过 `FALSEN: yes` 拼写漂移，正则改为 `\bfa(?:ll|lse|ls)\w*\s*[:：]?\s*(yes|no)\b`
3. **`config/config.yaml` / `config.yaml` / `docker-compose.yaml`**
   - genai 切到 agnes 端点 + `agnes-3.0-flash`

## 6. 验证命令

```bash
cd /Users/mac/project/frigate
python3 free_vision.py --list                     # 确认 agnes 两个槽位 OK
python3 free_vision.py --ask /tmp/falltest/fallen.png   # 单图端到端
python3 fall_alert_bridge.py --vision-list
```

实测输出：`agnes/agnes-3.0-flash 2964ms -> FALLEN: yes — the person is lying flat on the ground horizontally.`

## 7. 复检（22:12 全链路）

| 环节 | 状态 | 说明 |
|---|---|---|
| Docker / frigate | ✅ | 27.4.0；frigate 0.17.1 healthy，宿主端口 **5001** |
| frigate API | ✅ | `/api/stats`、`/api/events` 正常，历史 person 事件可读 |
| frigate → go2rtc 地址 | ✅ 已修 | 原 `192.168.1.100` 是过期 DHCP 地址 → 改为 `192.168.1.100`（容器内 TCP 连通已验证） |
| go2rtc | ⚠️ | 容器版拿不到小米流（UDP i/o timeout），已停用；改原生 `start_go2rtc.sh` 运行（darwin/amd64） |
| 摄像头 | ❌ **离线** | 192.168.1.100 ping 100% 丢包，不在存活主机列表中；LAN 存活：1/100/101/104/106/110/111(小米路由器)/112 |
| agnes-3.0-flash | ✅ | 7.8s，`FALLEN: yes` 判定正确 |
| `FRIGATE_URL` | ✅ | 默认已是 5001，无需改 |

**当前唯一阻塞项：摄像头离线。** 需在米家 App 或小米路由器 DHCP 列表确认摄像头新 IP（或检查是否断电/掉 WiFi），拿到后：
1. 改 `go2rtc.yaml` 里 `xiaomi://...@<新IP>`；
2. 重启原生 go2rtc；
3. 建议给 Mac 与摄像头在路由器上做 DHCP 静态租约，避免再次因 IP 变动断流。

go2rtc 常驻（可选）：`launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.user.go2rtc.plist`
（plist 已就位；在自动化会话内 launchctl 报 `5: Input/output error`，需用户手动执行）
