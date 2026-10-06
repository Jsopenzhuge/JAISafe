"""凭证检测正则与分类。

移植自 SlotGuard `crates/slotguard-core/src/secrets.rs::credential_patterns`,
额外补充了若干常见形态（Google / Stripe / Azure / HuggingFace 等）以便实际可用。
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

# --------------------------------------------------------------------------- #
# 分类
# --------------------------------------------------------------------------- #
CAT_API_KEY = "api_key"
CAT_DATABASE = "database_credential"
CAT_OAUTH = "oauth_token"
CAT_PII = "pii_identifier"
CAT_CRYPTO = "cryptographic_key"
CAT_HOST = "internal_hostname"

CATEGORY_LABELS = {
    CAT_API_KEY: "API 密钥",
    CAT_DATABASE: "数据库连接串",
    CAT_OAUTH: "OAuth / JWT 令牌",
    CAT_PII: "个人标识（邮箱等）",
    CAT_CRYPTO: "私钥文件",
    CAT_HOST: "内部主机名",
}

INTERNAL_HOST_SUFFIXES = (".internal", ".intranet", ".corp", ".local", ".lan")

# --------------------------------------------------------------------------- #
# 模式表：顺序即优先级（与 SlotGuard 一致，先出现者覆盖重叠区间）
# --------------------------------------------------------------------------- #
_PATTERNS: List[Tuple[str, str]] = [
    # GitHub PAT（classic / fine-grained）
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9_])(ghp_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{82,})"),
    # Slack token
    (CAT_OAUTH, r"(?:^|[^A-Za-z0-9-])(xox[baprs]-[A-Za-z0-9-]{10,})"),
    # OpenAI 风格
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9-])(sk-[A-Za-z0-9_\-]{20,})"),
    # 常见第三方前缀密钥
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9-])((?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,})"),
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9-])(hf_[A-Za-z0-9]{30,})"),
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9-])(AIza[0-9A-Za-z_\-]{35})"),
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9-])(glpat-[A-Za-z0-9_\-]{20,})"),
    (CAT_API_KEY, r"(?:^|[^A-Za-z0-9-])(npm_[A-Za-z0-9]{36})"),
    # AWS access key id
    (CAT_API_KEY, r"(?:^|[^A-Z0-9])(AKIA[0-9A-Z]{16})"),
    # JWT
    (CAT_OAUTH, r"(?:^|[^A-Za-z0-9_-])(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})"),
    # 数据库连接串（含内嵌口令）
    (CAT_DATABASE,
     r"((?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)://[^\s:@/]+:[^\s@/]+@[^\s/]+(?:/[^\s'\"]*)?)"),
    # PEM 私钥块
    (CAT_CRYPTO,
     r"(-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]+?-----END [A-Z ]*PRIVATE KEY-----)"),
    # 内部主机名
    (CAT_HOST,
     r"(?:^|[^A-Za-z0-9-])([a-z0-9][a-z0-9-]*\.(?:internal|intranet|corp|local|lan)(?:\.[a-z0-9-]+)*)"),
    # 键值式赋值：API_KEY=... / token: ...
    (CAT_API_KEY,
     r"(?i)(?:api[_-]?key|apikey|access[_-]?key|secret[_-]?key|auth[_-]?token)\s*[=:]\s*['\"]?([A-Za-z0-9_\-./+=]{16,128})['\"]?"),
    (CAT_OAUTH, r"(?i)(?:bearer|token)\s+([A-Za-z0-9_\-./+=]{20,512})"),
    (CAT_API_KEY,
     r"(?i)(?:aws[_-]?secret[_-]?access[_-]?key|secret[_-]?key)\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
    (CAT_OAUTH,
     r"(?i)(?:secret|password|passwd|token|credential)\s*[=:]\s*['\"]?([a-f0-9]{32,128})['\"]?"),
    # PII：邮箱形态（放在最后，避免吃掉上面更具体的形态）
    (CAT_PII, r"(?i)\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b"),
]

_COMPILED: Optional[List[Tuple[str, "re.Pattern[str]"]]] = None


def compiled_patterns() -> List[Tuple[str, "re.Pattern[str]"]]:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = [(cat, re.compile(rx)) for cat, rx in _PATTERNS]
    return _COMPILED


class CredentialMatch:
    __slots__ = ("category", "raw", "start", "end", "origin")

    def __init__(self, category: str, raw: str, start: int, end: int, origin: str) -> None:
        self.category = category
        self.raw = raw
        self.start = start
        self.end = end
        self.origin = origin

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Match {self.category} {self.start}:{self.end} {self.raw[:12]!r}>"


def detect_credentials(text: str, extra_suffixes: Tuple[str, ...] = ()) -> List[CredentialMatch]:
    """按模式顺序检测，先命中者覆盖重叠区间（与 SlotGuard 一致）。"""
    hits: List[CredentialMatch] = []
    covered: List[Tuple[int, int]] = []
    for category, rx in compiled_patterns():
        for caps in rx.finditer(text):
            group = caps.group(1) if caps.lastindex else None
            if group is None:
                group = caps.group(0)
                start, end = caps.start(0), caps.end(0)
            else:
                start, end = caps.start(1), caps.end(1)
            if category == CAT_HOST and extra_suffixes:
                lowered = group.lower()
                if not any(lowered.endswith(sfx) for sfx in extra_suffixes):
                    continue
            if any(start < e and s < end for s, e in covered):
                continue
            covered.append((start, end))
            hits.append(CredentialMatch(category, group, start, end, "pattern"))
    hits.sort(key=lambda m: m.start)
    return hits


def is_internal_host(candidate: str, suffixes: Tuple[str, ...] = INTERNAL_HOST_SUFFIXES) -> bool:
    lowered = candidate.lower()
    return any(lowered.endswith(sfx) for sfx in suffixes)
