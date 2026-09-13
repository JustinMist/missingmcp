# Garmin China + Global 多租户实现结果

日期：2026-09-13

## 结果概览

Garmin region 已成为 account-level 属性。新账号使用
`cn:<normalized-email>` / `global:<normalized-email>`；已有裸 Garmin key 继续按
Global 解释，并在兼容条件满足时复用。相同邮箱的 CN 与 Global 账号拥有不同的
account key、加密 token blob、worker 目录和 worker 进程。

store 中的 Garmin token 使用 versioned wrapper：

```json
{"region":"cn|global","tokens":{"di_client_id":"…","di_refresh_token":"…","di_token":"…"},"v":1}
```

worker 只接收 raw token JSON，服务端根据账号 blob 写入
`GARMIN_IS_CN=true|false`。MFA、verification、refresh 和 restart capture 均以账号自身
region 为权威；region 不是 MCP tool 参数，不能由客户端请求覆盖。Garmin password
不会持久化。

## 最新 REVIEW_RESULT 修复状态

本轮以 2026-09-13 最新 `REVIEW_RESULT.md` 为准：BLOCKER 0 / HIGH 0 /
MEDIUM 2 / LOW 1。审查结论已允许进入隔离 E2E；以下非阻断项已全部处理。

- MEDIUM M1：已修复。token verification 成功后直接规范化并序列化已认证
  `g.client` 的内存状态，不再从候选临时文件回读。即使 `garminconnect` refresh 成功后
  静默忽略自动 `dump()` 的 I/O 失败，OAuth 仍持久化刷新后的 G2，而不是旧 G1；内存
  序列化失败或 token schema 非法则保持 verify-then-persist，账号和授权码不落库。
- MEDIUM M2：已修复。账号删除被排队请求观察到时，如旧 worker 仍活着，则保留其可信
  generation baseline 直至安全退役；同 key 重新登录会把新 DB generation 与旧 worker
  baseline 比较并强制替换，不再复用旧认证进程。若旧 worker 仍有 inflight 请求，新请求
  fail closed 并重试，不会路由到旧 generation；既有 stop/wait、orphan、cooling 和 CAS
  ownership 保持不变。
- LOW L1：已处理测试缺口。原先返回同一 fake port 的记录器测试升级为真实加密 store、
  真实 `WorkerManager`/`GarminWorkerForward` 和三个可区分 HTTP worker。测试直接断言 CN、
  Global、legacy Global 的响应身份、session、worker env 和独立 workdir，并验证伪造
  header/query/body/session 不能跨区、不存在账号不能 fallback。

## 修改文件

### 核心实现

- `src/missingmcp/adapters/base.py`
  - 保留 verification result 兼容接口，并记录可选的 restart read-back 准备 hook。
- `src/missingmcp/adapters/garmin/blob.py`（新增）
  - 严格 pack/unpack versioned Garmin blob；校验 token schema；裸 token 默认 Global。
- `src/missingmcp/adapters/garmin/login.py`
  - CN/Global 登录、MFA、verification 显式使用 `is_cn`。
  - 登录/verification 使用请求私有 tokenstore；MFA state 移除 password、PreparedRequest
    body 和过期 `_tokenstore_path`；verification 返回认证后的 token generation。
  - 私有依赖契约说明更新到 `garminconnect==0.3.6`；verification 从已认证 client 内存
    序列化最终 generation，避免 dependency 自动写盘失败回退旧 token。
- `src/missingmcp/adapters/garmin/__init__.py`
  - region 表单校验、server-owned MFA region、regional account key、blob 包装。
  - materialize/read-back 保留 region，输出账号专属 `GARMIN_IS_CN`。
  - restart read-back 通过 `prepare_read_back` 从 DB blob 恢复 expected-region；不依赖
    sidecar 自报 region，也不 materialize 覆盖待恢复 token。
- `src/missingmcp/adapters/__init__.py`
  - 为 Garmin 注入 legacy/canonical account-key resolver，不改变其他 adapter。
- `src/missingmcp/oauth.py`
  - login、MFA、upstream OAuth 均 verify-then-persist，保存 verification 后 blob；错误和
    timeout 不持久化候选凭据。
- `src/missingmcp/store.py`
  - account blob compare-and-swap；regional Garmin identity 兼容查询和 donation 匹配。
- `src/missingmcp/app.py`
  - 接入 account-key resolver、worker CAS persist callback 和锁内权威 blob loader。
- `src/missingmcp/workers.py`
  - SHA-256 workdir、父环境 Garmin fallback credential 清理、stop/wait/cooling/orphan
    ownership、expected-generation CAS 和 durable pending marker。
  - restart/停止后回读前调用 adapter 的只读 baseline 准备 hook。
  - pending retry 前比较 durable generation；新登录/删除使旧 capture 失效，store 读取
    失败或 generation 未变化时继续保留并 fail closed；periodic path 使用相同规则。
  - 活 worker 在账号删除后继续持有旧 baseline 作为退役依据；同 key 重建必换 worker，
    busy 旧 worker 不承接新 generation 请求。
- `src/missingmcp/log.py`
  - password/token/MFA/client-secret/Authorization 结构化及文本脱敏；覆盖 escaped
    quotes、Basic 及完整 Digest header。
- `src/missingmcp/templates/authorize.html`
  - China / Global 选择器，默认 Global。

### 运维、依赖与 CI

- `scripts/backfill_garmin_tokens.py`
  - wrapped/legacy blob、hashed/legacy workdir、region marker 和 CAS。
  - `--apply` 只恢复 manager-proven pending capture；未知 lineage、碰撞、torn、region
    mismatch 和 DB race 均 fail closed。
- `scripts/revoke.py`、`scripts/usage.py`
  - regional Garmin key 的运维说明。
- `pyproject.toml`、`uv.lock`
  - 精确锁定 `garminconnect==0.3.6`；lock 解析 48 packages。
- `garmin-worker-override.txt`（新增）
  - 为固定 worker ref 提供独立的 `0.3.6` exact pin 和 PyPI wheel/sdist hashes。
- `Dockerfile`
  - gateway 继续使用本仓库 frozen lock；固定 worker commit 继续使用自身 frozen lock，
    随后通过 hash-required override 将 worker venv 的 Garmin 客户端升级为同一精确
    `0.3.6`，并在构建期断言版本。
- `.github/workflows/test.yml`（新增）
  - 复现双 frozen 环境、应用 worker dependency override、断言两套 runtime 版本并运行
    完整测试。

### 文档

- `CLAUDE.md`
- `CONTEXT.md`
- `README.md`
- `docs/architecture.md`
- `garmin-ai-codex/IMPLEMENTATION_RESULT.md`（本文件）
- `garmin-ai-codex/userguide_cookbook.md`（新增）

文档已同步 regional identity、0.3.6 dependency contract、CN DI endpoint、MFA temp-path、
worker region、restart capture region authority、pending invalidation 和 backfill 规则。
Cookbook 另覆盖部署、双区域连接、MFA、legacy、状态、撤销、backfill、故障恢复、安全日志
和真实 E2E 验收步骤。

### 测试

- `tests/test_garmin_blob.py`（新增）
- `tests/test_garmin_dependency_contract.py`（新增）
- `tests/test_adapters.py`
- `tests/test_backfill_garmin_tokens.py`
- `tests/test_beers.py`
- `tests/test_garmin_login.py`
- `tests/test_log.py`
- `tests/test_oauth.py`
- `tests/test_proxy.py`
- `tests/test_store.py`
- `tests/test_telemetry.py`
- `tests/test_workers.py`

本轮新增 regression tests：

- `Client.dump()` 抛出 I/O 错误但真实 dependency refresh 成功时，CN/Global verification
  都返回内存 G2，并使用各自 DI endpoint。
- 完整 OAuth authorize 层在同一 dump 故障下持久化 G2 wrapper、保留 region、不持久化
  password，并正常签发授权码。
- `delete_account` 被排队 request 观察后同 key G9 重建：旧进程退役、G9 文件落盘、后续
  G10 refresh CAS 成功、旧 Bearer 已撤销。
- browser re-login 遇到 busy 旧 worker 时不终止 inflight，也不把新请求发往旧 generation。
- 真实 store/manager + 可区分 worker 的 CN/Global/legacy proxy 隔离；覆盖相反 region、
  account key、session 的 header/query/body 伪造及 missing-account no-fallback。
- 精确依赖版本和 gateway/worker build pin。
- 真实 `Client._exchange_service_ticket` 的 CN/Global DI URL。
- 真实 token-expiry/refresh 控制流下的 verification、MFA continuation 和 worker
  materialized-token CN/Global DI URL。
- restart pending capture × CN/Global × marker missing/opposite/invalid；失败时检查 DB、
  pending、磁盘、不 spawn，修复 sidecar 后检查实际 worker env。
- damaged pending × torn/missing/region-invalid × relogin/deleted/unchanged；覆盖 request 和
  periodic retry。
- 带逗号参数的 Digest Authorization 在 stdout、file tee、sink、traceback 中脱敏。
- 基于合法 wrapper 的单变量 version/region/extra-field 拒绝测试。

现有测试未删除，安全检查未降低。

## 测试结果

- dependency/blob/log 定向测试：`53 passed`。
- restart region + damaged pending 定向测试：`18 passed`；最终周期路径复跑
  `15 passed`。
- 最新 Garmin login/dependency/OAuth/worker 定向回归：
  `165 passed, 1 warning in 41.43s`。
- 可区分 worker proxy 回归：`14 passed, 1 warning in 9.35s`。
- 最终完整测试（精确 `garminconnect==0.3.6`）：
  **`551 passed, 1 warning in 75.66s`**。
- Python `compileall`：通过。
- `uv lock --check`：通过，48 packages。
- worker exact+hash override 独立安装及版本断言：通过，实际加载 `0.3.6`。
- `git diff --check`：通过。

唯一 warning 是第三方 Starlette TestClient 对 AnyIO `BlockingPortal` 旧别名的弃用提示。
完整 suite 保留 MissingMCP 原有多租户、OAuth/DCR/PKCE、WHOOP、remote/local adapter、
proxy、store、telemetry 和 worker 行为。

## 尚未解决的问题

- 最新 `REVIEW_RESULT.md` 中没有遗留 BLOCKER、HIGH；两项 MEDIUM 已安全修复，LOW
  测试缺口也已补齐。
- 未使用真实 Garmin 凭据，尚未执行真实 Garmin China、Global、MFA、refresh 和
  `garmin_mcp` 网络 E2E；没有执行生产 backfill、部署、提交或推送。
- 固定 worker commit 自身的历史 lock 仍记录 0.3.2；镜像与 CI 在 frozen sync 后应用
  本仓库 exact + hash-locked 的 0.3.6 override，并立即断言实际版本。最终 reviewer 应
  确认接受该双锁 + 显式 override 策略；若后续选择新的已审查 worker ref 且其 lock 已
  包含 0.3.6+ 修复，可移除 override，但不得改用浮动版本。
- 历史未知 lineage 的磁盘 token 仍不会自动恢复。mtime 更新也只报告
  `untrusted-generation`；需重新授权或使用新版 manager 生成、digest 匹配且 region
  校验通过的 pending capture。这是有意的 fail-closed 限制。

## 最终 reviewer 需要特别检查

1. 复核 `garminconnect==0.3.6` 的 `_di_token_url`、MFA 私有字段、load/dump 和 token
   rotation 契约；确认 gateway 与 worker 镜像实际加载同一版本。
2. 确认不通过修改模块全局常量实现 CN 路由；每个 Client 的 endpoint 必须从实例
   `domain` 派生，两个区域可并发存在。
3. 确认 `prepare_read_back` 只建立内存 expected-region，不写磁盘；缺失/非法/对域
   marker 不得进入 persist callback，DB wrapper region 必须保持不变。
4. 确认 pending generation 检查发生在旧文件解析前；新登录/删除应解除损坏 capture
   阻塞，DB 未变化或 load 失败时必须继续保留 capture。
5. 确认 verification 返回 `g.client.dumps()` 的已认证内存 generation；dependency
   自动 dump 失败不得使 OAuth 落回候选文件的旧 generation。
6. 复核 delete → queued request → same-key re-login：旧 worker baseline 在退役前仍可用于
   generation 比较；busy worker 不被强杀，也不能接收新 generation 请求。
7. 确认可区分 worker proxy 测试验证的是最终响应身份、session、env 和 workdir，而不只是
   manager 入参；missing account 与 legacy Global 均不得跨账号 fallback。
8. 确认 Authorization header 脱敏不会在 Digest 逗号处停止，quoted JSON/dict 又不会
   无边界吞掉后续结构。
9. 复核 legacy Global 策略：裸 token 默认 Global；已有裸 key 可复用；新 Global 使用
   `global:`；裸 key与 canonical Global 并存时 fail closed。
10. 上线前完成最新 `REVIEW_RESULT.md` 的 E2E gate：真实同邮箱 CN/Global、错误 MFA 后
   重试、强制 refresh endpoint、两台可区分 worker 的数据归属、restart/reap/evict、
   persist 故障/取消、legacy Bearer 和非 Garmin OAuth smoke。
