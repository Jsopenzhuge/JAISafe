"""本地上下文脱敏（SlotGuard 思路的 Python 实现，可逆转）。

目标：不把本机绝对路径、凭证、内部主机名发给上游模型；同时在网关侧保留
handle -> raw 的映射，使**响应可以把 handle 还原成真值**，客户端零改动。

与 SlotGuard 的差异（有意为之）：
  * 只脱敏「配置的根目录 / 用户主目录之下」的路径，不再无条件改写
    `/usr/bin/xxx` 这类外部绝对路径，显著降低误伤。
  * 内部主机名统一走凭证 FPS 通道，不再单独生成 `[INTERNAL_HOST_n]` 槽位。
  * 邮箱统一走 PII 的 FPS（保持形态），不再生成 `[EMAIL_n]` 槽位。
  * 不移植 SEG 的 edges/redirections —— 实测在 SlotGuard 中该机制是死代码，
    真正的跨轮传播靠「节点集合 + 子串扫描」，此处照此实现。
"""
from __future__ import annotations

from .config import (MASK_MODES, MODE_DRY, MODE_ENFORCE, MODE_OFF, effective_mode,
                     global_mode, load_policy, policy_preview, resolve_session_id)
from .engine import MaskEngine, MaskHit, MaskPolicy
from .slots import SlotStore
from .stream import CanonicalChunkUnmasker, RebindStream
from .walk import mask_body, summarize_hits, unmask_body

__all__ = [
    "MaskEngine", "MaskPolicy", "MaskHit",
    "SlotStore", "RebindStream", "CanonicalChunkUnmasker",
    "mask_body", "unmask_body", "summarize_hits",
    "MODE_OFF", "MODE_DRY", "MODE_ENFORCE", "MASK_MODES",
    "global_mode", "effective_mode", "load_policy", "policy_preview",
    "resolve_session_id",
]
