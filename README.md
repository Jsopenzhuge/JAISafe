<p align="center">
  <img src="logo.png" alt="JAISafe — Local Privacy Boundary for AI Agents" width="620">
</p>

<h1 align="center">把本机上下文留在本机</h1>

<p align="center">
面向 AI 编码代理（Claude Code、Cline、Cursor、自研 Agent…）的<strong>本地隐私边界</strong>
</p>

---

JAISafe 在你的代理和模型供应商之间运行，把出站请求里的**本机身份信息**——绝对路径、API 密钥、
数据库连接串、私钥、敏感文件名——替换成句柄之后再发出去，并在响应返回时**还原成真值**。

客户端零改动：你的代理完全不知道这件事发生过。

```
        本地（可信）                         │              上游（不可信）
                                            │
  C:\Users\me\proj\secret\layoff.pdf   ────┼──▶  [WORKSPACE_ROOT_1]\secret\[SENSITIVE_DOC_1].pdf
  ghp_9fK2…（真 token）                ────┼──▶  ghp_Xy3k…（同长度同前缀假值）
  postgres://svc:hunter2@db.corp.internal ─┼──▶  postgres://vq3f:9Kd2…@db-a7f2.svc.synthetic
                                            │
  C:\Users\me\proj\secret\layoff.pdf   ◀───┼────  模型回显的内容里若有句柄，自动还原
```

> 它不是 NewAPI / One-API 那类多租户 API 网关。协议转换、渠道路由、故障转移都是为了
> 让隐私边界能落在**一个统一的位置**而顺带解决的工程问题——见「为什么放在网关」。

---

## 它解决什么问题

AI 编码代理会把大量**本机上下文**塞进请求：文件绝对路径、`grep` 结果、报错堆栈、
`.env` 片段、终端历史、Git 分支名。这些内容一旦发往上游，就离开了你的机器。

问题不在于「模型看到了我的代码」——那是你请它做的事。问题在于**你的代码之外的东西**：

- 你的用户名、主目录结构、内网目录命名
- 项目里的**敏感文件名**（`layoff_plan_2026.pdf`、`acme-acquisition-2026/`）
- 环境变量里的真实密钥（一粘贴 `.env` 就全出去了）
- 数据库连接串、内部主机名、证书私钥

这些东西对完成任务毫无帮助，却会被上游记录、可能进入训练数据、可能出现在日志审计里。
JAISafe 做的事很具体：**把这一层剥掉再发出去**。

一次真实的 Agent 请求，脱敏前后：

| | 客户端原文（本机） | 实际发送给上游 |
| --- | --- | --- |
| 路径 | `C:\Users\1\Desktop\MyWork\JAISafe\llm-gateway\app\relay.py` | `[WORKSPACE_ROOT_1]\llm-gateway\app\relay.py` |
| 凭证 | `ghp_9fK2mQ…`（36 位真 token） | `ghp_Xy3kPq…`（同长度同前缀，HMAC 派生） |
| 文件名 | `layoff_plan_2026.pdf` | `[SENSITIVE_DOC_1].pdf` |
| 主机 | `vault.acme-corp.internal` | `host-a7f2q9.corp.internal` |

模型依然能理解「这个文件在项目的 `app/` 下」「这是个 GitHub token」「这是个内部主机」，
但它**不知道你是谁、你的机器长什么样、你的密钥是什么**。

---

## 它保护什么、不保护什么

隐私工具最容易变成安全剧场，所以这一节放在最前面。

| ✅ 保护 | ❌ 不保护 |
| --- | --- |
| 本机绝对路径（盘符 / UNC / POSIX）、主目录结构 | **你主动发给模型的代码、提示词、业务内容** |
| 环境变量与代码里的凭证（Key / JWT / 连接串 / 私钥） | 上游能推断出的业务信息（项目类型、技术栈、代码风格） |
| 敏感文件名与目录名（可配置关键词） | 请求元数据：模型名、时间、Token 数、流式时长、`user` 字段 |
| 邮箱、内部主机名 | 你在**别的**工具里直接调用上游时发出的内容 |
| 模型回显出来的上述内容（响应侧还原，客户端无感） | 已经离开机器后，上游如何使用这些内容 |

**明确一点：这个工具不会让模型看不见你的代码。** 它的目标是剥掉「本机身份」，不是加密你的业务。
如果你需要的是「代码本身也不能出网」，那需要的是本地模型，不是本工具。

另外两个前提：

- **运行 JAISafe 的机器必须是你信任的**。它是信任边界本身，不是边界之内的一层加密。
- **兜底存储在你的本机磁盘上以明文存在**（见「威胁模型与代价」）。

---

## 工作原理

内部以 OpenAI Chat Completions 作为规范格式，请求进来先转规范化、脱敏，再转成上游协议发出；
响应回来反向走一遍，最后还原。

```
客户端                    JAISafe                                     上游
  │                        │                                           │
  │  /v1/messages          │                                           │
  ├───────────────────────▶│  1. 协议解析 → 规范格式                    │
  │                        │  2. 脱敏：路径 / 凭证 → 句柄                │
  │                        │     ↳ 句柄↔真值 存入会话表                  │
  │                        │  3. 规范格式 → 上游协议                     │
  │                        ├──────────────────────────────────────────▶│ 只看得到句柄
  │                        │◀──────────────────────────────────────────┤
  │                        │  4. 上游协议 → 规范格式                     │
  │                        │  5. 还原：句柄 → 真值                       │
  │◀───────────────────────┤                                           │
  │  与未脱敏时完全一致     │                                           │
```

### 为什么放在网关，而不是改客户端

| | 改客户端 / 用库 | 放在网关（本项目） |
| --- | --- | --- |
| 接入成本 | 每个 Agent 各接一次 | **一处接入，覆盖所有客户端** |
| 响应还原 | 需要 Agent 自己实现句柄还原 | **网关能看到响应，客户端完全无感** |
| 换供应商 | 每换一家重写一次 | 网关内部做协议转换 |
| 客户端改版 | 需要跟着改 | 不受影响 |

第二行是关键。参考实现 [SlotGuard](https://openreview.net/forum?id=waW0KyByrv) 是一个 Rust 库，
需要在每个 Agent runtime 里接入，而且**它只做出站改写、没有回程还原**（其 `raw_for` 在全仓库只被
单元测试调用过）——因为一个库看不到响应。网关天然能看到，所以能做到客户端零改动。

---

## 快速开始

```bash
pip install -r requirements.txt
python run.py                      # 默认 http://127.0.0.1:8000
```

Windows 可直接双击 `start.bat`。

### 1. 打开 WebUI，改掉默认密码

<http://127.0.0.1:8000/> → 默认管理密码 `admin` → **「设置」页立刻改掉**。

### 2. 添加渠道

「渠道」→「新增渠道」。填上游的 Base URL 与 API Key：

| 供应商 | Base URL |
| --- | --- |
| OpenAI | `https://api.openai.com/v1` |
| Anthropic | `https://api.anthropic.com` |
| DeepSeek | `https://api.deepseek.com/v1` |
| 硅基流动 | `https://api.siliconflow.cn/v1` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` |

> 上游是**本地模型**时，把该渠道的「本地上下文脱敏」设为 `off` —— 内容本来就没出机器，
> 脱敏只会带来误伤和多余的映射落盘。

### 3. 把代理指向 JAISafe

改代理的 base_url 与 api_key 即可，别的不动：

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=sk-jai-xxxx
```

### 4. 配置要保护的范围

「脱敏」→ 填**路径根目录**（你的代码工作区），例如：

```
C:\Users\1\Desktop\MyWork\JAISafe
```

命中根目录的路径会保留完整相对路径（可读性最好）；未命中但位于主目录下的路径会折叠中段。

### 5. 先干跑，再强制

这是**推荐的上线路径**，不要跳步：

| 步骤 | 模式 | 你会看到 |
| --- | --- | --- |
| 1 | `dry_run` | 请求原样发出，但日志里记录「本应改成什么」 |
| 2 | 用「规则预览」粘贴几段真实提示词 | 确认哪些内容被命中、有没有误伤代码 |
| 3 | `enforce` | 真正脱敏，响应自动还原 |

![脱敏设置](docs/screenshots/mask.png)

---

## 脱敏覆盖范围

| 类型 | 例子 | 替换成 | 依据 |
| --- | --- | --- | --- |
| 工作区 / 仓库根 | `C:\ws\proj\src\a.py` | `[WORKSPACE_ROOT_1]\src\a.py` | 配置的根目录 |
| 用户主目录（折叠中段） | `/home/u/a/b/c/d/e.txt` | `[USER_HOME_1]/[SENSITIVE_PATH_SEGMENT_1]/d/e.txt` | 主目录 |
| 敏感文件名 | `layoff_plan_2026.pdf` | `[SENSITIVE_DOC_1].pdf` | 关键词 + 年份/`for_` 形态 |
| 敏感目录段 | `acme-acquisition-2026/` | `[SENSITIVE_PATH_SEGMENT_2]/` | 关键词 + 分隔符/数字 |
| API Key | `sk-… ghp_… AKIA… xoxb-… hf_… glpat-… npm_… AIza…` | 同长度同前缀假值 | 正则 + 格式保持合成 |
| JWT / OAuth | `eyJ…eyJ…xxx` | 三段同长度、仍以 `eyJ` 开头 | 正则 |
| 数据库连接串 | `postgres://u:p@host/db` | 保留 scheme / 端口 / 路径 | 正则 |
| 私钥 PEM | `-----BEGIN … PRIVATE KEY-----` | 保留 PEM 框架，替换 base64 正文 | 正则 |
| 邮箱 / PII | `dave@team.example` | 保留 TLD 的假邮箱 | 正则 |
| 内部主机名 | `vault.acme-corp.internal` | 保留 `.internal` 等后缀 | 后缀表 |

**假值由 HMAC 派生，不依赖原文的字节。** 这一点是刻意的：如果合成值是原文的某种变换，
攻击者一旦猜到函数就能仅凭上游可见的假值反推真值。派生输入是「会话密钥 + 类别 + 计数器」，
所以同一真值在同一会话内稳定映射到同一假值（否则模型每轮看到的路径都在变，任务会崩）。

**默认只脱敏「配置的根目录 / 主目录之下」的路径**，不会去动 `/usr/bin/python`、`/etc/hosts`
这类公共路径——它们不包含你的身份信息，改写只会徒增噪音。需要更激进的策略可以打开
「范围外的绝对路径也脱敏」。

敏感文件名关键词内置中英词表（`layoff` / `salary` / `contract` / `裁员` / `薪资` / `机密` …），
可在 UI 里覆盖。

---

## 三种模式

| 模式 | 上游收到 | 用途 |
| --- | --- | --- |
| `off`（默认） | 原文 | 未启用。行为与不加这一层完全一致 |
| `dry_run` | **原文** | 只记录命中，不改写请求。用来验证规则 |
| `enforce` | 句柄 | 真正脱敏，响应返回前还原 |

默认 `off` 而不是 `enforce`：静默改变一个正在运行的代理所发内容风险太大——一旦还原有偏差，
代理会拿到假路径去执行。请按「快速开始」第 5 步的顺序启用。

渠道级可以覆盖：`inherit`（跟随全局）/ `off`（本地模型）/ `enforce`（外部厂商）。

---

## 审计：知道到底发出去了什么

隐私工具如果不能回答「刚才那次请求，究竟哪些内容离开了我的机器」，就没法验证也没法追责。
每条日志的「脱敏」页签给出：

- 命中统计（路径 N 处 / 凭证 M 处，按类型细分）
- **客户端原文** 与 **实际发送给上游的内容** 并排对比
- 本次请求产生的**句柄↔真值映射表**（默认打码，点击显示）
- 按句柄反查真值
- 干跑模式下的「本应改成什么」预览

![脱敏回溯](docs/screenshots/log_mask_tab.png)

---

## 威胁模型与代价

### 可逆脱敏的固有代价：映射以明文落盘

要让响应能还原，网关必须记住「句柄 → 真值」。它是无状态的请求处理器，做不到只靠内存，
因此这份映射**持久化在 `data/gateway.db` 里，明文**。

这不是实现缺陷，是可逆脱敏的必然后果。它意味着：

- `data/gateway.db` 成为一个**包含明文凭证的文件**——请控制它的访问权限（NTFS ACL / `chmod 600`）
- 管理后台默认密码是 `admin`，**启用脱敏前必须先改掉**
- 日志页默认也会记录客户端原始 body（可在「设置」里关掉 `log_request_body`）
- 「脱敏」页提供独立保留期与一键清空

**想彻底避免落盘**：把「保存句柄与真值的映射」关掉。代价是响应无法还原、日志无法回溯——
适合你只在乎「出站不泄漏」而不在乎客户端体验的场景。

### 不该被误解的地方

- **边界是 JAISafe 自己**。它以明文接触全部请求内容。它一旦被攻破或配置错误，
  它就不再是保护层。不要把它当成零信任加密方案。
- **它不隐藏你在用 AI**。模型名、时间、Token 数、请求模式仍然可见。
- **它不阻止上游推断**。脱敏后的结构仍透露技术栈与项目形态。

---

## 实测效果

算法移植自 SlotGuard，并用其**公开语料**（200 会话 / 852 条凭证，含 direct / embedded /
split / derived / cross-turn 五种曝光方式）做了差分对齐。

凭证合成的格式保真度（`tests/mask_test.py`）：

| 类别 | 样本 | 长度一致 | 前缀一致 | 同类可识别 |
| --- | --- | --- | --- | --- |
| api_key | 200 | 1.00 | 1.00 | 1.00 |
| oauth_token | 200 | 1.00 | 1.00 | 1.00 |
| cryptographic_key | 52 | 1.00 | 1.00 | 1.00 |
| database_credential | 200 | 0.27 | 1.00 | 1.00 |
| pii_identifier | 200 | 0.00 | 1.00 | 1.00 |

`api_key` 三项均为 1.00，与 SlotGuard 论文的 `format_validity.json` 一致。
连接串与邮箱的长度会变，是因为合成时要换掉用户名/口令/主机，这是设计使然。

真实条件下的残留率（不预置答案）：

| 曝光方式 | 本实现 | SlotGuard 论文 |
| --- | --- | --- |
| direct | 0 / 200 | 0 / 200 |
| embedded | 0 / 200 | 0 / 200 |
| derived | 0 / 200 | 0 / 200 |
| cross-turn | 0 / 52 | 0 / 52 |
| **split** | **105 / 200** | 0 / 200 ⚠️ |

**split 是本实现的能力边界，也是参考实现的「数字陷阱」。** 把 token 从中间劈开、用引号或空白
隔开（`"AAA" "BBB"`）时，没有「已知答案」就无法识别。SlotGuard 之所以报 0/200，是因为它的
实验脚本在跑之前**用答案集的 ground truth 预先把两半注册成图节点**
（`experiments/src/main.rs:6235`），真实网关没有这份预知。

---

## 已知边界

- **拼写变体、Base64 编码后的凭证**：正则是字面匹配，编码/变形后无法识别。
- **被拆开的凭证**：见上表。
- **`enforce` 下流式响应不再字节直通**。流式还原必须在结构化分片上做，因此会走
  「解析 → 还原 → 重渲染」，上游特有的少数字段（Anthropic 的 `signature_delta`、`ping` 等）
  不再透传。这是为还原能力付出的代价，且只在 `enforce` 时生效。
- **工具定义的 JSON Schema 内部不脱敏**（只脱敏 `description`），避免破坏 schema 关键字。
- **Embeddings 只脱敏不还原**：向量无法还原，且客户端要的本来就是脱敏后文本的向量。

### 流式还原为什么难

句柄是普通字符串，会被 SSE 分片切开（`[WORKSPACE` + `_ROOT_1]/src`）。实现用句柄前缀集合
维护滑动窗口，只有当缓冲区尾部不可能是任何句柄前缀时才把字符下发给客户端。
另外 `tool_calls` 的参数是 JSON 片段，替换值会先做 JSON 转义——否则 Windows 路径的 `\U`
会注入非法转义，客户端直接解析失败。

---

## 支持的协议

隐私边界要落在统一位置，就必须能听懂客户端和上游各自说的协议。这部分是手段，不是目的。

### 入口

| 方法 | 路径 | 协议 |
| --- | --- | --- |
| POST | `/v1/chat/completions` | OpenAI Chat Completions |
| POST | `/v1/messages` | Anthropic Messages |
| POST | `/v1/messages/count_tokens` | Anthropic Token 估算 |
| POST | `/v1/responses` | OpenAI Responses API |
| POST | `/v1/completions` | OpenAI Completions（legacy） |
| POST | `/v1/embeddings` | Embeddings（脱敏后透传） |
| GET | `/v1/models` | 模型列表 |
| GET | `/health` | 健康检查 |

渠道类型支持 OpenAI 兼容 / Anthropic Messages / OpenAI Responses。**任意入口 ↔ 任意上游**
自动互转，含

- **SSE 事件级流式转换**（不是拼接）：`chat.completion.chunk` ↔ `message_start`/`content_block_delta`
  ↔ `response.output_text.delta`
- **工具调用全链路**：`tool_calls` ↔ `tool_use`/`tool_result` ↔ `function_call`，
  含流式增量参数
- **思考等级**：`reasoning_effort` ↔ `reasoning.effort` ↔ `thinking.budget_tokens`
- **同格式直通**：入口与上游协议一致时，请求体与响应字节**原样透传**，不做多余改写
  （`enforce` 且流式时例外，见「已知边界」）

### 客户端接入

```python
# OpenAI SDK
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="sk-jai-xxxx")
client.chat.completions.create(model="gpt-4o-mini",
                               messages=[{"role": "user", "content": "你好"}])
```

```python
# Anthropic SDK（上游配成 OpenAI 兼容渠道也会被完整翻译）
from anthropic import Anthropic
client = Anthropic(base_url="http://127.0.0.1:8000", api_key="sk-jai-xxxx")
client.messages.create(model="claude-3-5-sonnet-20241022", max_tokens=1024,
                       messages=[{"role": "user", "content": "你好"}])
```

---

## 其它功能

| 功能 | 说明 |
| --- | --- |
| **渠道管理** | base_url / api_key / 模型白名单 / 模型映射 / 额外请求头 / 超时 / 每渠道独立脱敏策略 |
| **故障转移** | 按优先级依次重试，连接失败、超时、4xx/5xx 自动切下一个；日志记录重试次数 |
| **网关密钥** | `Authorization: Bearer` 或 `x-api-key`；存在启用密钥时自动强制校验 |
| **请求可视化** | 每次请求完整记录客户端请求体、上游请求体、上游响应、原始 SSE、耗时、Token |
| **接口调试** | 内置调试台，可选协议、流式实时输出、一键生成 curl |
| **零依赖** | 无需 Redis / MySQL / Node，一个 Python 进程 + 一个 SQLite 文件 |

---

## 界面预览

| 概览 | 渠道 |
| --- | --- |
| ![概览](docs/screenshots/overview.png) | ![渠道](docs/screenshots/channels.png) |

| 接口调试 | 设置 |
| --- | --- |
| ![调试](docs/screenshots/playground.png) | ![设置](docs/screenshots/settings.png) |

---

## 配置项

「脱敏」页可视化配置，对应 settings：

| Key | 默认 | 说明 |
| --- | --- | --- |
| `mask_mode` | `off` | `off` / `dry_run` / `enforce` |
| `mask_paths` `mask_credentials` | `1` | 分别开关路径与凭证脱敏 |
| `mask_workspace_roots` `mask_repo_roots` | 空 | 每行一个绝对路径；仓库根优先级更高 |
| `mask_include_home` | `1` | 主目录作为兜底根（折叠中段） |
| `mask_preserve_segments` | `3` | 折叠时保留的末级段数 |
| `mask_outside_roots` | `0` | 范围外的绝对路径是否也脱敏 |
| `mask_credential_mode` | `fps` | `fps`（格式保持）/ `placeholder`（`[ACCESS_TOKEN_1]`） |
| `mask_sensitive_keywords` | 内置中英词表 | 敏感文件名/目录关键词 |
| `mask_internal_suffixes` | `.internal,.intranet,.corp,.local,.lan` | 内部主机名后缀 |
| `mask_propagate` | `1` | 已出现过的真值即使正则漏掉也替换（跨轮） |
| `mask_session_source` | `auto` | 句柄作用域：`X-Mask-Session` 头 → 请求体 `user` → API Key → IP |
| `mask_store_map` | `1` | 是否保存映射；**关掉就无法还原** |
| `mask_retention_days` | `7` | 脱敏映射独立保留期 |

句柄作用域（会话）的稳定性比隔离性更重要：同一真值在不同请求里若拿到不同句柄，
模型每轮看到的路径都在变，任务会直接失败。默认 `auto` 在「请求头 → 请求体 user → API Key → IP」
中依次取值。

---

## 与 SlotGuard 的关系

算法移植自 [SlotGuard](https://openreview.net/forum?id=waW0KyByrv)（Rust，论文原型），
按网关场景做了取舍：

| 项 | SlotGuard | 本项目 |
| --- | --- | --- |
| 形态 | Rust crate + CLI，无 HTTP 服务 | 本地服务，直接嵌进代理链路 |
| 接入方式 | 每个 Agent runtime 各接一次 | 一处接入，覆盖所有客户端 |
| **响应还原** | **没有**（`raw_for` 只被单元测试调用） | 有，含流式滑动窗口 |
| Windows 路径 | **不支持**（正则只匹配 POSIX 绝对路径） | 支持盘符 / UNC / 正反斜杠 |
| SEG 图机制 | 有，但 `edges`/`redirections` 是死代码 | 不移植，只保留真正生效的「节点集合 + 子串扫描」 |
| 邮箱 / 内部主机 | 独立槽位机制 | 统一走 FPS，减少机制数量 |
| 路径脱敏范围 | 无条件改写所有绝对路径 | 只改写配置根目录 / 主目录之下，误伤更少 |
| 论文脚手架 | 6400 行实验代码 | 不移植 |

---

## 目录结构

```
llm-gateway/
├── run.py                    # 启动入口
├── start.bat / start.sh
├── requirements.txt
├── app/
│   ├── main.py               # FastAPI 应用与路由
│   ├── relay.py              # 请求处理：鉴权 / 路由 / 脱敏 / 还原 / 转发 / 日志
│   ├── admin.py              # 管理后台 API
│   ├── db.py                 # SQLite 存储层
│   ├── masking/              # ★ 本地上下文隐私边界
│   │   ├── patterns.py       #   凭证正则与分类
│   │   ├── fps.py            #   HMAC 派生 + 格式保持合成器
│   │   ├── paths.py          #   跨平台路径（POSIX / 盘符 / UNC）
│   │   ├── engine.py         #   路径抽象 + 凭证替换 + 还原
│   │   ├── slots.py          #   会话级句柄表（SQLite 持久化）
│   │   ├── walk.py           #   四种协议的消息树遍历
│   │   ├── stream.py         #   流式还原滑动窗口
│   │   └── config.py         #   设置装配与会话标识
│   ├── formats/              # 协议解析与互转（承载隐私边界的手段）
│   └── static/               # WebUI（原生 HTML/CSS/JS，无构建步骤）
├── tests/
│   ├── mock_upstream.py      # 模拟三种协议的上游
│   ├── mask_test.py          # 脱敏引擎 + 语料差分对齐
│   ├── smoke_test.py         # 进程内端到端（格式矩阵 / 脱敏往返 / 推理等级）
│   └── live_check.py         # 真实 HTTP 实例联调（非破坏式）
└── data/gateway.db           # 运行后自动生成（含句柄映射，注意权限）
```

---

## 测试

```bash
# 脱敏引擎：路径抽象 / FPS 确定性 / 往返一致 / 流式分片 / 与 SlotGuard 语料差分
python tests/mask_test.py                       # 59 项断言

# 进程内端到端：4 入口 × 3 上游 × 流式/非流式，含脱敏往返与推理等级
python tests/smoke_test.py                      # 389 项断言

# 真实 HTTP 联调（非破坏式，只创建/清理 __livetest__ 前缀的资源）
python tests/mock_upstream.py                   # 终端 A
python run.py --port 18000                      # 终端 B
python tests/live_check.py                      # 终端 C
```

`smoke_test.py` 的脱敏部分会断言：**上游收到的内容里不含原始路径与凭证**、
**客户端拿回的是还原后的真值**、**日志里保留了可回溯的映射**。

---

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `JAI_HOST` / `JAI_PORT` | `127.0.0.1` / `8000` | 监听地址 |
| `JAI_DATA_DIR` | `./data` | 数据目录（含句柄映射，注意权限） |
| `JAI_ADMIN_PASSWORD` | `admin` | 首次初始化时的管理密码 |
| `OPENAI_BASE_URL` + `OPENAI_API_KEY` | — | 首次启动且无渠道时自动创建默认渠道 |

---

## 部署建议

- **只监听回环地址**。默认就是 `127.0.0.1`；确需对局域网提供服务时，务必先改管理密码、
  开启强制密钥校验，并置于反向代理之后。
- **限制 `data/` 的访问权限**。里面有你所有中继密钥和句柄映射（明文）。
- **把 `data/` 排除在版本控制与备份之外**。`.gitignore` 已包含。
- **设置合理的 `mask_retention_days`**。历史映射过期即清，减少明文驻留窗口。
- **上游是本地模型时，把该渠道脱敏设为 `off`**。不必要地脱敏只会带来误伤。
- **首次上线用 `dry_run` 观察一个完整任务周期**，确认没有误伤代码标识符之后再切 `enforce`。

---

## 常见问题

**Q：开了脱敏，模型还能正常干活吗？**
大概率可以，但请理解我的验证边界：**我验证的是「格式保真度」，不是「任务成功率」。**
路径保留了可读的末段、凭证保留了长度与前缀，模型仍能判断文件位置、识别"这是个 API key"；
200 会话 / 852 凭证的语料上合成值的三项格式指标见「实测效果」。
但没有任何脱敏方案能保证下游任务 100% 不受影响——**这正是推荐先跑 `dry_run` 的原因**：
用你自己的真实提示词看命中列表，确认它没有动到模型推理所依赖的标识符。

**Q：为什么我的代码标识符没被脱敏？**
这是有意的。脱敏只针对**本机身份信息**（路径、凭证、敏感文件名），不碰代码内容。
`abstract_components` 只抽象敏感目录段与敏感文件名，普通源文件名与符号名原样保留，
否则模型会失去全部上下文。

**Q：`enforce` 之后客户端收到的内容和之前完全一样吗？**
是。响应侧的句柄会被还原成真值，客户端感知不到差异。唯一例外是流式响应不再字节直通
（见「已知边界」），语义上等价，但上游特有字段可能被丢弃。

**Q：能不能只脱敏、不还原？**
可以。把「保存句柄与真值的映射」关掉即可——出站不泄漏，但客户端会看到句柄。
适合纯出站审计的场景。

**Q：上游返回 404 / 401，但直接在别的工具里能用？**
检查 Base URL 是否重复包含 `/v1`。本程序会自动补 `/v1`，并且当 base_url 已以
`/v1`、`/v2`、`/v1beta` 结尾或直接以目标端点结尾时不会重复追加。

**Q：为什么日志里的 Token 是 0？**
上游没有返回 `usage`。流式请求需要上游支持 `stream_options.include_usage`；
部分兼容服务不支持，可在渠道里关闭「流式请求向上游索取 usage 统计」。

**Q：Anthropic 客户端报 `max_tokens: field required`？**
Anthropic 协议要求必填。用 OpenAI 协议请求、上游是 Anthropic 时，网关默认补 `4096`；
目标模型上限更低时请显式传 `max_tokens`。

**Q：多个渠道都匹配同一个模型时会怎样？**
按优先级从高到低依次尝试，第一个成功即返回；失败自动切换下一个，日志记录重试次数。
注意这意味着**同一个请求可能被发给多个上游**——如果你在意暴露面，请确保只有真正需要的渠道
处于启用状态。
