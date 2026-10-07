#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Frigate 跌倒告警桥接器 (Phase 3)
================================
把 Frigate 的"人物检测"转换为"跌倒告警"，并通过**飞书**群机器人 Webhook 推送。
（2026-09-30 起告警通道由企业微信切换为飞书；post_webhook 内做协议适配，
 两类 webhook 可混配，见 `webhook_kind()`。）

两条检测通道（互为补充）：
  1) 几何判定 (GEOMETRIC, 默认开启, 无需任何 API Key)
     - 轮询 /api/events 取当前被追踪人物的归一化包围框 box
     - 跌倒特征：框"宽 > 高"(躺平) + 重心偏下(在画面下半部) + 静止
     - 需连续 FALL_FRAMES 次轮询都满足条件才触发，避免瞬时蹲下/弯腰误报
  2) GenAI 判定 (可选, 需要有效的 OPENAI 兼容视觉模型 Key)
     - 读取事件详情的 data.description（frigate 0.17 无 GET 描述端点，详见函数注释）
     - 无 Key / 容器未启用 GenAI 时该通道自动失效，不影响几何通道

告警推送：
  - 先发 markdown 文本告警（相机 / 时间 / 判定方法 / 理由）
  - 再上传现场截图并通过 image 消息发出（视觉确认）
  - 截图上传失败则降级为纯文本告警

集中配置（2026-10-02 新增）：
  脚本同目录的 `.ENV` 文件是可调参数的**集中来源**（KEY=VALUE，每行一条）。
  启动时自动读入并注入运行环境，因此下面列出的环境变量都可以写在 `.ENV` 里；
  优先级 真实环境变量 > `.ENV` > 代码默认值（想让文件压过环境变量：DOTENV_PRIORITY=file）。
  六类内容：① 摄像头连接参数 ② 监控使用的大模型 ③ 判断方式
            ④ 监控频率 ⑤ 告警频率 ⑥ 敏感度参数
  核对当前生效值：`python3 fall_alert_bridge.py --env-check`
  ⚠️ 改完 `.ENV` 需重启告警桥：`launchctl kickstart -k gui/$(id -u)/com.user.fallalert`
  ⚠️ 同一键**不要**在 launchd plist 里重复声明，否则 plist 会盖住 `.ENV`。
  （webhook / 飞书应用密钥不放这里，走 `feishu_app.conf`。）

环境变量 (均可选, 见下方默认值)：
  FRIGATE_URL      默认 http://127.0.0.1:5001
  ALERT_WEBHOOKS    告警 webhook 完整 URL, 多个用逗号/空白分隔
                    （告警会同时推送到所有群, 单个失败不影响其它群）
                    自动识别通道类型：飞书 /open-apis/bot/v2/hook/ 走飞书协议,
                    企业微信 qyapi.weixin.qq.com 走企业微信协议
  WECHAT_WEBHOOKS   旧名（企业微信时代）, 仅在未设 ALERT_WEBHOOKS 时生效, 兼容保留
  WECHAT_WEBHOOK    更旧的单数名, 兜底兼容
  CAMERAS          逗号分隔相机名, 默认 xiaomi_ptz_2k (ALL=全部)
  POLL_INTERVAL    轮询间隔秒, 默认 3
  FALL_FRAMES      需连续满足跌倒条件的轮询次数, 默认 3
  FALL_ASPECT      宽高比阈值(>=触发), 默认 1.2
  FALL_LOWER       重心 y 阈值(>=触发, 0~1), 默认 0.45
  ALERT_COOLDOWN   同相机告警冷却秒, 默认 120
  FALL_KEYWORDS    GenAI 关键词(逗号), 默认 fallen,fell,lying on the ground,on the floor,collapsed,unconscious,跌倒,摔倒,倒地,躺地,晕倒,瘫倒,坠倒
  VISION_ENABLED   是否启用免费视觉模型通道, 默认 1
  VISION_EVERY     同一事件多久重新问一次模型(秒), 默认 10
  VISION_DEADLINE  单次模型调用硬超时(秒), 超时即切换下一个模型, 默认 18
  VISION_SLOW_MS   慢响应阈值(毫秒), 超过则降级该模型, 默认 8000
  VISION_MAX_TRIES 一次请求最多尝试几个模型, 默认 3
  VISION_COOLDOWN  降级模型冷却时长(秒), 默认 300
  FRAME_MIN_BYTES  有效帧最小字节数, 默认 2000
  FRAME_RETRIES    单个取帧来源重试次数, 默认 3
  MIN_CAM_FPS      低于该采集帧率视为流已断并跳过视觉判定, 默认 1.0
  FALL_WINDOW      跌倒确认的时间窗口(秒), 默认 12
  STREAM_WATCH     是否启用视频流中断监测, 默认 1
  STREAM_ALERT_SEC 连续无有效画面超过多少秒才告警, 默认 1800（30 分钟）
  STREAM_ALERT_COOLDOWN 断流告警的重复间隔秒, 默认 600（10 分钟）
  STREAM_ALERT_SILENT   静默断流告警, 默认 0。置 1 则只写日志不推送（见下）
  STREAM_SILENCE_FILE   静默标记文件, 默认 <脚本目录>/stream_alert.silent
                        文件存在即静默；内容为空=永久静默, 或写 "+90m"/"+2h"/
                        "2026-10-01 08:00" 形式的到期时间。命令行开关：
                        --silence-stream [分钟] / --unsilence-stream / --stream-status
                        ⚠️ 静默只影响"视频流类通知"（断流告警 / 自愈结果），
                           跌倒告警、休眠唤醒提醒**永远不受静默影响**。
  FRIGATE_ERROR_FRAME_MD5  frigate 报错占位图 md5(升级 frigate 后可能需更新)
  STREAM_RECOVER   画面异常时自动执行 recover_monitor.sh 自愈, 默认 1
  STREAM_RECOVER_AFTER    画面异常持续多少秒后开始自愈, 默认 60
  STREAM_RECOVER_COOLDOWN 两次自愈的最小间隔秒, 默认 300
  RECOVER_SCRIPT   自愈脚本路径, 默认 ./recover_monitor.sh
  RECOVER_TIMEOUT  自愈脚本总超时秒, 默认 180
  WAKE_GAP_SEC     主循环停顿多久判定为"系统休眠"并推送告知, 默认 60
  STALL_GAP_SEC    仅卡顿(非休眠)的停顿阈值秒, 默认 120
  WAKE_NOTICE      休眠唤醒后是否推送告知消息, 默认 1
  KEEP_AWAKE       是否常驻 caffeinate 阻止系统休眠, 默认 1（监控主机必须常醒）

日志（2026-09-30 改造，见下方"日志"区块）：
  日志按天落盘到 logs/fall_alert_bridge-YYYY-MM-DD.log，时间戳带完整日期。
  保留期默认 7 天，启动时与每小时自动清理超期文件；
  logs/fall_alert_bridge.latest.log 始终指向当天文件，便于 tail -f。
  LOG_DIR            日志目录, 默认 <脚本目录>/logs
  LOG_RETENTION_DAYS 日志保留天数, 默认 7
  LOG_MIRROR_STDOUT  是否同时把日志打到标准输出, 默认 1
                     （launchd 下建议置 0，避免与 launchd 的 StandardOutPath 重复写盘）
  LOG_PRUNE_EVERY    自动清理的间隔秒, 默认 3600
  跨服务的日志滚动/清理由 log_maintenance.py 统一负责（launchd: com.user.logclean）。

  <PROVIDER>_KEY   视觉模型 Key: AIHUBMIX_KEY / ZHIPU_KEY / SILICONFLOW_KEY /
                   DASHSCOPE_KEY / OPENROUTER_KEY（aihubmix 默认读 ENV 文件）

告警策略（2026-09-30 新增）：
  FLOOR_ALERT_STRICT  贴地姿态严格策略, 默认 1
                      1=严格：模型描述里"人本身贴地"（sitting/lying/kneeling… 紧接
                        on the floor）即便模型判 no 也升级告警（坐在地上也告警）
                      0=宽松：只认明确倒地姿态（lying/collapsed…），坐在地上不告警
  FLOOR_POSTURE_GAP   姿态词到 "on the floor" 的最大字符间隔, 默认 20
  MULTI_PERSON_MODE   多人同框策略, 默认 geo
                      geo=只忽略几何通道（AI 视觉通道照常判定）
                      all=画面里 ≥2 人时整条忽略；off=不忽略
  MULTI_PERSON_MIN    判定"同框多人"的人数下限, 默认 2

飞书发图（2026-09-30 新增）：
  飞书自定义机器人 webhook 只能发文本/富文本/卡片，**发图片必须先用应用凭证**
  把图上传到开放平台换 image_key，再用 webhook 的 image 消息发出。
  配置 FEISHU_APP_ID / FEISHU_APP_SECRET（自建应用需开启机器人能力 + im:resource
  权限）后，跌倒告警会自动带上现场截图；未配置时降级为只发文字，告警不丢。
  凭证两种放法：环境变量（plist 的 EnvironmentVariables），或直接写进
  脚本同目录的 feishu_app.conf（KEY=VALUE 每行一条；环境变量优先）。
  配好后用 `--test-image` 一条命令即可验证整条发图链路。

用法：
  python3 fall_alert_bridge.py                # 正常运行
  python3 fall_alert_bridge.py --selftest     # 自测：模拟一次跌倒并真实推送 webhook
  python3 fall_alert_bridge.py --bench        # 免费视觉模型连通性 + 延迟基准
  python3 fall_alert_bridge.py --vision-list  # 查看模型候选池与健康状态
  python3 fall_alert_bridge.py --debug-events # 打印一次原始 events JSON(便于确认字段)
  python3 fall_alert_bridge.py --test-webhook # 给当前配置的告警通道发一条测试消息
  python3 fall_alert_bridge.py --stream-status        # 打印流健康 + 静默状态(不推送)
  python3 fall_alert_bridge.py --silence-stream [分钟] # 静默断流告警(默认永久)
  python3 fall_alert_bridge.py --unsilence-stream      # 取消静默
  python3 fall_alert_bridge.py --logs         # 列出日志文件 / 保留策略 / 当前指向
  python3 fall_alert_bridge.py --prune-logs   # 立即执行一次超期日志清理
  python3 fall_alert_bridge.py --test-image [图片路径]  # 验证飞书发图链路
                                              # （不给路径则用当前相机快照）
  python3 fall_alert_bridge.py --env-check    # 打印 .ENV 各项的当前生效值
"""
import os
import re
import sys
import time
import json
import uuid
import hashlib
import base64
import urllib.request
import urllib.error
import urllib.parse
import subprocess
import io

# ---------- 集中配置文件 .ENV（2026-10-02 新增） ----------
# 把监控服务的可调参数（摄像头 / 大模型 / 判断方式 / 监控频率 / 告警频率 / 敏感度）
# 集中到脚本同目录的 `.ENV`（KEY=VALUE，dotenv 风格），启动时读入并注入 os.environ，
# 于是下面所有 `os.environ.get(...)` 常量自动被这个文件驱动 —— 这就是"键值映射到服务"。
# 优先级：真实环境变量 > .ENV 文件 > 代码默认值 —— 这是 dotenv 的标准约定，
#        也是本项目的既定调试方式（用环境变量临时压过配置，跑完即恢复）；
#        想让文件反过来压过环境变量，设 DOTENV_PRIORITY=file。
# ⚠️ 因为环境变量优先，launchd plist 里**不要**再重复声明 .ENV 已管的键，
#    否则 plist 的旧值会把 .ENV 的新值盖住（2026-10-02 已把 VISION_ENABLED /
#    VISION_EVERY 从 com.user.fallalert.plist 移除，只留 NO_PROXY/PATH/LOG_* 等基础设施变量）。
# 自检命令：python3 fall_alert_bridge.py --env-check
HERE_DIR     = os.path.dirname(os.path.abspath(__file__))
DOTENV_FILE  = os.environ.get("DOTENV_FILE") or os.path.join(HERE_DIR, ".ENV")
_ENV_KEY_RE  = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# 行尾注释：# 前面必须有空白才算注释，于是值里出现 `abc#def` 这类内容不会被误切
_ENV_CMT_RE  = re.compile(r"\s+#")


def _strip_env_value(v):
    """取出 `KEY=` 右侧的值：先剥行尾注释，再去两端引号。

    整行值被引号包住时按字面量处理（`KEY="a # b"` 不会被当注释切掉）；
    否则在第一个"空白+#"处截断，再去掉包裹引号。
    """
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    m = _ENV_CMT_RE.search(v)
    if m:
        v = v[:m.start()]
    return v.strip().strip('"').strip("'")


def load_dotenv(path=None):
    """解析 .ENV（`KEY=VALUE  # 功能注释` 每行一条）并注入 os.environ，返回 {键: 值}。

    只认"合法标识符键名 + 等号"的行：因此文件里的说明文字、`url:` 这类冒号行
    都不会污染环境变量（同一目录的历史 ENV 文件是 `key: value` 混排格式）。
    """
    path = path or DOTENV_FILE
    loaded = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return loaded
    except OSError as e:
        print(f"[ENV] 读取 {path} 失败：{e}", file=sys.stderr)
        return loaded

    override = str(os.environ.get("DOTENV_PRIORITY", "env")).strip().lower() == "file"
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if not _ENV_KEY_RE.match(k):
            continue
        v = _strip_env_value(v)
        loaded[k] = v
        if override or k not in os.environ:
            os.environ[k] = v
    return loaded


DOTENV_VALUES = load_dotenv()


def _go2rtc_camera_ip():
    """从 go2rtc.yaml 取实际配置的相机 LAN 地址（用于与 .ENV 的 CAMERA_IP 交叉核对）。

    真正的权威值仍是 go2rtc 运行时的 producer remote_addr；这里只做静态比对，
    用于发现".ENV 登记值与实际取流地址不一致"，不参与取流逻辑。
    """
    try:
        with open(os.path.join(HERE_DIR, "go2rtc.yaml"), "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return None
    m = re.search(r"xiaomi://[^\s@]*@(\d{1,3}(?:\.\d{1,3}){3})", txt)
    return m.group(1) if m else None


def cmd_env_check():
    """打印 .ENV 里的每一项及其**生效值**，确认键值确实映射到了服务。"""
    print("=" * 72)
    print("监控配置核对：.ENV → 服务生效值")
    print("=" * 72)
    exists = os.path.exists(DOTENV_FILE)
    print(f"配置文件 : {DOTENV_FILE}")
    print(f"文件状态 : {'存在' if exists else '不存在（服务将回退到 plist/默认值）'}")
    print(f"解析键数 : {len(DOTENV_VALUES)}")
    print(f"优先级   : {'文件覆盖环境变量' if str(os.environ.get('DOTENV_PRIORITY','env')).lower()=='file' else '环境变量覆盖文件'}"
          f"（DOTENV_PRIORITY={os.environ.get('DOTENV_PRIORITY','env')}）")

    # 相机地址交叉核对（.ENV 登记值 vs go2rtc.yaml 实际取流地址）
    cfg_ip = DOTENV_VALUES.get("CAMERA_IP") or os.environ.get("CAMERA_IP") or ""
    real_ip = _go2rtc_camera_ip()
    if cfg_ip or real_ip:
        flag = "✅ 一致" if (cfg_ip and real_ip and cfg_ip == real_ip) else "⚠️ 不一致"
        print(f"相机地址 : .ENV={cfg_ip or '(未登记)'} | go2rtc.yaml={real_ip or '(未读到)'}  {flag}")
        if cfg_ip and real_ip and cfg_ip != real_ip:
            print("           → 取流以 go2rtc.yaml 为准；sync_camera_ip.sh 会自动纠偏，"
                  "也可同步修正 .ENV 的 CAMERA_IP。")

    # 按 .ENV 里的小节标题分组打印"键 = 生效值"
    print("-" * 72)
    try:
        with open(DOTENV_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        lines = []
    n_shown = 0
    for raw in lines:
        s = raw.strip()
        if s.startswith("#") and ("①" in s or "②" in s or "③" in s or "④" in s or "⑤" in s or "⑥" in s):
            print(f"\n{s.lstrip('#').strip()}")
            continue
        if not s or s.startswith("#") or "=" not in s:
            continue
        k = s.split("=", 1)[0].strip()
        if not _ENV_KEY_RE.match(k):
            continue
        eff = os.environ.get(k, "")
        file_v = DOTENV_VALUES.get(k, "")
        mark = "" if eff == file_v else f"   ← 被外部覆盖（文件值 {file_v}）"
        print(f"  {k:<22} = {eff}{mark}")
        n_shown += 1
    print("-" * 72)
    print(f"共 {n_shown} 项键值已注入服务环境。")
    return 0


# ---------- 日志：按天落盘 + 7 天保留 ----------
# 历史做法：把日志交给 launchd 的 StandardOutPath 追加到**单个** fall_alert_bridge.log。
# 两个坑（2026-09-30 排查 21:31 漏报时全部踩到）：
#   1) 时间戳只有 HH:MM:SS、没有日期，文件又是跨天累积的 —— 按 "[21:" 过滤会看到
#      "未来时间"的行（其实是几天前的旧运行），极易误判成"多进程乱序"。
#   2) 单文件无限增长，没有任何轮转/清理。
# 现在由进程自己按天写 logs/fall_alert_bridge-YYYY-MM-DD.log，时间戳带完整日期；
# 启动时与每小时清理超过保留期(默认 7 天)的旧日志。
LOG_DIR = os.environ.get("LOG_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_RETENTION_DAYS = int(os.environ.get("LOG_RETENTION_DAYS", "7") or 7)
LOG_MIRROR_STDOUT = str(os.environ.get("LOG_MIRROR_STDOUT", "1")).strip().lower() \
    not in ("0", "false", "no", "off", "")
LOG_PRUNE_EVERY = float(os.environ.get("LOG_PRUNE_EVERY", "3600") or 3600)
LOG_ROTATE_BYTES = int(os.environ.get("LOG_ROTATE_BYTES", "2000000") or 2000000)
LOG_PREFIX = "fall_alert_bridge"
LOG_FILE_RE = re.compile(
    r"^%s-(\d{4})-(\d{2})-(\d{2})\.log(\.gz)?$" % re.escape(LOG_PREFIX))

_log_state = {"day": None, "fh": None, "last_prune": 0.0, "failed": False}


def _log_path(day=None):
    return os.path.join(LOG_DIR, f"{LOG_PREFIX}-{day or time.strftime('%Y-%m-%d')}.log")


def _open_log(day):
    """打开（或按天切换）当天日志文件，并刷新 latest 符号链接。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    fh = open(_log_path(day), "a", encoding="utf-8")
    _log_state["fh"], _log_state["day"] = fh, day
    _log_state["failed"] = False
    # 稳定入口：tail -f logs/fall_alert_bridge.latest.log（指向当天文件）
    latest = os.path.join(LOG_DIR, f"{LOG_PREFIX}.latest.log")
    try:
        tmp = latest + ".tmp"
        if os.path.islink(tmp) or os.path.exists(tmp):
            os.remove(tmp)
        os.symlink(os.path.basename(_log_path(day)), tmp)
        os.replace(tmp, latest)   # 原子替换，避免 tail 读到半截链接
    except OSError:
        pass
    return fh


def prune_logs(force=False, quiet=False):
    """删除超过保留期的本桥日志（按文件名日期，兜底用 mtime）。返回删除条数。

    定期调用即可，幂等；只碰 fall_alert_bridge-YYYY-MM-DD.log 这一种命名，
    不越界动 logs/ 下其它服务的文件（那是 log_maintenance.py 的职责）。
    """
    now = time.time()
    if not force and now - _log_state["last_prune"] < LOG_PRUNE_EVERY:
        return 0
    _log_state["last_prune"] = now
    today = time.strftime("%Y-%m-%d")
    removed = []
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return 0
    for name in names:
        m = LOG_FILE_RE.match(name)
        if not m:
            continue
        d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        try:
            age = (time.mktime(time.strptime(today, "%Y-%m-%d"))
                   - time.mktime(time.strptime(d, "%Y-%m-%d"))) / 86400.0
        except ValueError:
            continue
        if age >= LOG_RETENTION_DAYS:
            p = os.path.join(LOG_DIR, name)
            try:
                os.remove(p)
                removed.append(name)
            except OSError as e:
                if not quiet:
                    log(f"[LOG] 删除失败 {name}: {e}")
    if removed and not quiet:
        log(f"[LOG] 清理超期日志 {len(removed)} 个（保留 {LOG_RETENTION_DAYS} 天）: "
            + ", ".join(sorted(removed)[:6])
            + (" …" if len(removed) > 6 else ""))
    return len(removed)


def maintain_logs(force=False):
    """统一日志维护：清理本桥超期日志 + 借 log_maintenance 滚动/清理整个 logs/。

    为什么把跨服务的维护也放进桥里：本机在受限环境下 `launchctl bootstrap`
    恒返回 EIO（2026-09-30 实测，最小探针 plist 也一样），依赖外部定时任务
    有可能**悄悄不生效**；桥是 KeepAlive 常驻进程，让它顺手打扫最可靠
    （logs/ 里都是监控链路的日志，本就不该互相甩锅）。
    只有真的滚动/删除了东西才写日志，避免每小时刷一条噪声。
    """
    now = time.time()
    if not force and now - _log_state["last_prune"] < LOG_PRUNE_EVERY:
        return
    prune_logs(force=True)          # 自己的按天日志（快路径）
    try:
        import log_maintenance as _lm
        res = _lm.run(LOG_DIR, LOG_RETENTION_DAYS, LOG_ROTATE_BYTES, verbose=False)
    except Exception as e:          # 维护失败绝不能影响告警链路
        log(f"[LOG] log_maintenance 跳过: {type(e).__name__}: {e}")
        return
    if res["rolled"] or res["pruned"]:
        def _brief(xs):
            return ", ".join(xs[:4]) + ("…" if len(xs) > 4 else "")
        log(f"[LOG] 滚动 {len(res['rolled'])} 个（{_brief(res['rolled'])}）"
            f" / 清理 {len(res['pruned'])} 个（{_brief(res['pruned'])}）")


def log(msg="", *_a, **_k):
    """写一行日志：按天落盘（时间戳带日期），按需镜像到 stdout。

    写盘失败不能拖垮主循环（告警链路优先），失败时降级打到 stderr + stdout。
    """
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    day = time.strftime("%Y-%m-%d")
    ok = False
    try:
        if _log_state["fh"] is None or _log_state["day"] != day:
            _open_log(day)
        _log_state["fh"].write(line + "\n")
        _log_state["fh"].flush()
        ok = True
    except Exception as e:
        if not _log_state["failed"]:
            _log_state["failed"] = True
            try:
                sys.stderr.write(f"[LOGFAIL] 日志写入失败，降级到标准输出: {e}\n")
            except Exception:
                pass
        _log_state["fh"] = None
    if LOG_MIRROR_STDOUT or not ok:
        try:
            print(line, flush=True)
        except Exception:
            pass

# ---------- 配置 ----------
FRIGATE_URL   = os.environ.get("FRIGATE_URL", "http://127.0.0.1:5001").rstrip("/")


def _env_flag(name, default="0"):
    """环境变量布尔开关：0/false/no/off/空 一律视为关闭（空串不能当成开启）。"""
    v = os.environ.get(name, default)
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


# 告警推送目标：可配置**多个**群机器人，告警同时推到所有群。
# 多群是冗余手段：某个机器人被限频/停用/删群时，另一个群仍能收到告警，
# 避免"跌倒了却没人知道"。
# 2026-09-30：通道由企业微信**切换为飞书**（原两个企业微信群机器人已移除）。
# 优先级：ALERT_WEBHOOKS > 旧名 WECHAT_WEBHOOKS > 更旧名 WECHAT_WEBHOOK > 下面默认值。
_DEFAULT_WEBHOOKS = [
    "https://open.feishu.cn/open-apis/bot/v2/hook/__REDACTED__",
]
ALERT_WEBHOOKS = [
    u for u in re.split(
        r"[,\s]+",
        os.environ.get("ALERT_WEBHOOKS") or os.environ.get("WECHAT_WEBHOOKS")
        or os.environ.get("WECHAT_WEBHOOK")
        or ",".join(_DEFAULT_WEBHOOKS),
    ) if u
]
# 兼容别名：旧代码/旧文档里的 WECHAT_WEBHOOKS 名字仍然可用
WECHAT_WEBHOOKS = ALERT_WEBHOOKS

# 飞书发图凭证（2026-09-30 新增）。
# 飞书**自定义机器人 webhook 发不了图片**：它只认 image_key，而 image_key 必须由
# 具备机器人能力的**应用凭证**调 im/v1/images 上传得到（webhook 本身没有任何数据权限）。
# 因此这里允许配置一个自建应用：app_id/app_secret -> tenant_access_token -> 上传图片
# -> image_key -> 用 webhook 的 image 消息发出（一张图一条消息，紧跟文字卡片）。
# 未配置时自动降级为"只发文字"，并在日志里说明原因（告警本身不会丢）。
FEISHU_APP_ID     = (os.environ.get("FEISHU_APP_ID") or "").strip()
FEISHU_APP_SECRET = (os.environ.get("FEISHU_APP_SECRET") or "").strip()
FEISHU_TOKEN_URL  = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_IMAGE_URL  = "https://open.feishu.cn/open-apis/im/v1/images"
# 图片超过飞书上限（10MB）或拿不到 image_key 时，只发文字不阻塞告警
FEISHU_IMAGE_MAX  = int(os.environ.get("FEISHU_IMAGE_MAX", str(9 * 1024 * 1024)))
# 凭证也可以放在单独的小文件里（KEY=VALUE，每行一条；环境变量优先）。
# 为什么单独一个文件：让用户"粘一次就生效"，不必去改 plist 或重启 launchd 配置。
FEISHU_APP_CONF = (os.environ.get("FEISHU_APP_CONF")
                   or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "feishu_app.conf"))


def _load_feishu_conf(path=None):
    """把 feishu_app.conf 里的凭证补进来（只填空缺，不覆盖环境变量）。返回来源说明。"""
    global FEISHU_APP_ID, FEISHU_APP_SECRET
    if FEISHU_APP_ID and FEISHU_APP_SECRET:
        return "环境变量"
    path = path or FEISHU_APP_CONF
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().upper()
                v = v.strip().strip('"').strip("'")
                if k == "FEISHU_APP_ID" and not FEISHU_APP_ID:
                    FEISHU_APP_ID = v
                elif k == "FEISHU_APP_SECRET" and not FEISHU_APP_SECRET:
                    FEISHU_APP_SECRET = v
    except FileNotFoundError:
        return ""
    except OSError as e:
        return f"读取 {path} 失败：{e}"
    return f"{os.path.basename(path)}" if FEISHU_APP_ID and FEISHU_APP_SECRET else ""


FEISHU_CRED_SOURCE = _load_feishu_conf()

CAMERAS       = os.environ.get("CAMERAS", "xiaomi_ptz_2k")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "3"))
FALL_FRAMES   = int(os.environ.get("FALL_FRAMES", "3"))     # 几何判定：连续几次确认
# 视觉判定独立阈值：模型单次调用就要 6~12s 且受 VISION_EVERY 节流，
# 沿用 FALL_FRAMES(=3) 会导致计数永远攒不满 → 跌倒也发不出告警（2026-09-17 实测踩坑）。
GENAI_FRAMES  = int(os.environ.get("GENAI_FRAMES", "1"))    # 视觉判定（强信号）：连续几次确认
# 视觉判定（弱信号）独立阈值 —— 2026-10-02 新增。
# 事故：10-01 23:23:40 模型把"坐在沙发上"在一帧里误读成
# "The person is sitting on the floor behind a cluttered coffee table"，
# 严格贴地策略随即软升级 → GENAI_FRAMES=1 让它**单帧就发告警**（用户报的误报）。
# 弱信号（坐/跪/蹲在地这类"软升级"、以及"疑似贴地"兜底）与"躺/倒地"不同：
# 模型很容易把"坐在家具上"看成"坐在地上"，因此要求窗口内累计命中 ≥ 本阈值才告警。
# 真实的"坐在地上"会连续多帧被描述出来（每 VISION_EVERY=10s 一次），照常命中；
# 单帧幻觉则被挡掉。强信号（FALLEN: yes / 躺倒在地）仍按 GENAI_FRAMES 立即告警，不降召回。
GENAI_WEAK_FRAMES = int(os.environ.get("GENAI_WEAK_FRAMES", "2"))
# 弱信号去抖间隔（秒）—— 2026-10-07 新增。
# 事故：10-07 22:49 人坐在沙发上，模型连续两帧（22:49:08 / 22:49:24，相隔仅 16s）
# 把同一个人幻觉成 "sitting on the floor in the corner"，弱信号 2/2 立刻告警。
# 根因：GENAI_WEAK_FRAMES=2 只数"命中几次"，不区分这两次是**独立观测**还是
# **同一次幻觉的延续**。相隔十几秒的两帧看到的是同一个姿势、同一个画面，
# 本质是 1 次证据被重复计数。
# 规则：两次弱命中必须与上一次**已计入**的弱命中相隔 ≥ WEAK_MIN_GAP 才算数；
# 间隔不足的当作同一帧幻觉的延续，只更新窗口、不增加计数。
# 取值依据：VISION_EVERY=10s，实测视觉帧间隔 15~25s。20s 能挡掉"连续两帧同幻觉"，
# 而真正坐在地上的人会持续被描述成贴地，第 2 次有效命中仍能落在 FALL_WINDOW=45s 内
# （t=0 与 t≥20），召回不降。
WEAK_MIN_GAP = float(os.environ.get("WEAK_MIN_GAP", "20"))
FALL_ASPECT   = float(os.environ.get("FALL_ASPECT", "1.05"))
FALL_LOWER    = float(os.environ.get("FALL_LOWER", "0.55"))
# 近失阈值（只用于日志观测，不触发告警）：宽高比接近躺平 / 重心非常低
NEAR_ASPECT   = float(os.environ.get("NEAR_ASPECT", "0.75"))
NEAR_LOWER    = float(os.environ.get("NEAR_LOWER", "0.70"))
# 标定采样 CSV：把每个事件每次采样的 (宽高比, 重心, 静止, 包围框) 落盘，
# 用于用真实倒地数据来定阈值，而不是凭感觉调数字。
GEO_CALIB_CSV = os.environ.get("GEO_CALIB_CSV", "fall_geo_samples.csv")
GEO_CALIB     = os.environ.get("GEO_CALIB", "1") not in ("0", "false", "False")
# frigate 0.17 不再返回 stationary 字段，改用 data.average_estimated_speed 近似静止
STATIONARY_SPEED = float(os.environ.get("STATIONARY_SPEED", "1.0"))
GEO_LOG_EVERY = float(os.environ.get("GEO_LOG_EVERY", "20"))   # 几何状态日志节流(秒)
ALERT_COOLDOWN= float(os.environ.get("ALERT_COOLDOWN", "120"))
FALL_KEYWORDS = [k.strip().lower() for k in
                 os.environ.get("FALL_KEYWORDS",
                    "fallen,fell,lying on the ground,on the floor,collapsed,unconscious,跌倒,摔倒,倒地,躺地,晕倒,瘫倒,坠倒"
                 ).split(",") if k.strip()]

# ---------- 贴地姿态策略（2026-09-30 新增）----------
# 用户要求"有人坐在地上也要告警" → 默认启用**严格策略**：模型描述里只要出现
# "人本身贴地"的姿态（sitting/seated/lying/kneeling… 紧接 on the floor/ground），
# 即便模型判 `FALLEN: no` 也升级为告警（安全优先，宁可误报不可漏报）。
# FLOOR_ALERT_STRICT=0 退回宽松策略：只认"明确倒地"姿态，坐/跪在地上不告警。
FLOOR_ALERT_STRICT = _env_flag("FLOOR_ALERT_STRICT", "1")
# 姿态词与 "on the floor" 之间的最大字符间隔。20 是刻意卡的：
#   "sitting on the floor"                           间隔 0    -> 算贴地
#   "sitting on a sofa with their legs up on the floor" 间隔 ≈30 -> 不算
#     （身体在沙发、只有腿搭在地板上，2026-09-23 那次真实误报就是这个句式）
FLOOR_POSTURE_GAP = int(os.environ.get("FLOOR_POSTURE_GAP", "20") or 20)

# ---------- 多人同框策略（2026-09-30 新增）----------
# 实测（2026-09-30 21:2x）：同框 2 人时 frigate 的 person 框会把两人合并/拆散，
# 宽高比只剩 0.19~0.42，几何通道完全不可信且容易产生"某人躺平"的假信号，
# 用户要求忽略这类告警。默认只忽略几何通道——AI 视觉通道照常判定，
# 所以"一人坐沙发 + 一人坐地上"这类**真实**信号仍然会告警。
MULTI_PERSON_MODE = (os.environ.get("MULTI_PERSON_MODE") or "geo").strip().lower()
MULTI_PERSON_MIN = int(os.environ.get("MULTI_PERSON_MIN", "2") or 2)

# ---------- GenAI 视觉通道（免费模型 + 延迟自适应切换） ----------
# 由 free_vision.py 提供：多 Provider × 多免费视觉模型候选池，
# 单次调用超时/慢响应/报错 → 自动切换下一个模型。
VISION_ENABLED = os.environ.get("VISION_ENABLED", "1") not in ("0", "false", "False")
VISION_EVERY   = float(os.environ.get("VISION_EVERY", "10"))   # 同一事件多久重新问一次模型
VISION_MIN_CONF= os.environ.get("VISION_MIN_CONF", "0")        # 预留

# ---------- 多帧序列判定（2026-09-20 新增）----------
# ⚠️ 为什么必须改：单帧静态姿态无法区分"弯腰捡东西"和"正在倒下"——
# 23:25:55 的快照里人躯干近水平扶在脚踏凳上，单帧模型判 FALLEN: no（它有道理：
# 那一瞬间确实和弯腰没区别）。跌倒的充分特征在**时间维度**：
# 快速下坠 → 贴地 → 不再恢复站立。只有序列才看得出来。
# 代价：帧数越多越慢，1080p 单帧约 11s，4 帧 1080p 直接超时。
# 因此连拍后统一降采样到 VISION_FRAME_HEIGHT（实测 4 帧 480p ≈ 5~16s）。
VISION_FRAMES       = int(os.environ.get("VISION_FRAMES", "4"))     # 连拍帧数
VISION_FRAME_GAP    = float(os.environ.get("VISION_FRAME_GAP", "0.8"))  # 帧间隔(秒)
VISION_FRAME_HEIGHT = int(os.environ.get("VISION_FRAME_HEIGHT", "480")) # 降采样目标高
VISION_FRAME_QUALITY= int(os.environ.get("VISION_FRAME_QUALITY", "75")) # 重编码质量
VISION_DEADLINE     = float(os.environ.get("VISION_DEADLINE_MULTI", "26"))  # 多帧单次硬超时

# ---------- 视觉盲窗告警（2026-09-20 新增）----------
# 实测 23:27:04 出现过"两个模型同时 TIMEOUT"，那一整段没有任何判定产出——
# 如果真实跌倒恰好落在这个窗口里，系统会静默漏报（与 22:19 断流事故同一性质的失效）。
# 因此：连续失败且持续超过 VISION_BLIND_SEC 就推一条告警，明确告知"监护暂时失效"。
VISION_BLIND_SEC      = float(os.environ.get("VISION_BLIND_SEC", "240"))     # 持续失败多久算盲窗
VISION_BLIND_COOLDOWN = float(os.environ.get("VISION_BLIND_COOLDOWN", "1800"))

# ---------- 取帧质量校验 ----------
# ⚠️ 2026-09-20 踩坑：/api/{cam}/latest.jpg 在摄像头流中断时**仍然返回 200**，
# 只是内容换成了 frigate 渲染的错误占位图（"No frames have been received..."）。
# 旧代码只判断 len(data) > 200 就直接使用，于是把占位图喂给视觉模型，
# 模型的回答变成 "No person is visible ... only an error message is displayed"，
# 白花 25~30s 推理还得不到有效结论。因此这里加了三层防护：
#   1) 取帧前用 /api/stats 的 camera_fps 判断流是否真的在出帧
#   2) 取帧后做 JPEG 魔数 + 结束符校验（挡住 HTML/JSON 错误页与截断帧）
#   3) 校验不过就重试，仍不过才降级到快照兜底
FRAME_MIN_BYTES  = int(os.environ.get("FRAME_MIN_BYTES", "2000"))   # 正常监控帧远大于此
FRAME_RETRIES    = int(os.environ.get("FRAME_RETRIES", "3"))        # 同一 URL 最多取几次
FRAME_RETRY_SLEEP= float(os.environ.get("FRAME_RETRY_SLEEP", "0.6"))
STATS_CACHE_TTL  = float(os.environ.get("STATS_CACHE_TTL", "5"))    # camera_fps 缓存秒数
MIN_CAM_FPS      = float(os.environ.get("MIN_CAM_FPS", "1.0"))      # 低于此值视为流已断

# ---------- 视频流健康监测 ----------
# ⚠️ 2026-09-20 漏报事故（16 分钟静默失效）后新增：断流时 frigate 的表现极具欺骗性——
#   • /api/stats 的 camera_fps 仍虚报 5.x（完全误导）
#   • /api/{cam}/latest.jpg 仍返回 HTTP 200 + 约 51.8KB 黑底报错占位图
#     该图魔数/结束符都合法，能通过 is_valid_jpeg（实测的坑）
#   • go2rtc 的 /api/frame.jpeg 恒返回 500，健康时也一样，**不能**当判据
# 唯一可靠的办法：把 frigate 的报错占位图当作指纹识别（同版本内 md5 恒定）。
STREAM_WATCH     = _env_flag("STREAM_WATCH", "1")
STREAM_PROBE_EVERY = float(os.environ.get("STREAM_PROBE_EVERY", "15"))  # 探测间隔秒
# 2026-09-30 调整（按需求）：断流要**连续 30 分钟**没有画面才告警，之后每 10 分钟重复一次。
# 旧值 90s/1800s 的问题是：相机临时重启、网络抖一下也会立刻响，噪音大；
# 而自愈链路（60s 后重启 go2rtc）本来就该先把这类瞬时故障吃掉，真正值得打扰人的
# 是"自愈反复失败、链路确实瘫了半小时"。所以阈值抬到 30 分钟、重复间隔收到 10 分钟。
STREAM_ALERT_SEC   = float(os.environ.get("STREAM_ALERT_SEC", "1800"))  # 连续无画面多久告警(30min)
STREAM_ALERT_COOLDOWN = float(os.environ.get("STREAM_ALERT_COOLDOWN", "600"))  # 重复告警间隔(10min)

# ---------- 断流告警静默开关（2026-09-30 新增）----------
# 需求：要能"设置静默视频流断开告警"——例如明知要拔摄像头/搬家/维护，不想被反复打扰。
# 两种开启方式，任一命中即静默（不需要重启进程，探测时实时判断）：
#   ① 环境变量 STREAM_ALERT_SILENT=1
#   ② 静默标记文件存在（内容为空=永久；或 "+90m"/"+2h"/"2026-10-01 08:00" 到期时间）
# 边界（重要）：静默只覆盖**视频流类通知**（断流告警、自愈结果），
# 跌倒告警与休眠唤醒提醒**永远推送**——安全兜底不能被误静默掉。
STREAM_ALERT_SILENT  = _env_flag("STREAM_ALERT_SILENT", "0")
STREAM_SILENCE_FILE  = os.environ.get(
    "STREAM_SILENCE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "stream_alert.silent"))
# frigate 报错占位图指纹（frigate 升级后可能变化，可用环境变量覆盖）
ERROR_FRAME_MD5  = os.environ.get("FRIGATE_ERROR_FRAME_MD5",
                                  "c7b3c8f612faa361ff1a6441b9c4f1fc")

# ---------- 画面冻结自愈 + 休眠唤醒监测（2026-09-23 新增）----------
# 事故复盘：22:36:22 Mac 空闲休眠（pmset sleep=10，AC 下 10 分钟无操作即睡），
# 22:48:55 唤醒（休眠 12 分 33 秒）。期间整条链路冻结，唤醒后 go2rtc 的小米 P2P 已断、
# frigate 的采集 ffmpeg 也不再重连 → 画面永远冻结在报错占位图上，跌倒检测静默失效 20 分钟。
# 旧实现的缺口有两个：
#   1) 只"告警"不"动手"：识别到画面异常后只是推一条消息（冷却还长达 30 分钟），
#      不会尝试恢复链路，人不处理就一直瘫着；
#   2) 休眠这种事只体现为日志断档，事后才能发现，当时没有任何外部告警。
# 现在补上：异常持续 STREAM_RECOVER_AFTER 秒 → 调用 recover_monitor.sh 自愈；
# 主循环停顿超过 WAKE_GAP_SEC（说明系统刚睡醒）→ 立即探测 + 立即自愈 + 推一条休眠告知。
STREAM_RECOVER          = os.environ.get("STREAM_RECOVER", "1") not in ("0", "false", "False")
STREAM_RECOVER_AFTER    = float(os.environ.get("STREAM_RECOVER_AFTER", "60"))    # 异常多久后动手
STREAM_RECOVER_COOLDOWN = float(os.environ.get("STREAM_RECOVER_COOLDOWN", "300"))# 两次自愈最小间隔
RECOVER_SCRIPT   = os.environ.get("RECOVER_SCRIPT",
                                  os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               "recover_monitor.sh"))
RECOVER_TIMEOUT  = float(os.environ.get("RECOVER_TIMEOUT", "180"))  # 自愈脚本总超时(秒)
WAKE_GAP_SEC     = float(os.environ.get("WAKE_GAP_SEC", "60"))      # 暂停多久算"睡过"
STALL_GAP_SEC    = float(os.environ.get("STALL_GAP_SEC", "120"))    # 仅卡顿（非休眠）的停顿阈值
WAKE_NOTICE      = os.environ.get("WAKE_NOTICE", "1") not in ("0", "false", "False")

# 常驻防休眠：本机就是监控主机，休眠等于整套跌倒检测停摆（2026-09-23 事故）。
# 实现方式说明：本来应该用一个独立的 launchd agent 常驻 caffeinate，但当前环境下
# launchctl bootstrap 无法在 gui 域注册新服务（EIO），所以改由本进程（已被 launchd
# 托管、常驻）顺带拉起一个 caffeinate 子进程，并在主循环里周期性确认它还活着。
KEEP_AWAKE       = os.environ.get("KEEP_AWAKE", "1") not in ("0", "false", "False")
KEEP_AWAKE_CMD   = ["/usr/bin/caffeinate", "-i", "-s"]

# ---------- 跌倒确认窗口 ----------
# 原实现要求"连续 FALL_FRAMES 次满足"才告警，实测姿态一抖就清零：
# 2026-09-20 22:37:01 出现过一次 宽高比=1.74(躺平) 的完美判定，
# 但下一轮人就坐起来了，计数归零 → 告警没发出去。
# 改为滚动时间窗口内累计满足次数，容忍中途抖动。
# 窗口取 45s 而非 12s：几何判定每个事件每轮只算一次，而轮次间隔受模型调用影响
# （约 10~15s），窗口太短会导致 3 次命中永远攒不满 → 几何通道再次形同虚设。
FALL_WINDOW      = float(os.environ.get("FALL_WINDOW", "45"))  # 判定时间窗口(秒)

_vision_router = None
_vision_last   = {}   # event_id -> 上次调用时间（保留用于诊断）
# 全局模型调用节流（2026-09-20 新增）：单轮可能含多个事件，逐个调模型（每次 5~13s）
# 会把轮次拖到几十秒，进而拖垮几何判定的连续性。改为限制**整个进程**的问模型频率。
_vision_global = [0.0]


def get_vision_router():
    """惰性加载视觉路由；free_vision.py 缺失或零可用模型时返回 None（自动降级几何通道）。"""
    global _vision_router
    if _vision_router is not None:
        return _vision_router
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import free_vision
        r = free_vision.VisionRouter()
        usable = [s for s in r.slots if s.provider.get("api_key")]
        if not usable:
            log("[WARN] 未配置任何视觉模型 Key，GenAI 通道关闭（几何判定仍工作）")
            _vision_router = False
            return None
        log(f"[INIT] 视觉通道就绪，候选模型 {len(usable)} 个："
              f"{', '.join(s.key for s in usable[:4])}{' ...' if len(usable) > 4 else ''}",
              flush=True)
        _vision_router = r
        return r
    except Exception as e:
        log(f"[WARN] 视觉通道初始化失败: {e}（几何判定仍工作）")
        _vision_router = False
        return None


# 强制本机代理旁路（frigate 在 127.0.0.1）
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"


# ---------- HTTP 工具 ----------
def http_get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def http_get_bytes(url, timeout=10):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------- 事件获取 ----------
def get_active_person_events(recent_limit=5):
    """拉取**当前正在进行**的 person 事件。

    ⚠️ 2026-09-20 修复（本次排查暴露的架构缺陷）：
    原实现只按 `label == "person"` 过滤，把 **已结束的历史事件**也一并当成活跃事件，
    于是每轮循环都要对几十个陈旧事件逐个调用视觉模型（单次 5~13s）。
    实测后果：单轮耗时被拖到数分钟 ——
      • 流中断监测（在一轮末尾执行）久久不触发，[STREAM] 日志 100 秒都不出现
      • 跌倒告警响应被严重延迟
      • 云端模型 API 被大量无意义的重复调用烧掉
    现在只保留 end_time 为空（进行中）的事件，并按开始时间倒序取最近 recent_limit 个。
    """
    try:
        if CAMERAS.upper() == "ALL":
            url = f"{FRIGATE_URL}/api/events?limit=50"
        else:
            cams = ",".join([c.strip() for c in CAMERAS.split(",") if c.strip()])
            url = f"{FRIGATE_URL}/api/events?cameras={urllib.parse.quote(cams)}&limit=50"
        events = http_get_json(url)
        active = [e for e in events
                  if e.get("label") == "person" and e.get("end_time") is None]
        active.sort(key=lambda e: e.get("start_time") or 0, reverse=True)
        return active[:recent_limit]
    except Exception as e:
        log(f"[WARN] 获取 events 失败: {e}")
        return []


# ---------- 多人同框判定（2026-09-30 新增）----------
def count_persons_by_camera(events):
    """统计每个相机当前活跃的 person 事件数。

    frigate 对**每个**被跟踪的人各开一个 person 事件，所以"同框 2 人"表现为
    同一相机下同时存在 ≥2 个未结束的 person 事件。几何通道在这种场景下不可信
    （包围框可能把两人合并或拆散），需要单独识别出来做忽略处理。
    """
    n = {}
    for e in events or []:
        cam = e.get("camera") or "?"
        n[cam] = n.get(cam, 0) + 1
    return n


def multi_person_mode():
    """归一化多人同框策略，非法值回退到 geo（最保守可选：只忽略不可信的几何通道）。"""
    m = (MULTI_PERSON_MODE or "").strip().lower()
    return m if m in ("geo", "all", "off") else "geo"


def apply_multi_person_policy(geo_yes, reason, person_count, mode=None):
    """同框多人时裁剪告警。返回 (geo_yes, reason, skip_event)。

    geo 模式：同框多人 -> person 框不可信，丢掉几何判定（reason 里注明原因），
              事件继续走 AI 视觉通道 —— "一人坐沙发、另一人坐地上"这类真实信号
              仍然会被 AI 通道报出来；
    all 模式：同框多人 -> skip_event=True，整条忽略（不告警）；
    off     ：原样返回（旧行为）。
    """
    mode = mode or multi_person_mode()
    if mode == "off" or person_count < MULTI_PERSON_MIN:
        return geo_yes, reason, False
    if mode == "all":
        return False, reason, True
    if geo_yes:
        return False, f"同框{person_count}人-几何判定已忽略（{reason}）", False
    return geo_yes, reason, False



# ---------- 几何判定 ----------
def parse_box(box, wh=False):
    """统一返回 (x1,y1,x2,y2) 归一化 0~1。

       wh=False: box 为 [x1,y1,x2,y2]（frigate <0.14 的顶层 box 字段）
       wh=True : box 为 [x,y,w,h]（frigate 0.17 的 data.box）

       ⚠️ 两种格式在数值上无法可靠区分（[0.1,0.2,0.3,0.4] 两种解释都合法），
       必须由调用方显式指定，靠猜会在某些位置上把站立的人算成躺平。
    """
    if not box or len(box) != 4:
        return None
    a, b, c, d = [float(v) for v in box]
    # wh=True，或第三/四值 >1.0（绝对像素的宽高）→ 按 x1,y1,x1+w,y1+h 处理
    if wh or c > 1.0 or d > 1.0:
        return (a, b, a + c, b + d)
    return (a, b, c, d)


def extract_box(event):
    """从 frigate 事件里取包围框，返回归一化 (x1,y1,x2,y2)；取不到返回 None。

    ⚠️ 2026-09-18 修复：frigate 0.17 把包围框挪进了 data.box（格式 [x,y,w,h]），
    顶层 box 字段恒为 None。旧代码只读顶层 box，导致几何判定每次都走
    "无有效包围框" 分支——整条几何通道静默失效，全部依赖视觉模型单腿走路。
    """
    data = event.get("data") or {}
    if isinstance(data, dict) and data.get("box"):
        return parse_box(data["box"], wh=True)
    if event.get("box"):
        return parse_box(event["box"], wh=False)
    return None


def extract_stationary(event):
    """静止判定。frigate 0.17 没有 stationary 字段，
    退化为 data.average_estimated_speed ≈ 0。取不到返回 None（不参与主判定）。"""
    if event.get("stationary") is not None:
        return bool(event["stationary"])
    data = event.get("data") or {}
    if isinstance(data, dict) and data.get("average_estimated_speed") is not None:
        try:
            return float(data["average_estimated_speed"]) <= STATIONARY_SPEED
        except (TypeError, ValueError):
            return None
    return None


def geometric_fall(box, stationary=None):
    """box 为已归一化的 (x1,y1,x2,y2)，由 extract_box() 产出。

    返回 (is_fallen, reason, near)：
      is_fallen  命中跌倒判据
      reason     始终包含宽高比与重心 y 的**实际值**（便于排查）
      near       接近命中（宽高比 ≥ NEAR_ASPECT 或重心很低），用于观测标定

    ⚠️ 2026-09-20 重标定：原阈值 FALL_ASPECT=1.2 在本机俯拍机位下几乎不可达——
    当晚 43 次采样宽高比全部落在 0.21~0.62（均值 0.40），0 次命中，几何通道形同空转。
    阈值下调为 1.05，并把重心门槛提到 0.55（要求人确实沉到画面下部）。
    但下调不等于可信：本机任何一次真实倒地都没被记录过，所以**不要**把几何通道
    当成唯一防线。新增标定采样（GEO_CALIB_CSV）用于用真实数据定阈值。
    """
    p = box if (box and len(box) == 4) else None
    if not p:
        return False, "无有效包围框", False
    x1, y1, x2, y2 = p
    w = max(x2 - x1, 1e-4)
    h = max(y2 - y1, 1e-4)
    ar = w / h
    cy = (y1 + y2) / 2.0
    # ⚠️ 两个条件都写进 reason（含不满足的一侧）——2026-09-20 排查漏报时，
    # 日志只显示"重心y=0.52≥0.45(偏下); 静止"而看不到宽高比实际值，
    # 无法判断是数据问题还是阈值问题，白白多花一轮排查。
    reasons = []
    if ar >= FALL_ASPECT:
        reasons.append(f"宽高比={ar:.2f}≥{FALL_ASPECT}(躺平)")
    else:
        reasons.append(f"宽高比={ar:.2f}<{FALL_ASPECT}")
    if cy >= FALL_LOWER:
        reasons.append(f"重心y={cy:.2f}≥{FALL_LOWER}(偏下)")
    else:
        reasons.append(f"重心y={cy:.2f}<{FALL_LOWER}")
    if stationary is True:
        reasons.append("静止")
    is_fallen = (ar >= FALL_ASPECT) and (cy >= FALL_LOWER)
    # 近失：至少一项接近门槛。用于在日志里看到"差一点"，而不是永远只有一排 0.3x
    near = (not is_fallen) and (ar >= NEAR_ASPECT or cy >= NEAR_LOWER)
    return is_fallen, "; ".join(reasons), near


# ---------- GenAI 视觉判定 ----------
# ⚠️ 2026-09-20 修复：frigate 0.17 的 `/api/events/{id}/description` 是 **POST（写入描述）**，
# 根本没有对应的 GET 读取端点 → 旧代码的 GET 每轮都吃 405，日志里成对刷屏。
# 容器生成的描述真正存放在事件详情的 data.description 里（frigate 源码 embeddings.py：
# `event.data.get("description")`）。本机未启用 GenAI，该字段恒空，属正常状态。
_desc_state = {"unavailable": False, "empty_noticed": False}
_stats_cache = {"t": 0, "fps": {}}
_bad_frame_log = {}   # camera -> 上次打印"无有效帧"的时间（日志节流）


def frigate_event_description(event_id, event=None):
    """读取 frigate 容器侧 GenAI 生成的事件描述；取不到返回 None。

    优先复用主循环已拿到的 event（零额外请求），必要时才回查事件详情。
    通道不可用只提示一次后熔断，避免每轮轮询刷屏。
    """
    if _desc_state["unavailable"]:
        return None
    text = None
    if event is not None:
        data = event.get("data") or {}
        if isinstance(data, dict):
            text = (data.get("description") or "").strip() or None
    if text is None:
        try:
            detail = http_get_json(f"{FRIGATE_URL}/api/events/{event_id}", timeout=6) or {}
            d = detail.get("data") or {}
            text = (d.get("description") or "").strip() or None
        except Exception as e:
            log(f"[WARN] frigate 描述通道不可用({e})，已关闭，判定改用本地视觉模型")
            _desc_state["unavailable"] = True
            return None
    if not text and not _desc_state["empty_noticed"]:
        _desc_state["empty_noticed"] = True
        log("[INIT] frigate 未启用 GenAI（事件无 description），容器描述通道空转；"
            "视觉判定由本地视觉模型单独承担（双通道并行，另一条通道暂缺）")
    return text


def camera_fps(camera):
    """相机当前实际采集帧率；查不到返回 None（表示无法判断 → 不拦截判定）。带 5s 缓存。"""
    now = time.time()
    if now - _stats_cache["t"] < STATS_CACHE_TTL:
        return _stats_cache["fps"].get(camera)
    try:
        stats = http_get_json(f"{FRIGATE_URL}/api/stats", timeout=5) or {}
        cams = stats.get("cameras") or {}
        _stats_cache["fps"] = {k: (v or {}).get("camera_fps") for k, v in cams.items()}
        _stats_cache["t"] = now
        return _stats_cache["fps"].get(camera)
    except Exception:
        return None   # 查询失败不写缓存，下次继续尝试


def genai_fall(event_id, camera=None, event=None):
    """两条子通道：
       1) frigate 容器内 GenAI 已生成的描述（data.description，需容器启用 GenAI）
       2) 直连免费视觉模型分析现场快照（free_vision.py 路由，慢/挂自动换模型）

    返回 (is_fallen, reason, evaluated, tier)：
      evaluated=False 表示本次**没做判定**（被 VISION_EVERY 节流 / 通道不可用 /
      取不到图 / 流已断 / 模型全挂），调用方不能把它当作"未跌倒"。
      tier ∈ {"strong","weak",""}：本次命中的信号强度（空串 = 未命中），
      供调用方对弱信号（坐/跪在地等软升级）要求更多次确认后再告警。
    """
    if not VISION_ENABLED:
        return False, "", False, ""

    # ---------- ① frigate 容器 GenAI 描述通道 ----------
    # 与 ② 本地视觉模型**并行使用**（2026-10-01 用户要求"同时使用 agnes 模型和
    # GenAI 通道"）：任一说跌倒即告警，① 判 no **不再短路跳过** ②。
    # 两条通道的输入本就不同（① 用 frigate 容器生成的事件描述，② 自己抓实时帧直连
    # agnes），互为交叉验证 —— 单通道漏掉的由另一条兜住。代价是 ① 命中时也要多问
    # 一次模型，所以保留 ② 的全局节流。
    text = frigate_event_description(event_id, event)
    if text:
        ok, why, _tier = _interpret_core(text)
        if ok:
            return True, f"frigate-genai: {why}", True, _tier
    # ① 是否已做出判定：判 no 也算"判过"，这样即使 ② 被节流/不可用，
    # 本次也不算"完全没判定"（调用方用它区分"没跌倒"与"没判"）。
    evaluated_frigate = bool(text)

    # ---------- ② 直连免费视觉模型 ----------
    router = get_vision_router()
    if not router:
        return False, "", evaluated_frigate, ""
    cam = camera or CAMERAS.split(",")[0].strip()
    now = time.time()
    # 全局节流（见 _vision_global 注释）：保证主循环轮次足够快，
    # 几何判定的连续性不被慢速模型调用拖垮
    if now - _vision_global[0] < VISION_EVERY:
        return False, "", evaluated_frigate, ""   # 节流跳过；①已判定则仍算已判定

    # 流健康检查：camera_fps≈0 表示摄像头已停止出帧，此刻 latest.jpg 是 frigate 的
    # 错误占位图，喂给模型只会得到 "only an error message is displayed"，白等 25s。
    fps = camera_fps(cam)
    if fps is not None and fps < MIN_CAM_FPS:
        if now - _bad_frame_log.get(cam, 0) >= GEO_LOG_EVERY:
            log(f"[WARN] 相机 {cam} 无有效视频帧(camera_fps={fps})，跳过本次视觉判定")
            _bad_frame_log[cam] = now
        return False, "", evaluated_frigate, ""

    snap = fetch_snapshot(event_id, cam)
    if not snap:
        return False, "", evaluated_frigate, ""
    _vision_last[event_id] = now
    _vision_global[0] = now

    res = router.analyze(snap)
    if not res or not res.get("text"):
        atts = (res or {}).get("attempts", [])
        log("[WARN] 视觉模型全部失败: " +
              "; ".join(f"{a['model']}({a['error'] or a['ms']}ms)" for a in atts))
        return False, "", evaluated_frigate, ""
    log(f"[VISION] {res['slot']} {res['ms']}ms -> {res['text'][:110]}")
    ok, why, tier = _interpret_core(res["text"])
    # 理由里带通道前后缀，便于在告警卡片/日志里分辨是哪条通道命中的
    return ok, f"本地视觉模型 {res['slot']} {res['ms']}ms: {why}", True, (tier if ok else "")


# 贴地姿态词表（供 _interpret 的"兜底升级"使用）
#   STRICT（默认）：坐在地上也算 —— 用户 2026-09-30 明确要求"坐在地上也要告警"
#   LOOSE        ：只认明确倒地姿态，坐/跪在地上不告警
_FLOOR_POSTURE_STRICT_RE = (
    r"sitting|seated|sits|sat|lying|lies|lay|reclining|reclined|sprawled|collapsed|"
    r"kneeling|knelt|prone|face[- ]down|crouched|slumped|curled|crumpled"
)
_FLOOR_POSTURE_LOOSE_RE = (
    r"lying|lies|lay|reclining|reclined|sprawled|collapsed|"
    r"prone|face[- ]down|slumped|curled|crumpled"
)
# "坐/跪/蹲" 类贴地姿态 —— 严格策略下也算告警，但属**弱信号**：
# 模型最容易把"坐在家具上"误读成"坐在地上"（2026-10-01 23:23 实测），
# 因此这类命中要累计 GENAI_WEAK_FRAMES 次才告警，避免单帧幻觉误报。
_FLOOR_POSTURE_SEATED_RE = r"sitting|seated|sits|sat|kneeling|knelt|crouched"

# 姿态词的**支撑面**：姿态词与 "on the floor" 之间夹着的这段如果是家具，
# 说明贴地的是家具，人明明坐在家具上 —— 2026-10-07 新增。
# 实测误报族（同一场景、同一批帧里反复出现）：
#   "The tracked person is sitting on a small stool on the floor, which is a normal posture."
#     → 贴地的是矮凳，人坐在凳子上；旧逻辑里 body_floor 只看"姿态词 + on the floor"
#       的字符间隔（17 < FLOOR_POSTURE_GAP=20）就升级成弱信号，家具线索被完全忽略。
#   "sitting on a sofa with their legs up on the floor"（09-23 老误报，间隔 ~30 靠
#       GAP 卡住）；GAP 一旦调大就会漏进来，这里再做一道结构性兜底。
# 注意**不**收录 rug/mat/carpet：坐在地毯/地垫上本质上就是坐在地上，属于要告警的场景。
_FURNITURE_SURFACE_RE = re.compile(
    r"\b(?:on|onto|in|into|atop)\s+(?:the|a|an)\s+(?:[a-z'-]+\s+){0,2}?"
    r"(?:sofa|couch|settee|chaise|armchair|chair|bed|bench|stool|ottoman|mattress|"
    r"recliner|furniture|seat|seating|cushion)\b")


def _interpret_core(text: str):
    r"""解析模型输出。返回 (是否告警, 判定理由, 信号强度 tier)。

    tier ∈ {"strong", "weak", "none"}：
      strong —— 明确证据：模型判 "FALLEN: yes"，或描述人"躺/趴/瘫倒在地"；
      weak   —— 软升级：严格策略下把"坐/跪/蹲在地上"也算告警（模型最容易把它与
                "坐在家具上"混淆），以及"提到人贴地但无家具/肢体线索"的疑似兜底；
      none   —— 未告警。
    调用方用 tier 决定确认次数：强信号单次即可，弱信号需窗口内累计多次
    （GENAI_WEAK_FRAMES），以挡住"单帧把坐沙发误读成坐在地上"的模型幻觉
    —— 2026-10-01 23:23:40 实测误报就是这么发生的。

    标记识别做了容错：实测 agnes-3.0-flash 曾输出过 "FALSEN: yes" 这类拼写漂移，
    故用 fa(ll|lse|ls)\w* 兼容 fallen / fall / falsen / fallen-down 等写法。
    """
    low = text.lower()
    m = re.search(r"\bfa(?:ll|lse|ls)\w*\s*[:：]?\s*(yes|no)\b", low)
    if m:
        if m.group(1) == "yes":
            return True, text[:120], "strong"
        # 兜底升级：模型判 no 但描述里说人"贴地"（sitting/lying/reclining on the
        # floor/ground），这是 23:50 实测漏报的场景——模型把半躺靠家具归类成
        # "sitting"。养老场景宁可误报不漏报。
        #
        # ⚠️ 2026-09-18 修正：升级前必须先剥离否定语境。实测误报：
        #   "FALLEN: no. The person is reclining on a sofa with legs elevated
        #    on the chaise, not ON THE FLOOR."
        # 字面含 "on the floor" + "person" → 被错误升级成告警（人明明坐在沙发上）。
        # 因此先用正则吃掉 "not/never/rather than/instead of/off/above ... on the
        # floor/ground" 这类否定表述，再判断剩余文本是否真的提到贴地。
        neg = re.sub(
            r"(?:not|n't|never|isn't|aren't|rather\s+than|instead\s+of|off|above)\s+"
            r"(?:[a-z']+\s+){0,3}on\s+the\s+(?:floor|ground)",
            " ", low,
        )
        # 姿态排除：站着/走着的人说 "on the floor" 只是在地板上站立，不是跌倒。
        # 实测误报：人站着拎塑料袋 → "standing upright on the floor while holding
        # a plastic bag" 被升级成告警。注意不要排除 sitting/lying/kneeling，
        # 这些才是真正需要兜底升级的姿态。
        upright_words = ("standing", "stands", "upright", "walking", "walks",
                         "on his feet", "on her feet", "on their feet")
        if any(w in neg for w in upright_words):
            return False, text[:120], "none"
        floor_words = ("on the floor", "on the ground", "on floor")
        person_words = ("person", "someone", "people", " man ", "woman", "figure", "they ", "user")
        if any(w in neg for w in floor_words) and any(w in neg for w in person_words):
            # ⚠️ 2026-09-23 修正（真实误报，两个告警群都收到了假警报）：
            #   FALLEN: no / "The person is sitting on a sofa with their legs up on the floor."
            # 描述里同时出现 "person" 与 "on the floor"，但那是**腿脚**在地板上，
            # 身体明明坐在沙发上，纯关键词升级把它误判成跌倒。
            # 因此升级前再加两道排除（有"身体贴地"的明确表述时不排除）：
            #   a) 贴地的主体是肢体（legs/feet/hands…），不是身体；
            #   b) 描述的是人坐在/躺在**家具**上。
            lying_floor = re.search(
                r"\b(?:lying|lies|prone|face[- ]down|sprawled|collapsed)\b[^.]{0,16}"
                r"\b(?:on|at|near|beside)\s+(?:the\s+)?(?:floor|ground)\b", neg)
            limb_floor = re.search(
                r"\b(?:legs?|feet|foot|shoes?|socks?|hands?|arms?|toes?)\b[^.]{0,24}"
                r"\bon\s+the\s+(?:floor|ground)\b", neg)
            # 家具豁免也允许家具名词前带 0~2 个形容词：实测模型爱说
            # "a small stool" / "the leather sofa"，旧正则只认 "on a stool" 而漏掉。
            furniture_phrase = re.search(
                r"\bon\s+(?:the|a|an)\s+(?:[a-z'-]+\s+){0,2}?(?:sofa|couch|settee|chaise|"
                r"armchair|chair|bed|bench|stool|ottoman|mattress|recliner|furniture|"
                r"seat|seating)\b", neg)
            # ⚠️ 2026-09-30 修正（真实漏报，21:31:26 实测）：
            #   FALLEN: no / "One man is sitting on a couch while the other is sitting on the floor."
            # 一句里同时出现"家具"和"人坐在地板上"（**多主体**：一人坐沙发、另一人坐地上）。
            # 旧的 furniture_phrase 只看句子有没有家具，直接把这条真信号豁免掉了 → 漏报。
            # 家具豁免的本意只是"身体在家具上"（如腿搭在地板上），所以这里再加一条判据：
            # 只要**姿态词紧贴地板**（sitting/lying/kneeling… ≤FLOOR_POSTURE_GAP 字符内接
            # on the floor），说明贴地的是人本身而不是腿脚，家具豁免就不该生效。
            #
            # 2026-09-30 二次调整（用户要求"有人坐在地上也告警，采用严格策略"）：
            # 姿态词表与兜底行为都改成可配置——严格模式下 sitting 也在词表里，
            # 且"有人+贴地"但没有任何家具/肢体线索时照样升级。
            posture_re = (_FLOOR_POSTURE_STRICT_RE if FLOOR_ALERT_STRICT
                          else _FLOOR_POSTURE_LOOSE_RE)
            body_floor = re.search(
                rf"\b({posture_re})\b([^.]{{0,{FLOOR_POSTURE_GAP}}})"
                r"\bon\s+the\s+(?:floor|ground)\b", neg)
            if lying_floor:
                # 躺/趴/瘫在地板 —— 明确倒地，强信号
                return True, "升级(贴地描述): " + text[:110], "strong"
            if body_floor:
                # ⚠️ 2026-10-07 修正：先看"姿态词后面紧跟的支撑面"是不是家具。
                #   "sitting on a small stool on the floor" 里贴地的是凳子，人坐在家具上；
                #   旧逻辑只量"姿态词→on the floor"的字符间隔（17<20）就当身体贴地。
                #   这里把间隔段交给 _FURNITURE_SURFACE_RE 判一次，命中即整体豁免。
                if _FURNITURE_SURFACE_RE.search(body_floor.group(2)):
                    return False, text[:120], "none"
                # ⚠️ 2026-10-01 23:23 误报修正：贴地的姿态决定信号强弱。
                #   躺/瘫/蜷 = 强信号（人身明显倒地）；坐/跪/蹲 = 弱信号。
                #   弱信号必须累计 GENAI_WEAK_FRAMES 次才告警——因为模型很容易把
                #   "坐在沙发上"在一帧里误读成 "sitting on the floor"（实测原话：
                #   "The person is sitting on the floor behind a cluttered coffee table"），
                #   单帧幻觉不该惊动告警群。真实的"坐在地上"会连续多帧被描述出来，照常命中。
                _tier = ("weak" if re.fullmatch(_FLOOR_POSTURE_SEATED_RE, body_floor.group(1))
                         else "strong")
                return True, "升级(贴地描述): " + text[:110], _tier
            if limb_floor or furniture_phrase:
                return False, text[:120], "none"
            # 兜底：提到"人 + 贴地"，但既无家具也无肢体线索（如 "the person is on the floor"）。
            # 严格策略下照样升级（可能是坐/跪在地上，用户要求告警）；
            # 宽松策略下这类描述很可能只是"坐在地板上"，不升级。
            if FLOOR_ALERT_STRICT:
                # "疑似贴地"没有姿态词佐证（连坐/跪都没说清），只会更不可靠 -> 弱信号
                return True, "升级(疑似贴地): " + text[:110], "weak"
            return False, text[:120], "none"
        return False, text[:120], "none"
    # 无标记：排除 'fallen'/'fell' 这类在否定句里也会出现的词，避免误报
    kws = [k for k in FALL_KEYWORDS if k not in ("fallen", "fell")]
    return any(k in low for k in kws), text[:120], "strong"


def _interpret(text: str):
    """向后兼容的 2 元组包装：返回 (是否告警, 判定理由)。

    新代码请用 _interpret_core()（多带一个信号强度 tier，供"弱信号多次确认"使用）。
    """
    ok, why, _tier = _interpret_core(text)
    return ok, why


def confirm_need(source: str, tier: str) -> int:
    """本次命中需要累计多少次才告警（纯函数，便于回归测试）。

      • 几何通道                -> FALL_FRAMES（默认 3）：包围框比例抖动大，必须多次确认；
      • 视觉·强信号 tier=strong -> GENAI_FRAMES（默认 1）：明确倒地，立即告警，不降召回；
      • 视觉·弱信号 tier=weak   -> GENAI_WEAK_FRAMES（默认 2）：坐/跪/蹲在地上这类"软升级"，
        模型最容易把"坐在家具上"误读成"坐在地上"，要求再次佐证才告警
        —— 2026-10-01 23:23:40 实测误报的修复。
    """
    if not str(source).startswith("genai"):
        return FALL_FRAMES
    return GENAI_WEAK_FRAMES if tier == "weak" else GENAI_FRAMES


def weak_countable(last_counted_ts: float, now: float) -> bool:
    """弱信号去抖（纯函数，便于回归测试）：本次弱命中算不算一次**独立观测**。

    背景（2026-10-07 22:49 实测误报）：人坐在沙发上，模型在 22:49:08 和 22:49:24
    连续两帧把同一个人误读成 "sitting on the floor"，相隔仅 16s。旧逻辑只数命中次数，
    于是"同一次幻觉说了两遍"被当成两次独立佐证，2/2 直接告警。

    规则：
      • WEAK_MIN_GAP <= 0            -> 功能关闭，每次命中都算数；
      • 从未计过（last_counted_ts=0） -> 首次命中，算数；
      • 否则必须与上一次**已计入**的弱命中相隔 ≥ WEAK_MIN_GAP 才算新证据。
        注意基准是"已计入"而非"上一次命中"：若用后者，等间隔到达的连续贴地帧会
        永远被判定为间隔不足，真实"坐在地上"再也攒不满 2 次 -> 漏报。
    """
    if WEAK_MIN_GAP <= 0 or not last_counted_ts:
        return True
    return (now - last_counted_ts) >= WEAK_MIN_GAP


# ---------- 截图 ----------
def is_valid_jpeg(data):
    """确认拿到的是一张完整 JPEG，而不是错误页 / 占位图 / 截断帧。返回 (ok, why)。

    frigate 异常时会返回 JSON 错误体或截断数据，旧代码只判断 len>200 就直接使用，
    于是把错误页当现场画面喂给视觉模型（2026-09-20 实测）。
    """
    if not data:
        return False, "空响应"
    if len(data) < FRAME_MIN_BYTES:
        return False, f"过小({len(data)}B<{FRAME_MIN_BYTES}B)"
    if not (data[0] == 0xFF and data[1] == 0xD8 and data[2] == 0xFF):
        return False, "非JPEG头"
    if not (data[-2] == 0xFF and data[-1] == 0xD9):
        return False, "JPEG未正常结束(截断帧)"
    return True, ""


def fetch_snapshot(event_id, camera):
    """取当前画面供视觉判定。

    ⚠️ 必须用实时帧：frigate 的 `/api/events/{id}/snapshot.jpg` 和
    `/api/{cam}/person/snapshot.jpg` 都是**事件开始时冻结的帧**（实测间隔 20s 取两次
    md5 完全相同）。人若在事件进行中才摔倒，模型看到的仍是站立画面 → 判定 FALLEN: no
    → 永远不告警（2026-09-17 23:40 未告警的根因）。
    因此以 `/api/{cam}/latest.jpg` 实时帧为主，快照仅作兜底。

    ⚠️ 2026-09-20 补充：每个来源最多重试 FRAME_RETRIES 次且必须通过 JPEG 校验
    （原因见文件头 FRAME_* 注释与 is_valid_jpeg 说明），全部失败才返回 None。
    """
    for url in (
        f"{FRIGATE_URL}/api/{camera}/latest.jpg",
        f"{FRIGATE_URL}/api/{camera}/person/snapshot.jpg",
        f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg",
    ):
        why = "未知"
        for attempt in range(FRAME_RETRIES):
            try:
                data = http_get_bytes(url, timeout=10)
                ok, why = is_valid_jpeg(data)
                if ok:
                    return data
            except Exception as e:
                why = f"{type(e).__name__}"
            if attempt < FRAME_RETRIES - 1:
                time.sleep(FRAME_RETRY_SLEEP)
        log(f"[WARN] 取帧无效({why})，换下一来源: .../{url.split('/api/', 1)[-1]}")
    return None


# ---------- 视频流健康监测 ----------
_stream_state = {"bad_since": None, "last_alert": 0.0, "last_probe": 0.0,
                 "probed_ok": False, "last_recover": 0.0, "last_keep": 0.0,
                 "last_iter": 0.0, "last_mono": 0.0,
                 # 2026-09-30 新增：统一的"流通知"节流时间戳（断流告警与自愈结果共用，
                 # 保证同一次故障每 10 分钟最多打扰人一次，而不是两条消息各响各的）
                 "last_stream_push": 0.0}


def probe_frame_health(camera):
    """探测相机是否真的在出画面。返回 (healthy: bool, detail: str)。

    判据按可靠性排序：
      1) 取不到帧 / is_valid_jpeg 校验失败
      2) 帧内容命中 frigate 报错占位图指纹（断流时唯一可靠的信号）
      3) /api/stats 报 camera_fps <= 0
    ⚠️ 不要用 go2rtc 的 /api/frame.jpeg 判断：它对小米 CS2 源在**健康时也返回 500**（实测）。
    """
    try:
        data = http_get_bytes(f"{FRIGATE_URL}/api/{camera}/latest.jpg", timeout=10)
    except Exception as e:
        return False, f"取帧失败({type(e).__name__})"
    ok, why = is_valid_jpeg(data)
    if not ok:
        return False, f"帧无效({why})"
    if hashlib.md5(data).hexdigest() == ERROR_FRAME_MD5:
        return False, "frigate 报错占位图(No frames have been received)"
    fps = camera_fps(camera)
    if fps is not None and fps <= 0:
        return False, f"camera_fps={fps}"
    return True, f"{len(data)}B"


# ---------- 断流告警静默（2026-09-30 新增） ----------
def _parse_silence_expiry(txt):
    """把静默文件内容解析成"到期时间戳(epoch)"；无法解析返回 None（按永久静默处理）。

    支持：`+90m` / `90m` / `2h` / `45s` / `1d`，或绝对时间
    `2026-10-01 08:00[:00]` / `2026-10-01T08:00` / `2026-10-01`。
    """
    t = (txt or "").strip()
    if not t:
        return None
    m = re.fullmatch(r"\+?(\d+(?:\.\d+)?)\s*([smhd])", t, re.I)
    if m:
        unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
        return time.time() + float(m.group(1)) * unit
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(t, fmt))
        except ValueError:
            continue
    return None


def stream_alert_silenced():
    """返回 (是否静默, 原因说明)。探测时实时判断，改开关不需要重启进程。

    只对"视频流类通知"生效（断流告警 / 自愈结果）；跌倒告警与休眠唤醒提醒不走这里。
    """
    if STREAM_ALERT_SILENT:
        return True, "环境变量 STREAM_ALERT_SILENT=1"
    if not os.path.exists(STREAM_SILENCE_FILE):
        return False, ""
    try:
        with open(STREAM_SILENCE_FILE, encoding="utf-8") as f:
            txt = f.read().strip()
    except Exception as e:
        return True, f"静默文件存在但读取失败({type(e).__name__})，保守按静默处理"
    if not txt:
        return True, f"静默文件无到期时间({os.path.basename(STREAM_SILENCE_FILE)})，永久静默"
    exp = _parse_silence_expiry(txt)
    if exp is None:
        return True, f"静默文件内容无法解析({txt!r})，保守按永久静默处理"
    left = exp - time.time()
    if left > 0:
        return True, (f"静默至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}"
                      f"（剩 {left / 60:.0f} 分钟）")
    return False, "静默已到期"


def stream_push(md, tag, throttle=False):
    """视频流类通知的统一出口：静默则只落日志，永不静默跌倒告警。

    throttle=True 时按 STREAM_ALERT_COOLDOWN 节流（自愈结果用，避免每次自愈都打扰人）。
    返回 True 表示本次真的推送了。
    """
    sil, why = stream_alert_silenced()
    if sil:
        log(f"[SILENT] {tag}：断流告警处于静默状态（{why}），仅记录日志不推送")
        return False
    now = time.time()
    if throttle and now - _stream_state["last_stream_push"] < STREAM_ALERT_COOLDOWN:
        log(f"[THROTTLE] {tag}：距上次流通知不足 "
              f"{STREAM_ALERT_COOLDOWN / 60:.0f} 分钟，本轮不推送（仅日志）")
        return False
    try:
        r = post_webhook({"msgtype": "markdown", "markdown": {"content": md}})
        log(f"[PUSH] {tag} 已推送: {r}")
    except Exception as e:
        log(f"[ERROR] {tag} 推送失败: {e}")
        return False
    _stream_state["last_stream_push"] = now
    return True


def send_stream_alert(camera, why, down_sec):
    """视频流中断告警。

    这类故障会让整套跌倒检测**静默失效**（2026-09-20 实测停了 16 分钟没人知道），
    所以必须主动推送到群，不能只写日志。
    触发条件：连续无有效画面 ≥ STREAM_ALERT_SEC（默认 30 分钟），
    之后每 STREAM_ALERT_COOLDOWN（默认 10 分钟）重复一次。可被静默开关压制。
    """
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    sil, silwhy = stream_alert_silenced()
    md = (
        "## ⚠️ 摄像头画面中断\n"
        f"> **相机**: {camera}\n"
        f"> **时间**: {ts}\n"
        f"> **已中断**: 约 {down_sec / 60:.1f} 分钟\n"
        f"> **原因**: {why}\n\n"
        "**跌倒检测当前处于失效状态**，请检查摄像头电源与网络；"
        "若摄像头正常，可重启容器恢复连接：`docker restart frigate`\n\n"
        f"（该告警每 {STREAM_ALERT_COOLDOWN / 60:.0f} 分钟重复一次；"
        "临时静默：`fall_alert_bridge.py --silence-stream 60`，恢复：`--unsilence-stream`）"
    )
    log(f"[DETECT] 视频流已中断 {down_sec / 60:.1f} 分钟 -> 推送断流告警（{why}）"
          + ("（静默中）" if sil else ""))
    stream_push(md, "流中断告警")


def recover_stream(camera, down_sec, reason, notify=True):
    """调用 recover_monitor.sh 自愈链路（重启 go2rtc，必要时再重启 frigate 容器）。

    为什么是脚本而不是内置重启逻辑：恢复动作要复用项目里既有的进程启动方式
    （launchd 托管 + start_go2rtc.sh 兜底），脚本化后既能被 bridge 调，也能人工执行。
    `notify=False` 时只执行与记录日志、不推群（由 maybe_recover 按节流规则决定）——
    自愈本身要照常动手，不能因为"不想被打扰"就不修。
    返回 (ok, tail)：ok 表示脚本报告画面已恢复。
    """
    if not os.path.exists(RECOVER_SCRIPT):
        log(f"[RECOVER] 脚本不存在，跳过自愈: {RECOVER_SCRIPT}")
        return False, "自愈脚本不存在"
    log(f"[RECOVER] 画面异常 {down_sec:.0f}s（{reason}）-> 执行 "
          f"{os.path.basename(RECOVER_SCRIPT)}")
    ok, tail = False, ""
    try:
        p = subprocess.run(["/bin/bash", RECOVER_SCRIPT], capture_output=True,
                           text=True, timeout=RECOVER_TIMEOUT)
        ok = (p.returncode == 0)
        out = (p.stdout or "") + (p.stderr or "")
        for line in out.strip().splitlines()[-6:]:
            log(f"[RECOVER] {line}")
        tail = out.strip().splitlines()[-1] if out.strip() else ""
    except Exception as e:
        tail = f"{type(e).__name__}: {e}"
        log(f"[ERROR] 自愈脚本执行失败: {tail}")

    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    md = (
        f"## {'✅' if ok else '❌'} 画面中断自动恢复{'成功' if ok else '失败'}\n"
        f"> **相机**: {camera}\n"
        f"> **时间**: {ts}\n"
        f"> **中断时长**: 约 {down_sec / 60:.1f} 分钟\n"
        f"> **原因**: {reason}\n"
        f"> **动作**: 重启 go2rtc"
        + ("（重启后 frigate 已自行重连）" if ok else " → 重启 frigate 容器")
        + "\n"
    )
    if not ok:
        md += ("\n**自动恢复未能恢复画面，跌倒检测仍处于失效状态，"
               "请人工检查摄像头电源与网络。**\n")
    if notify:
        stream_push(md, f"自愈结果({'成功' if ok else '失败'})")
    else:
        log(f"[RECOVER] 自愈结果本轮不推送（节流）：{'成功' if ok else '失败'} {tail}")
    return ok, tail


def maybe_recover(camera, reason, down_sec=None):
    """带冷却的自愈入口。成功恢复画面返回 True。

    2026-09-30 起：自愈结果的**推送**也纳入节流——本次故障的首次尝试必推（让人知道
    系统已经在动手修），其后不再逐次推（自愈每 5 分钟重试一次，逐次推会把群刷爆），
    持续故障的"节奏"统一交给断流告警的 10 分钟重复间隔。自愈动作本身不受影响。
    """
    if not STREAM_RECOVER:
        return False
    now = time.time()
    if now - _stream_state["last_recover"] < STREAM_RECOVER_COOLDOWN:
        return False
    if down_sec is None:
        down_sec = now - (_stream_state["bad_since"] or now)
    first_try = _stream_state["last_recover"] <= 0.0
    _stream_state["last_recover"] = now
    ok, tail = recover_stream(camera, down_sec, reason, notify=first_try)
    if first_try:
        # 自愈消息已占用本次"流通知"窗口，避免紧随其后的断流告警重复轰炸同一次事故
        _stream_state["last_alert"] = max(_stream_state["last_alert"], now)
    if ok:
        _stream_state["bad_since"] = None
        _stream_state["probed_ok"] = True
        # 恢复成功 -> 复位自愈冷却与"首次"标记，下次故障重新按首次处理
        _stream_state["last_recover"] = 0.0
        return True
    return False


def notify_sleep_wake(gap_sec, camera, healthy, detail):
    """唤醒告知：休眠期间跌倒检测是失效的，必须让用户知道，而不是只在日志里留白。"""
    if not WAKE_NOTICE:
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    md = (
        "## ⏰ 监控主机休眠提醒\n"
        f"> **相机**: {camera}\n"
        f"> **时间**: {ts}\n"
        f"> **停顿时长**: 约 {gap_sec / 60:.1f} 分钟\n"
        f"> **醒来后画面**: {'正常（{0}）'.format(detail) if healthy else '异常（{0}）'.format(detail)}\n\n"
        "**休眠期间未做任何跌倒判定**（进程被系统挂起）。"
        + ("" if healthy else "\n\n已尝试自动恢复链路，详见紧随其后的恢复结果消息。")
    )
    try:
        r = post_webhook({"msgtype": "markdown", "markdown": {"content": md}})
        log(f"[WAKE] 休眠提醒已推送: {r}")
    except Exception as e:
        log(f"[ERROR] 休眠提醒推送失败: {e}")


def ensure_keep_awake(quiet=False):
    """确保有 caffeinate 常驻，阻止系统空闲休眠（AC 下 pmset sleep=10 → 10 分钟不操作就睡）。

    2026-09-23 事故根因就是休眠：22:36:22 睡 → 22:48:55 醒，12 分半里 frigate/go2rtc/
    告警桥全部被挂起，摄像头 P2P 断开且醒来后没能自愈，跌倒检测静默失效。
    """
    if not KEEP_AWAKE:
        return
    try:
        p = subprocess.run(["pgrep", "-f", "caffeinate -i -s"],
                           capture_output=True, text=True)
        pids = [x for x in p.stdout.split() if x]
        if pids:
            if not quiet:
                log(f"[KEEP] 防休眠已在运行（caffeinate pid {pids[0]}）")
            return
        proc = subprocess.Popen(KEEP_AWAKE_CMD, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        log(f"[KEEP] 已拉起 caffeinate -i -s（pid {proc.pid}），本机将不再空闲休眠")
    except Exception as e:
        log(f"[WARN] 拉起 caffeinate 失败: {type(e).__name__}: {e}")


def check_stream_health(force=False):
    """周期性检查视频流；异常时先自愈，自愈不了再告警（均带冷却）。

    告警节奏（2026-09-30 需求）：连续无画面 ≥30 分钟才告警，之后每 10 分钟重复。
    自愈链路的尝试节奏（5 分钟一次）与告警节奏解耦——修是持续修的，打扰人的
    只有那条 10 分钟一次的"确实瘫了"提醒。
    """
    now = time.time()
    if not force and now - _stream_state["last_probe"] < STREAM_PROBE_EVERY:
        return
    _stream_state["last_probe"] = now

    for cam in [c.strip() for c in CAMERAS.split(",") if c.strip()]:
        healthy, detail = probe_frame_health(cam)
        if healthy:
            if _stream_state["bad_since"] is not None:
                down = now - _stream_state["bad_since"]
                log(f"[STREAM] 相机 {cam} 画面已恢复（中断约 {down:.0f}s / {detail}）")
                _stream_state["bad_since"] = None
                _stream_state["last_recover"] = 0.0   # 复位自愈状态，下次故障重新按首次处理
            elif not _stream_state["probed_ok"]:
                # 首次探测成功也留一条记录：监测本身如果静默，我们就无从确认它还在跑
                _stream_state["probed_ok"] = True
                log(f"[STREAM] 相机 {cam} 画面正常（{detail}），画面中断监测已启用"
                      f"（连续 {STREAM_ALERT_SEC / 60:.0f} 分钟无画面告警，"
                      f"每 {STREAM_ALERT_COOLDOWN / 60:.0f} 分钟重复）")
            continue
        if _stream_state["bad_since"] is None:
            _stream_state["bad_since"] = now
            log(f"[WARN] 相机 {cam} 画面异常: {detail}"
                  f"（开始计时，{STREAM_RECOVER_AFTER:.0f}s 后自愈，"
                  f"{STREAM_ALERT_SEC / 60:.0f} 分钟后告警）")
            continue
        down = now - _stream_state["bad_since"]
        # ① 先自愈：只是告警没有意义，链路必须有人（或脚本）去拉起来
        if down >= STREAM_RECOVER_AFTER and maybe_recover(cam, detail, down):
            continue
        # ② 自愈失败或未启用自愈 -> 推一条明确的"监护失效"告警
        if down >= STREAM_ALERT_SEC and now - _stream_state["last_alert"] >= STREAM_ALERT_COOLDOWN:
            send_stream_alert(cam, detail, down)
            _stream_state["last_alert"] = now


# ---------- 告警推送（飞书 / 企业微信 双协议） ----------
# 内部统一用企业微信风格的 {"msgtype": "markdown"|"text"|"image", ...} 构造消息，
# 推送时按目标 URL 的域名判断通道类型再做协议转换。好处：业务代码只管"发一条消息"，
# 换通道（2026-09-30 企业微信 -> 飞书）不用改每个 send_* 函数。
def webhook_kind(url):
    """按 URL 判断通道类型：'feishu' 或 'wechat'。"""
    u = (url or "").lower()
    if "/open-apis/bot/v2/hook/" in u and ("feishu" in u or "larksuite" in u):
        return "feishu"
    if "qyapi.weixin.qq.com" in u:
        return "wechat"
    # 未知域名：按企业微信协议发（保持旧行为），日志里会体现
    return "wechat"


def mask_webhook(url):
    """日志脱敏：webhook 里的 key 属于凭据。

    企业微信的 key 在 query（?key=...），飞书的 key 在 path 末段（/hook/<key>），
    两种位置都要遮掉——旧实现只切 `?`，对飞书会把 key 原样打进日志。
    """
    if "?" in url:
        return url.split("?")[0] + "(key=***)"
    parts = url.rstrip("/").split("/")
    if len(parts) > 1:
        parts[-1] = "***"
    return "/".join(parts)


def _feishu_template(title):
    """按标题语义给卡片选颜色：红=告警，绿=恢复，橙=提醒。"""
    if "✅" in title or "恢复成功" in title:
        return "green"
    if "⚠️" in title or "跌倒" in title or "中断" in title:
        return "red"
    if "⏰" in title:
        return "orange"
    return "blue"


def _md_to_feishu_card(md):
    """内部 markdown -> 飞书交互卡片。

    飞书自定义机器人的 lark_md 不支持 `## 标题` 和 `> 引用`，直接发会露出原始符号，
    所以这里：首个 `## ` 行升级成卡片头（header），其余去掉 `>`/`##` 前缀后作为正文。
    """
    title = "监控告警"
    body = []
    for raw in (md or "").strip().splitlines():
        s = raw.strip()
        s = re.sub(r"^>\s*", "", s)          # 去掉企业微信的引用前缀
        if s.startswith("## "):
            s = s[3:].strip()
            if title == "监控告警" and not body:   # 第一行标题 -> 卡片头
                title = s or title
                continue
        body.append(s)
    content = "\n".join(body).strip() or title
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": _feishu_template(title),
                "title": {"tag": "plain_text", "content": title[:60]},
            },
            "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": content}}],
        },
    }


# ---------- 飞书图片：应用凭证 -> tenant_access_token -> image_key ----------
# 2026-09-30 新增。用户的诉求是"告警里要能看到现场画面"，而飞书自定义机器人
# webhook 不支持内联图片（对比：企业微信直接吃 base64+md5）。飞书只认 image_key，
# 且 image_key 只能由**具备机器人能力的应用**调 im/v1/images 上传得到
# （自定义机器人自身"不具有任何数据访问权限"）。所以这里：
#   文字卡片照旧走 webhook（不需要凭证）
#   图片：token -> 上传 -> image_key -> webhook 的 image 消息
# 结果：一张现场截图紧跟文字卡片，出现在同一个群里。
_feishu_tok = {"value": "", "exp": 0.0}


def feishu_image_enabled():
    """是否具备发图能力（配了应用凭证才算）。"""
    return bool(FEISHU_APP_ID and FEISHU_APP_SECRET)


def feishu_token(force=False):
    """取 tenant_access_token（缓存，过期前 5 分钟自动续）。返回 (token 或 None, 错误)。"""
    if not feishu_image_enabled():
        return None, "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET"
    now = time.time()
    if not force and _feishu_tok["value"] and now < _feishu_tok["exp"]:
        return _feishu_tok["value"], ""
    body = json.dumps({"app_id": FEISHU_APP_ID,
                       "app_secret": FEISHU_APP_SECRET}).encode("utf-8")
    req = urllib.request.Request(FEISHU_TOKEN_URL, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, f"获取 token HTTP {e.code}"
    except Exception as e:
        return None, f"获取 token 失败 {type(e).__name__}: {e}"
    if d.get("code") != 0:
        # 10003/10014 = app_id 或 app_secret 不正确；10004 = 应用未开启机器人能力
        return None, f"获取 token 失败 code={d.get('code')} msg={d.get('msg')}"
    _feishu_tok["value"] = d.get("tenant_access_token") or ""
    # expire 默认 7200s；留 5 分钟余量，避免在途请求用到刚过期的 token
    _feishu_tok["exp"] = now + max(int(d.get("expire") or 7200) - 300, 60)
    return _feishu_tok["value"], ""


def feishu_upload_image(image_bytes):
    """上传 JPEG 到飞书，返回 (image_key 或 None, 错误串)。"""
    if not image_bytes:
        return None, "图片内容为空"
    if len(image_bytes) > FEISHU_IMAGE_MAX:
        return None, f"图片 {len(image_bytes)} 字节超过飞书上限 {FEISHU_IMAGE_MAX}"
    tok, err = feishu_token()
    if not tok:
        return None, err
    # 手搓 multipart/form-data：标准库没有现成封装，也不想为这一处引入依赖
    boundary = "----fallalert" + uuid.uuid4().hex[:16]
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image_type"\r\n\r\n'
        "message\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="image"; filename="snapshot.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    req = urllib.request.Request(FEISHU_IMAGE_URL, data=head + image_bytes + tail,
                                method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", "Bearer " + tok)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8"))
        except Exception:
            return None, f"上传 HTTP {e.code}"
    except Exception as e:
        return None, f"上传失败 {type(e).__name__}: {e}"
    if d.get("code") != 0:
        # 234007 = 应用未开启机器人能力；99991672 = 缺少 im:resource(上传图片) 权限
        return None, f"上传失败 code={d.get('code')} msg={d.get('msg')}"
    return (d.get("data") or {}).get("image_key"), ""


def adapt_payload(url, payload):
    """按通道类型转换消息体。返回 (新 payload 或 None, 跳过原因)。"""
    if webhook_kind(url) != "feishu":
        return payload, None
    mt = payload.get("msgtype")
    if mt == "markdown":
        return _md_to_feishu_card(payload.get("markdown", {}).get("content", "")), None
    if mt == "text":
        return {"msg_type": "text",
                "content": {"text": payload.get("text", {}).get("content", "")}}, None
    if mt == "image":
        # 飞书 webhook 的 image 消息只认 image_key（不支持内联 base64）：
        # 先上传换成 image_key 再发。没有应用凭证就跳过图片，文字告警照常送达。
        img = payload.get("image") or {}
        b64 = img.get("base64")
        if not b64:
            return None, "图片内容为空"
        if not feishu_image_enabled():
            return None, (f"飞书发图需应用凭证（自建应用 + 机器人能力 + im:resource 权限）："
                          f"把 FEISHU_APP_ID / FEISHU_APP_SECRET 写进 {FEISHU_APP_CONF} "
                          f"或设为环境变量，本次仅文字告警")
        try:
            raw = base64.b64decode(b64)
        except Exception as e:
            return None, f"图片 base64 解码失败 {type(e).__name__}"
        key, err = feishu_upload_image(raw)
        if not key:
            return None, f"飞书图片上传失败：{err}"
        return {"msg_type": "image", "content": {"image_key": key}}, None
    return ({"msg_type": "text",
             "content": {"text": json.dumps(payload, ensure_ascii=False)}}, None)


def resp_ok(resp):
    """各平台成功判定：企业微信 errcode==0；飞书 code==0 或旧版 StatusCode==0。"""
    if not isinstance(resp, dict):
        return False
    if resp.get("errcode") == 0 or resp.get("code") == 0 or resp.get("StatusCode") == 0:
        return True
    return False


def post_webhook(payload):
    """把一条消息推送到**所有**已配置的群机器人（飞书 / 企业微信均可混配）。

    实测偶发 `urlopen error timed out`（本机出口走代理），单次失败就丢告警，
    所以每个 webhook 各自做 3 次重试；并且 **一个 webhook 失败不影响其它 webhook**
    （多群的价值就是冗余，不能因为 1 群挂了 2 群也收不到）。

    返回 {序号: 响应字典 或 错误字符串}；只要有一个群成功即算推送成功。
    """
    results = {}
    for idx, url in enumerate(ALERT_WEBHOOKS, 1):
        kind = webhook_kind(url)
        body, skip = adapt_payload(url, payload)
        if skip:
            results[idx] = f"SKIP[{kind}]: {skip}"
            log(f"[PUSH] webhook#{idx}({kind}) 跳过：{skip}")
            continue
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last = None
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    results[idx] = json.loads(r.read().decode("utf-8"))
                    break
            except urllib.error.HTTPError as e:
                # 飞书参数/签名错误会返回 4xx，正文里有 code/msg，读出来才有排查线索
                try:
                    results[idx] = json.loads(e.read().decode("utf-8"))
                except Exception:
                    results[idx] = f"HTTP {e.code}"
                last = e
                log(f"[ERROR] webhook#{idx}({kind}) HTTP {e.code}: {results[idx]}")
                break          # 4xx 是请求本身的问题，重试无意义
            except Exception as e:
                last = e
                if attempt < 2:
                    log(f"[WARN] webhook#{idx}({kind}) 第 {attempt + 1} 次失败"
                          f"({type(e).__name__})，重试")
                    time.sleep(2)
        else:
            results[idx] = f"ERROR {type(last).__name__}: {last}"
            log(f"[ERROR] webhook#{idx}({kind}) 3 次重试均失败: {type(last).__name__}: {last}")
    n_ok = sum(1 for v in results.values() if resp_ok(v))
    log(f"[PUSH] {n_ok}/{len(ALERT_WEBHOOKS)} 个告警通道已接收: {results}")
    return results


def send_image(image_bytes):
    """群机器人 image 消息：企业微信直接内联 base64 + md5；飞书则先上传换 image_key。

    2026-09-30 起飞书也支持发图了——代价是多一次上传请求（见 feishu_upload_image），
    所以这里保持"内部统一格式、由 adapt_payload 按通道转换"的老结构：
    业务侧只需要"发一张图"，不必关心目标平台是哪个。
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    md5 = hashlib.md5(image_bytes).hexdigest()
    return post_webhook({"msgtype": "image", "image": {"base64": b64, "md5": md5}})


def send_alert(event, method, reason, scene=""):
    """跌倒告警：一条中文文字卡片 + 一张现场截图。

    2026-09-30 调整（用户要求）：
      • 文案全中文（原来标题是 "跌倒告警 / Fall Detected" 这类中英混排）
      • 补发现场截图（飞书需应用凭证，未配置时自动降级为仅文字，告警不丢）
      • scene 用于说明"同框几人"这类会影响判定的现场信息
    """
    camera = event.get("camera", "?")
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time()))
    eid = event.get("id", "")
    md = (
        "## ⚠️ 跌倒告警\n"
        f"> **相机**: {camera}\n"
        f"> **时间**: {ts}\n"
        f"> **判定方式**: {method}\n"
        f"> **判定依据**: {reason}\n"
        + (f"> **现场情况**: {scene}\n" if scene else "")
        + "\n请确认现场情况，必要时联系相关人员。\n"
        f"事件编号：`{eid}`"
    )
    # 1) 文字告警（markdown -> 飞书交互卡片）
    try:
        r1 = post_webhook({"msgtype": "markdown", "markdown": {"content": md}})
        log(f"[ALERT] 文字告警推送: {r1}")
    except Exception as e:
        log(f"[ERROR] 文字告警推送失败: {e}")
    # 2) 现场截图（企业微信内联 base64；飞书先上传换 image_key）
    snap = fetch_snapshot(eid, camera)
    if snap:
        try:
            r2 = send_image(snap)
            log(f"[ALERT] 截图推送: {r2}")
        except Exception as e:
            log(f"[ERROR] 截图推送失败: {e}")
    else:
        log("[ALERT] 未获取到截图，已发送文字告警")


# ---------- 主循环 ----------
def main_loop():
    maintain_logs(force=True)   # 启动先做一次日志维护
    log("=" * 60)
    log("跌倒告警桥接启动 / Fall Alert Bridge")
    log(f"Frigate : {FRIGATE_URL}")
    log(f"Cameras : {CAMERAS}")
    log(f"Config  : {os.path.basename(DOTENV_FILE)} → 生效 {len(DOTENV_VALUES)} 项"
          f"{'' if os.path.exists(DOTENV_FILE) else '（文件缺失，全部走 plist/默认值）'}"
          f" | 优先级={os.environ.get('DOTENV_PRIORITY', 'env')}")
    _cfg_ip  = os.environ.get("CAMERA_IP") or ""
    _real_ip = _go2rtc_camera_ip()
    if _cfg_ip and _real_ip and _cfg_ip != _real_ip:
        log(f"[WARN] {os.path.basename(DOTENV_FILE)} 里 CAMERA_IP={_cfg_ip} 与 go2rtc.yaml "
              f"实际取流地址 {_real_ip} 不一致（取流以 go2rtc.yaml 为准，"
              f"sync_camera_ip.sh 会自动纠偏）")
    elif _cfg_ip:
        log(f"CameraIP: {_cfg_ip}（与 go2rtc.yaml 一致）")
    log(f"Keywords: {FALL_KEYWORDS}")
    # 只打印主机部分：webhook URL 里的 key 属敏感凭据，不允许落进日志文件
    log("Webhook : " + f"{len(ALERT_WEBHOOKS)} 个 -> " + ", ".join(
        f"#{i} [{webhook_kind(u)}] {mask_webhook(u)}"
        for i, u in enumerate(ALERT_WEBHOOKS, 1)))
    _sil, _silwhy = stream_alert_silenced()
    log(f"StreamSilence: {'ON' if _sil else 'OFF'}"
          + (f"（{_silwhy}）" if _sil else f"（开关：{os.path.basename(STREAM_SILENCE_FILE)}）"))
    log(f"Interval: {POLL_INTERVAL}s | Frames: {FALL_FRAMES} | "
          f"Aspect>={FALL_ASPECT} | LowerY>={FALL_LOWER} | Cooldown: {ALERT_COOLDOWN}s",
          flush=True)
    log(f"Window: {FALL_WINDOW}s | Vision: {'ON' if VISION_ENABLED else 'OFF'} | "
          f"StreamWatch: {'ON' if STREAM_WATCH else 'OFF'}"
          f"（连续 {STREAM_ALERT_SEC / 60:.0f} 分钟无画面告警，"
          f"每 {STREAM_ALERT_COOLDOWN / 60:.0f} 分钟重复）",
          flush=True)
    log(f"Policy  : 贴地="
          f"{'严格（坐在地上也告警）' if FLOOR_ALERT_STRICT else '宽松（仅明确倒地）'}"
          f"（弱信号需 {GENAI_WEAK_FRAMES} 次确认，强信号 {GENAI_FRAMES} 次）"
          f" | 同框多人（≥{MULTI_PERSON_MIN} 人）="
          f"{ {'geo': '忽略几何通道', 'all': '整条忽略', 'off': '不忽略'}[multi_person_mode()] }"
          f" | 飞书发图={'可用（凭证来源：' + FEISHU_CRED_SOURCE + '）' if feishu_image_enabled() else '不可用（缺应用凭证）'}",
          flush=True)
    ensure_keep_awake()
    log(f"Recover : {'ON' if STREAM_RECOVER else 'OFF'}"
          f"（画面异常 {STREAM_RECOVER_AFTER:.0f}s 后自愈，冷却 {STREAM_RECOVER_COOLDOWN:.0f}s）"
          f" | WakeGuard: {WAKE_GAP_SEC:.0f}s | KeepAwake: {'ON' if KEEP_AWAKE else 'OFF'}")
    log(f"LogFile : {_log_path()}（按天滚动，保留 {LOG_RETENTION_DAYS} 天；"
          f"入口 {os.path.join(LOG_DIR, LOG_PREFIX + '.latest.log')}）")
    log("=" * 60)

    # 每个 event_id 的"跌倒命中"时间戳列表 —— 滚动窗口判定
    # ⚠️ 2026-09-20 改动：原来是"连续 FALL_FRAMES 次满足"的计数器，姿态一抖就清零。
    # 实测代价：22:37:01 出现过一次 宽高比=1.74(躺平) 的完美判定，下一轮人坐起来
    # 计数即归零，告警没发出去。改为窗口内累计命中次数后，中途抖动不再丢告警。
    hits = {}
    weak_last = {}     # event_id -> 上一次**已计入**的弱命中时间戳（弱信号去抖用）
    last_alert = {}    # camera -> 上次跌倒告警时间
    last_geo_log = {}  # camera -> 上次打印几何状态的时间
    last_multi_log = {}  # camera -> 上次打印"同框多人已忽略"的时间（节流）

    while True:
        try:
            # 休眠/挂起检测（2026-09-23 事故新增）：
            # 用"墙钟增量 - 单调钟增量"识别系统睡眠——进程被挂起期间墙钟照走、
            # 单调钟(mach_absolute_time)不走，两者之差≈真实休眠时长；而单轮推理慢
            # （视觉模型 30~40s）会让两个钟同步增长，差≈0，因此不会误报。
            # 睡眠期间整条检测链是停摆的，醒来必须立刻确认画面并自愈。
            _wall_now, _mono_now = time.time(), time.monotonic()
            _wall_gap = _wall_now - _stream_state["last_iter"] if _stream_state["last_iter"] else 0.0
            _mono_gap = _mono_now - _stream_state["last_mono"] if _stream_state["last_mono"] else 0.0
            _stream_state["last_iter"], _stream_state["last_mono"] = _wall_now, _mono_now
            slept = max(_wall_gap - _mono_gap, 0.0)
            if slept >= WAKE_GAP_SEC:
                log(f"[WAKE] 检测到系统休眠约 {slept:.0f}s，立即检查画面并按需自愈")
                if STREAM_WATCH:
                    check_stream_health(force=True)
                    cams = [c.strip() for c in CAMERAS.split(",") if c.strip()]
                    cam = cams[0] if cams else "?"
                    healthy, detail = probe_frame_health(cam)
                    notify_sleep_wake(slept, cam, healthy, detail)
                    if not healthy:
                        # 醒来后画面通常已断：不等 STREAM_RECOVER_AFTER，直接动手
                        maybe_recover(cam, f"系统休眠 {slept:.0f}s 后画面未恢复", slept)
            elif _wall_gap >= STALL_GAP_SEC:
                log(f"[STALL] 主循环停顿 {_wall_gap:.0f}s（长推理/系统卡顿），强制检查画面")
                if STREAM_WATCH:
                    check_stream_health(force=True)

            # 视频流健康监测放在**轮次开头**：单轮耗时取决于活跃事件数与视觉模型
            # 调用次数，若放在轮次末尾会被长轮次严重延迟（2026-09-20 实测 100s 不触发）
            if STREAM_WATCH:
                check_stream_health()

            # 防休眠守护自检（每 5 分钟一次）：caffeinate 若被清理掉要能自己补回来
            if KEEP_AWAKE and _wall_now - _stream_state["last_keep"] >= 300:
                _stream_state["last_keep"] = _wall_now
                ensure_keep_awake(quiet=True)

            # 日志维护（默认每小时）：清理超期日志 + 滚动 logs/ 下的固定名日志。
            # 放在桥内是为了"桥活着就一定有人打扫"，不依赖外部定时任务
            # （本机 launchctl bootstrap 在受限环境下不可用，见 maintain_logs 注释）。
            maintain_logs()

            events = get_active_person_events()
            persons = count_persons_by_camera(events)
            mp_mode = multi_person_mode()
            seen = set()
            for e in events:
                eid = e.get("id")
                cam = e.get("camera", "?")
                seen.add(eid)
                box = extract_box(e)
                stationary = extract_stationary(e)
                geo_raw, reason, geo_near = geometric_fall(box, stationary)
                # 多人同框：frigate 的 person 框会被合并/拆散，几何通道不可信
                # （2026-09-30 实测宽高比只剩 0.19~0.42），按策略忽略
                multi_n = persons.get(cam, 0)
                multi = multi_n >= MULTI_PERSON_MIN
                geo_yes, reason, skip_ev = apply_multi_person_policy(
                    geo_raw, reason, multi_n, mp_mode)
                if geo_raw and not geo_yes:
                    geo_near = False        # 判定已被忽略，不再标"近失"
                if skip_ev:
                    if time.time() - last_multi_log.get(cam, 0) >= GEO_LOG_EVERY:
                        log(f"[MULTI] {cam} 同框 {multi_n} 人 -> 按 all 策略整条忽略跌倒告警")
                        last_multi_log[cam] = time.time()
                    continue
                # 几何通道可观测性：节流打印（reason 现在始终带宽高比实际值）
                if time.time() - last_geo_log.get(cam, 0) >= GEO_LOG_EVERY:
                    log(f"[GEO]{' 近失' if geo_near else ''} {cam} event={eid} -> {reason}"
                          + (f"  [同框 {multi_n} 人]" if multi else ""))
                    last_geo_log[cam] = time.time()
                is_fallen, source = geo_yes, "geo"
                # 本次命中的信号强度。几何通道命中视为强信号（它已有 FALL_FRAMES=3 次确认）。
                hit_tier = "strong"
                # 同框多人时仍走 AI 视觉通道：这是唯一能区分"两人都在坐"和
                # "一人倒地"的通道，告警里会注明场景人数，便于人工判断
                scene = (f"画面内约 {multi_n} 人（几何判定不可信，本告警来自 AI 视觉判定）"
                         if multi else "")
                # GenAI 通道：evaluated=False 表示本次只是被节流跳过/不可用，
                # 不能当成"未跌倒"（窗口判定下它只是不贡献命中）
                genai_evaluated = False
                if not geo_yes and eid:
                    # 传 event 进去，描述通道可直接复用已拿到的字段，省一次 HTTP 往返
                    gf, greason, genai_evaluated, gtier = genai_fall(eid, cam, e)
                    if gf:
                        is_fallen, reason = True, f"AI 视觉判定：{greason}"
                        # 区分是双通道里的哪一条命中的（见 genai_fall）
                        source = ("genai-frigate"
                                  if greason.startswith("frigate-genai")
                                  else "genai-local")
                        hit_tier = gtier or "strong"

                if is_fallen:
                    now = time.time()
                    win = [t for t in hits.get(eid, []) if now - t <= FALL_WINDOW]
                    # 弱信号去抖（2026-10-07）：与上一次**已计入**的弱命中相隔不足
                    # WEAK_MIN_GAP 的，视为同一次模型幻觉的延续，不重复计数。
                    # 实测事故：22:49:08 / 22:49:24 相隔仅 16s 的两次 "sitting on the
                    # floor" 是同一个坐沙发的人被连续误读，旧逻辑直接 2/2 告警。
                    counted = weak_countable(weak_last.get(eid, 0.0), now) \
                        if hit_tier == "weak" else True
                    if counted:
                        win.append(now)
                        if hit_tier == "weak":
                            weak_last[eid] = now
                    hits[eid] = win
                    # 视觉判定代价高、节流周期长(VISION_EVERY)，用独立且更低的确认阈值。
                    # 弱信号（坐/跪/蹲在地等"软升级"、疑似贴地）再单独抬高阈值：
                    # 模型很容易把"坐在沙发上"在单帧里误读成"坐在/跪在地上"
                    # （2026-10-01 23:23:40 实测误报），需累计 GENAI_WEAK_FRAMES 次佐证。
                    need = confirm_need(source, hit_tier)
                    if hit_tier == "weak" and len(win) < need:
                        if counted:
                            log(f"[WEAK] {cam} event={eid} 弱信号待确认 {len(win)}/{need}: {reason}")
                        else:
                            log(f"[WEAK-DEDUP] {cam} event={eid} 弱信号 {len(win)}/{need}"
                                + f" 距上次命中仅 {now - weak_last.get(eid, now):.0f}s"
                                + f"<{WEAK_MIN_GAP:.0f}s，判为同一次幻觉的延续，不计入: {reason[:80]}")
                    if len(win) >= need and now - last_alert.get(cam, 0) >= ALERT_COOLDOWN:
                        log(f"[DETECT] 相机={cam} 事件={eid} 窗口命中={len(win)}/{need}"
                            + ("（弱信号）" if hit_tier == "weak" else "")
                            + f" 理由={reason}")
                        # 「判定方式」按实际命中的通道标注：几何 / frigate GenAI 描述 /
                        # 本地视觉模型 —— 双通道并行时必须能看出是谁报的
                        label = {"geo": "几何判定（画面比例）",
                                 "genai-frigate": "AI 视觉判定（frigate GenAI 描述）",
                                 "genai-local": "AI 视觉判定（本地视觉模型）",
                                 }.get(source, "AI 视觉判定（画面理解）")
                        send_alert(e, label, reason, scene=scene)
                        last_alert[cam] = now
            # 清理已结束 / 已过期事件
            now = time.time()
            for eid in list(hits.keys()):
                if eid not in seen:
                    hits.pop(eid, None)
                    continue
                hits[eid] = [t for t in hits[eid] if now - t <= FALL_WINDOW]
                if not hits[eid]:
                    hits.pop(eid, None)
                    # 计数归零 → 弱信号去抖的基准也一并清掉，下一轮从第一次命中重新起算
                    weak_last.pop(eid, None)

        except Exception as e:
            log(f"[ERROR] 主循环异常: {e}")
        time.sleep(POLL_INTERVAL)


# ---------- 自测 ----------
def selftest():
    log("[SELFTEST] 开始端到端自测...")
    # 1) 几何判定逻辑
    fallen_box = [0.2, 0.55, 0.8, 0.75]   # 宽0.6 高0.2 -> ar=3.0, 重心y=0.65
    ok, reason, _near = geometric_fall(fallen_box, stationary=True)
    log(f"[SELFTEST] 模拟跌倒框={fallen_box} -> is_fallen={ok}, {reason}")
    assert ok, "几何判定应识别为跌倒"
    stand_box = [0.45, 0.1, 0.55, 0.7]   # 窄高 -> ar=0.17
    ok2, r2, _near2 = geometric_fall(stand_box, stationary=False)
    log(f"[SELFTEST] 模拟直立框={stand_box} -> is_fallen={ok2}, {r2}")
    assert not ok2, "几何判定不应把直立识别为跌倒"

    # 2) 真实推送（用一张真实相机帧作为测试图，证明 image 消息链路）
    try:
        test_img = http_get_bytes(f"{FRIGATE_URL}/api/{CAMERAS.split(',')[0].strip()}/latest.jpg", timeout=10)
    except Exception:
        test_img = None
    if not test_img:
        log("[SELFTEST] 未能获取相机帧，仅测试文本告警")
    fake_event = {
        "camera": CAMERAS.split(",")[0].strip(),
        "id": "selftest-" + uuid.uuid4().hex[:8],
    }
    send_alert(fake_event, "自测（模拟跌倒）",
               "模拟跌倒框 宽高比=3.0 重心y=0.65 静止")
    log("[SELFTEST] 完成。请在告警群中确认是否收到『自测』告警"
          f"（文字卡片 + 现场截图；截图需飞书应用凭证，当前"
          f"{'已配置' if feishu_image_enabled() else '未配置'}）。")


# ---------- 运维命令 ----------
def cmd_test_webhook():
    """给当前配置的每个告警通道发一条测试消息（用于换通道/排障后确认连通性）。"""
    log(f"[TEST] 目标通道 {len(ALERT_WEBHOOKS)} 个：" + ", ".join(
        f"#{i} [{webhook_kind(u)}] {mask_webhook(u)}"
        for i, u in enumerate(ALERT_WEBHOOKS, 1)))
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    md = (f"## 🔔 告警通道连通性测试\n"
          f"> **时间**: {ts}\n"
          f"> **来源**: fall_alert_bridge.py --test-webhook\n\n"
          f"收到本条消息说明该告警通道可用。")
    r = post_webhook({"msgtype": "markdown", "markdown": {"content": md}})
    n_ok = sum(1 for v in r.values() if resp_ok(v))
    log(f"[TEST] 结果：{n_ok}/{len(ALERT_WEBHOOKS)} 个通道返回成功 -> {r}")
    log("[TEST] 发图能力："
          + (f"已配置应用凭证（来源：{FEISHU_CRED_SOURCE}），跌倒告警会同时发送现场截图"
             if feishu_image_enabled()
             else f"未配置应用凭证 —— 把 FEISHU_APP_ID / FEISHU_APP_SECRET 写进 "
                  f"{FEISHU_APP_CONF}（或用环境变量），再跑 --test-image 验证"))
    return 0 if n_ok else 1


def cmd_test_image(args):
    """验证发图链路：图片 -> 飞书上传换 image_key -> webhook 图片消息。

    不给路径时用当前相机快照——也就是跌倒告警实际会发的那张图，
    所以这条命令能端到端证明"告警会不会带图"。
    """
    path = args[0] if args else ""
    if path:
        if not os.path.exists(path):
            log(f"[TEST] 图片不存在：{path}")
            return 1
        with open(path, "rb") as f:
            raw = f.read()
        log(f"[TEST] 使用本地图片 {path}（{_fmt_size(len(raw))}）")
    else:
        cams = [c.strip() for c in CAMERAS.split(",") if c.strip()]
        raw = fetch_snapshot("", cams[0] if cams else "?") or b""
        if not raw:
            log("[TEST] 未取到相机快照，请手动指定图片路径：--test-image /路径/图片.jpg")
            return 1
        log(f"[TEST] 使用当前相机快照（{_fmt_size(len(raw))}）")
    log("[TEST] 飞书应用凭证："
          + (f"已配置（来源：{FEISHU_CRED_SOURCE}）" if feishu_image_enabled()
             else f"未配置 —— 把 FEISHU_APP_ID / FEISHU_APP_SECRET 写进 "
                  f"{FEISHU_APP_CONF}，或设为环境变量后重启告警桥"))
    r = send_image(raw)
    for idx, v in r.items():
        log(f"[TEST] 通道#{idx} -> {v}")
    n_ok = sum(1 for v in r.values() if resp_ok(v))
    if n_ok:
        log("[TEST] 图片消息已提交，请到群里确认是否看到截图")
        return 0
    return 1


def cmd_stream_status():
    """打印当前画面健康 + 静默状态，不推送任何消息。"""
    sil, why = stream_alert_silenced()
    log("=" * 60)
    log(f"StreamWatch : {'ON' if STREAM_WATCH else 'OFF'}"
          f"（连续 {STREAM_ALERT_SEC / 60:.0f} 分钟无画面告警，"
          f"每 {STREAM_ALERT_COOLDOWN / 60:.0f} 分钟重复）")
    log(f"Recover     : {'ON' if STREAM_RECOVER else 'OFF'}"
          f"（异常 {STREAM_RECOVER_AFTER:.0f}s 后自愈，冷却 {STREAM_RECOVER_COOLDOWN:.0f}s）")
    log(f"Silence     : {'ON' if sil else 'OFF'}"
          + (f"（{why}）" if sil else "（未静默）"))
    log(f"SilenceFile : {STREAM_SILENCE_FILE}"
          + ("（存在）" if os.path.exists(STREAM_SILENCE_FILE) else "（不存在）"))
    for cam in [c.strip() for c in CAMERAS.split(",") if c.strip()]:
        healthy, detail = probe_frame_health(cam)
        log(f"Camera {cam}: {'正常' if healthy else '异常'}（{detail}）"
              + ("" if healthy else "  ⚠️ 此时不计入连续中断时长（本命令不维护计时器）"))
    log("=" * 60)
    return 0


def cmd_silence_stream(args):
    """开启静默。args 可给分钟数（到点自动恢复），不给则永久静默。"""
    minutes = None
    if args:
        try:
            minutes = float(args[0])
        except ValueError:
            log(f"[SILENCE] 分钟数无法解析: {args[0]!r}，按永久静默处理")
    if minutes and minutes > 0:
        exp = time.time() + minutes * 60
        content = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp))
        note = f"静默至 {content}（{minutes:.0f} 分钟后自动恢复）"
    else:
        content = ""
        note = "永久静默（需手动 --unsilence-stream 恢复）"
    with open(STREAM_SILENCE_FILE, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"[SILENCE] 已写入 {STREAM_SILENCE_FILE}：{note}")
    log("[SILENCE] 生效范围：断流告警 / 自愈结果；跌倒告警与休眠提醒不受影响")
    return 0


def cmd_unsilence_stream():
    if os.path.exists(STREAM_SILENCE_FILE):
        os.remove(STREAM_SILENCE_FILE)
        log(f"[SILENCE] 已删除 {STREAM_SILENCE_FILE}，断流告警恢复正常")
    else:
        log(f"[SILENCE] 静默文件不存在（当前本就未静默）：{STREAM_SILENCE_FILE}")
    return 0


def _fmt_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0


def cmd_logs():
    """列出日志目录内容与保留策略（不写入、不推送）。"""
    print(f"日志目录  : {LOG_DIR}")
    print(f"保留天数  : {LOG_RETENTION_DAYS} 天（清理间隔 {LOG_PRUNE_EVERY:.0f}s）")
    print(f"stdout镜像: {'ON' if LOG_MIRROR_STDOUT else 'OFF'}")
    print(f"当前文件  : {_log_path()}")
    latest = os.path.join(LOG_DIR, f"{LOG_PREFIX}.latest.log")
    if os.path.islink(latest):
        print(f"latest    : {latest} -> {os.readlink(latest)}")
    elif os.path.exists(latest):
        print(f"latest    : {latest}（不是符号链接）")
    else:
        print(f"latest    : (尚未创建)")
    try:
        names = sorted(os.listdir(LOG_DIR))
    except OSError as e:
        print(f"⚠️ 目录不可读: {e}")
        return 1
    today = time.strftime("%Y-%m-%d")
    print("-" * 66)
    print(f"{'文件':<44}{'大小':>10}{'保留':>10}")
    kept = expired = 0
    for name in names:
        p = os.path.join(LOG_DIR, name)
        if os.path.islink(p):
            continue
        if not os.path.isfile(p):
            continue
        m = LOG_FILE_RE.match(name)
        if m:
            d = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            try:
                age = (time.mktime(time.strptime(today, "%Y-%m-%d"))
                       - time.mktime(time.strptime(d, "%Y-%m-%d"))) / 86400.0
            except ValueError:
                age = 0
            mark = "❌超期" if age >= LOG_RETENTION_DAYS else f"{int(age)}天"
            if age >= LOG_RETENTION_DAYS:
                expired += 1
            else:
                kept += 1
        else:
            mark = "-"
        print(f"{name:<44}{_fmt_size(os.path.getsize(p)):>10}{mark:>10}")
    print("-" * 66)
    print(f"按天日志: 保留 {kept} 个 / 超期待清 {expired} 个")
    # 整目录（含其它服务日志与归档）的完整视图交给 log_maintenance
    try:
        import log_maintenance as _lm
        print()
        _lm.cmd_status(LOG_DIR, LOG_RETENTION_DAYS, LOG_ROTATE_BYTES)
    except Exception as e:
        print(f"(log_maintenance 不可用: {e})")
    return 0


def cmd_prune_logs():
    """立即执行一次完整日志维护（本桥超期日志 + logs/ 滚动与清理）。"""
    maintain_logs(force=True)
    print(f"维护完成（保留 {LOG_RETENTION_DAYS} 天，目录 {LOG_DIR}）")
    try:
        import log_maintenance as _lm
        print()
        _lm.cmd_status(LOG_DIR, LOG_RETENTION_DAYS, LOG_ROTATE_BYTES)
    except Exception as e:
        print(f"(log_maintenance 不可用: {e})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    elif len(sys.argv) > 1 and sys.argv[1] == "--bench":
        # 免费视觉模型连通性 + 延迟基准（自动切换顺序的依据）
        import free_vision
        free_vision.VisionRouter().bench()
    elif len(sys.argv) > 1 and sys.argv[1] == "--vision-list":
        import free_vision
        for _s in free_vision.VisionRouter().slots:
            log(_s)
    elif len(sys.argv) > 1 and sys.argv[1] == "--debug-events":
        evs = get_active_person_events()
        log(json.dumps(evs[:3], ensure_ascii=False, indent=2))
        if not evs:
            log("(当前无 person 事件；有人出现在镜头前时此处会出现数据，可用于确认 box 字段格式)")
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-webhook":
        sys.exit(cmd_test_webhook())
    elif len(sys.argv) > 1 and sys.argv[1] == "--stream-status":
        sys.exit(cmd_stream_status())
    elif len(sys.argv) > 1 and sys.argv[1] == "--silence-stream":
        sys.exit(cmd_silence_stream(sys.argv[2:]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--unsilence-stream":
        sys.exit(cmd_unsilence_stream())
    elif len(sys.argv) > 1 and sys.argv[1] == "--logs":
        sys.exit(cmd_logs())
    elif len(sys.argv) > 1 and sys.argv[1] == "--prune-logs":
        sys.exit(cmd_prune_logs())
    elif len(sys.argv) > 1 and sys.argv[1] == "--test-image":
        sys.exit(cmd_test_image(sys.argv[2:]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--env-check":
        # 打印 .ENV 各项当前生效值，确认键值已映射到服务
        sys.exit(cmd_env_check())
    else:
        main_loop()
