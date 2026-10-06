"""脱敏引擎单元测试 + 与 SlotGuard 语料的差分对齐。

运行: python tests/mask_test.py
差分部分需要 SlotGuard 仓库存在（默认在 ../SlotGuard-main）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

DATA_DIR = os.path.join(ROOT, "data_mask_test")
if os.path.isdir(DATA_DIR):
    shutil.rmtree(DATA_DIR, ignore_errors=True)
os.environ["JAI_DATA_DIR"] = DATA_DIR
os.environ.pop("OPENAI_BASE_URL", None)
os.environ.pop("OPENAI_API_KEY", None)

from app import db  # noqa: E402
from app.masking import MaskEngine, MaskPolicy  # noqa: E402
from app.masking.engine import mask_json_field, unmask_json_field  # noqa: E402
from app.masking.fps import format_preserving_synthetic  # noqa: E402
from app.masking.patterns import detect_credentials  # noqa: E402
from app.masking.paths import canonical, is_under, split_path  # noqa: E402
from app.masking.slots import get_store  # noqa: E402
from app.masking.stream import RebindStream  # noqa: E402

PASSED = 0
FAILED: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  [FAIL] {name} {detail}")


def engine_for(session: str = "t1", **kw: Any) -> MaskEngine:
    policy = MaskPolicy(**kw)
    return MaskEngine(policy, get_store(session))


# --------------------------------------------------------------------------- #
# 1. 路径抽象
# --------------------------------------------------------------------------- #
def test_paths() -> None:
    print("\n== 1. 路径抽象（Windows / POSIX） ==")
    check("canonical windows", canonical(r"C:\Users\me\a") == "c:/users/me/a",
          canonical(r"C:\Users\me\a"))
    check("canonical posix", canonical("/home/u/a") == "/home/u/a", canonical("/home/u/a"))
    check("windows 反斜杠等价",
          is_under(r"C:\WS\proj\a\b.py", r"C:/WS/proj") is True)
    check("大小写不敏感（win）",
          is_under(r"c:\ws\PROJ\a", r"C:\WS\proj") is True)
    check("不同风格不匹配", is_under("/home/u/a", r"C:\home\u") is False)
    check("split_path windows", split_path(r"C:\a\b\c")[1] == ["a", "b", "c"],
          str(split_path(r"C:\a\b\c")))
    check("split_path 双反斜杠", split_path(r"C:\\a\\b")[1] == ["a", "b"],
          str(split_path(r"C:\\a\\b")))

    e = engine_for("p1", workspace_roots=[r"C:\Users\me\projects\myapp"],
                   include_home=False)
    out = e.abstract_path(r"C:\Users\me\projects\myapp\src\app\relay.py")
    check("workspace 根被抽象", out is not None and out.startswith("[WORKSPACE_ROOT_1]\\"),
          str(out))
    check("保留分隔符风格", out is not None and "\\" in out, str(out))
    # 幂等：同一路径两次得到同一 handle
    out2 = e.abstract_path(r"C:\Users\me\projects\myapp\src\app\relay.py")
    check("同一路径 handle 稳定", out == out2, f"{out} vs {out2}")
    # 不同路径共享根
    out3 = e.abstract_path(r"C:\Users\me\projects\myapp\run.py")
    check("同根共享 root handle",
          out3 is not None and out3.startswith("[WORKSPACE_ROOT_1]\\"), str(out3))
    check("根目录自身",
          e.abstract_path(r"C:\Users\me\projects\myapp") == "[WORKSPACE_ROOT_1]")
    # 未配置的根不抽象
    check("范围外不抽象",
          e.abstract_path(r"D:\other\thing\file.txt") is None,
          str(e.abstract_path(r"D:\other\thing\file.txt")))
    # 敏感文件名
    e2 = engine_for("p2", workspace_roots=["/ws"], include_home=False)
    out4 = e2.abstract_path("/ws/team/layoff_plan_2026.pdf")
    check("敏感文件名抽象", out4 is not None and "SENSITIVE_DOC" in out4, str(out4))
    check("敏感文件保留扩展名", out4 is not None and out4.endswith(".pdf"), str(out4))

    # home 折叠：保留最后 N 段
    e3 = engine_for("p3", include_home=True, workspace_roots=[])
    import app.masking.paths as _p
    home = _p.home_dir()
    if home:
        deep = os.path.join(home, "a", "b", "c", "d", "e.txt")
        out5 = e3.abstract_path(deep)
        check("home 折叠隐藏中段",
              out5 is not None and "SENSITIVE_PATH_SEGMENT" in out5, str(out5))
    else:
        check("home 折叠隐藏中段", True, "无 home，跳过")


# --------------------------------------------------------------------------- #
# 2. 凭证检测与 FPS
# --------------------------------------------------------------------------- #
def test_credentials() -> None:
    print("\n== 2. 凭证检测与 FPS ==")
    cases = [
        ("ghp_" + "a" * 36, "api_key"),
        ("sk-" + "A" * 40, "api_key"),
        ("AKIA" + "A" * 16, "api_key"),
        ("xoxb-1234567890-abcdefghij", "oauth_token"),
        ("postgres://u:p@host.internal/db", "database_credential"),
        ("dave@team.example", "pii_identifier"),
        ("vault.acme-corp.internal", "internal_hostname"),
    ]
    for raw, expect in cases:
        hits = detect_credentials(raw)
        got = [h.category for h in hits]
        check(f"检测 {expect}", expect in got, f"{raw!r} -> {got}")

    pem = ("-----BEGIN RSA PRIVATE KEY-----\n" + "A" * 64 + "\n"
           "-----END RSA PRIVATE KEY-----")
    check("检测 PEM", any(h.category == "cryptographic_key"
                          for h in detect_credentials(pem)))

    key = b"k" * 32
    s1 = format_preserving_synthetic("api_key", "sk-" + "A" * 30, key, 1)
    s2 = format_preserving_synthetic("api_key", "sk-" + "A" * 30, key, 1)
    s3 = format_preserving_synthetic("api_key", "sk-" + "B" * 30, key, 1)
    check("FPS 确定性", s1 == s2, f"{s1} vs {s2}")
    check("FPS 不依赖原文", s1 == s3, f"{s1} vs {s3}")
    check("FPS 保持长度", len(s1) == len("sk-" + "A" * 30), f"{len(s1)}")
    check("FPS 保持前缀", s1.startswith("sk-"), s1)
    check("FPS 不同会话不同值",
          format_preserving_synthetic("api_key", "sk-" + "A" * 30, b"x" * 32, 1) != s1)
    check("FPS 不同计数不同值",
          format_preserving_synthetic("api_key", "sk-" + "A" * 30, key, 2) != s1)


# --------------------------------------------------------------------------- #
# 3. 往返（脱敏 -> 还原）
# --------------------------------------------------------------------------- #
def test_roundtrip() -> None:
    print("\n== 3. 脱敏/还原往返 ==")
    e = engine_for("rt1", workspace_roots=[r"C:\ws"], include_home=False)
    samples = [
        r"请读取 C:\ws\proj\src\main.py 并检查 layoff_plan_2026.pdf",
        "token 是 ghp_" + "Z" * 36 + " 别外传",
        "连接串 postgres://svc:pw123456@db.acme-corp.internal:5432/prod",
        "邮件给 dave@team.example，主机 vault.corp.internal",
        "no secrets here at all",
    ]
    for text in samples:
        masked, hits = e.mask_text(text)
        back = e.unmask_text(masked)
        check(f"往返一致: {text[:24]!r}", back == text, f"\n    masked={masked}\n    back  ={back}")
        if hits:
            check(f"确实发生脱敏: {text[:24]!r}", masked != text, masked)

    # 敏感内容确实没有出现在脱敏结果里
    masked, _ = e.mask_text("key=" + "ghp_" + "Z" * 36)
    check("脱敏后不含原凭证", "ghp_" + "Z" * 36 not in masked, masked)

    # 跨轮稳定性：同一真值第二轮仍得到同一 handle
    _, h1 = e.mask_text("file " + r"C:\ws\a\b.py")
    _, h2 = e.mask_text("again " + r"C:\ws\a\b.py")
    check("跨轮 handle 稳定",
          [h.handle for h in h1] == [h.handle for h in h2],
          f"{[h.handle for h in h1]} vs {[h.handle for h in h2]}")

    # 传播：正则因边界不匹配而漏掉的已知真值，仍应被子串扫描抓到
    e2 = engine_for("rt2", workspace_roots=["/ws"], include_home=False)
    secret = "ghp_" + "Q" * 36
    e2.mask_text("here " + secret)
    embedded = "prefix" + secret + "suffix"   # 前导字母使正则的边界类不成立
    check("正则确实漏掉该形态", detect_credentials(embedded) == [],
          str([h.raw for h in detect_credentials(embedded)]))
    masked2, hits2 = e2.mask_text("just " + embedded)
    check("片段传播命中（子串扫描兜底）", secret not in masked2, masked2)


def test_json_field() -> None:
    print("\n== 4. tool_call arguments（JSON 字符串字段） ==")
    e = engine_for("js1", workspace_roots=[r"C:\ws"], include_home=False)
    args = json.dumps({"path": r"C:\ws\proj\a.py", "token": "sk-" + "A" * 30},
                      ensure_ascii=False)
    hits: List[Any] = []
    masked = mask_json_field(args, e, hits)
    check("arguments 脱敏后仍是合法 JSON", _is_json(masked), masked)
    parsed = json.loads(masked)
    check("arguments 路径被替换", parsed["path"] != r"C:\ws\proj\a.py", str(parsed))
    check("arguments 凭证被替换", parsed["token"] != "sk-" + "A" * 30, str(parsed))
    back = unmask_json_field(masked, e)
    check("arguments 还原一致", json.loads(back) == json.loads(args),
          f"\n    masked={masked}\n    back  ={back}")
    check("还原后反斜杠正确",
          json.loads(back)["path"] == r"C:\ws\proj\a.py", str(json.loads(back)))


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# 5. 流式还原
# --------------------------------------------------------------------------- #
def test_stream() -> None:
    print("\n== 5. 流式还原（分片切断） ==")
    e = engine_for("st1", workspace_roots=[r"C:\ws"], include_home=False)
    raw = r"请读 C:\ws\proj\src\main.py 然后继续"
    masked, _ = e.mask_text(raw)
    handle = masked[masked.index("[WORKSPACE_ROOT_1]"):][:len("[WORKSPACE_ROOT_1]")]

    # 每个分片 1 个字符，最大化切断概率
    s = RebindStream(e)
    out = "".join(s.push(ch) for ch in masked) + s.close()
    check("逐字符分片可完整还原", out == raw, f"\n    out={out}\n    raw={raw}")

    # 在 handle 中间切开
    cut = masked.index("[WORKSPACE_ROOT_1]") + 5
    s2 = RebindStream(e)
    out2 = s2.push(masked[:cut]) + s2.push(masked[cut:]) + s2.close()
    check("handle 中间切分可还原", out2 == raw, out2)

    # JSON 模式：替换值必须做转义，保持 JSON 合法
    args_raw = json.dumps({"path": r"C:\ws\proj\a.py"}, ensure_ascii=False)
    hits: List[Any] = []
    args_masked = mask_json_field(args_raw, e, hits)
    sj = RebindStream(e, json_mode=True)
    pieces = [args_masked[i:i + 3] for i in range(0, len(args_masked), 3)]
    rejoined = "".join(sj.push(p) for p in pieces) + sj.close()
    check("流式 JSON 仍合法", _is_json(rejoined), rejoined)
    check("流式 JSON 还原一致",
          _is_json(rejoined) and json.loads(rejoined) == json.loads(args_raw), rejoined)


# --------------------------------------------------------------------------- #
# 6. 与 SlotGuard 语料差分对齐
# --------------------------------------------------------------------------- #
SLOTGUARD = os.path.join(os.path.dirname(ROOT), "SlotGuard-main", "experiments")


def test_format_validity() -> None:
    """对齐 SlotGuard 论文的 format_validity.json：
    合成值应保持长度、前缀约定，并仍能被同类正则识别。"""
    print("\n== 6. 与 SlotGuard 语料差分（FPS 格式保真） ==")
    corpus_path = os.path.join(SLOTGUARD, "data", "credentials.json")
    if not os.path.exists(corpus_path):
        print("  (跳过：未找到 SlotGuard 语料)")
        return
    with open(corpus_path, encoding="utf-8") as fh:
        corpus = json.load(fh)

    key = b"differential-key-0123456789abcdef"
    by_cat: Dict[str, Dict[str, int]] = {}
    counter = 0
    for session in corpus["sessions"]:
        for cred in session["credentials"]:
            counter += 1
            cat = cred["category"]
            raw = cred["raw"]
            synth = format_preserving_synthetic(cat, raw, key, counter)
            stats = by_cat.setdefault(cat, {"n": 0, "len": 0, "prefix": 0, "regex": 0,
                                            "differ": 0})
            stats["n"] += 1
            if len(synth) == len(raw):
                stats["len"] += 1
            if _same_prefix(raw, synth):
                stats["prefix"] += 1
            if synth != raw:
                stats["differ"] += 1
            if _same_class(raw, synth):
                stats["regex"] += 1

    print(f"  {'category':<22}{'n':>5}{'长度一致':>10}{'前缀一致':>10}{'同类可识别':>12}")
    for cat, s in sorted(by_cat.items()):
        n = s["n"]
        print(f"  {cat:<22}{n:>5}{s['len']/n:>10.2f}{s['prefix']/n:>10.2f}{s['regex']/n:>12.2f}")

    # SlotGuard 论文声称 api_key 的长度/前缀/可识别率均为 1.0
    api = by_cat.get("api_key")
    if api:
        check("api_key 长度一致率 = 1.0", api["len"] == api["n"],
              f"{api['len']}/{api['n']}")
        check("api_key 前缀一致率 = 1.0", api["prefix"] == api["n"],
              f"{api['prefix']}/{api['n']}")
        check("api_key 同类可识别率 = 1.0", api["regex"] == api["n"],
              f"{api['regex']}/{api['n']}")
    check("合成值不与原文相同（全部）",
          all(s["differ"] == s["n"] for s in by_cat.values()),
          str({c: f"{s['differ']}/{s['n']}" for c, s in by_cat.items()}))


def _same_prefix(raw: str, synth: str) -> bool:
    for prefix in ("ghp_", "github_pat_", "sk-", "AKIA", "xoxb-", "xoxa-", "xoxp-",
                   "postgres://", "postgresql://", "mysql://", "mongodb://", "redis://",
                   "-----BEGIN"):
        if raw.startswith(prefix):
            return synth.startswith(prefix)
    if raw.startswith("eyJ"):
        return synth.startswith("eyJ")
    return True


def _same_class(raw: str, synth: str) -> bool:
    """合成值是否仍能被「同一类」正则匹配到（结构可用性）。"""
    checks = [
        (r"^ghp_[A-Za-z0-9]{36,}$", r"^ghp_[A-Za-z0-9]{20,}$"),
        (r"^sk-[A-Za-z0-9_\-]{20,}$", r"^sk-[A-Za-z0-9_\-]{16,}$"),
        (r"^AKIA[0-9A-Z]{16}$", r"^AKIA[0-9A-Z]{12,}$"),
        (r"^xox[baprs]-", r"^xox[baprs]-"),
        (r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$",
         r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"),
        (r"^(?:postgres|postgresql|mysql|mongodb|redis)://[^:]+:[^@]+@[^/]+",
         r"^(?:postgres|postgresql|mysql|mongodb|redis)://[^:]+:[^@]+@[^/]+"),
        (r"^-----BEGIN [A-Z ]*PRIVATE KEY-----", r"^-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        (r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", r"@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"),
    ]
    for raw_rx, synth_rx in checks:
        if re.search(raw_rx, raw):
            return bool(re.search(synth_rx, synth))
    return True


def test_leak_rate() -> None:
    """会话级泄漏率。

    度量方式对齐 SlotGuard 的 `credentials` 实验：把整段会话的脱敏结果拼起来后
    **去掉空白与引号**再判残留（否则 `"AAA" "BBB"` 这类拆分会天然躲过子串检查）。

    注意：SlotGuard 的 fps_with_seg 条件在跑之前会用答案集预置图节点
    （见 experiments/src/main.rs 的 split 处理），那是 oracle 条件；这里不预置，
    因此本表反映的是「真实网关没有答案可查」时的表现。
    """
    print("\n== 7. 会话级泄漏率（无 oracle，真实条件） ==")
    corpus_path = os.path.join(SLOTGUARD, "data", "credentials.json")
    if not os.path.exists(corpus_path):
        print("  (跳过：未找到 SlotGuard 语料)")
        return
    with open(corpus_path, encoding="utf-8") as fh:
        corpus = json.load(fh)

    stats: Dict[str, Dict[str, int]] = {}
    for session in corpus["sessions"]:
        e = engine_for("leak-" + session["id"], mask_paths=False)
        sanitized = [e.mask_text(turn["content"])[0] for turn in session["turns"]]
        # SlotGuard 的归一化：去掉空白与引号
        normalized = re.sub(r"[\s\"']", "", "".join(sanitized))
        for cred in session["credentials"]:
            exposure = cred["exposure"]
            stats.setdefault(exposure, {"n": 0, "leak": 0})
            stats[exposure]["n"] += 1
            if re.sub(r"[\s\"']", "", cred["raw"]) in normalized:
                stats[exposure]["leak"] += 1

    print(f"  {'exposure':<14}{'n':>5}{'残留':>7}{'残留率':>9}   SlotGuard(有 oracle)")
    published = {"direct": "0/200", "embedded": "0/200", "split": "0/200",
                 "derived": "0/200", "cross_turn": "0/52"}
    for exposure, s in sorted(stats.items()):
        print(f"  {exposure:<14}{s['n']:>5}{s['leak']:>7}{s['leak']/s['n']:>9.2f}"
              f"   {published.get(exposure, '-')}")

    for exposure in ("direct", "embedded", "cross_turn"):
        s = stats.get(exposure)
        if s:
            check(f"{exposure} 曝光无残留", s["leak"] == 0, f"{s['leak']}/{s['n']}")

    split = stats.get("split")
    if split:
        # 这是已知的能力边界，不是回归：没有答案预置时无法识别被引号/空白拆开的凭证
        print(f"  [说明] split 残留 {split['leak']}/{split['n']}："
              f"SlotGuard 在该项同样是 200/200（fps_only 条件），"
              f"其 0/200 依赖用答案预置图节点")


# --------------------------------------------------------------------------- #
def main() -> int:
    db.init_db()
    test_paths()
    test_credentials()
    test_roundtrip()
    test_json_field()
    test_stream()
    test_format_validity()
    test_leak_rate()
    print(f"\n通过: {PASSED}  失败: {len(FAILED)}")
    for f in FAILED:
        print("  - " + f)
    return 0 if not FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
