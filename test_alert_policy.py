# -*- coding: utf-8 -*-
"""2026-09-30 四项告警策略改动的回归测试。

覆盖：
  ① 贴地姿态严格策略（坐在地上也告警 / FLOOR_ALERT_STRICT）
  ② 多人同框策略（忽略几何通道 / MULTI_PERSON_MODE）
  ③ 飞书发图（应用凭证 -> image_key；无凭证时降级跳过）
  ④ 通知文案全中文（不含 Fall Detected / Camera Feed Lost / Geometric 这类英文）

用法：/usr/bin/python3 test_alert_policy.py    （退出码 0 = 全过）
"""
import base64
import importlib
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(HERE, "fall_alert_bridge.py")


def load(env=None, name="bridge"):
    """按指定环境变量载入一份新的模块实例（策略开关是模块级常量，必须重载）。"""
    for k in ("FLOOR_ALERT_STRICT", "FLOOR_POSTURE_GAP",
              "MULTI_PERSON_MODE", "MULTI_PERSON_MIN",
              "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_CONF"):
        os.environ.pop(k, None)
    # 明确指向不存在的凭证文件：避免本机真存在 feishu_app.conf 时把"无凭证"用例带偏
    os.environ["FEISHU_APP_CONF"] = "/nonexistent/feishu_app.conf"
    for k, v in (env or {}).items():
        os.environ[k] = str(v)
    spec = importlib.util.spec_from_file_location(name, BRIDGE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load()                      # 默认配置（严格策略 + geo + 无飞书凭证）

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  | {detail}" if detail else ""))


# ---------- ① 贴地姿态严格策略 ----------
print("\n① 贴地姿态策略（默认严格：坐在地上也告警）")
STRICT_YES = [
    ("坐在地上（无家具）", "FALLEN: no — the person is sitting on the floor."),
    ("⭐ 21:31 多主体：一人坐沙发、一人坐地上",
     "FALLEN: no One man is sitting on a couch while the other is sitting on the floor."),
    ("跪在地上", "FALLEN: no. The person is kneeling on the floor."),
    ("无明显家具/肢体线索的贴地", "FALLEN: no. The person is on the floor."),
    ("躺在地上", "FALLEN: no. The person is lying on the floor."),
    ("趴在地面", "FALLEN: no. Someone is sprawled on the ground."),
]
for why, text in STRICT_YES:
    got, reason = m._interpret(text)
    check(f"严格：{why} -> 告警", got is True, reason[:60])

STRICT_NO = [
    ("09-23 误报句：身体在沙发、腿搭地板",
     "FALLEN: no. The person is sitting on a sofa with their legs up on the floor."),
    ("站着在地板上", "FALLEN: no. The person is standing upright on the floor."),
    ("否定句里的 on the floor",
     "FALLEN: no. The person is reclining on a sofa with legs elevated, not ON THE FLOOR."),
    ("坐沙发上", "FALLEN: no The person is sitting upright on a sofa."),
]
for why, text in STRICT_NO:
    got, reason = m._interpret(text)
    check(f"严格：{why} -> 不告警", got is False, reason[:60])

# 宽松策略：坐/跪在地上不告警，明确倒地仍然告警
ml = load({"FLOOR_ALERT_STRICT": "0"}, name="bridge_loose")
print("\n① 宽松策略（FLOOR_ALERT_STRICT=0）")
for why, text in [("坐在地上", "FALLEN: no — the person is sitting on the floor."),
                  ("跪在地上", "FALLEN: no. The person is kneeling on the floor."),
                  ("无线索的贴地", "FALLEN: no. The person is on the floor.")]:
    got, _ = ml._interpret(text)
    check(f"宽松：{why} -> 不告警", got is False)
for why, text in [("躺在地上", "FALLEN: no. The person is lying on the floor."),
                  ("倒地", "FALLEN: no, the person collapsed on the ground."),
                  ("模型直接判 yes", "FALLEN: yes the man is at floor level")]:
    got, _ = ml._interpret(text)
    check(f"宽松：{why} -> 告警", got is True)

# FLOOR_POSTURE_GAP 旋钮：窗口放大到 40 后，姿态词与 "on the floor" 间隔较远的
# 描述也会被判贴地（说明开关生效）。
# ⚠️ 2026-10-07 换了探针句。原来用 09-23 的"沙发 + 腿搭地板"句，但那句现在由
# _FURNITURE_SURFACE_RE 做**结构性豁免**（身体的支撑面是沙发）——它本就是误报，
# 不应该因为 GAP 调大就复活。新探针改为"间隔长 + 无家具支撑面 + 句尾另有家具"，
# 这样 GAP 小的时候走家具豁免=False、GAP 大的时候走 body_floor=True，旋钮仍可验证。
# 注意间隔段里不能夹进第二个姿态词（严格词表含 slumped/curled 等），否则会就近命中
_GAP_PROBE = ("FALLEN: no. The person is sitting in a rather unusual position on the floor "
              "with an arm on the couch.")
mg = load({"FLOOR_POSTURE_GAP": "40"}, name="bridge_gap")
got, _ = mg._interpret(_GAP_PROBE)
check("窗口=40 时「姿态词 + 长间隔 + 贴地」命中（证明 FLOOR_POSTURE_GAP 生效）", got is True)
got, _ = m._interpret(_GAP_PROBE)
check("窗口=20 时同一句不命中（间隔 30 > 20，走家具豁免）", got is False)

# ---------- 2026-10-07 修复：支撑面豁免优先于 FLOOR_POSTURE_GAP ----------
# 22:56:25 实测：人坐在矮凳上，模型写成 "sitting on a small stool on the floor"，
# 贴地的是凳子不是身体 —— 旧逻辑按字符间隔(17<20)当成身体贴地，升成弱信号。
_SOFA_LEGS = "FALLEN: no. The person is sitting on a sofa with their legs up on the floor."
_STOOL = ("FALLEN: no. The tracked person is sitting on a small stool on the floor, "
          "which is considered a normal posture.")
check("10-07 修复：坐在矮凳上（凳子贴地）不命中", m._interpret(_STOOL)[0] is False)
check("10-07 修复：09-23 句式在 GAP=40 下仍不命中（支撑面豁免优先于 GAP）",
      mg._interpret(_SOFA_LEGS)[0] is False)
check("10-07 修复：矮凳句式在 GAP=40 下仍不命中", mg._interpret(_STOOL)[0] is False)

# ---------- ② 多人同框策略 ----------
print("\n② 多人同框策略")
persons = m.count_persons_by_camera([
    {"camera": "cam1"}, {"camera": "cam1"}, {"camera": "cam2"}, {"camera": None},
])
check("按相机统计人数", persons.get("cam1") == 2 and persons.get("cam2") == 1
      and persons.get("?") == 1, str(persons))
check("空事件列表不报错", m.count_persons_by_camera([]) == {})
check("多值归一化：ALL -> all", m.multi_person_mode.__call__() == "geo")

mm = load({"MULTI_PERSON_MODE": "ALL"}, name="bridge_all")
check("大写 ALL 归一化为 all", mm.multi_person_mode() == "all")
mb = load({"MULTI_PERSON_MODE": "whatever"}, name="bridge_bad")
check("非法值回退 geo", mb.multi_person_mode() == "geo")

# 默认（geo）：同框 2 人 -> 丢掉几何判定，但事件不跳过（仍走 AI 视觉通道）
g, r, skip = m.apply_multi_person_policy(True, "宽高比=1.30≥1.05(躺平)", 2, "geo")
check("geo：同框2人的几何判定被忽略", g is False and skip is False, r)
check("geo：忽略原因写进 reason", "同框2人" in r, r)
g, r, skip = m.apply_multi_person_policy(False, "宽高比=0.30<1.05", 2, "geo")
check("geo：同框2人的非命中判定原样保留", g is False and skip is False)
g, r, skip = m.apply_multi_person_policy(True, "r", 1, "geo")
check("geo：单人时几何通道照常采信", g is True and skip is False)
g, r, skip = m.apply_multi_person_policy(True, "r", 2, "all")
check("all：同框2人整条忽略", g is False and skip is True)
g, r, skip = m.apply_multi_person_policy(True, "r", 1, "all")
check("all：单人不受影响", g is True and skip is False)
g, r, skip = m.apply_multi_person_policy(True, "r", 3, "off")
check("off：不忽略（旧行为）", g is True and skip is False)
g, r, skip = m.apply_multi_person_policy(True, "r", 3, "geo")
check("geo：3 人也算同框多人", g is False and skip is False)

# ---------- ③ 飞书发图 ----------
print("\n③ 飞书发图链路")
FS_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/__REDACTED__"
WX_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx"
IMG_PAYLOAD = {"msgtype": "image",
               "image": {"base64": base64.b64encode(b"\xff\xd8\xff" + b"x" * 200).decode(),
                         "md5": "0" * 32}}
check("通道识别：飞书", m.webhook_kind(FS_URL) == "feishu")
check("通道识别：企业微信", m.webhook_kind(WX_URL) == "wechat")

body, skip = m.adapt_payload(FS_URL, IMG_PAYLOAD)
check("无凭证时跳过图片并给出原因",
      body is None and skip and "FEISHU_APP_ID" in skip, str(skip))
check("无凭证时 feishu_image_enabled=False", m.feishu_image_enabled() is False)
tok, err = m.feishu_token()
check("无凭证时取 token 报错但不抛异常", tok is None and "未配置" in err, err)

body, skip = m.adapt_payload(WX_URL, IMG_PAYLOAD)
check("企业微信通道的图片消息原样透传（不动 base64）", body is IMG_PAYLOAD and skip is None)

mf = load({"FEISHU_APP_ID": "cli_xxx", "FEISHU_APP_SECRET": "sec_xxx"},
          name="bridge_feishu")
check("有凭证时 feishu_image_enabled=True", mf.feishu_image_enabled() is True)

mf.feishu_upload_image = lambda raw: ("img_v2_abc", "")     # 不打网络
body, skip = mf.adapt_payload(FS_URL, IMG_PAYLOAD)
check("上传成功 -> 转成 image_key 图片消息",
      skip is None and body == {"msg_type": "image",
                                "content": {"image_key": "img_v2_abc"}}, str(body))

mf.feishu_upload_image = lambda raw: (None, "上传失败 code=234007 msg=app no bot")
body, skip = mf.adapt_payload(FS_URL, IMG_PAYLOAD)
check("上传失败 -> 跳过图片且带上错误码",
      body is None and skip and "234007" in skip, str(skip))

mf.feishu_upload_image = lambda raw: ("img_x", "")
body, skip = mf.adapt_payload(FS_URL, {"msgtype": "image", "image": {}})
check("空图片内容 -> 跳过", body is None and "为空" in (skip or ""), str(skip))

body, skip = mf.adapt_payload(FS_URL, {"msgtype": "markdown",
                                       "markdown": {"content": "## 跌倒告警\n> x"}})
check("飞书 markdown -> 交互卡片", skip is None and body.get("msg_type") == "interactive"
      and body["card"]["header"]["title"]["content"] == "跌倒告警", str(body)[:90])
check("飞书成功判定 code=0", mf.resp_ok({"code": 0, "msg": "success"}) is True)
check("飞书失败判定 code!=0", mf.resp_ok({"code": 19024, "msg": "Key Words Not Found"}) is False)
check("企业微信成功判定 errcode=0", mf.resp_ok({"errcode": 0, "errmsg": "ok"}) is True)

# 凭证文件（feishu_app.conf）：粘一次就生效，不必改 plist
conf = "/tmp/test_feishu_app.conf"
with open(conf, "w", encoding="utf-8") as f:
    f.write("# 注释行\nFEISHU_APP_ID=cli_from_file\n"
            "FEISHU_APP_SECRET=\"sec_from_file\"\n")
mc = load({"FEISHU_APP_CONF": conf}, name="bridge_conf")
check("从 feishu_app.conf 读到凭证",
      mc.feishu_image_enabled() and mc.FEISHU_APP_ID == "cli_from_file"
      and mc.FEISHU_APP_SECRET == "sec_from_file", mc.FEISHU_APP_ID)
check("凭证来源标注为文件", mc.FEISHU_CRED_SOURCE == "test_feishu_app.conf",
      mc.FEISHU_CRED_SOURCE)
mc2 = load({"FEISHU_APP_CONF": conf, "FEISHU_APP_ID": "cli_env",
            "FEISHU_APP_SECRET": "sec_env"}, name="bridge_env_win")
check("环境变量优先于凭证文件", mc2.FEISHU_APP_ID == "cli_env" and mc2.FEISHU_CRED_SOURCE == "环境变量")
check("凭证文件不存在时不报错且能力为关",
      m.feishu_image_enabled() is False and m.FEISHU_CRED_SOURCE == "")
os.remove(conf)

# ---------- ④ 通知文案全中文 ----------
print("\n④ 通知文案全中文")

BANNED = ("Fall Detected", "Camera Feed Lost", "Host Was Asleep",
          "Geometric", "GenAI", "SELFTEST", "Fall Alert")
captured = []


def _cap(payload):
    captured.append(payload)
    return {1: {"code": 0, "msg": "success"}}


m.post_webhook = _cap
m.fetch_snapshot = lambda eid, cam: None        # 不打网络、不发图
m.send_alert({"camera": "xiaomi_ptz_2k", "id": "selftest-1"},
             "几何判定（画面比例）", "宽高比=1.30≥1.05(躺平)", scene="画面内约 2 人（…）")
m.send_stream_alert("xiaomi_ptz_2k", "画面无有效帧", 2000)
m.notify_sleep_wake(300, "xiaomi_ptz_2k", True, "画面正常")

joined = "\n".join(p["markdown"]["content"] for p in captured)
print("  —— 实际发送的文案 ——")
for p in captured:
    print("   " + p["markdown"]["content"].replace("\n", " ⏎ ")[:120])
hit = [w for w in BANNED if w in joined]
check("三段通知文案均无英文残留", not hit, f"命中: {hit}")
check("跌倒告警标题为中文", "## ⚠️ 跌倒告警" in joined)
check("断流告警标题为中文", "## ⚠️ 摄像头画面中断" in joined)
check("休眠提醒标题为中文", "## ⏰ 监控主机休眠提醒" in joined)
check("跌倒告警带现场情况字段", "**现场情况**" in joined)
check("发送了 3 条通知（跌倒/断流/休眠）", len(captured) == 3, str(len(captured)))

# ---------- 结果 ----------
fails = [n for n, ok, _ in RESULTS if not ok]
print()
print(f"共 {len(RESULTS)} 项，失败 {len(fails)} 项"
      + (f" -> {fails}" if fails else " -> 全过"))
sys.exit(1 if fails else 0)
