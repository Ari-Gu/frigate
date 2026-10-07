# -*- coding: utf-8 -*-
"""弱信号多次确认门控的回归测试（2026-10-02 新增）。

背景（误报事故）：2026-10-01 23:23:40，模型把"坐在沙发上"在一帧里误读成
  "The person is sitting on the floor behind a cluttered coffee table"
严格贴地策略随即把它软升级成告警，而视觉通道的确认阈值 GENAI_FRAMES=1
→ **单帧幻觉就直接发了告警**（用户报的"人坐在沙发上却告警"）。

修复：视觉命中分强/弱两档信号，用不同的确认次数（见 confirm_need()）：
  strong —— 明确证据（模型判 FALLEN: yes / 描述人躺·趴·瘫倒在地）→ 1 次即告警，不降召回；
  weak   —— 软升级（坐/跪/蹲在地上的描述、以及"疑似贴地"兜底）→ 需 GENAI_WEAK_FRAMES
            次（默认 2）累计命中才告警。真实的"坐在地上"会连续多帧被描述出来，照常命中；
            单帧幻觉则被挡掉。

用法：/usr/bin/python3 test_weak_signal.py    （退出码 0 = 全过）
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "bridge", os.path.join(HERE, "fall_alert_bridge.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def ck(name, cond, extra=""):
    print(("  OK  " if cond else "  FAIL") + f"  {name}" + (f"  -> {extra}" if extra else ""))
    if not cond:
        fails.append(name)


print("== 1. 信号分级（强 / 弱）==")
CASES = [
    ("strong", "模型直接判 yes", "FALLEN: yes — the man is at floor level, appearing to have fallen"),
    ("strong", "躺在地板上", "FALLEN: no. The person is lying on the floor."),
    ("strong", "瘫倒在地", "FALLEN: no, the person collapsed on the ground."),
    ("strong", "趴在地面", "FALLEN: no. Someone is sprawled on the ground next to the sofa."),
    ("weak", "⭐ 10-01 23:23 误报原话：坐在地上（实为坐沙发）",
     "FALLEN: no The person is sitting on the floor behind a cluttered coffee table."),
    ("weak", "坐在地上（无家具）", "FALLEN: no — the person is sitting on the floor."),
    ("weak", "跪在地上", "FALLEN: no. The person is kneeling on the floor."),
    ("weak", "⭐ 09-30 多主体：一人坐沙发、另一人坐地上",
     "FALLEN: no One man is sitting on a couch while the other is sitting on the floor."),
    ("weak", "疑似贴地（无姿态词）", "FALLEN: no. The person is on the floor."),
    ("none", "坐沙发上（正常姿态）", "FALLEN: no The person is sitting upright on the sofa."),
    ("none", "站着在地板上", "FALLEN: no. The person is standing upright on the floor."),
    ("none", "09-23 误报句：身体在沙发、腿搭地板",
     "FALLEN: no. The person is sitting on a sofa with their legs up on the floor."),
]
for want, why, text in CASES:
    ok, _r, tier = m._interpret_core(text)
    ck(f"{want:6s} | {why}", (tier if ok else "none") == want, f"tier={tier} ok={ok}")

print("\n== 2. 确认次数门控（confirm_need）==")
ck("几何通道 -> FALL_FRAMES", m.confirm_need("geo", "strong") == m.FALL_FRAMES)
ck("视觉·强信号 -> GENAI_FRAMES(=1)", m.confirm_need("genai-local", "strong") == m.GENAI_FRAMES)
ck("视觉·弱信号 -> GENAI_WEAK_FRAMES(=2)",
   m.confirm_need("genai-local", "weak") == m.GENAI_WEAK_FRAMES)
ck("frigate 描述通道同样适用", m.confirm_need("genai-frigate", "weak") == m.GENAI_WEAK_FRAMES)
ck("弱信号阈值 > 强信号阈值（这就是本次修复本身）",
   m.confirm_need("genai-local", "weak") > m.confirm_need("genai-local", "strong"))

print("\n== 3. ⭐ 复现 10-01 23:23 场景：单帧弱命中不得告警 ==")
text = "FALLEN: no The person is sitting on the floor behind a cluttered coffee table."
ok, _r, tier = m._interpret_core(text)
need = m.confirm_need("genai-local", tier)
ck("该描述被识别为弱信号", ok and tier == "weak", f"tier={tier}")
ck("需要 2 次确认", need == 2, f"need={need}")
win = [0.0]
ck("单次命中 1/2 -> 不告警（修复生效）", not (len(win) >= need), f"{len(win)}/{need}")
win.append(1.0)
ck("两次命中 2/2 -> 告警（真·坐在地上仍会报）", len(win) >= need, f"{len(win)}/{need}")

print("\n== 4. 强信号不受影响（不降召回）==")
ok, _r, tier = m._interpret_core("FALLEN: yes the man fell")
need = m.confirm_need("genai-local", tier)
ck("强信号 1 次即告警", need == 1 and len([0.0]) >= need, f"need={need}")

print("\n== 5. 开关可调（GENAI_WEAK_FRAMES 环境变量）==")
import importlib
os.environ["GENAI_WEAK_FRAMES"] = "3"
s2 = importlib.util.spec_from_file_location(
    "bridge_w3", os.path.join(HERE, "fall_alert_bridge.py"))
m3 = importlib.util.module_from_spec(s2)
s2.loader.exec_module(m3)
ck("GENAI_WEAK_FRAMES=3 生效", m3.GENAI_WEAK_FRAMES == 3
   and m3.confirm_need("genai-local", "weak") == 3)
os.environ["GENAI_WEAK_FRAMES"] = "1"
s3 = importlib.util.spec_from_file_location(
    "bridge_w1", os.path.join(HERE, "fall_alert_bridge.py"))
m1 = importlib.util.module_from_spec(s3)
s3.loader.exec_module(m1)
ck("设回 1 可恢复「单帧即报」的旧行为", m1.confirm_need("genai-local", "weak") == 1)
os.environ.pop("GENAI_WEAK_FRAMES", None)

print("\n== 6. ⭐ 弱信号去抖 WEAK_MIN_GAP（2026-10-07 22:49 坐沙发误报修复）==")
# 事故：22:49:08 / 22:49:24 相隔 16s 的两帧，模型把坐在沙发上的人连续误读成
# "sitting on the floor"，旧逻辑把"同一次幻觉说了两遍"当成两次独立佐证 -> 2/2 告警。
T0 = 1000.0
ck("首次弱命中算数（没有历史基准）", m.weak_countable(0.0, T0) is True)
ck("间隔 16s < 20s -> 同一次幻觉的延续，不计入", m.weak_countable(T0, T0 + 16) is False)
ck("间隔 20s 恰好达标 -> 计入", m.weak_countable(T0, T0 + 20) is True)
ck("间隔 32s -> 计入", m.weak_countable(T0, T0 + 32) is True)
# 文本层对"纯幻觉"无能为力（描述里既没家具也没肢体线索），只能靠去抖挡住
ck("22:49:08 原话仍是弱信号（靠去抖而非文本豁免挡住）",
   m._interpret_core("FALLEN: no The tracked person is sitting on the floor in the corner, "
                     "which counts as a fall-risk position.")[2] == "weak")


def replay(hit_times, gap, window=45.0):
    """按主循环逻辑累计弱命中，返回窗口内达到过的最大命中数。"""
    hits, last, peak = [], 0.0, 0
    for t in hit_times:
        hits = [x for x in hits if t - x <= window]
        if last == 0.0 or (t - last) >= gap:     # == weak_countable(last, t)
            hits.append(t)
            last = t
        peak = max(peak, len(hits))
    return peak


# 时间戳用 1000 起算：0.0 是 weak_last 的"从未计过"哨兵值，不能当真实时间用
ck("22:49 事故：两次幻觉相隔 16s -> 攒不到 2/2，不告警",
   replay([1000.0, 1016.0], m.WEAK_MIN_GAP) < 2,
   f"峰值={replay([1000.0, 1016.0], m.WEAK_MIN_GAP)}")
ck("真·坐在地上：0/16/32s 持续被描述成贴地 -> 仍攒满 2/2 告警（召回不降）",
   replay([1000.0, 1016.0, 1032.0], m.WEAK_MIN_GAP) >= 2,
   f"峰值={replay([1000.0, 1016.0, 1032.0], m.WEAK_MIN_GAP)}")
ck("对照：去抖关闭(WEAK_MIN_GAP=0)时同一序列会 2/2 误报（证明本修复确有作用）",
   replay([1000.0, 1016.0], 0) >= 2)

os.environ["WEAK_MIN_GAP"] = "30"
s4 = importlib.util.spec_from_file_location(
    "bridge_g30", os.path.join(HERE, "fall_alert_bridge.py"))
m4 = importlib.util.module_from_spec(s4)
s4.loader.exec_module(m4)
ck("WEAK_MIN_GAP 可调（环境变量生效）",
   m4.WEAK_MIN_GAP == 30 and m4.weak_countable(1000.0, 1020.0) is False)
os.environ.pop("WEAK_MIN_GAP", None)

print()
print(f"失败 {len(fails)} 项" + (f" -> {fails}" if fails else " -> 全过"))
sys.exit(1 if fails else 0)
