#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
日志维护：滚动 + 保留期清理（默认保留 7 天）
============================================
背景（2026-09-30）：
  链路里各服务的日志原先都由 launchd 的 StandardOutPath 追加到**单个固定文件**，
  没有任何轮转与清理 —— 时间一长既是磁盘负担，也让"按天回看"变得不可能
  （`fall_alert_bridge.log` 曾把多天运行混在一个文件里，时间戳还只有 HH:MM:SS）。

职责边界：
  - 本脚本负责 logs/ 目录下**所有**日志的滚动与超期清理。
  - `fall_alert_bridge.py` 自己按天写 `fall_alert_bridge-YYYY-MM-DD.log`，
    并在进程内清理自己的超期日志；本脚本是它的**兜底**（桥挂了也照常打扫）。

两件事：
  1) 滚动（roll）——针对 logs/ 下的**固定名**日志（如 sync_camera_ip.log、
     keepawake.log、*.console.log、log_maintenance.log 等）：
       触发条件：文件 > ROTATE_BYTES，**或** 最后修改日期不是今天（跨天必滚）
       动作：复制内容 → gzip 归档到 logs/archive/<名>-<时间戳>.log.gz → 原地截断
       用"复制+截断"而不是"改名"是因为 launchd 以 O_APPEND 持有这些文件的 fd，
       改名会让原进程继续往旧 inode 写、新文件永远空着（经典 logrotate 坑）；
       O_APPEND 语义下截断后新写入自然从 0 开始，是安全的。
  2) 清理（prune）——删除超过保留期的东西：
       - logs/<任意>-YYYY-MM-DD.log（按文件名日期判断）
       - logs/archive/*（按 mtime 判断，归档一经生成即不可变）

用法：
  python3 log_maintenance.py --status        # 只读：列出文件、大小、保留状态
  python3 log_maintenance.py --run           # 执行一次维护（定时任务用）
  python3 log_maintenance.py --run --dry-run # 只看会做什么，不落盘
  python3 log_maintenance.py --days 7 --rotate-bytes 2000000
环境变量：LOG_DIR / LOG_RETENTION_DAYS / LOG_ROTATE_BYTES
"""
import os
import re
import sys
import gzip
import time
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOG_DIR = os.path.join(HERE, "logs")
ARCHIVE_SUBDIR = "archive"
SELF_LOG = "log_maintenance.log"

# <任意名字>-YYYY-MM-DD.log   （日期标注式的按天日志，桥接器用的就是这种）
DATED_RE = re.compile(r"^(.+)-(\d{4})-(\d{2})-(\d{2})\.log(\.gz)?$")


def human(n):
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.0f}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024.0


def date_of(name):
    """从 <name>-YYYY-MM-DD.log 取日期串；取不到返回 None。"""
    m = DATED_RE.match(name)
    return f"{m.group(2)}-{m.group(3)}-{m.group(4)}" if m else None


def age_days(date_str):
    today = time.strftime("%Y-%m-%d")
    try:
        return (time.mktime(time.strptime(today, "%Y-%m-%d"))
                - time.mktime(time.strptime(date_str, "%Y-%m-%d"))) / 86400.0
    except ValueError:
        return None


def scan(log_dir, rotate_bytes):
    """返回 (fixed, dated, archives)——每项都是 dict 列表，供 status/run 共用。"""
    fixed, dated, archives = [], [], []
    arch_dir = os.path.join(log_dir, ARCHIVE_SUBDIR)
    try:
        names = os.listdir(log_dir)
    except OSError:
        names = []
    for name in sorted(names):
        p = os.path.join(log_dir, name)
        if os.path.islink(p) or not os.path.isfile(p):
            continue                      # 跳过 latest 符号链接 / 子目录
        size = os.path.getsize(p)
        d = date_of(name)
        if d is not None:
            dated.append({"name": name, "path": p, "size": size, "date": d,
                          "age": age_days(d)})
            continue
        mtime_date = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(p)))
        fixed.append({"name": name, "path": p, "size": size,
                      # 空文件没什么可归档的，不参与"跨天滚"，否则状态栏会一直喊要滚动
                      "stale_day": size > 0 and mtime_date != time.strftime("%Y-%m-%d"),
                      "over_size": size > rotate_bytes})
    try:
        for name in sorted(os.listdir(arch_dir)):
            p = os.path.join(arch_dir, name)
            if os.path.isfile(p):
                archives.append({"name": name, "path": p,
                                 "size": os.path.getsize(p),
                                 "age": (time.time() - os.path.getmtime(p)) / 86400.0})
    except OSError:
        pass
    return fixed, dated, archives


def roll(path, arch_dir, dry_run=False):
    """复制 + 截断式滚动；返回归档文件名（空文件跳过并返回 None）。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return f"ERR:{e}"
    if not data:
        return None
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    dest = os.path.join(arch_dir, f"{os.path.basename(path)}-{stamp}.gz")
    if dry_run:
        return os.path.basename(dest)
    os.makedirs(arch_dir, exist_ok=True)
    with gzip.open(dest, "wb") as g:
        g.write(data)
    with open(path, "wb"):        # 截断（O_APPEND fd 下安全）
        pass
    return os.path.basename(dest)


def run(log_dir, days, rotate_bytes, dry_run=False, verbose=True):
    arch_dir = os.path.join(log_dir, ARCHIVE_SUBDIR)
    fixed, dated, archives = scan(log_dir, rotate_bytes)
    rolled, pruned = [], []

    for item in fixed:
        if item["over_size"] or item["stale_day"]:
            why = []
            if item["over_size"]:
                why.append(f">{human(rotate_bytes)}")
            if item["stale_day"]:
                why.append("跨天")
            r = roll(item["path"], arch_dir, dry_run)
            if r and not r.startswith("ERR:"):
                rolled.append(f"{item['name']}({'+'.join(why)})")
            elif r and r.startswith("ERR:"):
                if verbose:
                    print(f"[WARN] 滚动失败 {item['name']}: {r[4:]}")

    for item in dated:
        if item["age"] is not None and item["age"] >= days:
            if not dry_run:
                try:
                    os.remove(item["path"])
                except OSError as e:
                    if verbose:
                        print(f"[WARN] 删除失败 {item['name']}: {e}")
                    continue
            pruned.append(item["name"])

    for item in archives:
        if item["age"] >= days:
            if not dry_run:
                try:
                    os.remove(item["path"])
                except OSError as e:
                    if verbose:
                        print(f"[WARN] 删除归档失败 {item['name']}: {e}")
                    continue
            pruned.append(f"{ARCHIVE_SUBDIR}/{item['name']}")

    return {"rolled": rolled, "pruned": pruned,
            "kept": len([d for d in dated if (d["age"] or 0) < days])}


def write_summary(log_dir, days, res, dry_run, rotate_bytes):
    """把一行摘要追加到 logs/log_maintenance.log（该文件同样受滚动/清理保护）。"""
    line = (f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"retention={days}d rotate>{human(rotate_bytes)} "
            f"rolled={len(res['rolled'])} pruned={len(res['pruned'])} "
            f"kept_dated={res['kept']}"
            + (" (dry-run)" if dry_run else ""))
    if res["rolled"]:
        line += " | rolled: " + ",".join(res["rolled"])
    if res["pruned"]:
        line += " | pruned: " + ",".join(res["pruned"][:8]) + \
                (" …" if len(res["pruned"]) > 8 else "")
    if dry_run:
        return line            # 演练模式不落盘
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, SELF_LOG), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        print(line)
    return line


def cmd_status(log_dir, days, rotate_bytes):
    fixed, dated, archives = scan(log_dir, rotate_bytes)
    print(f"日志目录   : {log_dir}")
    print(f"保留期     : {days} 天")
    print(f"滚动阈值   : {human(rotate_bytes)} 或跨天")
    print("-" * 72)
    print(f"{'文件':<42}{'大小':>10}{'状态':>16}")
    for it in dated:
        age = it["age"]
        mark = "❌超期待清" if (age is not None and age >= days) else f"保留({int(age or 0)}天)"
        print(f"{it['name']:<42}{human(it['size']):>10}{mark:>16}")
    for it in fixed:
        mark = []
        if it["over_size"]:
            mark.append("滚动:超大小")
        if it["stale_day"]:
            mark.append("滚动:跨天")
        print(f"{it['name']:<42}{human(it['size']):>10}"
              f"{('、'.join(mark) or '无需处理'):>16}")
    for it in archives:
        mark = "❌超期待清" if it["age"] >= days else f"归档({int(it['age'])}天)"
        print(f"{ARCHIVE_SUBDIR + '/' + it['name']:<42}{human(it['size']):>10}{mark:>16}")
    print("-" * 72)
    n_exp = len([d for d in dated if (d["age"] or 0) >= days]) + \
        len([a for a in archives if a["age"] >= days])
    print(f"按天日志 {len(dated)} 个 / 归档 {len(archives)} 个 / 超期待清 {n_exp} 个")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="日志滚动与保留期清理")
    ap.add_argument("--run", action="store_true", help="执行一次维护（默认动作）")
    ap.add_argument("--status", action="store_true", help="只读列出日志状态")
    ap.add_argument("--dry-run", action="store_true", help="只演示，不落盘")
    ap.add_argument("--dir", default=os.environ.get("LOG_DIR") or DEFAULT_LOG_DIR)
    ap.add_argument("--days", type=int,
                    default=int(os.environ.get("LOG_RETENTION_DAYS", "7") or 7))
    ap.add_argument("--rotate-bytes", type=int,
                    default=int(os.environ.get("LOG_ROTATE_BYTES", "2000000") or 2000000))
    a = ap.parse_args(argv)

    log_dir = os.path.abspath(a.dir)
    if a.status:
        return cmd_status(log_dir, a.days, a.rotate_bytes)
    res = run(log_dir, a.days, a.rotate_bytes, dry_run=a.dry_run)
    line = write_summary(log_dir, a.days, res, a.dry_run, a.rotate_bytes)
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
