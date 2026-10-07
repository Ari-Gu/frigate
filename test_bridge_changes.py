# -*- coding: utf-8 -*-
"""表驱动回归测试：本次改动（飞书通道 / 断流阈值 / 静默开关）。"""
import importlib.util, os, sys, time, json, base64

# 让"无凭证"用例保持可复现：显式指向不存在的凭证文件。
# （本机 2026-09-30 起真的配了 feishu_app.conf，否则这条断言会因为环境而失效。）
os.environ.pop("FEISHU_APP_ID", None)
os.environ.pop("FEISHU_APP_SECRET", None)
os.environ["FEISHU_APP_CONF"] = "/nonexistent/feishu_app.conf"

P = "/Users/mac/project/frigate/fall_alert_bridge.py"
spec = importlib.util.spec_from_file_location("bridge", P)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def ck(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + f"  {name}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        fails.append(name)


print("== 1. 通道配置 ==")
ck("默认通道数=1", len(m.ALERT_WEBHOOKS) == 1, m.ALERT_WEBHOOKS)
ck("默认通道是飞书", "open.feishu.cn/open-apis/bot/v2/hook/" in m.ALERT_WEBHOOKS[0])
ck("已无企业微信 webhook", not any("qyapi.weixin" in u for u in m.ALERT_WEBHOOKS))
ck("旧名别名可用", m.WECHAT_WEBHOOKS is m.ALERT_WEBHOOKS)

print("== 2. 通道识别 ==")
ck("飞书识别", m.webhook_kind("https://open.feishu.cn/open-apis/bot/v2/hook/abc-123") == "feishu")
ck("企业微信识别", m.webhook_kind("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x") == "wechat")

print("== 3. 脱敏（不得泄露 key） ==")
fe = "https://open.feishu.cn/open-apis/bot/v2/hook/__REDACTED__"
mx = m.mask_webhook(fe)
ck("飞书 key 被遮蔽", "14a023d1" not in mx, mx)
wx = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY"
mx2 = m.mask_webhook(wx)
ck("企业微信 key 被遮蔽", "SECRETKEY" not in mx2, mx2)

print("== 4. markdown -> 飞书卡片 ==")
md = ("## ⚠️ 跌倒告警 / Fall Detected\n"
      "> **相机**: xiaomi_ptz_2k\n"
      "> **时间**: 2026-09-30 20:00:00\n\n"
      "请确认现场情况。\n事件ID: `abc`")
card = m._md_to_feishu_card(md)
ck("msg_type=interactive", card["msg_type"] == "interactive")
ck("标题升级为卡片头", card["card"]["header"]["title"]["content"] == "⚠️ 跌倒告警 / Fall Detected",
   card["card"]["header"]["title"]["content"])
ck("告警用红色", card["card"]["header"]["template"] == "red")
body = card["card"]["elements"][0]["text"]["content"]
ck("正文无 '>' 残留", ">" not in body)
ck("正文无 '##' 残留", "##" not in body)
ck("正文保留 ** 粗体", "**相机**" in body)
ck("正文保留事件ID", "abc" in body, body.replace("\n", " | "))
ok_card = m._md_to_feishu_card("## ✅ 画面中断自动恢复成功\n> **相机**: c\n")
ck("恢复成功用绿色", ok_card["card"]["header"]["template"] == "green")

print("== 5. payload 适配 ==")
p, skip = m.adapt_payload(fe, {"msgtype": "markdown", "markdown": {"content": "## 标题\n正文"}})
ck("飞书 markdown -> 卡片", skip is None and p["msg_type"] == "interactive")
p2, skip2 = m.adapt_payload(fe, {"msgtype": "image", "image": {"base64": "x", "md5": "y"}})
# 2026-09-30 起飞书支持发图（应用凭证 -> image_key）；本机未配凭证 -> 降级为只发文字
ck("飞书发图无凭证时降级跳过", p2 is None and skip2 and "FEISHU_APP_ID" in skip2, skip2)
m.feishu_upload_image = lambda raw: ("img_test", "")
m.FEISHU_APP_ID, m.FEISHU_APP_SECRET = "cli_test", "sec_test"
p2b, skip2b = m.adapt_payload(fe, {
    "msgtype": "image",
    "image": {"base64": base64.b64encode(b"\xff\xd8\xff" + b"y" * 32).decode(),
              "md5": "y"}})
ck("飞书发图有凭证时转 image_key",
   skip2b is None and p2b == {"msg_type": "image", "content": {"image_key": "img_test"}}, p2b)
m.FEISHU_APP_ID = m.FEISHU_APP_SECRET = ""
p3, skip3 = m.adapt_payload(wx, {"msgtype": "image", "image": {"base64": "x", "md5": "y"}})
ck("企业微信图片透传不变", p3["msgtype"] == "image" and skip3 is None)
p4, _ = m.adapt_payload(fe, {"msgtype": "text", "text": {"content": "hi"}})
ck("飞书 text 转换", p4 == {"msg_type": "text", "content": {"text": "hi"}})

print("== 6. 成功判定 ==")
ck("飞书 code=0", m.resp_ok({"code": 0, "msg": "success"}))
ck("飞书旧版 StatusCode=0", m.resp_ok({"StatusCode": 0}))
ck("企业微信 errcode=0", m.resp_ok({"errcode": 0, "errmsg": "ok"}))
ck("飞书鉴权失败 False", not m.resp_ok({"code": 19021, "msg": "sign match fail"}))
ck("字符串 False", not m.resp_ok("ERROR TimeoutError"))

print("== 7. 断流阈值 ==")
ck("30 分钟阈值", m.STREAM_ALERT_SEC == 1800, m.STREAM_ALERT_SEC)
ck("10 分钟重复", m.STREAM_ALERT_COOLDOWN == 600, m.STREAM_ALERT_COOLDOWN)
ck("自愈阈值仍为 60s", m.STREAM_RECOVER_AFTER == 60)

print("== 8. 静默开关 ==")
sf = m.STREAM_SILENCE_FILE
backup = None
if os.path.exists(sf):
    backup = open(sf, encoding="utf-8").read()
    os.remove(sf)
try:
    ck("无文件=不静默", m.stream_alert_silenced()[0] is False)
    open(sf, "w", encoding="utf-8").write("")
    s, why = m.stream_alert_silenced()
    ck("空文件=永久静默", s is True and "永久" in why, why)
    exp = time.time() + 3600
    open(sf, "w", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)))
    s, why = m.stream_alert_silenced()
    ck("绝对时间未到期=静默", s is True and "剩" in why, why)
    past = time.time() - 60
    open(sf, "w", encoding="utf-8").write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(past)))
    s, why = m.stream_alert_silenced()
    ck("已到期=不静默", s is False and "到期" in why, why)
    ck("相对 +90m 解析", abs(m._parse_silence_expiry("+90m") - (time.time() + 5400)) < 5)
    ck("相对 2h 解析", abs(m._parse_silence_expiry("2h") - (time.time() + 7200)) < 5)
    ck("垃圾内容->永久", m._parse_silence_expiry("随便写点什么") is None)
    # 静默时 stream_push 不推送但返回 False（用假 post_webhook 验证不会被调用）
    called = []
    orig = m.post_webhook
    m.post_webhook = lambda *a, **k: called.append(1)
    open(sf, "w", encoding="utf-8").write("")
    r = m.stream_push("## 断流告警\n测试", "测试告警")
    ck("静默时 stream_push 不推送", r is False and not called, f"called={len(called)}")
    # 未静默时应推送
    os.remove(sf)
    r2 = m.stream_push("## 断流告警\n测试", "测试告警")
    ck("未静默时 stream_push 推送", r2 is True and len(called) == 1, f"called={len(called)}")
    # 节流：第二次立刻调用应被拦住
    r3 = m.stream_push("## 断流告警\n测试", "测试告警", throttle=True)
    ck("节流生效", r3 is False and len(called) == 1, f"called={len(called)}")
    m.post_webhook = orig
finally:
    if os.path.exists(sf):
        os.remove(sf)
    if backup is not None:
        open(sf, "w", encoding="utf-8").write(backup)

print("== 9. 跌倒告警不受静默影响 ==")
src = open(P, encoding="utf-8").read()
seg = src.split("def send_alert(")[1].split("def main_loop(")[0]
ck("send_alert 不经过 stream_push", "stream_push" not in seg)
ck("send_alert 不查静默", "stream_alert_silenced" not in seg)

print()
print("失败项：", fails if fails else "无")
sys.exit(1 if fails else 0)
