#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
free_vision.py —— 免费视觉大模型路由器（延迟自适应切换）
=========================================================
给 Frigate 跌倒检测桥接器提供「看图判断」能力。

核心行为
--------
1. 多 Provider × 多免费视觉模型组成候选池
   （aihubmix / 智谱 GLM / 硅基流动 / 阿里百炼 / OpenRouter）
2. **硬超时切换**：单次调用超过 VISION_DEADLINE 秒立即放弃，换下一个模型重来
3. **慢响应降级**：耗时 > VISION_SLOW_MS 记一次 strike，达到阈值后该模型进入冷却期
4. **错误即切换**：401/403/429/5xx/网络异常同记 strike 并切换
5. **EMA 延迟跟踪**：每个候选维护滑动平均延迟，冷却结束后按历史表现自动恢复/排序
6. **全挂则降级**：所有候选不可用时返回 None，调用方回落到纯几何判定（本项目的主通道）

配置来源（优先级高 → 低）
--------------------------
- 环境变量：VISION_DEADLINE / VISION_SLOW_MS / VISION_MAX_TRIES /
            VISION_SWITCH_STRIKES / VISION_COOLDOWN / VISION_PROXY
- 各 Provider Key：AIHUBMIX_KEY / ZHIPU_KEY / SILICONFLOW_KEY /
                   DASHSCOPE_KEY / OPENROUTER_KEY
- 项目根目录 ENV 文件（url / apiBase / apiKey）→ 作为 aihubmix 的 base_url 与 key

CLI
---
  python3 free_vision.py --bench            # 连通性 + 延迟基准测试，输出排序表
  python3 free_vision.py --ask image.jpg    # 用当前最优模型问一张图
  python3 free_vision.py --list             # 列出候选池
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.environ.get("VISION_ENV_FILE", os.path.join(HERE, "ENV"))
# 2026-10-02：监控服务的集中配置 `.ENV`（KEY=VALUE）。除 ENV 外也读它，
# 同名键以 `.ENV` 为准 —— 这样 AGNES_MODELS 等大模型选择由 .ENV 统一驱动。
DOTENV_FILE = os.environ.get("DOTENV_FILE", os.path.join(HERE, ".ENV"))

# ---------- 可调参数 ----------
# agnes-3.0-flash 实测中位 ~8s、P90 ~12s、偶发 20~32s 长尾，
# 12s 会频繁触发无谓切换（切过去还要再花 5~7s），故放宽到 18s。
DEADLINE = float(os.environ.get("VISION_DEADLINE", "18"))       # 单次调用硬超时(s)，超时即切换
# 慢阈值必须高于模型正常延迟，否则 agnes 常态 6~12s 会被误判"慢"而进冷却，
# 冷却期内只剩不可达的 provider，视觉通道实际上会停摆。
SLOW_MS = float(os.environ.get("VISION_SLOW_MS", "15000"))      # 慢阈值(ms)
MAX_TRIES = int(os.environ.get("VISION_MAX_TRIES", "3"))        # 一次请求最多试几个模型
STRIKES = int(os.environ.get("VISION_SWITCH_STRIKES", "2"))     # 记几次 strike 进冷却
COOLDOWN = float(os.environ.get("VISION_COOLDOWN", "120"))      # 冷却时长(s)
MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "150"))
PROXY = os.environ.get("VISION_PROXY", "")                      # 留空则跟随环境

# 推理型（thinking）模型：输出额度被 reasoning 吃掉大半，
# max_tokens 给小了会返回空 content 但 reasoning_content 有内容。
# 实测 agnes-2.5-flash 需 >=400（reasoning 约 368 tokens）才吐结论。
MODEL_MAX_TOKENS = {
    "agnes-2.5-flash": 700,
    "agnes-2.0-flash": 700,
}

FALL_PROMPT = (
    "This is a security camera frame showing a tracked person. "
    "Decide if the person is in a FALL-RISK position: their body is at floor "
    "level — lying, reclining, sitting, kneeling or collapsed ON THE FLOOR, "
    "even if leaning against furniture. This counts as fallen. "
    "Only standing, walking, or sitting on furniture (sofa, chair, bed, stool) "
    "is normal. If no person is visible, they are normal. "
    "Reply starting with exactly \"FALLEN: yes\" or \"FALLEN: no\", then one "
    "short sentence describing the posture. Be concise."
)

# 多帧序列提示词（2026-09-20 新增）。
# 单帧无法区分"弯腰捡东西"和"正在倒下"——两者某一瞬间的静态姿态几乎一样。
# 只有序列才能看出"快速下坠 → 贴地后不再恢复站立"这个跌倒的充分特征。
FALL_PROMPT_SEQ = (
    "These are {n} sequential frames from one security camera, in chronological "
    "order (first = earliest). A tracked person is present.\n"
    "Decide whether this sequence shows a FALL. A fall means: the person loses "
    "their upright stance and ends up at floor level (lying, collapsed, "
    "sprawled, or slumped against furniture on the floor) and does NOT return "
    "to a normal upright posture by the last frame.\n"
    "Judge by CHANGE ACROSS THE FRAMES, not any single frame:\n"
    "- Bending over to pick something up, then standing back up = NOT a fall.\n"
    "- Sitting down onto a sofa/chair/stool/bed = NOT a fall.\n"
    "- Squatting or kneeling briefly, then rising = NOT a fall.\n"
    "- Rapid descent to the floor, or ending up on the floor and staying there "
    "= FALL.\n"
    "- Slumped/kneeling on the floor and unable to get up = FALL.\n"
    "- If no person is visible in the frames, answer no.\n"
    "Reply starting with exactly \"FALLEN: yes\" or \"FALLEN: no\", then one "
    "short sentence describing what changed from the first frame to the last. "
    "Be concise."
)


# ---------- ENV 文件解析 ----------
_ENV_CMT_RE = re.compile(r"\s+#")


def _strip_env_value(v: str) -> str:
    """取出 `key=` / `key:` 右侧的值：先剥行尾注释，再去两端引号。

    行尾注释规则：# 前面必须有空白才算注释，所以 `FALL_ASPECT=1.05  # 阈值`
    取到 `1.05`，而值里出现 `abc#def` 不会被误切。整行被引号包住时按字面量处理。
    """
    v = v.strip()
    if len(v) >= 2 and v[0] in "\"'" and v[-1] == v[0]:
        return v[1:-1]
    m = _ENV_CMT_RE.search(v)
    if m:
        v = v[:m.start()]
    return v.strip().strip('"').strip("'")


def _parse_env_file(path: str) -> dict:
    """解析 `key: value` 与 `key=value` 两种写法的配置文件（ENV / .ENV 通用）。"""
    out: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                ci = line.find(":")
                ei = line.find("=")
                if ci < 0 and ei < 0:
                    continue
                # 取靠前的分隔符，避免把 URL 里的 "://" 或 "key=" 拆错
                if ci >= 0 and (ei < 0 or ci < ei):
                    k, v = line[:ci], line[ci + 1:]
                else:
                    k, v = line[:ei], line[ei + 1:]
                out[k.strip()] = _strip_env_value(v)
    except FileNotFoundError:
        pass
    return out


def load_env_file(path: str = ENV_FILE) -> dict:
    """读取模型凭证与参数：ENV 打底，再叠加集中配置 `.ENV`（同名键 .ENV 优先）。

    注意：本项目 ENV 里 agnes 段用的是 `AGNES_KEY=sk-...`（等号），
    而 aihubmix 段用的是 `apiBase: https://...`（冒号），必须两种都认。
    """
    out = _parse_env_file(path)
    if os.path.abspath(DOTENV_FILE) != os.path.abspath(path):
        out.update(_parse_env_file(DOTENV_FILE))
    return out


# ---------- Provider 定义 ----------
def build_providers(env: dict | None = None) -> list:
    env = env or {}
    ef_base = (env.get("apiBase") or "").rstrip("/")
    ef_key = env.get("apiKey") or ""

    providers = [
        {
            "name": "agnes",
            "label": "Agnes AI",
            "base_url": os.environ.get("AGNES_BASE") or "https://apihub.agnes-ai.com/v1",
            "key_env": "AGNES_KEY",
            "api_key": os.environ.get("AGNES_KEY") or env.get("AGNES_KEY", ""),
            "models": _split(os.environ.get("AGNES_MODELS"), [
                "agnes-3.0-flash",   # 主选：非推理，2~7s，输出 ~20 tokens
                "agnes-2.5-flash",   # 备：推理模型，5~7s 但更准（空场景不幻觉）
            ]),
            "note": "ENV 文件 AGNES_KEY；pro 系列余额 $0 不可用，2.0-flash 免费限流严",
        },
        {
            "name": "aihubmix",
            "label": "AiHubMix 聚合",
            "base_url": os.environ.get("AIHUBMIX_BASE") or ef_base or "https://aihubmix.com/v1",
            "key_env": "AIHUBMIX_KEY",
            "api_key": os.environ.get("AIHUBMIX_KEY") or ef_key,
            "models": _split(os.environ.get("AIHUBMIX_MODELS"), [
                "gemini-3.8-flash-free",
                "gemini-3.7-flash-free",
                "gemma-4-31b-it-free",
                "nemotron-nano-12b-v2-vl-free",
                "gpt-4o-free",
                "glm-4.7-flash-free",
            ]),
            "note": "ENV 文件 Key；带 -free 后缀为免费额度模型",
        },
        {
            "name": "bigmodel",
            "label": "智谱 GLM",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "key_env": "ZHIPU_KEY",
            "api_key": os.environ.get("ZHIPU_KEY", ""),
            "models": _split(os.environ.get("ZHIPU_MODELS"), [
                "glm-4v-flash",
                "glm-4.5v",
                "glm-4v-plus",
            ]),
            "note": "glm-4v-flash 长期免费",
        },
        {
            "name": "siliconflow",
            "label": "硅基流动",
            "base_url": "https://api.siliconflow.cn/v1",
            "key_env": "SILICONFLOW_KEY",
            "api_key": os.environ.get("SILICONFLOW_KEY", ""),
            "models": _split(os.environ.get("SILICONFLOW_MODELS"), [
                "Qwen/Qwen2.5-VL-32B-Instruct",
                "Pro/Qwen/Qwen2.5-VL-7B-Instruct",
            ]),
            "note": "部分 VL 模型有免费额度",
        },
        {
            "name": "dashscope",
            "label": "阿里百炼",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "key_env": "DASHSCOPE_KEY",
            "api_key": os.environ.get("DASHSCOPE_KEY", ""),
            "models": _split(os.environ.get("DASHSCOPE_MODELS"), [
                "qwen2.5-vl-72b-instruct",
                "qwen2.5-vl-32b-instruct",
                "qwen-vl-max-latest",
            ]),
            "note": "开通即赠 tokens",
        },
        {
            "name": "openrouter",
            "label": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "key_env": "OPENROUTER_KEY",
            "api_key": os.environ.get("OPENROUTER_KEY", ""),
            "models": _split(os.environ.get("OPENROUTER_MODELS"), [
                "qwen/qwen2.5-vl-32b-instruct:free",
                "google/gemma-3-27b-it:free",
                "meta-llama/llama-3.2-11b-vision-instruct:free",
                "nvidia/llama-3.1-nemotron-nano-vl-8b-v1:free",
            ]),
            "note": ":free 后缀模型完全免费",
        },
    ]

    # aihubmix 在本机不可达（DNS 污染 + TLS RST），默认不参与候选池：
    # 否则 agnes 一旦进冷却，MAX_TRIES 会被全部浪费在这个死掉的 provider 上。
    if os.environ.get("AIHUBMIX_ENABLE", "0") not in ("1", "true", "True"):
        providers = [p for p in providers if p["name"] != "aihubmix"]
    return providers


def _split(raw: str | None, default: list) -> list:
    if not raw:
        return default
    return [m.strip() for m in raw.split(",") if m.strip()]


# ---------- 工具：生成一张最小 PNG（用于 --bench） ----------
def tiny_png(w: int = 64, h: int = 64, rgb=(190, 190, 190)) -> bytes:
    raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


# ---------- 候选槽位 ----------
class Slot:
    """一个 (provider, model) 候选，带延迟统计与健康状态。"""

    def __init__(self, provider: dict, model: str):
        self.provider = provider
        self.model = model
        self.ema_ms: float | None = None
        self.strikes = 0
        self.disabled_until = 0.0
        self.calls = 0
        self.fails = 0
        self.last_error = ""

    @property
    def key(self) -> str:
        return f"{self.provider['name']}/{self.model}"

    @property
    def available(self) -> bool:
        return bool(self.provider.get("api_key")) and time.time() >= self.disabled_until

    def record(self, ms: float) -> None:
        self.calls += 1
        self.ema_ms = ms if self.ema_ms is None else (self.ema_ms * 0.6 + ms * 0.4)

    def strike(self, reason: str) -> None:
        self.strikes += 1
        self.fails += 1
        self.last_error = reason
        if self.strikes >= STRIKES:
            self.disabled_until = time.time() + COOLDOWN

    def __repr__(self) -> str:
        ema = f"{self.ema_ms:.0f}ms" if self.ema_ms else "-"
        cd = max(0, self.disabled_until - time.time())
        state = "OK" if self.available else (f"COOLDOWN {cd:.0f}s" if cd > 0 else "NO_KEY")
        return f"{self.key:<58} ema={ema:<9} strike={self.strikes} {state} {self.last_error[:40]}"


# ---------- 路由器 ----------
class VisionRouter:
    def __init__(self, providers: list | None = None, deadline: float | None = None):
        self.env = load_env_file()
        self.providers = providers or build_providers(self.env)
        self.deadline = deadline or DEADLINE
        self.slots: list[Slot] = []
        for p in self.providers:
            for m in p["models"]:
                self.slots.append(Slot(p, m))
        self.cursor = 0

    # -- 选模型：优先可用且 EMA 最小；都不可用则挑冷却最早结束的兜底 --
    def pick(self) -> Slot | None:
        avail = [s for s in self.slots if s.available]
        if not avail:
            return None
        avail.sort(key=lambda s: (s.ema_ms is None, s.ema_ms or 0.0))
        return avail[0]

    def _fallback_slot(self, exclude: set) -> Slot | None:
        cands = [s for s in self.slots if s.available and s.key not in exclude]
        if not cands:
            cands = [s for s in self.slots if s.key not in exclude and s.provider.get("api_key")]
        if not cands:
            return None
        cands.sort(key=lambda s: (s.disabled_until, s.ema_ms or 1e9))
        return cands[0]

    # -- 单次 HTTP 调用 --
    def _call(self, slot: Slot, images, prompt: str) -> str:
        """images 可以是单个 base64 字符串，也可以是 base64 列表（多帧序列）。

        多帧时会按顺序全部塞进同一条 user message 的 content 数组，
        模型能跨帧比较姿态变化——这是区分"弯腰"与"跌倒"的关键。
        """
        if isinstance(images, str):
            images = [images]
        content: list[dict] = [{"type": "text", "text": prompt}]
        for b64 in images:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
            })
        provider = slot.provider
        payload = {
            "model": slot.model,
            "messages": [{
                "role": "user",
                "content": content,
            }],
            "max_tokens": MODEL_MAX_TOKENS.get(slot.model, MAX_TOKENS),
            "temperature": 0.1,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            provider["base_url"].rstrip("/") + "/chat/completions",
            data=data, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {provider['api_key']}")
        if provider["name"] == "openrouter":
            req.add_header("HTTP-Referer", "https://localhost/frigate")
            req.add_header("X-Title", "frigate-fall-detection")

        opener = urllib.request.build_opener()
        if PROXY:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
        with opener.open(req, timeout=self.deadline) as r:
            body = json.loads(r.read().decode("utf-8"))
        try:
            msg = body["choices"][0]["message"]
        except (KeyError, IndexError):
            raise RuntimeError(f"响应格式异常: {str(body)[:160]}")
        text = (msg.get("content") or "").strip()
        if not text:
            # 推理型模型在 max_tokens 不足/被截断时会把额度全花在 reasoning 上，
            # content 为空。此时 reasoning_content 只有思考过程、没有结论，
            # 拿来当答案会被 _interpret 的关键词兜底误判，所以按失败处理（触发切换/重试）。
            if msg.get("reasoning_content"):
                raise RuntimeError("响应只有 reasoning 无正文（max_tokens 不足或超时截断）")
            raise RuntimeError(f"响应内容为空: {str(body)[:160]}")
        return text

    # -- 对外主入口：带自动切换的分析 --
    def analyze(self, image_bytes: bytes, prompt: str = FALL_PROMPT) -> dict | None:
        """单帧分析。返回 {text, model, provider, ms, attempts}；全部失败返回 None。"""
        return self.analyze_frames([image_bytes], prompt)

    def analyze_frames(self, frames: list, prompt: str = FALL_PROMPT_SEQ) -> dict | None:
        """多帧序列分析（推荐用于跌倒判定）。

        frames 是按时间顺序排列的 JPEG 字节列表。单帧时等价于 analyze()。
        {n} 占位符会被替换成实际帧数。
        """
        images_b64 = [base64.b64encode(f).decode("ascii") for f in frames]
        return self.analyze_b64(images_b64, prompt)

    def analyze_b64(self, images_b64: list, prompt: str = FALL_PROMPT_SEQ) -> dict | None:
        """内部：已编码为 base64 的多帧分析（走同一套失败切换逻辑）。"""
        prompt = prompt.replace("{n}", str(len(images_b64)))
        tried: set = set()
        attempts = []

        for _ in range(min(MAX_TRIES, max(1, len(self.slots)))):
            slot = self.pick() if not tried else self._fallback_slot(tried)
            if slot is None or slot.key in tried:
                slot = self._fallback_slot(tried)
            if slot is None:
                break
            tried.add(slot.key)

            t0 = time.time()
            try:
                text = self._call(slot, images_b64, prompt)
            except urllib.error.HTTPError as e:
                ms = (time.time() - t0) * 1000
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:120]
                except Exception:
                    pass
                slot.strike(f"HTTP{e.code} {detail}")
                attempts.append({"model": slot.key, "ms": round(ms), "error": f"HTTP{e.code}"})
                continue
            except Exception as e:  # 超时 / DNS / 连接重置 / 解析失败
                ms = (time.time() - t0) * 1000
                kind = "TIMEOUT" if isinstance(e, TimeoutError) or "timed out" in str(e) else type(e).__name__
                slot.strike(f"{kind} {str(e)[:60]}")
                attempts.append({"model": slot.key, "ms": round(ms), "error": kind})
                continue

            ms = (time.time() - t0) * 1000
            slot.record(ms)
            attempts.append({"model": slot.key, "ms": round(ms), "error": ""})
            if ms > SLOW_MS:
                # 出结果了，但太慢 → 标记降级，下次优先换人
                slot.strike(f"SLOW {ms:.0f}ms")
            return {
                "text": text,
                "model": slot.model,
                "provider": slot.provider["name"],
                "slot": slot.key,
                "ms": round(ms),
                "attempts": attempts,
            }

        return {"text": "", "model": "", "provider": "", "slot": "",
                "ms": 0, "attempts": attempts} if attempts else None

    # -- 连通性/延迟基准 --
    def bench(self, image_bytes: bytes | None = None) -> list:
        img = image_bytes or tiny_png()
        print(f"基准参数：deadline={self.deadline}s  slow_ms={SLOW_MS}  "
              f"max_tries={MAX_TRIES}  cooldown={COOLDOWN}s")
        print("-" * 96)
        rows = []
        for slot in self.slots:
            if not slot.provider.get("api_key"):
                print(f"  {'SKIP':<8} {slot.key:<58} (未配置 Key: ${slot.provider.get('key_env', slot.provider['name'].upper()+'_KEY')})")
                rows.append({"slot": slot.key, "ok": False, "ms": None, "err": "no_key"})
                continue
            t0 = time.time()
            try:
                text = self._call(slot, base64.b64encode(img).decode("ascii"),
                                  "Reply with exactly: OK")
                ms = (time.time() - t0) * 1000
                slot.record(ms)
                if ms > SLOW_MS:
                    slot.strike(f"SLOW {ms:.0f}ms")
                flag = "OK  " if ms <= SLOW_MS else "SLOW"
                print(f"  {flag:<8} {slot.key:<58} {ms:>7.0f}ms  {text[:40]!r}")
                rows.append({"slot": slot.key, "ok": True, "ms": round(ms), "err": ""})
            except urllib.error.HTTPError as e:
                ms = (time.time() - t0) * 1000
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "ignore")[:100]
                except Exception:
                    pass
                slot.strike(f"HTTP{e.code}")
                print(f"  {'FAIL':<8} {slot.key:<58} {ms:>7.0f}ms  HTTP{e.code} {detail}")
                rows.append({"slot": slot.key, "ok": False, "ms": round(ms), "err": f"HTTP{e.code}"})
            except Exception as e:
                ms = (time.time() - t0) * 1000
                kind = "TIMEOUT" if "timed out" in str(e) else type(e).__name__
                slot.strike(kind)
                print(f"  {'FAIL':<8} {slot.key:<58} {ms:>7.0f}ms  {kind}: {str(e)[:60]}")
                rows.append({"slot": slot.key, "ok": False, "ms": round(ms), "err": kind})
        return rows


# ---------- CLI ----------
def main() -> int:
    ap = argparse.ArgumentParser(description="免费视觉大模型路由器")
    ap.add_argument("--bench", action="store_true", help="连通性 + 延迟基准测试")
    ap.add_argument("--ask", metavar="IMAGE", help="用当前最优模型分析一张图片")
    ap.add_argument("--prompt", metavar="TEXT", help="自定义提示词（配合 --ask）")
    ap.add_argument("--list", action="store_true", help="列出候选池与状态")
    ap.add_argument("--image", metavar="URL_OR_PATH", help="--bench 使用的测试图（默认生成小 PNG）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    router = VisionRouter()

    if args.list:
        for s in router.slots:
            print(s)
        return 0

    if args.ask:
        with open(args.ask, "rb") as f:
            img = f.read()
        res = router.analyze(img)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res and res.get("text") else 1

    if args.bench:
        img = None
        if args.image:
            if args.image.startswith("http"):
                img = urllib.request.urlopen(args.image, timeout=10).read()
            else:
                with open(args.image, "rb") as f:
                    img = f.read()
        rows = router.bench(img)
        ok = [r for r in rows if r["ok"]]
        print("-" * 96)
        if ok:
            ok.sort(key=lambda r: r["ms"])
            print("可用（按延迟排序）：")
            for r in ok:
                print(f"  {r['ms']:>7}ms  {r['slot']}")
        else:
            print("⚠️ 当前没有任何可用模型：请检查 Key 配置或网络可达性。")
        if args.json:
            print(json.dumps(rows, ensure_ascii=False))
        return 0 if ok else 1

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
