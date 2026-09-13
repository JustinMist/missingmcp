# MissingMCP Garmin China + Global 使用与运维 Cookbook

更新日期：2026-09-13

## 1. 适用范围

本文面向 MissingMCP 部署者、支持人员和验收人员，说明如何部署、连接和排查 Garmin
China / Global 多租户功能。

核心约束：

- Garmin 区域属于账号，而不是某次 MCP 请求。
- China 账号使用 `garmin.cn`，Global 账号使用 `garmin.com`。
- 同一邮箱可同时连接两个区域；它们是两个独立账号、两个 token blob、两个 worker。
- Garmin 密码只用于登录，不会进入数据库或 worker 的持久化配置。
- MCP 请求里的 query、header、body 或 tool arguments 都不能切换账号区域。
- 老版本裸 Garmin token 继续可用，并按 Global 处理。

## 2. 部署前检查清单

必须准备：

- 一个长度至少 32 字符的随机 `GATEWAY_SECRET`；上线后必须稳定保存，丢失后无法解密
  已有账号 token。
- 对外 HTTPS 地址，例如 `https://mcp.example.com`，写入 `PUBLIC_URL`。
- 持久化且私有的 `DATA_DIR`；容器部署建议挂载到 `/data`。
- 单实例 gateway。Worker registry 和并发控制包含进程内状态，不能直接横向扩成多个
  gateway 副本共享同一目录。
- 能供每个活跃 Garmin 账号使用的 loopback worker 端口范围。

建议的生产配置：

```dotenv
GATEWAY_SECRET=<openssl rand -base64 48 的输出>
PUBLIC_URL=https://mcp.example.com
PORT=8080
DATA_DIR=/data
WORKER_PORT_START=9000
WORKER_PORT_END=9099
WORKER_IDLE_TTL=900
WORKER_STARTUP_TIMEOUT=20
MAX_WORKERS=10
ACCESS_TOKEN_TTL_DAYS=90
GATEWAY_LOG_LEVEL=info
```

不要配置部署级 `GARMIN_EMAIL`、`GARMIN_PASSWORD`、`GARMIN_EMAIL_FILE`、
`GARMIN_PASSWORD_FILE` 或 `GARMINTOKENS_BASE64`。实现会从 worker 环境中移除这些
fallback，但生产配置本身也不应包含共享 Garmin 凭据。

## 3. 构建和启动

### Docker

```bash
docker build -t missingmcp .
docker run -d --name missingmcp --restart unless-stopped \
  --env-file .env \
  -p 127.0.0.1:8080:8080 \
  -v missingmcp-data:/data \
  missingmcp
```

在 TLS 反向代理中关闭 `/garmin/mcp` 的响应缓冲，并为 SSE/长请求设置足够长的 read
timeout。worker 端口只绑定 `127.0.0.1`，不要对公网发布 `9000-9099`。

当前构建同时固定 gateway 与 worker 使用 `garminconnect==0.3.6`。镜像内可这样核验：

```bash
docker exec missingmcp /app/.venv/bin/python -c \
  "from importlib.metadata import version; print(version('garminconnect'))"
docker exec missingmcp /opt/garmin-mcp/.venv/bin/python -c \
  "from importlib.metadata import version; print(version('garminconnect'))"
```

两条命令都应输出 `0.3.6`。不要在未复核区域 endpoint、MFA 和 token dump 契约的情况
下单独升级其中一个环境。

### 本地开发

```bash
uv sync --frozen --extra dev
GATEWAY_SECRET="$(openssl rand -base64 48)" \
PUBLIC_URL=http://localhost:8088 \
PORT=8088 \
DATA_DIR=./.localdata \
GARMIN_MCP_CMD="uvx --python 3.12 --from git+https://github.com/Taxuspt/garmin_mcp garmin-mcp" \
uv run missingmcp
```

本地测试数据也包含长期 Garmin session token，应使用专用私有目录，不要提交到 Git。

## 4. 用户连接流程

在支持 OAuth 的 MCP 客户端中添加：

```text
https://mcp.example.com/garmin/mcp
```

以 Claude 为例：Settings → Connectors → Add custom connector，只填写上面的 URL；除非
客户端明确要求，不要手工填写 OAuth client ID 或 secret。

授权页面步骤：

1. 选择 `China (garmin.cn)` 或 `Global (garmin.com)`。
2. 输入该区域 Garmin 账号的邮箱和密码。
3. 如出现 MFA，输入验证码。
4. 授权完成后回到 MCP 客户端调用 Garmin 工具。

区域选择错误时不要尝试用 MCP 参数补救；删除或重新授权该 connector，并在登录页面
选择正确区域。

### 同邮箱连接两个区域

同一个邮箱可分别授权两次：

- China 的内部 account key 为 `cn:<规范化邮箱>`。
- Global 的内部 account key 为 `global:<规范化邮箱>`。

它们的 OAuth Bearer、加密 token、worker 目录、worker 进程和 Garmin endpoint 都互相
隔离。客户端如需同时使用，建议把两个 connector 命名为 `Garmin China` 与
`Garmin Global`，避免用户侧混淆。

### MFA 注意事项

- MFA 的 region 来自 gateway 保存的 pending state。
- MFA POST 中额外提交 `garmin_region` 不会改变区域。
- 错误验证码后按页面提示重试；MFA 会话丢失时回到登录页重新开始。
- gateway 会从 pending 对象中移除密码及密码请求体，不会持久化 Garmin 密码。

## 5. 兼容老账号

历史裸 `garmin_tokens.json` blob 没有 region wrapper，系统将其解释为 Global，并为 worker
设置 `GARMIN_IS_CN=false`。已有裸 account key 和 Bearer 会继续工作。

新授权的 Global 账号使用 `global:` key。若历史裸 key 与 canonical Global key 同时存在且
归属不明确，系统会 fail closed，不会猜测或自动合并。先备份数据库，再由人工确认应该
保留的账号并重新授权。

## 6. 日常状态与用量

以下脚本默认从 `DB_PATH`、`DATA_DIR/gateway.db`、`/data/gateway.db` 或
`./.localdata/gateway.db` 查找数据库。

```bash
python scripts/status.py
python scripts/status.py --detail
python scripts/usage.py
python scripts/usage.py --account garmin:cn:me@example.com
python scripts/usage.py --account garmin:global:me@example.com
```

`status.py --detail` 只显示 token hash 前缀，不显示 OAuth Bearer 或 Garmin session token。
`usage.py` 只记录 MCP method/tool 名称、次数和时间，不记录 tool arguments 或 Garmin 数据。

## 7. 撤销与下线账号

先查看精确区域 key：

```bash
python scripts/revoke.py --list
python scripts/status.py --detail
```

撤销单个客户端设备：

```bash
python scripts/revoke.py --device <至少 8 位 token-hash 前缀>
```

撤销一个区域账号的所有 MCP Bearer，但保留 Garmin token 以便重新授权：

```bash
python scripts/revoke.py --account garmin:cn:me@example.com
python scripts/revoke.py --account garmin:global:me@example.com
```

彻底下线区域账号并删除其加密账号 blob 与 usage：

```bash
python scripts/revoke.py --account garmin:cn:me@example.com --purge
```

不要用裸邮箱下线一个同时存在 CN/Global 的用户；始终写完整的
`garmin:<region>:<email>`，以免操作错区域。撤销会在下一次请求时生效；已在处理中的请求
按既有 inflight 规则结束，新的 credential generation 不会复用旧 worker。

## 8. Token 回读与 backfill

worker 可能刷新 Garmin refresh token。gateway 会把新 token 以 compare-and-swap 写回，
同时保留账号原 region。浏览器重新登录产生的新 generation 永远优先于旧 worker 的晚到
写回。

检查历史磁盘 token：

```bash
GATEWAY_SECRET="$GATEWAY_SECRET" \
python scripts/backfill_garmin_tokens.py --db /data/gateway.db --data-dir /data
```

默认是 dry-run。只有输出中 manager-proven、generation digest 匹配且 region 校验通过的
`pending-capture` 才可安全恢复：

```bash
GATEWAY_SECRET="$GATEWAY_SECRET" \
python scripts/backfill_garmin_tokens.py --db /data/gateway.db --data-dir /data --apply
```

判读原则：

- `persisted` / `in-sync`：正常。
- `pending-capture`：存在 manager 证明的待写回 generation；`--apply` 可恢复。
- `untrusted-generation`：只有磁盘差异或 mtime 证据，不足以判定新旧；重新授权。
- `region-missing` / `region-invalid`：区域 sidecar 不可信；不要强行写回。
- `ambiguous`：旧/新目录或 identity 冲突；人工确认后重新授权。
- `torn` / `invalid-db`：数据损坏；从备份恢复或重新授权。
- `db-changed`：扫描期间数据库 generation 已变化，CAS 已拒绝写入；重新 dry-run。

不要通过手工改 `.garmin_region`、改 mtime 或直接复制 token 文件绕过 fail-closed 判断。

## 9. 故障处理配方

### 登录提示 rate limit / blocked

这是 Garmin 侧 403/429 或连接阻断，不等同于密码错误。等待数分钟后再试，避免快速重复
提交。确认选择了正确区域，并检查 gateway 到 `garmin.cn` 或 `garmin.com` 的出口网络。

### 登录成功但客户端再次要求授权

检查：

```bash
python scripts/status.py --detail
```

重点查看账号是否存在、Bearer 是否仍有效、worker 是否反复退出。`invalid_token` +
`WWW-Authenticate` 表示客户端应重新走 OAuth；从客户端删除并重新添加 connector 可清理
缓存的失效 DCR/client state。

### MFA 页面消失或验证码一直失败

MFA pending state 有生命周期且只属于原授权流程。不要复用旧页面或修改隐藏字段；关闭旧
页面，从 MCP 客户端重新触发授权。确认设备时间准确，验证码尚未过期。

### CN 请求疑似访问了 Global

1. 用 `status.py --detail` 确认 Bearer 对应 `garmin:cn:...`。
2. 检查 `worker-spawn` 账号 key，但不要在日志中输出 token。
3. 在隔离环境验证该 worker 的 `GARMIN_IS_CN=true` 和私有 `.garmin_region=cn`。
4. 不要信任请求中的 `region`、`X-Garmin-Region` 或 tool argument；它们不是路由来源。
5. 若 DB blob/sidecar 不一致，停止使用并重新授权，不要手工降级为 Global。

### 账号 purge 后用同一 key 重新登录

新请求会以数据库 G9 一类的新 generation 为准；旧 worker 必须先安全退役。若旧 worker
仍有 inflight 请求，新请求会暂时失败并要求重试，而不会借用旧 credential generation。
等待旧请求结束后重试；不要强杀 gateway 或复用旧 token 目录。

### worker 无法启动

检查端口容量、`MAX_WORKERS`、worker 命令和构建版本。worker clean exit 通常代表 Garmin
session 已失效，用户应重新授权；非零退出、OOM 或持续 health timeout 属于运维故障，应
检查结构化事件 `worker-died`、`worker-unhealthy`、`worker-start-failed`。

### restart 后出现 pending capture

先运行 backfill dry-run。只有 digest 与 DB generation 相符且 sidecar region 与 DB wrapper
一致时才恢复。store 暂时不可读时保持文件和 marker，恢复数据库后重试；不要让旧文件
覆盖浏览器重新登录后的 token。

## 10. 安全日志要求

可以记录：adapter、区域化 account key、worker port、状态码、tool 名、耗时、字节数、
token hash 前缀。

禁止记录：Garmin password、MFA code、`di_token`、`di_refresh_token`、OAuth Bearer、完整
Authorization header、client secret、解密后的 blob。生产环境保持 `GATEWAY_LOG_LEVEL=info`；
临时 debug 日志也必须按敏感数据处理并及时清理。

## 11. 发布前 E2E 验收

自动测试不使用真实 Garmin 凭据。生产发布前至少完成：

1. 在隔离部署中确认 gateway/worker 都加载 `garminconnect==0.3.6`。
2. 使用可区分数据的真实 CN 与 Global 测试账号，最好同邮箱。
3. 两个独立客户端分别完成 DCR、PKCE S256 和授权。
4. CN 流程先输入错误 MFA，再输入正确验证码；同时确认 Global 流程不受影响。
5. 强制 token refresh，确认 CN 请求到 `diauth.garmin.cn`，Global 请求到
   `diauth.garmin.com`，数据库写回仍保留原 region。
6. 分别调用 profile 和日期指标，确认两边响应归属正确。
7. 在 request header、query、body、tool arguments 中伪造相反 region/account key，确认
   路由和响应不改变。
8. 覆盖 restart、idle reap、capacity eviction、旧 worker token 晚到写回与同 key 重新
   授权。
9. 验证 legacy Global Bearer；不存在账号不得 fallback 到其他账号或区域。
10. 对 WHOOP 或另一个非 Garmin adapter 做 OAuth/MCP smoke，确认通用 adapter 行为未受
    影响。

验收期间不要把真实密码、token、MFA code 写入测试命令、截图、issue 或提交记录。
