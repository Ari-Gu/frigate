#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志持久化 / 滚动清理 的回归测试（2026-09-30 新增功能）
=====================================================
覆盖：
  A. fall_alert_bridge.py  —— 日志按天落盘、时间戳带日期、latest 符号链接、超期清理
  B. log_maintenance.py    —— copytruncate 滚动 + gzip 归档、7 天保留、跳过符号链接/空文件

全部在临时目录里跑，不碰真实日志、不发网络请求。
用法：/usr/bin/python3 test_log_rotation.py    （退出码 0 = 全过）
"""
import os
import re
import sys
import gzip
import time
import shutil
import tempfile
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = time.strftime("%Y-%m-%d")

_fail = []
_n = 0


def check(cond, label, extra=""):
    global _n
    _n += 1
    if cond:
        print(f"  ✅ {label}")
    else:
        print(f"  ❌ {label}" + (f"  [{extra}]" if extra else ""))
        _fail.append(label)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def day(offset):
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + offset * 86400))


def touch(path, content=b"", mtime_days_ago=0):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    if mtime_days_ago:
        t = time.time() - mtime_days_ago * 86400
        os.utime(path, (t, t))
    return path


def main():
    bridge = load(os.path.join(HERE, "fall_alert_bridge.py"), "bridge_ut")
    logm = load(os.path.join(HERE, "log_maintenance.py"), "logm_ut")
    tmp = tempfile.mkdtemp(prefix="logrot_test_")
    try:
        # ---------- A1. 文件名正则 ----------
        print("A. 告警桥按天日志")
        check(bool(bridge.LOG_FILE_RE.match(f"fall_alert_bridge-{TODAY}.log")),
              "匹配 fall_alert_bridge-YYYY-MM-DD.log")
        check(bool(bridge.LOG_FILE_RE.match(f"fall_alert_bridge-{TODAY}.log.gz")),
              "匹配 .log.gz 变体")
        check(not bridge.LOG_FILE_RE.match("sync_camera_ip.log"),
              "不误匹配其它服务的日志名")

        # ---------- A2. 写入按天文件 + 完整日期时间戳 ----------
        bridge.LOG_DIR = tmp
        bridge._log_state.update({"day": None, "fh": None, "last_prune": 0.0})
        bridge.log("hello-log")
        p_today = os.path.join(tmp, f"fall_alert_bridge-{TODAY}.log")
        check(os.path.exists(p_today), "写出当天文件")
        txt = open(p_today, encoding="utf-8").read()
        check(re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] hello-log",
                       txt) is not None,
              "时间戳带完整日期（修复跨天歧义）", txt.strip()[:60])

        # ---------- A3. latest 符号链接 ----------
        latest = os.path.join(tmp, "fall_alert_bridge.latest.log")
        check(os.path.islink(latest) and os.readlink(latest) ==
              f"fall_alert_bridge-{TODAY}.log", "latest 链接指向当天文件")

        # ---------- A4. 跨天自动切换文件并刷新链接 ----------
        bridge._open_log(day(-1))                       # 模拟"昨天"的文件句柄
        check(os.readlink(latest) == f"fall_alert_bridge-{day(-1)}.log",
              "切换后 latest 跟随更新")
        bridge.log("next-day")                          # day 不匹配 -> 应切回今天
        check(bridge._log_state["day"] == TODAY and
              "next-day" in open(p_today, encoding="utf-8").read(),
              "跨天写入自动切回当天文件")

        # ---------- A5. 保留 7 天：删 >7、留 <=6 ----------
        for off in (-8, -7, -6, -3):
            touch(os.path.join(tmp, f"fall_alert_bridge-{day(off)}.log"), b"x\n")
        bridge.LOG_RETENTION_DAYS = 7
        bridge._log_state["last_prune"] = 0.0
        removed = bridge.prune_logs(force=True, quiet=True)
        check(not os.path.exists(os.path.join(tmp, f"fall_alert_bridge-{day(-8)}.log")),
              "删除 8 天前")
        check(not os.path.exists(os.path.join(tmp, f"fall_alert_bridge-{day(-7)}.log")),
              "删除 7 天前（边界）")
        check(os.path.exists(os.path.join(tmp, f"fall_alert_bridge-{day(-6)}.log")),
              "保留 6 天前")
        check(os.path.exists(os.path.join(tmp, f"fall_alert_bridge-{day(-3)}.log")),
              "保留 3 天前")
        check(removed == 2, f"清理计数=2（实得 {removed}）")

        # ---------- A6. 冷却：非 force 时按 LOG_PRUNE_EVERY 节流 ----------
        bridge._log_state["last_prune"] = time.time()
        check(bridge.prune_logs() == 0, "冷却期内不重复扫描")

        # ---------- B1. copytruncate 滚动 + gzip ----------
        print("B. 日志维护 log_maintenance.py")
        d = tempfile.mkdtemp(prefix="logm_test_")
        try:
            payload = b"line1\nline2\n" * 100
            p = touch(os.path.join(d, "svc.log"), payload)
            r = logm.roll(p, os.path.join(d, "archive"))
            check(r and not r.startswith("ERR:"), "滚动返回归档名", str(r))
            arc = os.path.join(d, "archive", r)
            check(os.path.exists(arc), "归档文件生成")
            with gzip.open(arc, "rb") as g:
                check(g.read() == payload, "归档内容与原文件一致（无丢失）")
            check(os.path.getsize(p) == 0, "原文件被截断（O_APPEND 下可继续写）")
            # 截断后继续追加不应出现空洞
            with open(p, "ab") as f:
                f.write(b"after\n")
            check(os.path.getsize(p) == 6, "截断后追加正常（无稀疏空洞）")

            # 空文件不滚动
            e = touch(os.path.join(d, "empty.log"), b"")
            check(logm.roll(e, os.path.join(d, "archive")) is None, "空文件跳过滚动")

            # ---------- B2. 一次维护：跨天滚 / 超期删 / 保留期边界 ----------
            d2 = tempfile.mkdtemp(prefix="logm_run_")
            try:
                touch(os.path.join(d2, "stale.log"), b"old\n", mtime_days_ago=1)
                touch(os.path.join(d2, "fresh.log"), b"new\n", mtime_days_ago=0)
                os.symlink("stale.log", os.path.join(d2, "x.latest.log"))
                touch(os.path.join(d2, f"svc-{day(-8)}.log"), b"a\n")
                touch(os.path.join(d2, f"svc-{day(-3)}.log"), b"b\n")
                touch(os.path.join(d2, "archive", "old.gz"), b"z", mtime_days_ago=9)
                touch(os.path.join(d2, "archive", "new.gz"), b"z", mtime_days_ago=1)
                res = logm.run(d2, days=7, rotate_bytes=2_000_000)
                check("stale.log(跨天)" in res["rolled"], "跨天日志被滚动")
                check(not any("fresh.log" in x for x in res["rolled"]),
                      "当天且未超大小的日志不滚动")
                check(os.path.exists(os.path.join(d2, "x.latest.log")),
                      "符号链接被跳过（未被误删）")
                check(os.path.exists(os.path.join(d2, "archive", "stale.log")) or
                      any(n.startswith("stale.log-") for n in
                          os.listdir(os.path.join(d2, "archive"))),
                      "跨天内容已归档")
                check(not os.path.exists(os.path.join(d2, f"svc-{day(-8)}.log")),
                      "删除 8 天前的按天日志")
                check(os.path.exists(os.path.join(d2, f"svc-{day(-3)}.log")),
                      "保留 3 天前的按天日志")
                check(not os.path.exists(os.path.join(d2, "archive", "old.gz")),
                      "删除超期归档")
                check(os.path.exists(os.path.join(d2, "archive", "new.gz")),
                      "保留未超期归档")
            finally:
                shutil.rmtree(d2, ignore_errors=True)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 60)
    if _fail:
        print(f"❌ {len(_fail)}/{_n} 项失败：")
        for f in _fail:
            print("   -", f)
        return 1
    print(f"✅ 全部 {_n} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
