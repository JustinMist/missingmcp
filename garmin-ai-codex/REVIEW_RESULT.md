# Garmin CN / Global — Final Security / Architecture Review

审查日期：2026-09-13（Asia/Shanghai）。审查对象：工作区相对 HEAD `1261d3e669a2d97f1e7c53510803dd3a321eb6b6` 的完整补丁，包含未跟踪的新增源码、测试、CI 和依赖 override。

## 结论

**APPROVED FOR E2E TEST**

| 等级 | 数量 |
| --- | ---: |
| BLOCKER | 0 |
| HIGH | 0 |
| MEDIUM | 2 |
| LOW | 1 |

此前报告中的两项 HIGH 已得到实质修复：0.3.6 的 CN DI endpoint 按实例 domain 选择；全新 forward 在 restart capture 前从加密 DB blob 恢复 expected-region，错误 sidecar 不再被提升为账户权威。本次没有确认可由远程 MCP 调用者触发的跨租户访问或 CN/Global 冒用。

**这不是“没有问题”的结论。** 完整测试为 **545 passed, 1 warning in 72.68s**，但独立合成探针确认两项非阻断生命周期缺陷：verification 的 token 文件写入失败可使它返回旧代凭据；删除被排队请求观察到后，同 key 重建可继续复用旧 worker。它们需要修复或明确处理后再作合并/发布决定。本批准仅允许进入隔离双账户 E2E，不代表已经通过真实 Garmin 验收。

本报告替代此前同名报告；下文 M1/M2/L1 是本次的新编号，不沿用旧问题编号。

## 审查范围与执行证据

按要求先阅读 `IMPLEMENTATION_SPEC.md`、`GARMIN_ARCHITECTURE.md`、`AUDIT_RESULT.md`、`IMPLEMENTATION_RESULT.md`、`prompts/03_review.md`，再阅读 `CLAUDE.md`、`CONTEXT.md`、git diff 及所有实际变更的源文件和测试。提示中引用的 `.codex-run/garmin-ai/01-audit.md`、`02-implement.md` 不存在，使用用户指定的审计/实现报告。未发现适用的 AGENTS.md，未启动子 agent。

源码范围：adapter registry/base、Garmin blob/login/forward、app、oauth、store、workers、log、authorize 模板、backfill/revoke/usage。测试范围：adapters、Garmin blob/login/dependency contract、workers、backfill、OAuth、proxy、store、log、beers、telemetry。另核查未修改的 proxy 认证转发链、Dockerfile、pyproject.toml、uv.lock、CI、override 文件、变更文档及启动脚本。

本次读取了本机精确 `garminconnect==0.3.6` 的登录、MFA、DI refresh、load/dump 源码；未导入或修改 `garmin_mcp` 内部模块。网关 `.venv` 原装 0.3.2，因此测试通过本机已有的 0.3.6 overlay 显式覆盖，没有改动仓库环境或依赖文件。测试中的实际版本断言通过。

完整测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/private/tmp/missingmcp-garmin036-site:/Users/charles/missingmcp/src \
.venv/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp=/private/tmp/missingmcp-review4-pytest-network
```

首次受限运行因 fake HTTP server 不能绑定本地端口，结果为 431 passed、113 errors、1 failed；获得自动审批允许本地监听后原样重跑，545 项全部通过，没有跳过或削弱测试。唯一 warning 为 Starlette/AnyIO BlockingPortal 别名弃用。`git diff --check` 通过。

临时证据：

- `/private/tmp/missingmcp-review4-pytest-network.log`：完整测试结果。
- `/private/tmp/missingmcp-review4-probes.py`、`.log`：下文两项确定性探针。只使用合成 token、临时 SQLite、模拟 process/health 和网络响应。
- `/private/tmp/missingmcp-review4-source-manifest.json`：报告写入前的 36 个既有变更/新增实现、测试、配置和文档文件哈希，用于核对交付未改变它们。
- `/private/tmp/missingmcp-review4-previous-report.md`：本次替换前的报告副本。

本阶段仓库内只更新本报告；未修改源码、测试、依赖、配置，未执行真实 Garmin 登录、生产 backfill、部署、提交或推送。中途临时探针写入曾因自动审批用量限制被拒绝；用户指示“继续”后重试成功，无遗留审批阻塞。

## BLOCKER

无已确认项。

## HIGH

无已确认项。没有把磁盘故障、同一账户的凭据代次问题或测试范围不足，升级表述成已证实的跨租户泄漏。

## MEDIUM

### M1 — verification 刷新成功但自动 dump 失败时，仍把旧文件当作已验证的新代凭据返回

**位置：** `src/missingmcp/adapters/garmin/login.py:157`、`:161-163`。消费者为 `src/missingmcp/adapters/garmin/__init__.py:229-230` 的 Verification 包装与 `oauth.py` 的 verify-then-persist 路径。依赖证据：本机 0.3.6 `garminconnect/client.py:1139-1149`。

`verify_tokens` 先将候选 G1 写到临时文件，然后调用 `g.login(d)`，最后读取同一文件作为 `VerifiedTokens.tokens_json`。但精确锁定的依赖在 `_refresh_session()` 中对自动 `dump()` 使用 `contextlib.suppress(Exception)`。如果 refresh 已得到 G2，而 dump 在打开/截断文件前遇到暂时性 I/O 错误，原来的合法 G1 文件仍在。随后 profile 使用内存 G2 成功，helper 的 chmod/read 也成功，于是静默返回 G1。

**独立复现：** 保留真实 Garmin.login、Client.load、JWT 过期判断、DI refresh 和 helper；仅替换 HTTP/profile 网络边界，并在 `Client.dump` 打开文件前注入 OSError（实际路径只尝试一次 dump）。CN、Global 都得到：

```text
refresh endpoint: 对应账户的正确 Garmin 区域
profile 使用的 refresh generation: refresh-new
dump_attempted: true
verify_tokens 返回的 refresh generation: refresh-old
```

探针没有伪造 `verify_tokens` 返回值。消费者会把这个旧 blob 当验证结果持久化。真实服务若已废弃旧 refresh token，重连或下一次刷新会失败，刚完成的授权不能可靠恢复会话。region 在此过程中没有改变；这是条件性凭据丢失和恢复故障，未证明身份越权，因此定为 MEDIUM。

**最小修复：** 成功验证后，从实际已认证的 `g.client` 序列化当前 token 状态（例如已审查版本的 `dumps()`），经现有 codec 校验后返回；或者显式执行一次会向调用者传播失败的最终 dump 并验证其结果。不能仅以“磁盘上存在合法 JSON”证明它就是验证所使用的 generation。

**测试缺口：** `tests/test_garmin_dependency_contract.py:325` 验证的是正常落盘 refresh；`tests/test_garmin_login.py` 的 rotation 测试也主动写好了 G2。补测 refresh 成功、自动 dump 在覆盖前失败，要求返回内存 G2或显式验证失败，不能返回 G1；再在 OAuth 层断言持久化值或无账户/授权码副作用。两域均须覆盖。

### M2 — 观察到账户删除后清掉 baseline，却保留活 worker，导致同 key 重建后复用旧代会话

**位置：** `src/missingmcp/workers.py:704-710`，配合 `:177-196`；周期回读的 baseline 缺失分支位于 `:525-529`。

`_authoritative_blob()` 发现账户已删除时删除 `_persisted[key]`，但没有停止或标记 `_workers[key]` 中可能仍存活的 worker。之后同 key 重新完成登录，`credentials_changed = last is not None and blob != last` 因 `last is None` 为 false，健康检查便直接返回旧 worker。周期回读也因没有 baseline 而不再持久化它的刷新。

**可达时序：** 请求已经通过 Bearer 校验并读到旧 blob，等待账户锁；运维删除/purge 账户；排队请求取得锁后看到删除、返回拒绝，并清掉 baseline；用户随后以相同区域/邮箱重新授权，写入已验证 G9。单进程、同一 manager 即可发生，无需多节点或重启后复用旧内存。

**独立复现：** 真实 WorkerManager、GarminForward、加密 SQLite 与 CAS，fake process/health，使用 asyncio 锁控制排队请求，并依次调用真实 `store.delete_account` / `upsert_account`。结果：

```text
old_worker_reused: true
spawn_count: 1
baseline_missing: true
disk_generation: refresh-1
db_generation: refresh-9
```

G9 已在 DB，但后续请求仍返回 G1 worker；周期 capture 无法补回基线。若旧会话已失效，用户重新登录仍不能恢复，直到旧 worker 被回收或网关重启。没有证明另一账户能取得数据；被撤销 Bearer 仍受每次请求的数据库认证约束。因此定为 MEDIUM，而不是已证实的 tenant isolation HIGH。

**最小修复：** 活 worker 缺失可信 baseline 必须视为需要退役/替换，不能视为凭据未变；或保留绑定到该 worker 的旧代信息直至确认退出，并显式标记删除。继续遵守 inflight、stop/wait、端口和 workdir ownership 规则，不得为了清理字典而直接释放仍可写文件的进程。

**测试缺口：** 现有 damaged-pending × deleted 测试的 worker 已经退出，不覆盖此情形。补充活 worker + 排队请求观察删除 + 同 key 重新登录，断言旧进程退役、新代材料化、后续 refresh CAS 有效，旧 Bearer 撤销不被绕过。不要把测试缩成单独断言删除后抛异常。

## LOW

### L1 — region proxy 测试只验证 manager 入参，无法证明最终响应来自正确租户

**位置：** `tests/test_proxy.py:51-56`、`:68-78`；相关独立 worker 测试 `tests/test_workers.py` 的 `test_same_email_regions_have_independent_workers_and_files`。

`RecordingManager.ensure_worker` 对 CN 和 Global 都返回同一 `fake_worker.port`。该测试能证明 header/query/body 没有改写传给 manager 的 key/blob，但无法发现 manager/端口/返回响应归属错误。另一测试检查两个端口和文件，却 mock 掉 health，并未通过 HTTP 判断响应的账户身份。dependency contract 中的“worker refresh”也直接调用 Garmin 类，不等同于启动 pinned `garmin-mcp`。

这是覆盖范围缺口，不是生产代码越权证据；现有断言本身有价值。两项新 MEDIUM 则说明成功路径全绿确实不能替代故障边界测试。

**最小补充：** 使用真实 manager/store 与两台返回不同 synthetic account 标识的 fake HTTP worker，持两枚同邮箱 CN/Global Bearer 交错请求，检查响应身份、实际 env、独立 workdir/rotation。交换合法 session-id、region/header/query/body、email/account_key 后，不能拿到另一台 worker 的数据；加入 legacy Bearer 和指定账户缺失但另一域存在的情形。真实 pinned worker 的同样检查列入以下 E2E gate。

## 此前问题的独立复核状态

| 前次报告项 | 本次复核 |
| --- | --- |
| 旧 H1：0.3.2 把 CN DI 请求发往 Global | 已修复。0.3.6 的 `_di_token_url` 从实例 domain 派生；真实 ticket exchange、verification refresh、MFA refresh、materialized-token refresh 的两域 URL 回归通过。没有修改模块全局 endpoint。 |
| 旧 H2：restart capture 丢失 expected-region | 已修复。`prepare_read_back(last, workdir)` 只设置内存基线，在恢复读取前执行。fresh forward × 两域 × marker missing/opposite/invalid 测试断言无 persist、无 spawn、DB/disk/pending 保留；修复 sidecar 后按原域恢复。 |
| 旧 M1：损坏 pending 阻止新登录 | 原场景已修复。request 与 periodic 路径在解析旧 token 前检查当前代次；新登录/删除使旧 pending 失效。本文 M2 是不同的活 worker baseline 生命周期。 |
| 旧 L1：Digest 逗号后的凭据未脱敏 | 已修复。新增 unquoted header 整行处理；含逗号 nonce/response 的 stdout、file、sink、traceback 回归通过。 |
| 旧 L2：wrapper 测试叠加错误掩盖版本校验 | 已修复。当前 version/region/extra-field 用合法 token wrapper 作对照、单独修改目标属性；源码仍使用精确整数类型和版本检查。 |

## 用户指定的 15 项检查

| 检查 | 本次结论与边界 |
| --- | --- |
| 1. tenant isolation | Bearer → adapter/key → DB → manager 的身份选择来自服务端；完整 key 的 SHA-256 目录消除了已知字符清洗碰撞。未确认公开跨租户读取。L1 的完整响应归属仍需 E2E。 |
| 2. CN / Global 串账号 | regional key、独立目录、每 worker env 和 per-instance DI endpoint 一致；正常路径与恢复路径均保持域。 |
| 3. 同 email collision | `cn:` 与 `global:` 不冲突。已有合法 Global 裸 key 复用；CN 不 fallback；合法 legacy 与 canonical Global 并存时拒绝自动合并。 |
| 4. MFA region | `(pending,email,region)` 保存在服务端；continuation 忽略 POST region。错误页与 CSRF restart 从已保存参数恢复区域。 |
| 5. verify_tokens region | 先 unpack，再显式传 `is_cn`；真实 refresh URL 已验证。返回 generation 在 I/O 故障下仍有 M1。 |
| 6. refresh 后 region | token 与 sidecar 分开材料化，read-back 从可信 expected-region 验证后重包。fresh restart 错域/缺 marker 不会降级 Global。 |
| 7. legacy migration | 合法 raw DI token 默认为 Global；旧物理 key 和 Bearer 保留。backfill 不信任单纯较新 mtime，不自动读取碰撞旧目录或未知 lineage。未核验生产存量数据。 |
| 8. worker env | 明确覆盖 `GARMIN_IS_CN` 与 `GARMINTOKENS`，去掉 Garmin password/email、两种 FILE 和 BASE64 fallback；没有并发修改 os.environ。其他父环境依旧继承，属于既有 worker 信任边界。 |
| 9. 客户端伪造 region | 首次凭据登录只接受闭枚举，缺字段兼容 Global；认证后的 MFA/MCP 不使用客户端 region 选择账户。 |
| 10. password/token 泄漏 | MFA password/PreparedRequest body/过期 tokenstore 引用已清理；敏感异常链在 adapter 边界抑制；结构化/常见文本秘密脱敏测试通过。DB blob 加密，Bearer/code/client secret hash 存储。没有证明新增持久化/日志泄漏；正则脱敏不等于任意第三方文本均有保证。 |
| 11. 临时文件权限 | gateway 临时目录私有，token/region 材料化 0600，worker 目录 0700；已有宽权限文件会替换修正。0.3.6 dump 强制私有权限，但仍是 O_TRUNC 写入，不应称原子刷新。 |
| 12. race condition | CAS、账户锁、锁内 reload、退出确认、取消 orphan ownership 有有效回归。仍有 M2 的删除/重建时序问题；不支持多网关进程协调。 |
| 13. materialize/read-back | 正常 capture、停止后 capture、持久化故障 pending、restart 校验已核查；M1/M2 是剩余边界。周期回写不是严格 persist-before-use，硬崩溃可丢最后一次未捕获刷新。 |
| 14. MissingMCP OAuth 回归 | login/MFA/upstream callback 均 verify-then-persist，传统字符串 verify result 兼容，完整 WHOOP/remote/local/OAuth 回归通过；M1/M2 影响故障后的会话恢复。 |
| 15. 测试安全边界 | 前次假阳性 fixture 已修复；新故障探针发现 M1/M2，L1 表明双租户返回数据的集成验证仍不足。545 passed 不能解释为真实双域 E2E 已通过。 |

加密 blob 的 region 是权限路由权威；工作目录私有权限与进程拆分不构成对恶意同 UID worker 的 OS 沙箱。这是现有单节点/受信 worker 模型的范围，不在本次误报为新引入漏洞。

## 构建与依赖策略判断

接受本次“固定 worker commit + 独立 frozen 环境 + exact/hash override”的受控策略进入 E2E：worker ref 保持 `e8554bcd761a4494dc12a98461224bb3dcf1fbc5`，gateway lock 为 0.3.6，worker frozen sync 后通过 require-hashes/no-deps override 安装同一 0.3.6。Docker 对 worker 版本作断言，CI 对两套运行时均作断言。

该环境有意偏离 worker 自身仍记录 0.3.2 的历史 lock；不能对它再次执行无 override 的 sync 后继续使用。CI suite 的合同测试和文本检查不等于完整 Docker 镜像已运行成功。本次没有重建 Docker 或执行真实 worker 网络 E2E，最终镜像的解释器、CLI、两套实际版本仍是下面验收的首项。

## 合并前必须完成的双账户 E2E 步骤

1. **隔离构建与账户准备。** 构建当前 Dockerfile，确认 `/app/.venv/bin/python` 与 `/opt/garmin-mcp/.venv/bin/python` 实际 `garminconnect` 都为 0.3.6，默认 `missingmcp`/`garmin-mcp` 来自预期环境。部署单网关实例、独立 DATA_DIR。准备一个 CN（含 MFA）和一个 Global 测试账户，优先同邮箱在两域分别注册且 profile/日指标可明确区分。没有同邮箱条件时记录该项未完成，不能用两条合成 DB 行代替真实域验收。
2. **独立授权。** 两个独立浏览器/MCP 会话分别走 DCR → PKCE S256 authorize，选择 CN/Global。CN 先错误 MFA，再正确重试，同时推进另一区域登录；核查 region 不交叉。缺 region 的兼容请求进入 Global，非法值在 Garmin 登录前拒绝。失败不得新增账户 blob/授权码；成功 code 只可兑换一次。
3. **验证实际数据归属。** 用两枚 Bearer 分别 initialize、notifications/initialized、tools/list，再调用已列出的只读 profile/简单日指标工具。逐次核对结果属于对应测试账户，不能只检查 200。确认不同 worker、端口、GARMINTOKENS 和 true/false。交换 session-id 与伪造 region/header/query/body、email/account_key；允许协议拒绝，但不得返回另一账户数据。删除目标账户时不得 fallback 到同邮箱另一域。
4. **真实刷新。** 对两域等待到期或采用隔离环境的受控过期方法触发真实 refresh，核查目的主机：CN `diauth.garmin.cn`，Global `diauth.garmin.com`；只记录主机/状态和代次摘要，不记录 token。检查 verify/worker 刷新后仍可调用、DB region 不变、捕获代次正确。不能以未过期 access token 的成功请求代替 refresh 验收。
5. **退出与恢复。** 每域执行 idle reap、cap eviction、worker 退出和正常网关 restart；重连后核查最终 token 捕获、0600/0700 和原域。故障注入 persist 失败形成 pending，再重启；marker 缺失/错域/非法必须拒绝回写与 spawn，修复后只能恢复原域。无 lineage 的旧磁盘 token 不得自动覆盖 DB。
6. **故障边界。** 在隔离部署复核 M1 的 refresh 成功但 dump 失败，及 M2 的活 worker/排队请求/删除/同 key 重新授权；记录实际结果并在后续修复后验证。另测 damaged pending 后的新登录、账户删除、store load 失败，以及启动清理期间重复取消；旧进程未退出不得重新材料化文件。
7. **legacy 与撤销。** 导入受控的合法 legacy Global raw blob/裸 key/旧 Bearer。验证启动、refresh、Global 重登后旧 Bearer 仍指向旧 key，同邮箱 CN 不受影响；指定域 revoke/purge 不影响另一域。legacy+canonical Global 双行冲突必须拒绝自动合并，旧 worker 不得复活被删账户。
8. **通用 OAuth 与日志。** 完成 WHOOP 或另一个非 Garmin adapter 的真实授权/callback/只读 MCP smoke，核查 discovery、DCR、PKCE、传统 verify 返回兼容。检查测试期 stdout、文件 tee、遥测、私有临时目录清理；秘密不进入测试记录、命令历史或提交。

E2E 结果应逐项记录通过/失败/未执行，并保留两域实际响应归属与刷新目的域名的无秘密证据。本文的 MEDIUM/LOW 不因进入 E2E 自动视为已解决。
