# AI-Alarm-Auto-Fix

飞书告警 **接收 → AI 分析 → 自动修复 → 结果通知 → 日报** 一体化服务（Python 版）。

> 本项目是 `ccd-java-system` 仓库 `com.xizhao.service.feishu.alarm` 模块的**全新 Python 重写**：
> Java 侧代码零改动；报文契约、落库表结构、Redis key 与 Java 版完全兼容，
> 两版可并存灰度、可随时回切。已于 2026-09-17 在 test 环境完成真实端到端验证（见 [§8](#8-测试与实测报告)）。

| | |
| --- | --- |
| 运行时 | Python ≥ 3.10（推荐 3.12+） |
| 技术栈 | FastAPI · httpx · PyMySQL · redis-py · pydantic v2 · APScheduler · PyYAML |
| 默认端口 | 8800 |
| 配置 | 单 YAML 文件 + 环境变量覆盖密钥（不依赖 Nacos） |
| 测试 | 133 个单元/冒烟测试 + test 环境端到端实测 |

---

## 目录

1. [项目是什么](#1-项目是什么)
2. [先读懂：四条链路是怎么跑的](#2-先读懂四条链路是怎么跑的)
3. [目录结构与 Java 版对照](#3-目录结构与-java-版对照)
4. [快速开始](#4-快速开始)
5. [配置详解](#5-配置详解)
6. [HTTP 接口文档](#6-http-接口文档)
7. [数据与存储](#7-数据与存储)
8. [测试与实测报告](#8-测试与实测报告)
9. [部署](#9-部署)
10. [与 Java 版的差异与增强](#10-与-java-版的差异与增强)
11. [常见问题 FAQ](#11-常见问题-faq)

---

## 1. 项目是什么

业务服务发生异常时，把告警推给本服务，本服务自动完成「看懂告警 → 定位代码 → 给出分析 →
尝试修复 → 群里通知 → 每天汇报」的闭环：

| 能力 | 一句话说明 | 触发方式 |
| --- | --- | --- |
| 告警分析 | 堆栈定位出错代码片段 + 查相关库表 + LLM 交叉分析，卡片推飞书群 | 其他服务 POST 告警报文 |
| 自动修复 | 按历史分析让 LLM 生成修复后**整文件**，提交到 `fix_bug_日期_traceId` 分支并群发结果 | 飞书卡片「修复」按钮（跳转 URL） |
| 卡片交互 | 卡片 confirm/ignore 按钮回调路由 | 飞书服务器 POST |
| 告警日报 | 昨日/本周/本月统计 + LLM 关键洞察，推多群 | 内置调度每天 09:40（北京时间） |
| Web 告警看板 | 浏览器查看 KPI（是否解决/是否合并）、问题数折线图、明细与处理详情 | 打开 `http://host:8800/dashboard`（只读，可选 Basic 认证） |

总架构（虚线为可降级通道）：

```
 业务服务(A/B/C…)          本服务 AI-alarm_autoFix                         外部系统
 ┌──────────┐   POST /inner/feishu/alarm/receive   ┌──────────────────┐
 │ 产生异常  │ ────────────────────────────────────▶ │ 接收 → 异步分析    │──┬─▶ Agent网关(读码/分析/提交) ─┐
 └──────────┘                                       │                  │  ├─▶ GitLab API(兜底读码/提交)  │ 内网
        飞书群 ◀── 告警卡片/修复结果卡片/日报 ──────── │ 修复/日报/卡片构建 │  ├─▶ MySQL(落库+查库上下文)     │
        │                                          │                  │  ├─▶ Nacos配置库(解析服务数据源) │
        └── 点「修复」按钮 GET /callback/fix?traceId=▶ │                  │  └─▶ 飞书开放平台(发消息)       │
                                                   └──────────────────┘
 降级规则：网关不通→GitLab直连；Agent失败→本地LLM→规则报告；Redis未配→进程内锁；查库/落库失败→跳过不阻断
```

---

## 2. 先读懂：四条链路是怎么跑的

> 本节即「现有实现讲解」：每一步标注了实现位置（文件/函数），对着代码读即可。

### 2.1 链路一：告警接收与 AI 分析

**第 0 步：接收（同步，毫秒级返回）** — `app/api/routes.py` `receive()`
推送方 POST camelCase 报文（`AlarmLog` 模型，`app/models/alarm.py`）。校验开关 `feishu.enable`
后把分析任务丢进有界线程池（`app/core/executor.py`，4 线程/队列 100，满则退化为调用线程执行不丢任务），
立即返回 `{"code":0}`——分析含 LLM 调用是分钟级，绝不让推送方等。

**第 1 步：提取核心信息** — `app/analysis/service.py` `_extract()`
从报文提取：服务名、级别归一化（WARNING→WARN）、异常类名（正则 `([\w.$]+(?:Exception|Error))`）、
请求接口（`requestUri` 字段 → `businessKeys.uri` 兜底）、请求参数（`businessKeys.params`）、
核心消息（message + 前 3 行堆栈，截 1500 字符）。
表名提取（`app/extractors/tables.py`）：两条正则——SQL 异常消息里的 `db.table` 形式优先、
`insert into/update/from/join` 后的表名兜底，去重最多 5 个。

**第 1.5 步：指纹重复/复发识别** — `app/core/fingerprint.py` + `analysis/service.py`
指纹 = `md5(service|requestUri|exceptionClass|首个业务帧类.方法)`（**不含行号**：修复增删代码
导致的行号平移不算新错误）。`analyse.repeat.window_hours`（默认 24h，≤0 关闭）窗口内查同指纹
最新记录：原告警**已修复（FIXED）**→ **复发 RECURRED**，卡片带「⚠️ 修复后复发」标注、可重新点修复；
其余（未发起/失败/无需处理/转人工/待人工）→ **重复 REPEATED**，**在取码/查库/LLM 之前直接短路**——
只落库计数（归因结论继承原告警，`root_id` 链到真根），不推卡片、不调模型。

**第 2 步：取出错代码片段** — `app/analysis/service.py` `_fetch_code_context()`
① 堆栈帧解析（`app/extractors/stack.py`）：只取 `com.xizhao` 业务包帧（跳过 spring/jdk/lambda 帧），
最多 3 帧；② 每帧类名转文件路径（`src/main/java/...`，测试类自动转 `src/test/java/`）；
③ 读文件**网关优先**（`GET /code/file`，网关本地 git workspace，新鲜度由 webhook+每晚同步保证）、
GitLab files API 兜底（base64 解码）；④ 分支顺序 `default_branch → master → main` 逐个尝试；
⑤ 截取出错行 ±15 行，加行号，头部写锚点注释：

```
// 文件: src/main/java/com/xizhao/xxx/YyyService.java (分支 test, 出错行 77)
  62|     public void handleAlarm(...) {
```

> 这个头注释是修复链路定位文件的**关键锚点**，格式别改。

**第 3 步：查库上下文** — `app/providers/db_context.py` `fetch_context()`
对提取到的表名（先白名单校验 `[a-zA-Z0-9_]+` 防注入，再经 `information_schema` 确认真实存在）：
`SHOW CREATE TABLE`（DDL）→ `COUNT(*)`（行数）→ 样本行（`businessKeys` 的键与表列名匹配时等值查
1 行，否则 LIMIT 3 行，渲染成 JSON 行）。
数据源从哪来（`app/providers/nacos.py`）：告警服务名就是 Nacos 上的服务，直连 Nacos 配置库
（`config_info` 表）拉它的 YAML 配置，兼容单库/druid/dynamic.master 三种写法，
`${conf.mysql.ip}` 占位符用公共配置 `xizhao-common.yaml` 压平后替换，结果按服务缓存 30 分钟。

**第 4 步：LLM 分析** — `app/analysis/report.py` `ReportGenerator`
拼上下文（日志→代码→数据，上限 24000 字符）后按优先级调用：
**外挂 Agent**（`app/providers/agent_gateway.py`：POST /analyze 提交任务 → 轮询 GET /analyze/{id}）
→ **本地 LLM**（`app/providers/llm.py`：OpenAI 兼容 /chat/completions，provider 注册表换厂商）
→ **规则兜底报告**（永不为空）。
报告按 `【问题定位】【报错分析】【修改建议】【问题归因】` 四段拆分：前两段 → `analysis`（卡片 analyze 变量），
第三段 → `proposal`（卡片 suggest 变量），归因段 → `problem_type`（归因段缺失或非法走规则兜底：
Feign/超时/连接类异常 → DEPENDENCY，无堆栈 → MANUAL_REQUIRED，否则 CODE）。

**第 5 步：落库** — `app/storage/alarm_repository.py` `save()`
写入 `alarm_analyze_result`（`analyze_content` 存完整分析 JSON，另落 problem_type/fingerprint/
repeat_type/root_id 列）——这是修复回调与日报的数据源。归因映射初始 `fix_status`：
DEPENDENCY→`HANDOVER`（已转人工排查）、MANUAL_REQUIRED→`MANUAL_PENDING`（待人工处理）、
NOT_A_PROBLEM→`NOT_NEEDED`、CODE→NULL（走自动修复按钮）。失败只记日志：卡片照发，
但该告警的修复按钮将不可用。

**第 6 步：构建卡片并发送** — `app/feishu/cards.py` + `client.py`
模板卡片 `{type:template, data:{template_id, template_variable:{...}}}`。飞书硬限 30KB：
各变量先按字节预算截断（短字段 512B / 参数 2KB / 分析建议各 6KB），序列化超 28KB 再按
「堆栈→param→suggest→analyze」优先级削减。卡片新增 `problem_type`（归因文案）/`repeat_label`
（复发标注，仅复发输出）/`root_id`/`ignor_url`（忽略按钮链接，配 `feishu.ignore_jump_url` 后输出）。
发送走飞书 `im/v1/messages`（tenant_access_token 缓存提前 5 分钟刷新），失败自动进 Redis 重发队列
（最多重试 3 次）。归因为 DEPENDENCY/MANUAL_REQUIRED 时**补发人工处理通知**
（模板 `analyse.fix_notify.manual_card_template_id`，未配则文本兜底，附建议步骤）。

**第 7 步：缓存** — 以 `messageId` 为键缓存分析结果 JSON（TTL 24h），供卡片交互回调关联。

### 2.2 链路二：自动修复（核心链路）

**入口**：告警卡片上的「修复」按钮是一段跳转链接 `{jump_url}?traceId=xxx`，指向
`GET /inner/feishu/alarm/callback/fix`（`app/api/routes.py` `callback_fix()`）。

**同步受理（毫秒级，`app/fixing/service.py` `try_accept()`）**，四道闸门依次：

| 顺序 | 校验 | 失败文案 |
| --- | --- | --- |
| 1 | Redis 执行锁 `SET NX EX 10min`（防双击/并发触发两轮 LLM） | 修复正在执行中，请稍候 |
| 2 | 按 traceId 查最新分析记录 | 未找到告警分析结果 / 业务库不可用提示 |
| 3 | `fix_status` 已完结校验：FIXED / NOT_NEEDED / HANDOVER / MANUAL_PENDING **永久防重** | 该告警已成功修复/已忽略（判定无需处理）/已转人工，请勿重复点击 |
| 4 | 同服务串行锁 15min（value=持有者 traceId）：fix 分支按服务共用，并发修复必在提交环节分支竞争 | 该服务正在修复其他告警，请稍后再试 |

**异步执行（`execute_fix()`）**：

1. **定位出错文件**（`app/fixing/generator.py`）：优先解析分析片段头注释 `// 文件: ... (分支 x, 出错行 N)`；
   片段缺失时**从堆栈自救**（重新解析业务帧），不依赖分析时是否取到片段。
2. **拉全文件**：从修复基线分支 `fix_base_branch`（test 环境配 test，生产配 prod——必须与 fix 分支
   基线一致，否则会带进别的环境的半成品代码）拉**完整源码**喂给 LLM。
3. **LLM 生成修复**：外挂 Agent fix 档 → 本地 LLM。输出 JSON 契约（两套键名都认）：

```json
{"fixed": true, "fix_type": "code",
 "fixed_file": "src/main/java/com/xizhao/xxx/YyyService.java",
 "fixed_content": "修复后的完整文件内容（不是 diff）",
 "fix_summary": "一句话修复说明（100 字内）",
 "manual_steps": "fix_type=manual 时的分步人工操作建议"}
```

   契约要点：需要人工操作（SQL 变更/配置/权限）才能解决的 → `fixed=false, fix_type=manual`，
   `manual_steps` 按「1. … 2. …」给出 2~5 步可执行操作；确属无需任何处理 → `fix_type=non_code`
   （**属正确结果不算失败**）；问题只存在于环境分支、基线分支没有 → 也判 non_code；
   确属代码问题但给不出可靠修复 → `fixed=false, fix_type=code`。

4. **提交**（`app/fixing/committer.py`）：分支名 `fix_bug_yyyyMMdd_<traceId清洗>`（同日各告警独立分支）；
   提交信息自动加 `[AI] ` 前缀（人工 token 提交，GitLab author 无法区分 AI，靠前缀区分）。
   通道：**网关 POST /fix/commit**（网关本地独立 worktree 建分支+push，本服务无需直连 GitLab）
   → 失败降级 **GitLab API 直连**（POST branches 建分支[已存在复用] → PUT files 服务端落 commit）。

5. **五态判定与落库**（效果看板统计口径）：

| fix_status | 触发条件 | 语义 | 可否重试 |
| --- | --- | --- | --- |
| `FIXED` | commit 成功 | 已提交修复分支 | 否（永久防重） |
| `NOT_NEEDED` | LLM 判 non_code，或人工忽略接口落库 | 确认无需任何处理，**正确结果不算失败，不计入失败数** | 否（永久防重） |
| `MANUAL_PENDING` | LLM 判 manual，或分析归因 MANUAL_REQUIRED | 待人工处理（附操作步骤） | 否（永久防重） |
| `HANDOVER` | 分析归因 DEPENDENCY | 已转人工排查（上下游依赖问题） | 否（永久防重） |
| `FAILED` | 生成/提交/执行异常 | 未修复 | **可重试** |
| `NULL` | 未发起过修复 | — | 可发起 |

   `fix_summary` 与 GitLab 提交记录一字不差：commit message 原文 + `commit: <shortId>` + `查看改动: <webUrl>`。

6. **群发修复结果卡片**（模板 `analyse.fix_notify.card_template_id`，7 变量：
   fix_result/service/interface/trace_id/fix_time/fix_branch/fix_summary；模板未配置自动降级文本消息）；
   转人工/待人工时**补发人工处理通知**（模板 `analyse.fix_notify.manual_card_template_id`，
   变量 handover_type/service/interface/trace_id/handover_time/steps，未配则文本兜底）。

7. **finally 释放两把锁**（无论成败；失败不标记完结，允许重试）。

**忽略接口**：`GET /inner/feishu/alarm/callback/ignore?traceId=`（入参与修复回调一致）——
人工判定无需修复时忽略告警，落库 `fix_status=NOT_NEEDED`（`fix_summary=「人工忽略：判定无需修复」`，
日报计入「无需修复」不计失败）。已完结告警忽略/修复均被拒（按状态提示已忽略/已修复/已转人工）；
**自动修复执行中（执行锁存在）拒绝忽略**，防忽略结果被修复覆盖。忽略按钮链接经
`feishu.ignore_jump_url` 配置后以卡片变量 `ignor_url` 下发。

### 2.3 链路三：卡片交互回调

`POST /feishu/alarm/callback`（公网，飞书服务器调用，`app/api/routes.py`）：
token 校验（配了 `feishu.callback_token` 才校验）→ 解析按钮 value 里的 action →
按 messageId 从 Redis 取分析结果 → confirm/ignore 路由（confirm 为扩展点，真实修复走 2.2 的跳转链接），
响应飞书 toast。

### 2.4 链路四：告警日报

`app/reporting/daily_report.py`，APScheduler 每天 09:40（北京时间，cron 可配）触发：

- **统计**：查 `alarm_analyze_result` 按创建时间分三个区间（昨日/本周[ISO 周一起]/本月）。
- **口径**（看板语义，勿改）：`修复成功率 = 成功 / (错误告警 − 无需处理 − 转人工 − 待人工 − 重复)`，
  非自动修复项剔除出分母（重复剔除防刷屏告警稀释成功率）；分母为 0 且有告警记 100%、无告警记 0% 不展示；
  本期无告警只展示「🔴 错误告警 0 条」；本月最早告警晚于月初时追加「从 MM-dd 起算」。
  重复/复发按 `repeat_type` 单独计数展示（次行仅非零段）。
- **关键洞察**：LLM 生成（统计概览+逐条明细喂给模型，引用告警短编号）→ 失败降级规则洞察
  （服务集中度 ≥2 条 + 未闭环编号列表）。
- **推送**：`daily_report.chat_ids` 多群，卡片模板（变量 `content`）优先，未配置/模板不可见自动文本兜底；
  单群失败不阻断其他群。

### 2.5 并发与锁汇总

| 锁 | Key | TTL | value | 目的 |
| --- | --- | --- | --- | --- |
| 执行锁 | `system:feishu:alarm:fix:lock:<traceId>` | 10min | "1" | 防同告警双击/并发两轮 LLM |
| 服务串行锁 | `system:feishu:alarm:fix:service-lock:<service>` | 15min | 持有者 traceId | 防同服务并发修复分支竞争（快速失败） |
| 分析结果缓存 | `alarm:analyse:msg:<messageId>` | 24h | 分析 JSON | 卡片交互回调关联 |
| 飞书重发队列 | `system:feishusendfailed:list` | — | 记录 JSON | 发送失败重试（≤3 次后丢弃） |

锁释放用 **Lua 比较删除**（值匹配才删，防误删他人的锁；Redis 未配置时退化进程内实现，TTL 兜底）。

### 2.6 失败降级总表（设计原则：失败降级不阻断主流程）

| 故障 | 行为 |
| --- | --- |
| Agent 网关不可用/任务失败 | 读码→GitLab 直连；分析→本地 LLM→规则报告；提交→GitLab 直连 |
| 本地 LLM 也失败/未启用 | 规则兜底报告（卡片永远有分析内容）；修复生成返回 null → FAILED 可重试 |
| LLM 返回空 content | 抛**带 finish_reason 与响应体摘要**的错误（思考型模型被 max_tokens 截断可一锤定位） |
| GitLab token 过期（401） | 只影响直连兜底通道；网关通道不受影响（日志会留 401 痕迹） |
| 查库失败/未提取到表名 | dbContext 为空照常分析 |
| 落库失败 | 卡片照发；该告警修复按钮不可用（日志 ERROR 提示） |
| 飞书发送失败 | 入重发队列（≤3 次）；卡片模板不可见(200381)等持久错误重试也无效，看日志 |
| Redis 未配置 | 锁/缓存退化进程内实现（**仅单实例可用**，多实例必须配） |

### 2.7 修复质量闸与 MR 合拢（生产加固）

**质量闸**（`app/fixing/validation.py`，`analyse.fix_validation` 配置）：LLM 生成的修复文件在
提交 fix 分支**之前**过两道校验，任一不过不提交、落库 FAILED（可重试）：

1. **语法预检**（确定性、零成本）：内容非空且以 `}` 结束（截断检测）、屏蔽字符串/注释后
   三类括号配对、package 与文件路径一致、顶层类型名与文件名一致、与原文件不同（no-op 检测）；
2. **LLM 自检**（`llm_self_check`，默认开）：第二遍模型对照修改前后全文，只判编译级错误
   （语法/括号引号/缺失 import/引用不存在的成员/截断），通道与生成一致（网关优先降级本地 LLM）。
   自检通道自身故障按通过处理（闸坏了不误杀好修复）。

**MR 合拢**（`app/fixing/merger.py`，`analyse.git.mr` 配置）：修复提交 push 成功后自动对
修复基线分支建 MR（描述含服务/traceId/修复说明/分析建议，合并后自动删源分支），MR 链接
附进 fix_summary 与修复通知卡片。token 无 api 权限时 MR 403 降级仅记日志，不影响修复本身。

**超时未合并催办**：APScheduler 按 `git.mr.stale_remind_cron`（默认每天 10:30 北京时间）遍历
`git.projects` 各项目列出 `fix_bug_*` 分支，最新 commit 超 `stale_hours`（默认 48h）且未合并
（merged=false）即为滞留：有打开态 MR 提醒合并、无 MR 提醒补建，文本消息发
`git.mr.chat_ids`（空则回退 `feishu.default_chat_id`）。

### 2.8 可观测性（/metrics 与 /readyz，`app/core/metrics.py`）

- **`GET /metrics`**：Prometheus 文本协议。核心指标：
  `alarm_received_total{source}`、`alarm_analyse_total{outcome=ok|repeated|error}`、
  `alarm_fix_accept_total{action,outcome}`、`alarm_fix_result_total{fix_status}`、
  `alarm_fix_validation_total{outcome}`、`alarm_mr_total{outcome}`、
  `feishu_send_total{kind,outcome}`（含失败重发）、
  `llm_call_seconds{channel=local|gateway,task=analyse|fix|selfcheck}`、
  `llm_call_total{channel,task,outcome}`、`llm_tokens_total{kind=prompt|completion}`（本地 LLM）、
  `alarm_executor_inflight`（在途+排队积压）、`alarm_executor_reject_total`。
  label 只用有限枚举值，traceId/service 不入 label。
- **`GET /readyz`**：就绪探针，区别于浅探活 `/healthz`——检查业务库（`SELECT 1`）与 Redis
  连通性，任一配置了的组件不可用返回 503；未配置业务库记 not_configured 不阻断（分析/卡片仍可用）。
  LLM/外挂网关按设计允许运行期降级，不作为就绪条件。
- `prometheus_client` 缺失时指标退化为 no-op、服务照常启动（正常部署由 requirements.txt 保证）。

---

## 3. 目录结构与 Java 版对照

```
AI-alarm_autoFix/
├── app/
│   ├── main.py                  # 入口：create_app 工厂、lifespan（重发线程/日报调度/优雅停机）
│   ├── container.py             # 依赖装配（等价 Spring 容器，显式构造无注解魔法）
│   ├── config.py                # 配置模型(严格校验)+加载+env覆盖+key常量     ← AlarmAnalyseProperties
│   ├── api/routes.py            # 4 个 HTTP 端点                              ← FeishuAlarmController / FeishuCardCallbackController
│   ├── models/alarm.py          # camelCase 报文模型+落库行                    ← AlarmLogBO / AlarmAnalyseResult / entity
│   ├── extractors/
│   │   ├── stack.py             # 堆栈帧解析/类名→路径/异常类                   ← GitLabCodeFetcher 内部解析逻辑
│   │   └── tables.py            # SQL 表名提取（双正则）                        ← SqlTableExtractor
│   ├── providers/
│   │   ├── agent_gateway.py     # 外挂 Agent 网关（读码/任务式分析/修复提交）     ← ExternalLlmChatClient + AgentGatewayCodeClient
│   │   ├── gitlab.py            # GitLab API v4（读文件/建分支/提交/查commit）   ← 两处 Java 类的 API 直连部分
│   │   ├── nacos.py             # 按服务名解析数据源(direct-db/open-api)        ← NacosConfigResolver
│   │   ├── db_context.py        # 表 DDL/行数/样本行采集（全参数化 SQL）         ← JdbcAlarmContextFetcher
│   │   └── llm.py               # OpenAI 兼容客户端 + provider 注册表           ← LlmChatClient
│   ├── analysis/
│   │   ├── service.py           # 分析主流程（提取/取码/编排）                   ← AlarmAnalyseServiceImpl
│   │   └── report.py            # 报告生成+三段拆分+规则兜底                    ← AlarmReportGenerator + AiCodeFixAnalyzer
│   ├── fixing/
│   │   ├── generator.py         # 修复生成（文件定位/全文件/JSON契约解析）        ← AlarmFixGenerator
│   │   ├── committer.py         # 修复提交（网关→GitLab 兜底/分支命名）          ← GitLabFixCommitter
│   │   └── service.py           # 受理四道闸门/异步执行/三态/通知/双锁           ← FeishuAlarmController.executeFix
│   ├── reporting/daily_report.py# 日报（统计/洞察/推送）                        ← AlarmDailyReportService + FeishuAlarmDailyReportJob
│   ├── web/
│   │   ├── dashboard.py         # Web 看板（KPI/趋势/明细 API + 合并状态解析器）  ← 新增（Java 版无）
│   │   └── templates/dashboard.html # 看板单页（原生 JS 自绘 SVG 图表，无前端构建链）
│   ├── feishu/
│   │   ├── client.py            # token缓存/发消息/失败重发队列                  ← FeishuMessageServiceImpl(chat部分)
│   │   └── cards.py             # 卡片构建+字节预算削减                          ← FeishuCardBuilder + TextTruncator
│   ├── storage/alarm_repository.py # 落库/按traceId查/更新修复结果/区间查询        ← IAlarmAnalyzeResultService
│   └── core/                    # http(超时+重试退避) kv(锁/缓存/队列) executor(有界线程池) text(字节截断) errors(异常体系) logging
├── docs/agent-gateway-deploy.md # ⭐ 外挂 Agent 网关部署手册（协议契约/env/冒烟/9 条坑）
├── sql/schema.sql               # alarm_analyze_result 建表 DDL（含旧表补列 ALTER）
├── e2e/                         # test 环境端到端实测脚本（预检/告警报文/日报驱动）
├── config.example.yaml          # 全量配置模板（逐项注释）
├── tests/                       # 133 个单元/冒烟测试
├── requirements.txt / pytest.ini / Dockerfile / .gitignore
└── config.yaml                  # 真实运行配置（gitignore，含凭证禁止提交）
```

---

## 4. 快速开始

**环境**：Python ≥ 3.10；可选 MySQL（落库）、Redis（多实例必配）、飞书应用、LLM key、Agent 网关。

```bash
# 1) 安装依赖
python -m venv .venv
.venv\Scripts\activate                 # Windows；Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2) 配置
cp config.example.yaml config.yaml     # 至少填：feishu.app_id/app_secret/default_chat_id/
                                       # default_card_template_id + storage.database
# 密钥可用环境变量覆盖（优先级高于文件）：
#   ALARM_FEISHU_APP_ID / ALARM_FEISHU_APP_SECRET / ALARM_GITLAB_TOKEN /
#   ALARM_LLM_API_KEY / ALARM_EXTERNAL_API_KEY / ALARM_REDIS_URL / ALARM_DB_PASSWORD

# 3) 建表（复用 Java 版已有表则跳过；旧表补 fix 三列见 sql/schema.sql 尾部注释）
mysql -h <host> -u <user> -p <db> < sql/schema.sql

# 4) 启动（缺省读 ./config.yaml；ALARM_CONFIG 环境变量可指定其他路径）
python -m app.main                     # 或 uvicorn app.main:create_app --factory --port 8800

# 5) 冒烟
curl http://127.0.0.1:8800/healthz
curl -X POST http://127.0.0.1:8800/inner/feishu/alarm/receive \
  -H "Content-Type: application/json" -d @e2e/alarm_payload.json
```

单元测试（不出网）：`python -m pytest -q` → 84 passed。

---

## 5. 配置详解

配置在启动时一次性加载并**严格校验**：未知键/类型错误直接启动失败（防配置静默失效）。
完整逐项注释见 `config.example.yaml`，此处列关键项。

### feishu（飞书通道）

| 键 | 说明 |
| --- | --- |
| `enable` | 总开关，false 时 receive 接口忽略告警 |
| `app_id` / `app_secret` | 飞书应用凭证（tenant_access_token 自动获取并缓存） |
| `default_chat_id` | 默认告警群；请求体 `chatId` 可覆盖 |
| `default_card_template_id` | 告警卡片模板（变量见 §7）；请求体 `templateId` 可覆盖 |
| `jump_url` | 卡片「修复」按钮跳转前缀，指到本服务的 `/inner/feishu/alarm/callback/fix?traceId=` |
| `ignore_jump_url` | 卡片「忽略」按钮跳转前缀（`/inner/feishu/alarm/callback/ignore?traceId=`）；配置后卡片输出 `ignor_url` 变量，空则不输出 |
| `callback_token` | 卡片交互回调校验 token（空则跳过校验） |
| `inner_auth_token` | `/inner/*` 接口头鉴权（`xizhao_request_auth`），空则不校验 |
| `env` | 展示在卡片上的环境标识（test/prod…） |

### analyse（分析/修复核心）

| 子节 | 关键键 | 说明 |
| --- | --- | --- |
| `git` | `default_branch` | **分析取码分支**：必须与产生告警的环境一致，行号才对得上（test 环境=test，生产=prod） |
| | `fix_base_branch` | **修复基线分支**：固定稳定分支，fix 分支从它拉出（避免 test/UAT 并行开发冲突） |
| | `projects` | 服务名→GitLab 项目 ID 映射（网关读码不依赖它；GitLab 直连兜底用） |
| | `max_snippets` / `context_lines` | 最多取 3 个片段 / 出错行 ±15 行 |
| `llm` | `provider` | deepseek / openai / qwen / custom（baseUrl/model 缺省按注册表取默认） |
| | `max_tokens` | **思考型模型注意**：思维链计入额度，修复链路要输出整文件，建议 ≥20000 |
| | `max_retries` | 429/5xx/网络错误指数退避重试次数 |
| `external` | `url` / `api_key` | Agent 网关地址与 Bearer token（部署见 docs/agent-gateway-deploy.md） |
| | `workspace` / `workspaces` | 网关侧 git 工作区：默认名 + 服务名映射 |
| | `code_read_enabled` / `fix_commit_enabled` | 读码/提交走网关的开关（失败均自动降级 GitLab 直连） |
| `db` | `nacos.config_db` | Nacos 配置库连接（direct-db 模式，建议只读账号 robot） |
| | `nacos.namespace` | 命名空间=test 环境（预检脚本可自动发现） |
| | `datasources` / `service_datasources` | 静态数据源兜底（Nacos 解析不到时用） |
| `fix_notify` | `card_template_id` | 修复结果卡片模板（7 变量）；空则文本兜底 |
| | `manual_card_template_id` | 人工处理/转人工通知卡片模板（6 变量）；空则文本兜底 |
| `repeat` | `window_hours` | 指纹重复/复发识别回溯窗口（小时），默认 24；0 或负数关闭 |
| `daily_report` | `cron` | 分钟级 cron（北京时间），默认 `40 9 * * *` |
| | `chat_ids` / `card_template_id` | 推送群列表 / 日报模板（变量 content）；模板不可见时自动文本兜底 |
| | `insights_llm_enabled` / `insight_max_items` | LLM 洞察开关 / 最多条数 |

### dashboard（Web 告警看板）

| 键 | 说明 |
| --- | --- |
| `enabled` | false 时不下线 /dashboard 路由 |
| `username` / `password` | 可选 HTTP Basic 认证，两者同时非空才启用（页面与 API 一起受保护）；密码可用环境变量 `ALARM_DASHBOARD_PASSWORD` 覆盖 |
| `merge_cache_seconds` | 「是否合并」GitLab 查询结果的进程内缓存秒数（默认 300） |

### storage / executor / server

| 键 | 说明 |
| --- | --- |
| `storage.redis_url` | 空=进程内实现（仅单实例）；多实例部署**必须**配 |
| `storage.database` | `alarm_analyze_result` 所在库（jdbc:mysql:// 或 mysql:// 前缀均可解析） |
| `executor.workers` / `queue_size` | 分析/修复线程池（4/100，满则退化调用线程执行） |
| `server.host` / `port` / `log_level` | 监听与日志级别 |

---

## 6. HTTP 接口文档

统一响应 `{"code": 0, "message": "success", "data": ...}`，code=0 成功。

### 6.1 POST /inner/feishu/alarm/receive —— 接收告警

请求体（camelCase，与 Java 推送方零改动兼容；`source`、`message` 必填）：

```json
{
  "source": "ccd-system",
  "level": "ERROR",
  "title": "数据库查询异常",
  "requestUri": "/api/xxx",
  "message": "### Error querying database. Cause: ...",
  "stackTrace": "java.sql.SQLSyntaxErrorException: ...\n\tat com.xizhao....",
  "traceId": "ccd-design-project-5290",
  "time": "2026-09-17 11:08:30",
  "businessKeys": {"uri": "/api/xxx", "params": "{\"id\":1}"},
  "chatId": "", "templateId": ""
}
```

响应 `{"code":0,"message":"success","data":true}` —— 受理即返回，分析异步执行。
`businessKeys.uri` 兜底请求接口、`businessKeys.params` 兜底请求参数并用于查库样本行等值匹配。

### 6.2 GET /inner/feishu/alarm/callback/fix?traceId=xxx —— 触发自动修复

受理失败（code=500）文案即原因：`修复正在执行中，请稍候` / `未找到告警分析结果` /
`该告警已成功修复，请勿重复点击` / `该告警已忽略（判定无需处理），请勿重复点击` /
`该告警已转人工排查（上下游依赖问题），请勿重复点击` / `该告警已转人工处理，请勿重复点击` /
`该服务正在修复其他告警，请稍后再试`。
受理成功（code=0）仅代表任务已受理，**实际结果看修复通知卡片与落库 fix_status**。

### 6.3 GET /inner/feishu/alarm/callback/ignore?traceId=xxx —— 忽略告警

人工判定无需修复时调用（卡片「忽略」按钮跳转，入参同修复回调）：落库
`fix_status=NOT_NEEDED`（永久防重，再点修复/忽略提示已忽略/已修复）。修复执行中拒绝忽略；
已完结（已修复/已忽略/已转人工/待人工）拒绝并按状态提示。

### 6.4 POST /feishu/alarm/callback —— 飞书卡片交互回调

飞书服务器调用（需公网可达）。响应 toast：
`{"toast":{"type":"success","content":"已确认，处理中"}}`；token 不匹配/未知 action 返回 error toast。

### 6.5 GET /healthz

`{"status":"UP"}`（浅探活，只证明进程活着）。

### 6.6 GET /readyz —— 就绪探针

检查依赖组件连通性：业务库 `SELECT 1`、Redis ping。全部通过返回 200
`{"status":"READY","checks":{"db":"ok","kv":"ok"}}`；配置了的组件不可用返回 503
（checks 里对应项为 `error`）。业务库未配置记 `not_configured` 不阻断就绪。
LLM/外挂网关按设计允许运行期降级，不作为就绪条件。

### 6.7 GET /metrics —— Prometheus 指标

文本协议输出（见 2.8 节指标清单）。

### 6.8 GET /dashboard —— Web 告警看板

只读页面 + 配套 JSON API（`dashboard.enabled=false` 时不下线；配置了
`dashboard.username/password` 后页面与 API 一起走 HTTP Basic）：

| 端点 | 说明 |
| --- | --- |
| `GET /dashboard` | 看板单页，**总览 / 告警明细** 两个标签页分区布局（原生 JS 自绘 SVG 图表，无 CDN/构建链） |
| `GET /api/dashboard/overview?days=14&service=` | KPI（总数/已解决+解决率/未处理/修复失败/待人工/无需处理/已合并+合并率）+ 状态与归因分布（含占比）+ 每日新增/修复折线 + **近 12 周对比柱图** + **服务问题点 TOP10**（堆叠条：已解决/未处理/失败） |
| `GET /api/dashboard/alarms?page=&size=&days=&service=&status=&q=` | 分页明细（页面默认每页 10 条、查近 30 天；FIXED 记录带合并状态徽标） |
| `GET /api/dashboard/alarms/{id}` | 详情：完整分析内容 + 处理情况（fix_summary/合并状态/链接） |
| `GET /api/dashboard/services` | 服务名筛选项 |

**「是否合并」判定**（以 GitLab 为准，进程内缓存 `merge_cache_seconds`）：

1. **commit 对比（主）**：fix_summary 里的 `commit: <shortId>` 调 GitLab「查询包含该 commit
   的分支」，修复基线分支 `fix_base_branch` 在其中 ⇒ 已合并；
2. **MR 状态（兜底）**：按 fix 分支名查任意状态 MR，`state=merged` ⇒ 已合并
   （兼容 squash merge 换 sha 的场景；`opened` ⇒ 待合并）；
3. 两者都无法确认（GitLab 未配置/查询失败/记录缺 commit 信息）⇒ 显示「未知」，
   详情页附 fix_summary 里的 MR/改动链接兜底。

**口径与日报一致（勿改出两套）**：解决率 = 已解决 / (总数 − 无需处理 − 转人工 − 待人工 − 重复)，
复发（RECURRED）不算重复、保留在分母；分母非正时看板不展示该项。
业务库未配置时 API 返回 code=500 与明确文案，页面本身仍可访问。
合并扫描每轮最多查最近 50 条已修复记录（超出在 KPI 副标题标注），首屏后走缓存。

---

## 7. 数据与存储

### 7.1 表 `alarm_analyze_result`（建表见 sql/schema.sql）

| 列 | 说明 |
| --- | --- |
| id / trace_id / service_name / `interface` / create_time / update_time | 基础字段（`interface` 是列名，SQL 中需反引号） |
| analyze_content | 完整分析结果 JSON（键 camelCase，与 Java 互操作） |
| fix_status | `FIXED` / `NOT_NEEDED` / `FAILED` / `HANDOVER` / `MANUAL_PENDING` / `NULL`(未发起) |
| fix_summary | commit message 原文+shortId+链接、人工操作步骤或忽略标记（应用层截 1000 字符） |
| fix_time | 修复结果时间 |
| problem_type | 归因：`CODE` / `DEPENDENCY` / `MANUAL_REQUIRED` / `NOT_A_PROBLEM` / `NULL`(未归因) |
| fingerprint | 告警指纹 md5(service\|requestUri\|exceptionClass\|首个业务帧)，索引 idx_fingerprint_time |
| repeat_type | `NORMAL` / `REPEATED`(重复压卡片) / `RECURRED`(修复后复发) |
| root_id | 首次出现记录 id（REPEATED/RECURRED 指向根告警） |

`analyze_content` JSON 字段：`service, env, level, title, coreMessage, exceptionClass, tables,
codeContext, stackTrace, dbContext, analysis, proposal, traceId, time, rawMessage, requestUri,
requestParam, url, problemType, fingerprint, repeatType, rootId`（空值省略）。

> 复用 Java 版旧表须先执行 `sql/schema.sql` 尾部注释里的归因四列 ALTER（2026-09-17 批次），
> 否则指纹查询/落库会因缺列报错。

### 7.2 Redis key（与 Java 版一致，可互通）

见 §2.5 锁汇总表。另：飞书手机号→openId 缓存等 Java 版其他 key 本服务不使用。

---

## 8. 测试与实测报告

### 8.1 单元/冒烟测试（133 个，不出网）

```bash
python -m pytest -q
```

覆盖：字节截断（含中文/emoji 边界）、SQL 表名提取、堆栈帧解析与类名转路径、异常类识别、
修复 JSON 契约解析（snake/camel/代码块包裹/缺字段）、分支命名清洗、报告三段拆分与兜底、
告警卡片变量与 28KB 预算削减、日报统计口径（成功率剔除 NOT_NEEDED）、配置加载与严格校验、
JDBC/MySQL URL 解析、锁语义（互斥/持有者/队列）、接口契约（TestClient 全装配冒烟）。

### 8.2 test 环境端到端实测（2026-09-17，真实环境全绿）

**准备**：`e2e/preflight.py` 预检 7 项连通性（Nacos 配置库/业务库表结构/Redis/GitLab/网关/DeepSeek/飞书），
真实凭证取自 Nacos 快照写入 `config.yaml`。

**实测步骤与结果**：

| 步骤 | 命令/动作 | 结果 |
| --- | --- | --- |
| 起服务 | `python -m app.main` | UP；日报调度、飞书重发线程就绪 |
| 推真实告警 | `POST /receive -d @e2e/alarm_payload.json`（指向真实类 `AlarmAnalyseServiceImpl:77`） | 受理后 10s：Agent 分析完成(1435字) → 落库 → **飞书卡片真实送达** `om_x100b658...d8c` → Redis 缓存(camelCase 键) |
| 触发修复 | 飞书群点卡片按钮 / `GET /callback/fix?traceId=...` | 网关 fix 任务(14KB 全文件) → 判 `NOT_NEEDED` → 三态落库(fix_status/summary/time) → 修复通知卡片送达测试群 |
| 防重 | 对已完结告警再点修复 | 精确拒绝：「该告警已判定无需修复（非代码问题），请勿重复点击」 |
| 并发锁 | 同一告警连发两个修复请求 | 一受理一拒绝（「修复正在执行中，请稍候」） |
| 日报 | `python e2e/run_daily_report.py` | 真实统计（9月54条，成功率22.9%=8/(54-19)，NOT_NEEDED 正确剔除分母）+ LLM 洞察（引用真实告警编号/Feign 异常归类）+ 推送测试群 |

**实测揪出并修复的 2 个 bug**（单测覆盖不到，只有真环境能暴露）：
1. `DatabaseStoreConfig` 缺 URL 解析方法 → 启动即崩（已修 + 回归测试）；
2. `FeishuClient` 缓存字段 `_token` 遮蔽同名方法 → 发消息必崩（已改名 `_access_token`）。

**实测发现的环境问题**（非本项目缺陷，但影响两版共用的基础设施）：
- Nacos 快照里的 GitLab read token 已过期（401）——Java 版直连兜底通道同样失效，全靠网关；
  新 token 已更新到本服务 config.yaml，**建议同步更新 Nacos**。
- SQL 表名提取正则不认带连字符的库名（`ccd-system.xxx` 提取不到）——**Java 版原样同样不匹配**，
  行为一致；如需支持两边一起改。

**复跑**：

```bash
python e2e/preflight.py                       # 预检
python -m app.main > e2e/server.log 2>&1 &    # 起服务
curl -X POST http://127.0.0.1:8800/inner/feishu/alarm/receive \
  -H "Content-Type: application/json" --data-binary @e2e/alarm_payload.json
curl "http://127.0.0.1:8800/inner/feishu/alarm/callback/fix?traceId=<上一步的traceId>"
PYTHONIOENCODING=utf-8 python e2e/run_daily_report.py   # 手动触发日报（Windows GBK 控制台需加环境变量）
```

---

## 9. 部署

**物理机**：按 §4 起服务；建议 systemd（Linux）/ NSSM（Windows）守护；**多实例必须配
`storage.redis_url`**；时区对日报统计有影响，Docker 内已固定 `TZ=Asia/Shanghai`。

**Docker**：

```bash
docker build -t ai-alarm-autofix .
docker run -d --name ai-alarm -p 8800:8800 \
  -v /opt/ai-alarm/config.yaml:/app/config.yaml \
  -e ALARM_DB_PASSWORD=xxx \
  ai-alarm-autofix
```

**外挂 Agent 网关**（生产建议启用，分析/修复链路的主通道）：
完整部署手册见 **[docs/agent-gateway-deploy.md](docs/agent-gateway-deploy.md)** ——
端点协议契约、.env token 说明、Docker 部署、workspace 预置、GitLab webhook 注册、curl 冒烟清单、
9 条历史踩坑（argv 100KB 限制 / compose 不换容器 / tzdata / 旧文档缺 /fix/commit 等）。

**上线 checklist**：① 建表或确认旧表已有 fix 三列；② 飞书卡片模板对该应用开放可见（否则 200381）；
③ `jump_url` 指到本服务可达地址；④ `default_branch` 与环境分支一致；⑤ 网关 api_key 两边一致；
⑥ 推送方把 receive 地址切过来（报文无需改动）。

---

## 10. 与 Java 版的差异与增强

**更灵活**

| # | 增强点 |
| --- | --- |
| 1 | 配置单文件化（YAML）+ 环境变量覆盖密钥，脱离 Nacos 独立部署；全部键严格校验 |
| 2 | Redis 可选：未配置自动退化进程内锁/缓存，本地开发零外部依赖 |
| 3 | LLM provider 注册表化，新增服务商加一行 |
| 4 | 日报自带调度（APScheduler），不依赖 xxl-job 平台 |
| 5 | `/inner/*` 内置可选头鉴权，不依赖网关白名单 |
| 6 | 依赖显式装配（container.py），替换任一组件无需注解魔法 |

**更严谨**

| # | 增强点 |
| --- | --- |
| 1 | 全量类型标注 + pydantic 校验；camelCase 契约有测试锁定 |
| 2 | SQL 全参数化/白名单（Java 版样本查询为手工转义拼接）；锁释放 Lua 比较删除 |
| 3 | 出站 HTTP 统一超时 + 429/5xx 指数退避重试；日志不落 header/body |
| 4 | **LLM 空 content 报错带 finish_reason 与响应体摘要**（修复 Java 版排障信息丢失缺陷） |
| 5 | 业务库不可用时修复接口友好降级（明确文案）而非 500 |
| 6 | 133 个单测 + test 环境真实验证，核心语义全部有测试锁定 |

**有意的行为差异**

| 项 | Java 版 | 本版 |
| --- | --- | --- |
| 日报调度 | xxl-job `0 40 9 * * ?` | APScheduler `40 9 * * *`（可配） |
| 线程池满 | CallerRuns | 同语义（退化调用线程执行） |
| 锁释放 | 直接 DEL | Lua 比较删除 |
| 修复接口响应报文 | ResponseVO | `{code, message, data}`（按钮走跳转链接不解析响应体，无影响） |
| 飞书重发队列 | 含 phone 系列消息 | 仅 chat 场景（本模块只用 chat），key 与 Java 相同 |

---

## 11. 常见问题 FAQ

- **卡片发出但变量空白/按钮无效**：模板变量以后台搭建工具实际配置为准。已知坑：`url` 变量必须传
  `{url, pc_url}` 对象且为完整 http(s) 链接（否则 200732/200737）；新模板必须对该应用开放可见范围
  （否则 200381 template is not visible）。本版已内置 url 对象包装。
- **LLM 返回空 content**：多为思考型模型 `max_tokens` 被思维链耗尽（调大到 20000+）。
  本版报错自带 finish_reason，可直接确认是 `length` 截断还是限流。
- **修复一直 FAILED 且日志见 non-fast-forward**：同服务同日重复修复撞已 push 的 fix 分支（遗留已知
  问题，根治需网关改基于已存在分支提交，见网关文档 §7）。临时处理：删远端 `fix_bug_yyyyMMdd_*` 分支或等次日。
- **日志里 GitLab 401 invalid_token**：直连兜底通道的 token 过期。网关通道正常则不影响功能，
  但建议更新 token（Java 版共用同一配置）。
- **告警里写了表名但没查库**：先看日志——「未提取到表名」多是库名带连字符（正则不含 `-`，Java 版
  行为一致）或表名不在 `from/join/update/insert into` 等关键字后；「未匹配到数据源」是 Nacos 里
  找不到该服务配置（检查 namespace 与 dataId 命名）。
- **想灰度/回切 Java 版**：报文、表结构、Redis key 三者完全兼容——把推送方 receive 地址、卡片
  jump_url 指回 Java 服务即可；未完结的修复两边都能接着处理（同一张表、同一套锁）。
- **Windows 控制台中文/emoji 乱码**：仅显示层问题；跑输出含 emoji 的脚本加 `PYTHONIOENCODING=utf-8`。
