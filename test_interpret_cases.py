# -*- coding: utf-8 -*-
"""_interpret() 兜底升级规则的回归用例表。

背景：模型答 "FALLEN: no" 时，代码会用关键词把"描述里人贴地"的情况**兜底升级**成告警
（养老场景宁可误报不漏报）。历史上这条升级规则误报过 4 次、漏报过若干次，
每次修都必须把下面这张表整体跑一遍——防误报的排除项和真需要升级的正例是同一条正则里的，
改一边很容易把另一边踩坏。

用法：/usr/bin/python3 test_interpret_cases.py    （退出码 0 = 全过）
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "bridge", os.path.join(HERE, "fall_alert_bridge.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# (期望是否升级, 说明, 模型原文)
CASES = [
    # ---------- 必须保持 no（历史误报 + 坐家具的正常姿态）----------
    (False, "09-18 误报①：沙发后仰，否定句里的 on the floor",
     "FALLEN: no. The person is reclining on a sofa with legs elevated on the chaise, not ON THE FLOOR."),
    (False, "09-18 误报②：站着拎袋子，只是'站在地板上'",
     "FALLEN: no. The person is standing upright on the floor while holding a plastic bag."),
    (False, "09-23 误报③：身体在沙发、腿搭在地板",
     "FALLEN: no. The person is sitting on a sofa with their legs up on the floor."),
    (False, "09-30 21:30:38：坐沙发 + 否定句",
     "FALLEN: no — The person is sitting upright on the sofa, not on the floor."),
    (False, "09-30 21:31:14：椅/沙发区 + 否定句",
     "FALLEN: no, the visible person is sitting on a chair/sofa area and is not on the floor."),
    (False, "纯坐沙发",
     "FALLEN: no The person is sitting upright on a sofa."),
    (False, "沙发后仰（furniture 措辞）",
     "FALLEN: no. The person is reclining on the sofa, which counts as sitting on furniture."),
    (False, "坐沙发整理茶几物品",
     "FALLEN: no. The person is sitting upright on a sofa, handling items on a coffee table."),
    (False, "无标记且无关键词",
     "The scene shows a person seated on furniture."),

    # ---------- 必须升级为告警（真实/疑似贴地）----------
    (True, "09-30 20:14 实测：坐在地上、旁边是沙发",
     "FALLEN: no The person is sitting on the floor next to the sofa, which does not indicate a collapsed or fallen state."),
    (True, "躺在地板上",
     "FALLEN: no. The person is lying on the floor."),
    (True, "倒在地面",
     "FALLEN: no, the person collapsed on the ground."),
    (True, "⭐ 09-30 21:31:26 漏报：一人坐沙发、另一人坐地上（多主体）",
     "FALLEN: no One man is sitting on a couch while the other is sitting on the floor."),
    (True, "坐在地上（无家具）",
     "FALLEN: no — the person is sitting on the floor."),
    (True, "趴在地面、沙发旁",
     "FALLEN: no. Someone is sprawled on the ground next to the sofa."),
    (True, "标记直接 yes",
     "FALLEN: yes — the man is at floor level, appearing to have fallen or collapsed"),
]


def main():
    fails = []
    for want, why, text in CASES:
        got, reason = m._interpret(text)
        ok = (got == want)
        tag = "OK  " if ok else "FAIL"
        print(f"  {tag} 期望={'升级' if want else '不升级'} 实得={'升级' if got else '不升级'}"
              f"  | {why}")
        if not ok:
            fails.append(why)
            print(f"        文本: {text}")
            print(f"        理由: {reason}")
    print()
    print(f"共 {len(CASES)} 例，失败 {len(fails)} 例" + (f" -> {fails}" if fails else " -> 全过"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
