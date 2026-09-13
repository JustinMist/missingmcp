# MissingMCP Garmin CN / Global 只读审计报告

审计日期：2026-09-09。仓库：`/Users/charles/missingmcp`。基线提交：`1261d3e669a2d97f1e7c53510803dd3a321eb6b6`。

本阶段只审计，不实现。仓库内唯一新增交付物是本文件；未修改应用、测试、配置、依赖声明或其他已有文件，未执行启动脚本的 implement/review 阶段，未连接生产数据库或真实 Garmin 账户，未部署、提交或推送。

## 1. 结论

**现有架构能承载 account-level `region=cn/global`，不需要新用户系统、数据库 schema migration、新 OAuth 服务或新加密层。但不能只修改 Garmin 构造函数和添加选择框就上线。**

| 审计重点 | 当前实现与结论 |
| --- | --- |
| Garmin 登录 | 浏览器表单 → `GarminAdapter.start_login(form)` → `garminconnect.Garmin(..., return_on_mfa=True)`。未传 `is_cn`，网关没有账户级 region。 |
| MFA | pending 是活的 Garmin 客户端及上游 continuation state，外层携带邮箱；进程内随机 login_id、300 秒 TTL、一次性 pop、错误时重新入库到内存。未携带独立 region；在本次解析的依赖版本中，已复现 pending 保留密码引用。 |
| token 存储 | `LoginOk.blob` 当前是原始 `garmin_tokens.json` 字符串，经 `adapter.verify` 后，使用 AES-256-GCM 加密到 SQLite `accounts.blob_enc`。worker 磁盘文件仍是明文。 |
| account_key | `email.strip().lower()`；数据库以 `(adapter, account_key)` 为主键。Garmin CN/Global 同邮箱目前无法独立表达。 |
| spawn/env | 每个 worker adapter 一个 manager；账户锁、按账户惰性 spawn。`Popen` 继承整个网关环境，再覆盖 adapter 返回的 env。当前没有覆盖 `GARMIN_IS_CN`。 |
| refresh read-back | 周期 tick、reap、evict、shutdown、respawn 回读，比较进程内 baseline 后加密 upsert；不是严格的 refresh persist-before-use。 |
| 最小 region 改动 | Garmin blob codec、登录/verify/MFA、表单、worker materialize/env/read_back；复用现有接口。安全上线还要修正已证实的目录碰撞、旧代凭据回写和回填脚本兼容。 |
| legacy Global | 原始 blob 默认 Global；**格式兼容与账户键兼容是两件事**。不能直接把所有旧邮箱键替换成 `global:email`，也不能允许 CN 回落到旧邮箱键。 |
| 串号风险 | Bearer → 数据库账户的逻辑路径正确；但目录名清洗不是一一映射，已用合成数据复现跨账户 token 回写。依赖还可能从网关级 `GARMINTOKENS` 读取别的账户 token，跳过表单登录。新增 region 若键、目录、回写只改一部分，会引入更多错误路由。不能给出“无跨租户风险”的结论。 |

## 2. 范围、依据与限制

按要求先阅读了：

- `garmin-ai-codex/IMPLEMENTATION_SPEC.md`
- `garmin-ai-codex/GARMIN_ARCHITECTURE.md`
- `garmin-ai-codex/prompts/01_audit.md`

随后阅读 `CLAUDE.md`、`CONTEXT.md`、`docs/architecture.md`，盘点全部当前代码、测试、脚本、模板、部署文件和设计资料。重点逐段追踪 `oauth.py`、`store.py`、`workers.py`、`proxy.py`、`app.py`、Garmin adapter/login、授权/MFA 模板及其测试；同时检查 WHOOP、通用策略、日志、遥测、备份、统计、运维脚本对账户键和 blob 的依赖。旧设计文档只作背景，代码是事实依据。

初始工作区已有未跟踪的 `garmin-ai-codex/` 和 `run_garmin_codex.sh`，没有覆盖或删除。未发现适用于此目录的 `AGENTS.md`。没有启动额外 agent。

下文 `路径:行号` 均为审计基线的位置；“已复现”指本地合成输入的代码行为，**不代表观测到了生产攻击或真实用户数据泄露**。上游公开文档与实际部署镜像分开判断：本地没有生产镜像/锁定的完整运行时，真实 CN/Global 登录、MFA、工具调用和刷新仍需后续 smoke test。

## 3. 当前登录与 MFA 全链路

### 3.1 普通登录

1. `app.py:343-390` 注册 `/garmin/oauth/register`、`/garmin/oauth/authorize`、`/garmin/oauth/token` 和 `/garmin/mcp`，没有独立 CN connector。
2. `oauth.register_client` 生成随机 client_id/client_secret；只存 client secret 的 SHA-256 hash（`oauth.py:47-75`）。
3. GET authorize 校验已注册 client、redirect_uri 精确匹配、非空 S256 challenge，然后发放 CSRF 并渲染 `authorize.html`（`oauth.py:181-235`）。模板只有邮箱和密码，没有 region（`templates/authorize.html:15-20`）。
4. POST 消费一次性 CSRF，再检查 client/redirect；通过 `_bounded` 在线程运行 adapter 登录（`oauth.py:267-274,282-405`）。应用设置 login/MFA 每 IP 5 次/60 秒，其他 OAuth 20 次/60 秒（`app.py:343-368`）。
5. `GarminAdapter.start_login` 从表单取 `garmin_email`、`garmin_password`，调用 `login.start_login(email,password)`（`adapters/garmin/__init__.py:97-109`）。
6. helper 构造 `Garmin(email=email,password=password,return_on_mfa=True)`，调用 `g.login()`。认证错误不重试；连接/限流及其他异常默认最多两次，间隔 6 秒（`adapters/garmin/login.py:35-63`）。**没有传 `is_cn`；也不读取 `GARMIN_IS_CN` 来决定网页登录区域。**
7. 非 MFA 成功时通过 `client.dump(temp_dir)` 读取 `garmin_tokens.json`，返回 `LoginResult.tokens_json`。adapter 返回 `LoginOk(normalized_email,raw_json)`。
8. `adapter.verify(blob)` 再执行一次独立 token login：临时文件 → `Garmin()` → `g.login(temp_dir)` → `get_full_name()`，空姓名仍算成功（`login.py:72-88`）。这一步同样没有 `is_cn`。
9. verify 成功才 `_finish`：加密 upsert 账户、生成一次性授权码、带回 OAuth state 跳转 redirect_uri（`oauth.py:238-264,387-405`）。
10. token endpoint 校验 client secret、授权码绑定的 client/redirect、PKCE，再发 Bearer。授权码和 Bearer 在 DB 中均存 hash，Bearer 绑定授权码里的 `(adapter,account_key)`（`oauth.py:408-438`）。

`login_timeout=30` 是每个阻塞步骤的上限，不是整个登录链的总上限；`wait_for(to_thread(...))` 超时并不会终止后台线程（`config.py:82-86`、`oauth.py:267-274`）。

### 3.2 MFA

- helper 返回 `LoginResult(status="needs_mfa", pending=(g,result2))`（`login.py:48-51`）。
- adapter 返回 `SecondFactorNeeded(state=(pending,email))`（`__init__.py:107-108`）。外层状态没有明文密码字段，但内层持有完整 Garmin 对象，不能据此宣称“状态里没有密码”。
- `AuthState._mfa[login_id] = (pending,oauth_params,adapter_name,monotonic_time)`，随机 ID、一次性 pop，校验 adapter ownership；TTL 300 秒（`oauth.py:99-127`）。CSRF 应用实例 TTL 是 1800 秒（`app.py:70-72`）。
- MFA 页面只回传 `csrf`、`login_id`、`mfa_code`，OAuth 参数和邮箱从服务端 pending 恢复；不依赖第二次 POST 提交的邮箱/redirect（`templates/mfa.html:10-14`）。
- continuation 在原 Garmin 对象上调用 `resume_login(state,code)`，dump 新 tokens，再验证、加密持久化（`login.py:66-69`、`oauth.py:297-350`）。
- 任何 resume 异常当前都包装成 `SecondFactorError`，重新保存同一 adapter state，发新 login_id/CSRF，返回 400 MFA 页面；重试也重置 300 秒 TTL。verify 失败、超时则回登录页。
- `_gc()` 仅在 put/pop 触发，不是后台定时清除；过期但无人再访问时对象仍可能留在内存。服务重启会丢失全部 MFA 状态。

region 改造应保存 `(pending,email,region)`，continuation 仅使用服务端保存的 region，忽略 MFA POST 里的区域覆盖。错误重试不得丢失 region。无需改变通用 `AuthState` 的不透明 pending 合约，也无需为了路由修改 MFA 模板。

### 3.3 密码与日志的真实边界

未发现将 Garmin 密码写入 `accounts`、授权码、Bearer 记录或 `LoginOk.blob` 的网关代码；密码表单值也不会被回显到 HTML。但 `del password` 只删除 adapter 局部引用，`request.form()`、后台线程和 MFA Garmin 对象仍可能持有它，不能等同于内存擦除。

本次安装到临时环境的 `garminconnect 0.3.13`：`Garmin.__init__` 保存 `self.password`；`login()` 在 `return_on_mfa=True` 分支提前返回，跳过后面的 `self.password=None`。使用真实 Garmin/helper、只 mock 上游 `Client.login` 为 MFA challenge，检查返回的 `pending[0].password`，确认仍等于合成密码。此发现有运行时证据，不只是对对象状态的推测；它不等同于密码已写入 DB。

网关登录异常使用 `raise ... from e`，`oauth.py:319,371,398` 等通过 `log_exc` 输出完整异常链；`log.py:114-123` 没有脱敏。`workers.py:32-50` 原样记录子进程输出，stdlib bridge 也原样输出 message/traceback（`log.py:40-53`）。因此“代码没有主动 log(password)”成立，**“无论上游抛出什么都绝不泄露密码/token”不成立**。用带合成秘密的 `GarminLoginError` 经真实 adapter 包装和 `log_exc`，已确认 cause 中的秘密会进入日志。启用遥测后，很多日志还会被原样 tee 到 PostHog（`telemetry.py:189-225`）。后续需要将此诊断转成回归测试并覆盖 worker 日志；不要用真实凭据验证泄露。

## 4. 加密、账户身份与认证隔离

### 4.1 持久化精确过程

`LoginOk.blob` → `adapter.verify` → `oauth._finish` → `store.upsert_account` → `store.encrypt` → SQLite `accounts.blob_enc`。

`store.py:12-31,199-227` 实际算法：

- `key = SHA256(GATEWAY_SECRET.encode()).digest()`，32 字节。
- 每次加密生成随机 12 字节 nonce，`AESGCM(key).encrypt(nonce,plaintext,None)`。
- 存储字符串为 `nonce.hex() + ":" + ciphertext_and_tag.hex()`。
- AAD 为 `None`，没有将 `(adapter,account_key)` 绑定进认证附加数据。持有 DB 写权限的人可以交换密文行；这不是公开 MCP 请求可直接完成的操作，但加密不替代账户索引和主机权限。
- `get_account_tokens` 按完整 `(adapter,account_key)` 查询后解密，无 region 解析。
- Bearer、client secret、授权码使用 SHA-256 hash；OAuth Bearer 默认 90 天，配置 TTL=0 才不失效（`config.py:75`），不能只根据 store 的旧注释称“永不过期”。

SQLite 主库/WAL/SHM 在初始化时尽力 chmod 0600（`store.py:176-194`）。备份走 SQLite snapshot，仍保存加密 blob，不含 worker token 目录（`backup.py`）。整个数据库不是全库加密：账户键、时间戳和统计等仍是明文。

**worker 文件位于持久 DATA_DIR，token 是明文。** 新建文件时 `os.open(...,0600)`，user/token 目录 chmod 0700（`workers.py:362-369`）。现有文件若已经是 0644，`os.open` 的 mode 参数不会修正权限；上游重写后的 mode 也未主动校验。验证登录临时 token 文件用普通 `open("w")`，权限受 umask 控制，不过外层 TemporaryDirectory 私有。严格的“所有 token 文件始终 0600”尚未由当前实现保证。

### 4.2 当前账户键

`base.normalize_account_key(email)` 只做 strip/lower（`adapters/base.py:33-37`）；Garmin 登录和 MFA 都调用它。对上游实际登录传入的邮箱没有先做这一步，标准化用于最终索引。`oauth._finish` 不再规范化或验证 identity，直接使用 adapter 给出的 key。

以下位置都依赖该键：

- `accounts` 主键 `(adapter,account_key)`；
- `access_tokens`、`oauth_codes`、`tool_usage`；
- `WorkerManager._workers/_locks/_persisted`；
- token 目录、worker snapshot、日志、遥测 distinct_id、运维过滤/撤销命令。

数据库已提供 adapter namespace，因此新格式宜为 **`cn:{normalized_email}` / `global:{normalized_email}`**，不要在 key 中重复 `garmin:`；运维 CLI 的完整账户参数才是 `garmin:cn:email`。

### 4.3 Bearer 到 worker 的边界

`proxy.authenticate` hash Bearer、查询 `(adapter,key)`，拒绝非当前 adapter 的 Bearer；`handle_mcp` 用这个 key 解密账户，再调用 `manager.ensure_worker(key,blob)`（`proxy.py:90-103,135-154,188-204`）。请求 body/header/query 不参与 key 的选择。只向 worker 转发 Accept、经过语法校验的 Mcp-Session-Id、Content-Type；Bearer 不转发，region header 不转发（`proxy.py:206-217`）。

MCP body 原样送给所选 worker，故未来也不能在 gateway 中用 MCP params 决定 region。worker 本身是否提供可改账户/区域的工具仍属于上游黑盒验收范围。

通用 OAuth 有一个现有边界差异：`get_client` 不返回 adapter，authorize 不校验 DCR client 的 adapter；token endpoint 不接收当前 adapter 参数，token 的 adapter 来自授权码。这允许跨路径使用 DCR/token endpoint，但没有直接把 Garmin Bearer 变成另一 adapter 的账户权限；MCP 的 adapter 校验仍生效。不要把它误报成已证明的 CN/Global 串号。

## 5. worker spawn、环境和 refresh read-back

### 5.1 调用次序

```text
app.build_app
  └─ 每个 worker adapter 创建 WorkerManager(forward, persist=闭包)
       └─ proxy.handle_mcp → ensure_worker(key, decrypted_blob)
            ├─ 账户锁；已有 worker 健康或 busy 则复用
            ├─ 需要替换：停止旧进程、LRU、尝试恢复其刷新 token
            ├─ _materialize(key,blob)
            │    ├─ 创建/保护 user_dir、tokens dir
            │    ├─ forward.materialize(blob,workdir)
            │    └─ _persisted[key] = blob
            ├─ 分配并预留端口
            ├─ _default_spawn(key,port,workdir)
            │    ├─ env = dict(os.environ)
            │    ├─ env.update(forward.env(port,workdir))
            │    └─ subprocess.Popen(forward.command(), env=env, ...)
            └─ /healthz + login gate 后注册 WorkerHandle
```

依据：`app.py:52-69`、`workers.py:130-240,358-403,436-453`。

当前 `GarminWorkerForward.env` 返回四项（`adapters/garmin/__init__.py:44-50`）：

```text
GARMIN_MCP_TRANSPORT=streamable-http
GARMIN_MCP_HOST=127.0.0.1
GARMIN_MCP_PORT=<账户 worker 的端口>
GARMINTOKENS=<账户 workdir>
```

`GARMIN_MCP_CMD` 来自配置 `.split()`，默认 `garmin-mcp`，Popen 传 list，不走 shell。Popen 没有设置 `cwd=workdir`；这里的 workdir 是凭据目录，通过 `GARMINTOKENS` 注入，不是进程工作目录。

**环境继承问题：** 当前父进程若设置 `GARMIN_IS_CN=true`，所有 Garmin worker 都会继承；网页登录/verify 却仍不传 CN。后续必须对每个 worker 明确覆盖 true 或 false；不能只对 CN 写 true、Global 留空，也不能通过修改共享 `os.environ` 来切换区域。

Popen 还继承网关密钥、其他服务环境变量，所有 worker 默认使用相同 OS 用户；0700/0600 是文件权限保护，并非恶意 worker 之间的强沙箱。当前只有 Garmin 是 worker adapter；WHOOP 是 local。`app.py:57-59` 已明确第二个 worker adapter 会共享端口池、目录和 snapshot，CN/Global 不应建成两个 manager 来绕过账户路由。

`Dockerfile` 固定 worker ref 为 `e8554bcd761a4494dc12a98461224bb3dcf1fbc5`，同时限制 `mcp<2`；网关 `garminconnect>=0.3.2` 没有锁文件，依赖范围不是实际部署版本证明。`CLAUDE.md` 所称 `main` 默认已经落后于 Dockerfile 的固定 SHA。

上游公开 README 明确支持 `GARMIN_IS_CN=true`，无需为地区另 fork worker。[上游文档](https://github.com/Taxuspt/garmin_mcp#garmin-connect-china-garmincn)。但本次读取固定 SHA 的 README 未成功（web fetch cache miss），也没有运行该 pinned worker，所以“公开契约支持”与“本部署 pin 已经完成 CN 黑盒测试”不能混为一谈；后者仍为验收项。没有导入、执行或修改 worker 内部模块。

### 5.2 read-back 的所有触发点

| 触发 | 代码位置 | 行为 |
| --- | --- | --- |
| 周期 | `app.py:400-426`、`workers.py:242-254` | 循环约每 60 秒，先 reap 再 persist_rotated；跳过正被持有的账户锁。备份/报表也在同循环，间隔并非严格 SLA。 |
| reap | `workers.py:256-274` | dead 或空闲过期时停止并 pop，锁未占用才回写。 |
| evict | `workers.py:337-355` | LRU 淘汰前后捕获最后刷新，busy worker 不杀。 |
| shutdown | `workers.py:324-332` | 逐个 terminate 后回写。 |
| respawn | `workers.py:150-161` | 先回写，若返回 rotated 内容，用其覆盖调用者传来的 stale blob，再 materialize。 |

adapter 的 `read_back(workdir)` 读文件、`json.loads` 检查能否解析，失败返回 None；成功返回原始字符串（`__init__.py:58-69`）。**它不验证 token 字段结构，也不校验所属邮箱/区域；`null`、数组、空对象也能通过语法检查。**

manager `_read_back_and_persist` 仅比较进程内 `_persisted[key]`，变化后调用 `persist(key,content)`，该闭包无条件 `store.upsert_account(...)` 再加密（`workers.py:372-403`、`app.py:60-65`）。无 baseline 的新进程信任 DB，避免把旧磁盘文件当新 token 自动恢复。

### 5.3 回写的现有局限

1. **同进程 re-login 丢失：** `_finish` 更新 DB 时不更新 manager baseline，不停止或更新现有 worker；健康 worker 也不比较新 blob。旧 worker 后续写出的 rotation 会覆盖新登录 blob。已用临时数据库和合成 token 复现。
2. **并非严格 persist-before-use：** worker 先刷新并可能已使用新 token，网关之后才轮询；崩溃窗口可能丢刷新。不能照搬 WHOOP 的 gateway-owned 原子刷新保证。
3. terminate 后并没有等待旧进程退出才读 token 或重写同目录；端口 cooling 只解决端口问题，不能保证旧进程不再写 token。
4. `verify_tokens` 若 token login 发生刷新，它只返回姓名，不把刷新后的 tokens 返回给 `_finish`；临时目录销毁后 `_finish` 仍存验证前的 blob。这是条件性 token 生命周期风险，需要用可模拟旋转的验证客户端测试。

仓库注释把刷新写入归于 garth；本次 `garminconnect 0.3.13` 已使用自身 `client.py`，raw dump 是 `di_token`、`di_refresh_token`、`di_client_id`，且自身采用原子写入/0600。其 token login 会主动刷新临近过期凭据。应以实际部署版本为准，不能把旧 garth 字段当唯一合法 token schema，也不能用新依赖的原子写入保证反推旧 worker。网关自身 materialize 仍是非原子 O_TRUNC。

这些无需重建 manager，但要用明确的登录代次/expected-blob 比较和旧进程停止顺序解决。仅加账户锁不足以覆盖独立的 OAuth `_finish` 和运维脚本写入。

## 6. 跨账户 / CN-Global 风险与优先级

### F1 — 高优先级：token 目录碰撞，已复现跨账户回写

`workers.py:14,358-360`：`_SAFE = re.compile(r"[^A-Za-z0-9_.@-]")`，所有其他字符替换为 `_`。该映射不唯一：

| 不同逻辑账户键 | 相同目录片段 |
| --- | --- |
| `a+b@example.com` 与 `a_b@example.com` | `a_b@example.com` |
| 新 `cn:person@example.com` 与 legacy `cn_person@example.com` | `cn_person@example.com` |
| 新 `global:person@example.com` 与 legacy `global_person@example.com` | `global_person@example.com` |

复现顺序：A materialize 合成 token A；B materialize 合成 token B；两个 key 的 workdir 相同；A 的 read-back 返回 B 的文件，manager 用 A 的 key 加密保存 B 的内容。每账户锁无法保护同一物理目录，因为 A/B 使用不同锁。

真实利用要求对应的不同上游身份能成功登录且命中碰撞，不是仅提交任意邮箱就获得 token。**但代码层面已经破坏凭据隔离，不应作为 V1 已满足的前提。**

建议小范围替换 workdir 命名，使用完整 key 的安全、确定性且抗碰撞的编码（例如 SHA-256，放在明确的新目录层级），覆盖 legacy 与新 key；不要只替换冒号或仅给 CN 加前缀。新进程从 DB materialize，不自动信任旧碰撞目录。关联的 backfill 路径必须同步适配，旧目录回收另行受控处理。

### F2 — 高优先级：仅在 blob 中加 region，仍用邮箱 key，会覆盖另一域账户

这是拟议补丁的必防回归：CN/Global 相同邮箱如果仍生成同一 key，`ON CONFLICT` 会覆盖对方 blob，已有 Bearer 仍指向那个 key，worker 也可能复用另一域会话。当前网关尚未提供 CN 登录，因此不能报告为已观测到的双域生产覆盖。必须同时完成身份、目录、blob、env 和回写隔离。

### F3 — 高优先级：re-login 与旧 worker 的代次竞争，已复现

见 5.3。应在已验证的新登录进入 store 时与 manager 协调，防止旧代刷新覆盖新代、旧健康 worker继续服务旧 blob；持久化使用 expected baseline 的条件更新/比较，不能盲目 upsert。没有 DB schema migration 的必要，现有 blob/ciphertext 比较即可设计并测试解决方案。

### F4 — 高优先级：region 标记缺失不能无条件降级 Global

Spec 的 `.garmin_region` 方案符合现有 `env(port,workdir)` / `read_back(workdir)` 接口，但“marker 缺失默认 Global”和“CN refresh 不得改变 region”在 CN marker 被删除时存在冲突。

建议 materialize 从已解密 blob 建立**按 workdir** 的预期 region 基线；env/read_back 检查磁盘 marker 与它一致。已知 CN 的 marker 缺失/非法/被改成 Global，应拒绝 spawn 或返回 None/记录安全错误，不能重包成 Global。仅真正没有 region 的 legacy 上下文允许默认 Global。不能把一个共享 `forward.region` 属性当基线，多账户会互相覆盖。

### F5 — 高优先级：运维 backfill 会剥掉 region 包装

`scripts/backfill_garmin_tokens.py:59-109` 直接比较 DB blob 和 token 文件，`--apply` 直接把 raw JSON upsert。versioned blob 与 raw 文件天然不相等，mtime 判断通过后会写掉 wrapper。CN 随后被 legacy 解析成 Global。

最小安全选择是暂时让该旧脚本明确跳过/拒绝 versioned 账户；若继续支持，则必须使用同一 codec 和 workdir 规则，以 DB 中 region 为权威重包，并在 apply 时重新检查文件、marker、DB 代次。当前检查 mtime 后再读取/写入也有 TOCTOU，不能只相信脚本文档的“live safe”。本次没有运行该脚本对任何真实 DB 执行 apply。

### F6 — 安全保证尚不充分：权限、密码 pending、日志

参见 3.3、4.1。应补充权限修复、pending 对象去密码及 synthetic-secret 日志测试。region codec 的错误应统一转换为安全的 `LoginError`；否则错误 blob 可能直接触发 500 或将原始字段放进 traceback。

### F7 — 高优先级、依赖部署条件：网关级 GARMINTOKENS 可能绕过表单凭据

网关 helper 在 `login.py:49` 调用无参数 `g.login()`。本次解析的 `garminconnect 0.3.13` 在 `Garmin.login` 开头使用 `tokenstore = tokenstore or os.getenv("GARMINTOKENS")`，成功加载缓存就不走表单 username/password 登录。

**触发前提：** 网关进程（不是仅子 worker）配置了可用的 `GARMINTOKENS`，其中是账户 B 的凭据。用户在网页提交邮箱 A，缓存 B 可被装载和验证；adapter 却按提交的 A 生成 key。后续 `verify` 只返回姓名，未核对 token owner 与提交邮箱的一致性，因此会错误关联身份，可能把 B 数据开放给拥有 A 对应 OAuth 流程的请求者。

合成探针保留真实 `Garmin.login` 控制流，只 mock token load、profile 查询和到期判断，并让任何 credential login 直接报错；仍得到 A 的 `LoginOk.account_key` 和 B 的 token，确认路径可达。该探针不证明生产设置了这个变量，也没有向 Garmin 发请求。

最小修正应使用所选依赖的公开接口保证网页登录只走本次凭据；或在网关启动时拒绝继承 account-specific `GARMINTOKENS` 配置，并维持 worker 独立注入。不要在并发请求中临时修改共享 `os.environ`。需测试“网关环境带缓存时不得绕过表单凭据”，以及 token 身份与提交身份不一致时 fail closed。加 region 前缀本身无法修复错误身份绑定。

## 7. 最小实现计划（仅计划，未执行）

### 7.1 Blob 合约

新增 `src/missingmcp/adapters/garmin/blob.py`，集中实现 `pack_blob(tokens_json,region)`、`unpack_blob(blob)`、region 校验，避免登录、worker、脚本分别解析：

```json
{"v":1,"region":"cn","tokens":{"...":"raw Garmin token fields"}}
```

- 仅接受精确 `cn` / `global`；HTTP 字段缺失默认 Global，空值、其他大小写/空白/未知字符串不自动当 Global。
- legacy raw token JSON object → `(global,raw_json)`。不要因为 wrapper 缺字段/未知版本而退回 legacy。
- wrapper 明确要求 `v` 为支持的整数版本（避免 Python `True == 1` 的宽松比较）、region 合法、tokens 是有效原始 token object。拒绝 null/数组/嵌套 wrapper/损坏 JSON。
- wrapper 的 region 缺失应作为损坏拒绝；“记录没有 region 默认 Global”用于可识别的 legacy raw 格式，而不是任意不完整 wrapper。
- 使用确定性序列化以免仅空格或键序变化导致每个 tick 都写库；若保留 raw 格式，比较时应明确哪些变化代表凭据更新。
- 当前测试中 `{"v":1}`、`{"t":1}` 是合成占位，不能由它们反推出真实 Garmin token schema。需基于所选依赖 dump 契约定义结构校验，避免误拒合法版本或把损坏 wrapper 当 legacy。

### 7.2 登录、MFA 和验证

修改 `src/missingmcp/adapters/garmin/login.py`：增加 helper 的显式 `is_cn` 关键字参数，普通登录和 token 验证均传入 Garmin 构造函数；保持阻塞包装、重试分类和 dump 约定；检查并清除保存到 pending 的客户端密码引用，测试 continuation 不依赖密码；阻止 F7 的环境缓存绕过。不要 import/修改 `garmin_mcp` 内部代码。

修改 `src/missingmcp/adapters/garmin/__init__.py`：校验表单 region 后才发起上游登录；构造区域身份；成功包 blob；MFA state 保存 region；verify 先 unpack，再使用对应 `is_cn`；错误以安全信息转换到既有异常类型。

修改 `src/missingmcp/templates/authorize.html`：邮箱密码上方添加 required 区域单选。为最小状态管理和旧用户体验，建议默认选 Global；该部署如选 CN，服务端缺字段默认仍必须 Global。若要求登录失败后保留用户选项，再小范围扩展 `oauth.render_authorize` 的渲染上下文，并追加测试。

`templates/mfa.html` 可以保持现状，region 已在服务端 pending；无需新增可被客户端篡改的区域权威字段。

### 7.3 worker 与 store

- `GarminWorkerForward.materialize` unpack，只写 raw tokens，写 `.garmin_region`；两文件显式保证 0600，包括修正已有宽权限文件。尽可能原子替换，materialize 成功后才 spawn。
- `env` 从该 workdir 的可信 marker/预期 region 得到 `GARMIN_IS_CN=true/false`，每次都覆盖父环境。
- `read_back` 检查 raw tokens、marker 与预期 region，重包后交给现有 persist callback；不要丢 region、不要从 MCP 请求取得 region。
- 小范围修改 `src/missingmcp/workers.py` 的目录映射及凭据代次保护；共享接口 `materialize(blob,workdir)` / `env(port,workdir)` / `read_back(workdir)` 不必变更。
- `src/missingmcp/app.py` 的 persist 闭包与 `src/missingmcp/store.py` 可以增加 expected-blob 条件更新作为 F3 修复；加密算法及表结构不变。登录提交处和 manager 的协调需同时覆盖普通登录/MFA，不能只修 periodic。
- `scripts/backfill_garmin_tokens.py` 必须适配或 fail closed；不能留作兼容盲点。

### 7.4 文件范围

| 文件 | 最小必要工作 |
| --- | --- |
| `src/missingmcp/adapters/garmin/blob.py`（新） | 格式、版本、region 解析与校验的单一实现。 |
| `src/missingmcp/adapters/garmin/login.py` | `is_cn`，pending 密码边界，verify 旋转的明确处理，环境缓存隔离。 |
| `src/missingmcp/adapters/garmin/__init__.py` | 区域身份、MFA、包装、verify、marker/env/read-back。 |
| `src/missingmcp/templates/authorize.html` | 区域选择 UI。 |
| `src/missingmcp/workers.py` | F1 目录隔离、F3 代次/旧进程写入保护。 |
| `src/missingmcp/app.py`、`src/missingmcp/adapters/__init__.py` | 如采用下节推荐的 legacy lookup 注入，增加小型可选回调；协调 worker persistence。 |
| `src/missingmcp/oauth.py`、`src/missingmcp/store.py` | F3 的登录提交/条件回写；有需要才扩展 region 重渲染上下文。不改 schema/加密格式。 |
| `scripts/backfill_garmin_tokens.py` | 新旧 blob、目录与代次兼容，或明确拒绝不支持格式。 |
| `src/missingmcp/adapters/base.py`、`CLAUDE.md`、`CONTEXT.md`、`docs/architecture.md`、`README.md` | 更新“account_key 等于裸邮箱”的说明、worker 明文文件边界、region/legacy 运维使用方法。normalize helper 继续只做邮箱 strip/lower，WHOOP 不变。 |
| `tests/` | 见第 9 节逐项清单。 |

无需修改 WHOOP adapter/local server、remote strategy、数据库结构、OAuth endpoint URL、worker 的 CLI 或工具集；当前没有证据要求升级 worker pin 来实现本地字段传播，固定 pin 的 CN 支持仍需黑盒确认。公共日志事件名与 status/reason 枚举需保持稳定。F6 日志保证的修复可能需要小改 `log.py` 或 Garmin 登录边界，并配相应 synthetic-secret 测试。

## 8. legacy Global 的兼容策略

### 8.1 必须同时保证的两个层次

**读取旧 token：** 旧 Bearer 仍映射到 `("garmin","person@example.com")`，读出旧 raw blob 后识别 Global。worker 从 DB materialize raw tokens 并显式 false；刷新后允许同一旧 key 下把 blob 升级为 v1 Global。无需迁移账户表或重发 Bearer。

**重新登录旧账户：** 若所有新登录一律生成 `global:person@example.com`，原 `person@example.com` 行和它的 Bearer 不会自动跟随。会出现两个 worker/两套刷新基线，统计重复，旧设备可能无法靠新登录恢复。只测试 raw unpack 无法覆盖这个问题。

### 8.2 推荐 V1：保留旧 Global 物理 key，新增账户用区域 key

以既有账户兼容为显式例外，使用 Garmin 专属的、只读的 legacy identity lookup（可由 `app.build_app` 基于当前 conn 注入 `build_adapters`/`GarminAdapter`）：

1. **CN 成功登录永远生成 `cn:email`**，绝不查询/更新裸邮箱 legacy 行作为 fallback。
2. Global 成功登录若存在该裸邮箱 legacy 行，且解密 blob 确认是 Global，则复用该旧物理 key；blob 更新为 v1 Global。不能仅按字符串看起来像邮箱来猜 region。
3. 没有 legacy Global 行的新 Global 账户生成 `global:email`。
4. lookup 只选存储身份，不能省略登录/verify；不依据用户 POST 的 account_key，也不通过未验证 token 更新账户。
5. 同邮箱已经同时有 legacy 和 canonical Global 两行时，不自动合并/覆写。默认安全报错或进入明确的运维修复路径，先核查各自 Bearer、worker、rotation 和统计。不要把两个 refresh owner 永久保留下来却称为完整兼容。
6. 对每个读取的 legacy raw blob 保持 Global 语义；不能从父进程 `GARMIN_IS_CN` 或目录名推断其实际曾经属于 CN。若操作员曾手工放入 CN raw blob，需重新认证并显式登记，不能自动猜。

此策略不改通用 key 规范化、不批量重写 `access_tokens/oauth_codes/tool_usage`，旧设备可以继续使用既有 identity，同时新 CN/Global 相互隔离。它是对 Spec “推荐显式区域键”的**legacy 特例**，需在实现文档写清楚；region 的逻辑身份仍是 Global，只有历史存储 key 保持不变。

### 8.3 如果坚持所有 Global key 都必须显式带前缀

则需要额外的账户身份转换计划，不是简单修改 adapter 返回字符串：至少在事务中一致处理 `accounts`、`access_tokens`、`oauth_codes`、`tool_usage`，处理已存在目标行/统计冲突，并停稳旧 worker、保存最新 token、清理旧 manager 状态和目录归属。它不一定需要 schema migration，但属于数据/运行态迁移，改动与风险均大于 8.2，不建议作为本次最小 V1 默认方案。

### 8.4 运维与遥测

- `scripts/revoke.py:61-68`、`scripts/usage.py:39-46` 按第一个冒号分隔 adapter。新账户要传 `garmin:cn:person@example.com` / `garmin:global:person@example.com`，不能传 `cn:person@example.com`（会把 cn 当 adapter）。legacy 裸邮箱命令仍然有效。
- `status.py` 与 usage/report/store 多数只把 key 当不透明文本，可以继续工作；账户数会把两个 Garmin 域算作两个账户，不能把它解释成独立自然人数。
- telemetry 的 distinct_id 直接用 account_key，新区域账户会形成新的 distinct_id；`tests/test_telemetry.py` 必须同步。不要为“同邮箱看起来是同一个人”而把 CN/Global 的认证账户自动合并。
- `store.account_key_exists` / `scripts/add_beer.py` 用裸邮箱做最佳努力捐赠归因，新区域 key 不会命中。属于非认证功能的语义变化，应记录或后续小修，不要为了归因更改权限身份。

### 8.5 目录切换和版本回退

修复目录映射时，不能把“旧 Bearer/key 不变”误当成“直接丢弃旧磁盘状态没有代价”。正常发布应先停止接收新请求、让旧 worker 停稳并捕获最后 token rotation，再由新版本从最新 DB 创建独立新目录。若 DB 已落后于旧目录，需先按账户核验/受控恢复，不能自动从可能碰撞的目录复制凭据。以上是后续发布条件，本阶段未执行。

v1 wrapper 写入 DB 后，旧版 Garmin adapter 会将整个 wrapper 写给 worker，并把它直接传给 token verify；因此简单回滚到原代码不具备格式兼容性。回滚应使用仍可读取 v1 的兼容代码，或有明确的数据转换/重新授权方案；不能直接恢复陈旧 DB 备份并假设已经旋转的 refresh token 仍有效。

## 9. 测试更新清单

现有 Garmin 测试全部 mock 上游；现有 suite 通过也不等于 CN 已验证。下面区分需要修改的现有断言、需新增的安全覆盖，以及应保持不变的通用回归。

### 9.1 必改现有测试

**`tests/test_garmin_login.py`**：8 个现有测试都涉及新 helper 参数：

- `test_login_no_mfa_returns_tokens`
- `test_login_needs_mfa_then_resume`
- `test_login_retries_blocked_then_succeeds`
- `test_login_auth_error_not_retried`
- `test_login_blocked_exhausted_raises_blocked`
- `test_verify_tokens_returns_name`
- `test_verify_tokens_succeeds_when_name_empty`
- `test_verify_tokens_raises_when_login_fails`

给调用补明确 `is_cn` 并参数化两域，不能只让 mock 接受任意 kwargs 而不检查构造参数；增加 pending 客户端密码引用、continuation、verify 旋转和网关级 GARMINTOKENS 缓存绕过测试。后几项应保留真实 Garmin 控制流、仅 mock 网络边界，避免整个 Garmin 类被替换后把缺口隐藏掉。

**`tests/test_adapters.py`**：

- `test_garmin_forward_env_is_the_documented_contract`：env 精确字典增加 true/false；真实临时 marker 场景。
- `test_garmin_forward_materialize_writes_0600_tokens_file`：blob wrapper → raw 文件，同时检查 marker 和已有宽权限文件。
- `test_start_login_ok_normalizes_account_key`：新区域 key 与 wrapper；保留 strip/lower 验证。
- `test_start_login_mfa_state_carries_email`：三元 state，分别检查 CN/Global。
- `test_resume_ok_returns_login_ok`、`test_resume_failure_is_retryable_with_same_state`：新 state、区域身份和包装；错误重试同一 region。
- `test_verify_ok_and_failure`：unpack 后传 raw + `is_cn`，增加 legacy、畸形 wrapper。
- `test_start_login_blocked_maps_message_and_reason`、`test_start_login_auth_error_maps_message`：保持错误文案/分类回归，增加区域调用断言。

`test_login_ok_is_frozen`、base 异常类型、registry/attrs/command 测试主要保持现有合约；若引入 legacy lookup 注入，则补 registry 默认行为与 Garmin-only 注入测试。

**`tests/test_oauth.py`**：

- `test_authorize_get_renders_form`：required selector、合法值及默认选项。
- `test_login_no_mfa_redirects_with_code`、`test_login_mfa_then_verify_redirects`：新账户 key、解密后 v1 blob、region。
- `test_authorize_post_mfa_rejects_tampered_redirect`、`test_mfa_wrong_code_reprompts`、`test_mfa_verify_failure_restarts`、`test_mfa_resume_login_error_restarts_login`、`test_mfa_login_id_from_another_adapter_is_rejected`：手工构造的 Garmin pending tuple 应调整为新形状；其中提前拒绝/全 mock 的场景未必立刻失败，也应避免残留旧状态 fixture。
- `test_login_verify_failure_rerenders_form`、`test_login_timeout_shows_retry_message`：原来检查裸邮箱没有记录，改成确认所有可能区域/legacy key 均无新增，防止“查错 key 得到 None”的假阳性。
- 如增加错误页选项保留，扩展 `test_login_blocked_shows_retry_message`、`test_authorize_post_bad_csrf_rerenders_form` 和 timeout/verify/MFA 重启场景。

增加 invalid/empty region 在上游调用前失败、缺字段 Global、MFA POST 伪造 region/email 无效、两个并发 MFA 会话互不污染、region 验证失败不得 mint code/存 blob。

**`tests/test_workers.py`**：

- `_token_file` 的直接目录拼接及 `test_materialize_tokens_sets_secure_perms`：同步新的账户路径映射与 marker 权限。
- 以下所有使用真实 GarminForward 和 `{"v":1}` / `{"v":2}` 合成凭据的回写测试，需要更换无歧义 raw fixture 或正式 wrapper，并按解包后内容断言：
  - `test_persist_rotated_captures_worker_rotation`
  - `test_persist_rotated_skips_torn_file_until_it_parses`
  - `test_reap_idle_captures_last_rotation`
  - `test_respawn_recovers_rotation_from_dead_worker`
  - `test_fresh_manager_trusts_store_over_disk`
  - `test_shutdown_captures_rotations`
  - `test_evicted_worker_rotation_is_captured`
  - `test_read_back_error_does_not_break_the_batch`
  - `test_read_back_returns_current_token_file`
- `test_read_back_error_does_not_break_the_batch` 中用邮箱子串判断路径的 `ExplodingReadBack`，目录改 hash 后也必须调整。
- `test_default_spawn_arms_gate_only_when_forward_can_classify_login` 增加 Popen env 断言；测试必须覆盖真实 `_default_spawn`，仅 `spawn=lambda` 无法证明 env 注入。
- startup/health/login gate、取消、inflight、LRU、reserved ports、port cooling、retry 相关现有测试保持原意义；不要为了 region 放宽这些保护。

**`tests/test_backfill_garmin_tokens.py`**：`_seed` 路径、raw/v1 fixtures 和以下 5 个测试全部复核：

- `test_drifted_file_is_persisted_only_with_apply`
- `test_relogin_after_file_write_wins`
- `test_torn_and_missing_files_never_persist`
- `test_other_adapters_untouched`
- `test_main_dry_run_output_masks_keys`

增加 CN/global wrapper 不丢失、same token 不误报 drift、marker 缺失/非法/冲突拒绝、apply 阶段 DB 已被 re-login 更新则不覆盖；兼容旧目录时增加碰撞拒绝测试。

**`tests/test_telemetry.py`**：

- `test_login_flow_emits_funnel_events_and_stitch` 的三个 distinct_id 断言随新区域 key 更新。
- `test_returning_account_status` 需分别覆盖 canonical 新账户和 legacy 重登；不能把它简单改成“new”来掩盖兼容退化。
- `test_login_failure_emits_login_failed` 保持 personless，不将未验证区域身份送入识别事件。

### 9.2 新增安全/兼容测试

建议新增 `tests/test_garmin_blob.py`；其余增加到现有对应文件：

| 文件 | 必需新增覆盖 |
| --- | --- |
| `tests/test_garmin_blob.py` | CN/Global pack-unpack、legacy default、非法 region/version/JSON/tokens shape、布尔版本、畸形 wrapper 不退化、确定性序列化。 |
| `tests/test_adapters.py` | 同邮箱两域不同身份、legacy lookup 仅用于 Global；marker 跨账户不共享；CN marker 缺失/篡改 fail closed。 |
| `tests/test_workers.py` | `a+b`/`a_b`、区域 key/旧邮箱的目录不碰撞；同邮箱两域端口/token/env/readback 独立；父 env=true 时 Global 仍 false；re-login 胜过旧 worker refresh；旧进程停止后才能重用 token 路径；所有回写触发保留区域。 |
| `tests/test_proxy.py` | 两个实际不同的 fake worker 与两枚 Bearer；交换 header/query/MCP region、email、account_key、session-id 均不能改变所选账户；legacy Bearer 仍只到 Global；不存在账户不得 fallback 到同邮箱另一域。 |
| `tests/test_store.py` | 两域和 legacy 记录共存、加密字段不含测试明文；带区域 key 的 token/code/revoke/usage；若增加条件回写，验证旧 baseline 不覆盖新登录且删除账户不会被旧 worker 复活。 |
| `tests/test_oauth.py` / `tests/test_app.py` | 新账户授权/MFA全链路、legacy 重登恢复旧设备、invalid region 无副作用、worker 与成功登录提交的协调。 |
| `tests/test_scripts.py` | `garmin:cn:email` / `garmin:global:email` 解析、撤销只影响指定域，旧裸邮箱语法仍有效。 |
| `tests/test_log.py` / Garmin OAuth tests | 合成 password/token 注入 cause chain / worker stdout，不得出现在日志；正常 login gate 分类仍有效。 |

### 9.3 保持不变的回归范围

`tests/test_whoop_api.py`、`test_whoop_mcp.py`、`test_whoop_e2e.py`、`test_remote_forward.py`、`test_local_forward.py`、通用 store/config/security、backup/report/usage、scripts、beers、hourly_digest、daily_triage、backfill_account_connected 都应运行。多数不应机械替换其裸邮箱 fixture：它们验证 WHOOP/通用/legacy 行为，仍然需要这种 key。

`tests/conftest.py` 的 Garmin fake factory / fake worker 可以补可区分账户的返回数据；remote stub 为通用流程借用 `authorize.html`，模板加 region 后仍不能让 remote adapter 被迫理解 Garmin region。

## 10. Spec 与当前 upstream 接口的差异

| Spec 假设/表述 | 审计确认及处理 |
| --- | --- |
| Gateway 已有账户隔离 | DB/Bearer 层成立，workdir 映射不成立；F1 是允许小改 manager 的实证理由，不是重构借口。 |
| 每账户 region | 当前无 region；不是为某个全局 Config 加开关。 |
| 建议 `garmin:region:email` | DB 已按 adapter 分区，新 key 使用 `region:email`；legacy 特例必须显式定义。 |
| `env(port,workdir)` 无 blob | `.garmin_region` 可桥接；材料化调用顺序满足要求，无需改通用签名。 |
| marker 缺失 → Global；refresh 永不改 region | 已知 CN 情况下两者冲突；必须保留/校验预期 region，默认只用于 legacy。 |
| raw JSON 可当 credential blob | 可以，store 完全不解析；wrapper 放在同一加密 blob 内不需 schema migration。 |
| password 不在 result/state | 外层无 password 字段，但 pending 包含完整 Garmin 实例；需核查并清除对象持有的密码，不只是 `del` 局部变量。 |
| read_back 验证 token | 当前仅 JSON 语法合法，无 token shape/区域/身份验证。 |
| refresh 生命周期已完整处理 | 已有五种回写触发，但仍有同进程 re-login、退出写入和 verify 旋转窗口。 |
| “encrypted at rest” | DB blob 加密；持久 worker token 文件明文且权限保护。不是整个 DATA_DIR 加密。 |
| legacy raw blob 默认 Global 即兼容完成 | 只满足读取兼容；改 account_key 还影响旧 Bearer、重登、worker 和脚本。 |
| 仅 adapter/tests 修改 | `backfill_garmin_tokens.py` 是第二个 raw blob 写入入口，不能漏；F1/F3 要小改核心。 |
| helper `start_login(..., *, is_cn, ...)` | 当前 attempts/backoff/sleep 允许位置参数。所有现有内部调用与测试须检查；推荐 is_cn 显式 keyword，保留原重试行为。 |
| 所有 Global 老用户均可自动识别 | 无 region 的历史数据只能按规范解释为 Global；不能证明人为导入的 raw CN 凭据属于哪个域。 |
| 凭据登录必然验证输入邮箱/密码 | 所选依赖会先尝试环境 GARMINTOKENS；必须阻止 F7 的缓存绕过。 |

## 11. 验证记录与后续验收

### 11.1 已执行的只读验证

- 全仓库文件盘点、关键符号跨文件检索与调用链追踪。
- Python 临时目录/内存 SQLite 合成探针：三组目录碰撞均成立；B token 在 A key 下持久化成立；旧 worker rotation 覆盖 re-login 成立。第一次探针使用真实 `WorkerManager`/store 和最小文件 forward，不需要上游联网。
- 所有诊断环境/日志/临时数据放在 `/private/tmp`；没有使用真实 password/token；没有执行生产 backfill 或发送运维通知。

**完整现有测试基线：416 passed，1 warning，用时 50.46 秒。没有修改测试来得到通过。**

本机 PATH 中无 uv，系统 Python 为 3.9.6；使用已有运行时的 Python 3.12.14 在 `/private/tmp/missingmcp-audit-venv` 建临时 venv，安装测试相关依赖。关键解析版本：garminconnect 0.3.13、Starlette 1.6.0、httpx 0.28.1、cryptography 50.0.1、pytest 9.1.1、pytest-asyncio 1.4.0、PostHog 7.47.3。测试命令：

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH=/Users/charles/missingmcp/src \
/private/tmp/missingmcp-audit-venv/bin/python -m pytest \
  -q -p no:cacheprovider \
  --basetemp=/private/tmp/missingmcp-audit-pytest-network
```

第一次在受限沙箱中运行，338 passed、77 errors、1 failed，错误/失败均涉及 fake HTTP server 的本地监听被禁止；放开 127.0.0.1 测试监听后原样重跑得到上述 416 passed。仅剩第三方 Starlette/AnyIO 的 BlockingPortal 别名弃用警告。依赖安装的初次网络限制也通过受审查的临时环境安装解决。没有未解决的自动审批拒绝。

本次 suite 是 fresh dependency resolution 的审计基线，不等于生产镜像 pin 的重建测试。测试日志在 `/private/tmp/missingmcp-audit-pytest-network.log`，安装日志在 `/private/tmp/missingmcp-audit-install.log`；它们是临时诊断物，关键结论已完整写入本报告。

补充合成探针结果：

| 检查 | 结果 |
| --- | --- |
| `a+b` / `a_b` 与区域前缀 / legacy 目录碰撞 | 三组均碰撞；A readback 可持久化 B token。 |
| 旧 rotation 覆盖刚完成的 re-login | 可复现。 |
| MFA pending 中 `Garmin.password` 仍是合成密码 | true，真实 Garmin/helper，仅 mock 上游 challenge。 |
| 对已有 0644 token 文件再次 materialize | 仍为 0644。 |
| read_back 接受 `null`、`[]`、`{}` | 三者均接受。 |
| backfill 对 v1 CN blob apply raw token | wrapper 被移除，可复现。 |
| adapter 异常 cause 的合成秘密进入 log_exc | true。 |
| 网关级 GARMINTOKENS 使表单 A 获得缓存 B 的 blob | 在缓存加载/profile 网络边界 mock 下可复现，凭据登录未调用。 |

依赖源码只读核查位置为临时环境的 `lib/python3.12/site-packages/garminconnect/__init__.py`（构造器约 369、login 666、MFA 提前返回 736-745、清除 password 794）及 `client.py`（dumps/dump/load 1504-1590）。公开项目源码可对照，但 master 不能代替本次固定解析版本：[python-garminconnect](https://github.com/cyberjunky/python-garminconnect/blob/master/garminconnect/__init__.py)。

补充平台限制：首次探针用 macOS 默认 `/var/folders/...` 临时目录时，0.3.13 的 token dump 因祖先 symlink 检查拒绝该路径；改用真实路径 `/private/tmp` 后诊断完成，没有修改仓库 helper。现有测试把 dump mock 掉，因此未覆盖此平台问题；本地真实登录 smoke test 需验证有效临时目录。不能据此判断 Linux 生产镜像有相同问题。

### 11.2 后续真实账户 smoke test（本阶段未执行）

需要 CN（含 MFA）和 Global 测试账户，各自完成浏览器授权、MCP initialize/tools-list、简单 profile/日指标读取、停止 worker、重新连接、重启网关、可控 token refresh。另须验证同邮箱两域、旧 Global Bearer、旧 Global 重新登录和并发 MFA，不得用日志/代码/命令历史保存凭据。

实现验收标准：全部测试通过、两域 smoke test 通过、F1–F7 的对应回归通过、密码/token 日志与文件权限保证获得实际测试支持。当前结论是**可复用现架构进入最小安全实现，但尚不满足直接上线 CN/Global 的条件**。
