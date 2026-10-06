"""格式保持的合成替换（FPS）。

忠实移植 SlotGuard `secrets.rs`：
  * `derive_stream` —— 由会话密钥 + 分类 + 计数派生确定性字节流（HMAC-SHA256 分块）
  * 6 个分类合成器，保持长度 / 字符集 / 前缀约定

关键性质：合成值**不依赖原文的字节**，只依赖会话密钥与计数器。否则攻击者若猜到
变换函数，仅凭上游可见的合成值就能反推原文。
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import List

from .patterns import (CAT_API_KEY, CAT_CRYPTO, CAT_DATABASE, CAT_HOST, CAT_OAUTH,
                       CAT_PII)

ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
LOWER_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
B64URL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
UPPER_ALNUM = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_STREAM_LEN = 256

_DB_URL_RE = re.compile(
    r"^(?P<scheme>postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp|mssql)://"
    r"(?P<user>[^:]+):(?P<pass>[^@]+)@(?P<host>[^:/]+)(?P<rest>(?::\d+)?(?:/.*)?)$"
)


def derive_stream(session_key: bytes, category: str, counter: int) -> bytes:
    """确定性伪随机字节流：按 block 递增做 HMAC 扩展，直到 >= 256 字节。"""
    out = bytearray()
    block = 0
    while len(out) < _STREAM_LEN:
        mac = hmac.new(session_key, digestmod=hashlib.sha256)
        mac.update(b"slotguard.v2.fps")
        mac.update(category.encode("utf-8"))
        mac.update(counter.to_bytes(8, "big"))
        mac.update(block.to_bytes(8, "big"))
        out.extend(mac.digest())
        block += 1
    return bytes(out)


def _pick(stream: bytes, offset: int, alphabet: str) -> str:
    return alphabet[stream[offset % len(stream)] % len(alphabet)]


def _fill(stream: bytes, offset: int, length: int, alphabet: str) -> str:
    if length <= 0:
        return ""
    return "".join(_pick(stream, offset + i, alphabet) for i in range(length))


# --------------------------------------------------------------------------- #
# 分类合成器
# --------------------------------------------------------------------------- #
def synth_api_key(original: str, stream: bytes) -> str:
    if original.startswith("ghp_"):
        return "ghp_" + _fill(stream, 0, len(original) - 4, ALNUM)
    if original.startswith("github_pat_"):
        return "github_pat_" + _fill(stream, 0, len(original) - 11, ALNUM)
    if original.startswith("sk-"):
        return "sk-" + _fill(stream, 0, len(original) - 3, ALNUM)
    if original.startswith("AKIA"):
        return "AKIA" + _fill(stream, 0, len(original) - 4, UPPER_ALNUM)
    # 保留已知的短前缀（sk_live_ / hf_ / AIza / glpat- / npm_ 等）
    for prefix in ("sk_live_", "sk_test_", "pk_live_", "pk_test_", "rk_live_",
                   "rk_test_", "glpat-", "npm_", "hf_"):
        if original.startswith(prefix):
            return prefix + _fill(stream, 0, len(original) - len(prefix), ALNUM)
    if original.startswith("AIza"):
        return "AIza" + _fill(stream, 0, len(original) - 4, B64URL)
    return _fill(stream, 0, max(len(original), 20), ALNUM)


def synth_oauth_token(original: str, stream: bytes) -> str:
    for prefix in ("xoxb-", "xoxa-", "xoxp-", "xoxr-", "xoxs-"):
        if original.startswith(prefix):
            return prefix + _fill(stream, 0, len(original) - len(prefix), ALNUM)
    if original.startswith("eyJ") and original.count(".") == 2:
        parts = original.split(".")
        new_parts: List[str] = []
        offset = 0
        for idx, part in enumerate(parts):
            length = len(part)
            if idx < 2:
                chunk = "eyJ" + _fill(stream, offset, max(length - 3, 0), B64URL)
            else:
                chunk = _fill(stream, offset, length, B64URL)
            offset += length
            new_parts.append(chunk)
        return ".".join(new_parts)
    return _fill(stream, 0, max(len(original), 20), ALNUM)


def synth_database_url(original: str, stream: bytes) -> str:
    m = _DB_URL_RE.match(original)
    if not m:
        return _fill(stream, 0, max(len(original), 20), ALNUM)
    user_len = max(len(m.group("user")), 4)
    pass_len = max(len(m.group("pass")), 8)
    new_user = _fill(stream, 0, user_len, LOWER_ALNUM)
    new_pass = _fill(stream, 64, pass_len, ALNUM)
    new_host = "db-" + _fill(stream, 128, 6, LOWER_ALNUM) + ".svc.synthetic"
    return f"{m.group('scheme')}://{new_user}:{new_pass}@{new_host}{m.group('rest')}"


def synth_pem_block(original: str, stream: bytes) -> str:
    lines = original.split("\n")
    if len(lines) < 3:
        return _fill(stream, 0, len(original), B64URL)
    header = lines[0]
    footer = lines[-1]
    body = [_fill(stream, idx * 64, len(line), B64URL) for idx, line in enumerate(lines[1:-1])]
    return "\n".join([header] + body + [footer])


def synth_internal_hostname(original: str, stream: bytes) -> str:
    parts = original.split(".")
    if not parts:
        return original
    # SlotGuard 用 max(len-5, 0)，短主机名会退化成 "host-"。这里给随机段
    # 保底 4 个字符，让输出看起来仍是正常主机名（角色后缀照旧保留）。
    leading_len = max(len(parts[0]), 4)
    leading = "host-" + _fill(stream, 0, max(leading_len - 5, 4), LOWER_ALNUM)
    return ".".join([leading] + parts[1:])


def synth_pii_identifier(original: str, stream: bytes) -> str:
    at = original.find("@")
    if at >= 0:
        local_len = max(at, 3)
        domain = original[at + 1:]
        new_local = _fill(stream, 0, local_len, LOWER_ALNUM)
        domain_parts = domain.split(".")
        if len(domain_parts) >= 2:
            tld = domain_parts[-1] or "example"
            return f"{new_local}@user-{_fill(stream, 32, 6, LOWER_ALNUM)}.{tld}"
        return f"{new_local}@example.com"
    return _fill(stream, 0, len(original), ALNUM)


_SYNTHESIZERS = {
    CAT_API_KEY: synth_api_key,
    CAT_OAUTH: synth_oauth_token,
    CAT_DATABASE: synth_database_url,
    CAT_CRYPTO: synth_pem_block,
    CAT_HOST: synth_internal_hostname,
    CAT_PII: synth_pii_identifier,
}


def format_preserving_synthetic(category: str, original: str, session_key: bytes,
                                counter: int) -> str:
    stream = derive_stream(session_key, category, counter)
    fn = _SYNTHESIZERS.get(category, synth_api_key)
    return fn(original, stream)
