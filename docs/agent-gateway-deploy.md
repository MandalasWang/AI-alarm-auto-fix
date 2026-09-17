# 外挂 Agent 网关部署文档

> 适用对象：AI-alarm_autoFix（Python 版）与 ccd-java-system（Java 版）共用的同一套 Agent 网关。
> 两个调用方实现的是**同一份网关协议**，网关升级对双方透明。
> server.js 全文以内嵌形式保存在 Java 仓库 `docs/alarm-agent-gateway-deploy.md`（v3 版，991 行文档内嵌），
> 本文不重复贴全文，只给部署步骤、协议契约与踩坑清单。

---

## 1. 网关是什么、为什么需要

网关是一台**内网独立 Node 服务**，上面跑着 Codex CLI（或多 provider Agent），并预置了各业务服务的
**git 源码工作区（workspace）**。本服务（AI-alarm_autoFix）只做编排与 JSON 契约校验，把三类脏活外包给它：

| 能力 | 网关端点 | 解决的问题 |
| --- | --- | --- |
| 读代码 | `GET /code/file` | prod 服务器到内网 GitLab 路由不通（实测三连 Connect timed out），读码必须借道网关本地 workspace |
| 分析/修复生成 | `POST /analyze` + `GET /analyze/{taskId}` | Agent（codex）在网关服务器 workspace 内直接读仓库干活，绕开「把代码喂给远端 LLM」的 prompt 大小限制 |
| 修复提交 | `POST /fix/commit` | push 同样依赖内网 GitLab 连通性；网关在本地独立 worktree 建分支提交并 push |

```
┌────────────────┐  HTTP(Bearer)   ┌──────────────────┐   git fetch/push   ┌─────────────┐
│ AI-alarm_autoFix│ ──────────────▶ │  Agent 网关(Node) │ ◀────────────────▶ │  GitLab     │
│  (本项目)       │  任务式协议      │  + codex CLI      │  webhook + 定时同步 │  (内网)      │
└────────────────┘ ◀────────────── └──────────────────┘                    └─────────────┘
                    任务结果 JSON        │ git 只读 workspace
                                       ▼
                                 workspaces/<服务仓库>/
```

工作区新鲜度由两条通道保证：GitLab push webhook（`/webhook`，X-Gitlab-Token 验签）+ 每晚定时同步（`SYNC_HOUR`）。

---

## 2. 端点协议契约（Python 侧按此对接，已实现于 `app/providers/agent_gateway.py`）

所有业务端点要求请求头 `Authorization: Bearer <GATEWAY_TOKEN>`（对应网关 `.env` 的 `GATEWAY_TOKEN`）。

| 端点 | 方法 | 请求 | 成功响应 | 失败语义 |
| --- | --- | --- | --- | --- |
| `/analyze` | POST | `{"taskType":"analyse"\|"fix","workspace":"<名>","prompt":"<全文>"}` | `{"code":0,"data":{"taskId":"..."}}` | code≠0 / 无 taskId = 提交被拒 |
| `/analyze/{taskId}` | GET | - | `{"code":0,"data":{"status":"PENDING\|RUNNING\|SUCCESS\|FAILED","lastMessage":"...","error":"..."}}` | SUCCESS 取 `lastMessage`（Agent 输出原文，**JSON 契约由调用方校验**）；FAILED 取 `error` |
| `/code/file` | GET | `?workspace=&path=&ref=` | HTTP 200 + `{"data":{"content":"<文件全文>"}}` | 非 200 / 无 content → 调用方降级 GitLab API 直连 |
| `/fix/commit` | POST | `{"workspace","branch","baseBranch","path","content","message"}` | `{"data":{"branch","shortId","webUrl","message"}}` | 非 200 / 无 branch → 降级 GitLab API 直连；**503 = 网关未配 `GITLAB_PUSH_TOKEN`** |
| `/webhook` | POST | GitLab push 事件 | - | `X-Gitlab-Token` 验签，**在 Bearer 校验之前** |

调用方降级矩阵（全部自动，无需人工干预）：

| 场景 | 降级行为 |
| --- | --- |
| 网关未启用（`external.enabled=false`） | 读码/分析走 GitLab API + 本地 LLM；修复提交走 GitLab API 直连 |
| 网关读码失败 | GitLab repository files API 直连（`analyse.git.*` 需配置） |
| Agent 分析任务失败/超时 | 本地 LLM（`analyse.llm.*`）→ 规则兜底报告 |
| Agent 修复生成失败 | 本地 LLM → fix_status=FAILED（可重试） |
| 网关修复提交失败/503 | GitLab API 直连（建分支 → 服务端落 commit） |

---

## 3. 服务器与目录要求

| 项 | 要求 |
| --- | --- |
| 位置 | 内网机器（能与 GitLab、npm 镜像连通）；prod 场景需调用方可达（test: `10.211.110.97:8787`，prod 公网入口: `120.24.229.200:8787`） |
| 运行时 | Docker + docker compose（v2），镜像 `node:20-slim` 基础上装 codex-cli |
| 目录 | `/opt/agent-gateway/`：`server.js`、`Dockerfile`、`docker-compose.yml`、`.env`、`prepare-workspace.sh`、`data/workspaces/<仓库>/` |

---

## 4. 部署步骤

### 4.1 准备文件

1. 从 Java 仓库取 **server.js v3 全文**：`ccd-java-system` 仓库 `docs/alarm-agent-gateway-deploy.md`
   内嵌版本（认准带 `/fix/commit` + `GITLAB_PUSH_TOKEN` 的 991 行文档；**Downloads 流传的 890 行旧版缺修复提交端点，
   用它部署会丢 /fix/commit，调用方降级 GitLab 直连且网络不通 = 修复链路全断**）。
2. 同文档内附 Dockerfile / docker-compose.yml / prepare-workspace.sh 模板。

### 4.2 配置 .env

```ini
# 调用方 Bearer 鉴权（本服务 config.yaml 的 analyse.external.api_key 必须与它一致）
GATEWAY_TOKEN=<48位随机串>
# 只读 token：同步 workspace 用；不配则仅降级同步（webhook 失效）
GITLAB_TOKEN=<read_api 权限>
# 可写 token：/fix/commit push 用；不配则该端点返回 503
GITLAB_PUSH_TOKEN=<api/write_repository 权限>
# GitLab webhook 验签
GITLAB_WEBHOOK_SECRET=<随机串>
# v3：prompt 改走 stdin 后的单任务上限（默认 512KB，够 fix 任务喂整文件）
MAX_PROMPT_BYTES=524288
# 每晚定时同步小时数；node:20-slim 无 tzdata，容器必须装 tzdata 且设 TZ，否则同步时刻漂移
SYNC_HOUR=22
TZ=Asia/Shanghai
```

### 4.3 启动

```bash
cd /opt/agent-gateway
docker compose up -d --build
# ⚠️ 坑：compose v2.27 的 up -d --build 可能只重建镜像不换容器，
#    必须补一条强制重建并确认容器内代码版本：
docker compose up -d --force-recreate
docker compose exec gateway node -e "console.log(require('fs').readFileSync('/app/server.js','utf8').length)"
```

### 4.4 预置工作区

对每个要分析的仓库执行 `prepare-workspace.sh <仓库名>`（clone 到 `data/workspaces/<仓库名>`）。
workspace 名即调用方 `external.workspace` / `workspaces` 映射里的值（默认 `ccd-java-system`）。

### 4.5 注册 GitLab webhook

- 事件：Push events；URL：`http://<网关>:8787/webhook`；Secret：`.env` 的 `GITLAB_WEBHOOK_SECRET`。
- ⚠️ 坑：用 GitLab **API** 注册 webhook 需要带 `api` 权限的 token，只读/可写两种业务 token 均 403；
  实操用 **Maintainer 账号在 GitLab UI 手动配置**。

---

## 5. 冒烟验证清单

```bash
GW=http://127.0.0.1:8787
AUTH="Authorization: Bearer $GATEWAY_TOKEN"

# 1. 鉴权生效（无 token 应 401）
curl -s -o /dev/null -w "%{http_code}\n" $GW/code/file           # 期待 401

# 2. 读码（workspace/path/ref 按实际替换；404 = ref 或路径不对，200 且 content 非空 = 通）
curl -s -H "$AUTH" "$GW/code/file?workspace=ccd-java-system&path=pom.xml&ref=prod" | head -c 200

# 3. fix/commit 探活（不带 body 应 400，说明端点存在；503 = 缺 GITLAB_PUSH_TOKEN）
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "$AUTH" $GW/fix/commit

# 4. 分析任务端到端（返回 taskId 后轮询 /analyze/{taskId} 至 SUCCESS）
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"taskType":"analyse","workspace":"ccd-java-system","prompt":"列出仓库 README 的标题"}' \
  $GW/analyze

# 5. webhook 验签（错误 secret 应被拒）
curl -s -o /dev/null -w "%{http_code}\n" -X POST -H "X-Gitlab-Token: wrong" $GW/webhook
```

任务运行日志在容器内 `data/tasks/logs/`（按天），每个任务另有全量 `<id>.out` / `<id>.err`。

---

## 6. 调用方（本服务）对接配置

```yaml
analyse:
  external:
    enabled: true
    url: http://10.211.110.97:8787     # test 网关；prod 填对应入口
    api_key: "<与网关 .env 的 GATEWAY_TOKEN 一致>"
    workspace: ccd-java-system         # 默认工作区
    workspaces: {}                     # 服务名 → 工作区（多服务时配）
    timeout_ms: 300000                 # Agent 仓库级分析分钟级，给足
    code_read_enabled: true
    fix_commit_enabled: true
```

⚠️ **Nacos 时代遗留检查**：Java 版把 `external.url` 配在 Nacos，test 环境曾长期指向 prod 公网网关。
Python 版配置文件里写哪个网关就是哪个网关，切换环境时改 `config.yaml` 即可，注意别照抄旧 Nacos 值。

## 7. 容量与并发（排障必读）

- 网关：`MAX_CONCURRENT=2` 个 codex 进程 + 队列 20，满则 **429** → 调用方任务判 FAILED（可重试）。
- 本服务：执行池 `executor.workers=4` + `queue_size=100`，满则退化调用线程执行（不丢任务）。
- **同服务串行**由本服务 Redis 服务锁（15min）保证：同服务并发修复在网关侧会分支竞争
  （worktree checkout 冲突 / push non-fast-forward），必失败，所以快速失败提示「该服务正在修复其他告警」。
- 遗留已知问题：同服务同日一条 FAILED 重试仍可能撞已 push 的 fix 分支 non-fast-forward；
  根治需网关 fixCommit 改为基于已存在 fix 分支提交（`--force-with-lease`）。

## 8. 历史踩坑清单（部署/联调时逐条核对）

| # | 坑 | 后果 | 对策 |
| --- | --- | --- | --- |
| 1 | Linux `MAX_ARG_STRLEN` 128KB 限制，argv 传 prompt | fix 任务喂整文件必炸 400 | 网关 v3 起 prompt 走 stdin（`codex exec "-"`）；确认部署的是 v3 |
| 2 | `compose up -d --build` 不换容器（v2.27 实测） | 改了 server.js 不生效 | 再执行 `up -d --force-recreate`，并进容器核对文件 |
| 3 | `node:20-slim` 无 tzdata | `SYNC_HOUR=22` 实际在北京时间 6 点同步 | Dockerfile 装 tzdata + env `TZ=Asia/Shanghai` |
| 4 | Dockerfile 里 npmmirror 源写在 `npm install` 之后 | npm install 走外网慢/失败 | registry 配置必须在 install 之前 |
| 5 | 用 890 行旧部署文档（缺 /fix/commit）部署 | 修复提交静默降级 GitLab 直连，网络不通时修复全断 | 用 991 行版（含 GITLAB_PUSH_TOKEN 段） |
| 6 | compose 未配 `GITLAB_PUSH_TOKEN` env 行 | /fix/commit 503 | `.env` 经 `env_file: .env` 注入即可，不必逐条写 environment |
| 7 | git 报错日志未脱敏 | push token 泄漏到日志/报文 | server.js v2+ 对 git stderr 做 `clean()`；自改代码时保持 |
| 8 | 思考型模型 `max_tokens` 过小（如 8000） | 思维链耗尽额度，content 返回空 | LLM 侧 `max_tokens: 32000`；Python 版会抛带 finish_reason 的明确错误便于定位 |
| 9 | 网关 `TASK_TIMEOUT_SECONDS` 与调用方 `external.timeout_ms` 相等 | 大任务两边同时超时，无法区分卡在哪侧 | 一起调大（如 600s），调用方略大于网关 |
