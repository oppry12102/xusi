# 集群互联对接说明（两节点 Phase 2 v1 实战记录）

> 写给对端 xusi 维护者：本文档记录的是本机 (`61FyM_3Lazg`, 入口
> `http://81.70.43.157:8601`) 与对端 (`YktX3tUdGjs`, 入口
> `http://82.157.131.225:8601`) 在 `commit fd97652` (Phase 2 v1 + `local_only` 修复)
> 上对接的全过程、踩坑、当前状态与已知未解决问题。请按下面的检查清单核对。
>
> **前置要求**：双边必须都是 `fd97652` 或之后（含 `api: /api/agents 加 local_only 参数，破双边 fan-in 回环`）。
> 任一方是老 `e98bc10` 都会触发双边 fan-in 回环（详见 §4）。

## 0. TL;DR

- 两边都设了同一个 `[cluster].secret`（HS256 共享密钥），token 走 JWT 跨节点 verify 通过
- 握手 (`/api/peer/id`) 通：latency ~30–700ms 不等
- **双边注册现在可工作**——`commit fd97652`（`/api/agents?local_only=1`）已破 fan-in 回环
  - 双边注册 = 两边互相在 `etc/peers.toml` 写入对方；每个节点 `/api/agents` 看到「本地 + 对方 local」共一层，不再递归
- 当前版本范围限定：**只读路径通了**；**写路径 v1 没做**，详见 §5

## 1. 对端需要核对 / 必改的配置

### 1.1 `[cluster].secret` 必须一致

```toml
[cluster]
secret = "eDO_pDQKHHnSaaNWlK41t3wc3QLgZb2e6IZvsytqfyM"
```

**注意**：

- 这是**对称密钥**——任何持有它的节点都能签发任意 admin token（HS256 是对称的）
- 上面的值已在两次会话中明文出现，等同泄露。建议在双方都验证通过后**用 `openssl rand -hex 32` 重新生成**并同步替换
- secret 改了之后：**本机现存的所有 token 立即失效**（Phase 1.1 的按输入形态分流就是为了让 secret 轮换正确）。需要重新 `python3 -m xusi token new admin`

### 1.2 `[node].public_url` 必须填外网入口

```toml
[node]
role = "worker"
public_url = "http://82.157.131.225:8601"   # ← 对端外网入口
```

**问题**：当前对端 `/api/peer/id` 返回的 `url` 是 `http://10.0.16.14:8601`（内网 IP）——这是 `host=0.0.0.0` 时 `_detect_outbound_ip()` 的兜底结果。但本机拿这个 url 去 fan-in 会**连不上**（内网不通）。

`[node].public_url` 字段优先级最高，**显式设成外网入口后**，对端 `/api/peer/id` 就会返回外网 url，本机 fan-in 才能通。

### 1.3 admin token 必须用集群模式重新签

集群模式开启后，`new_token()` 自动签 JWT 形态（`xxx.yyy.zzz` 三段）。

**现有明文 token（`secrets.token_urlsafe(32)` 形态、无 dot）只在签发本机通**——Phase 1.1 的 `verify()` 按输入形态分流：集群模式下 JWT 输入只走 JWT 校验路径，明文输入走本地 `tokens.json` 回退。**对端 `tokens.json` 里没有本机的明文串，所以跨节点 401**。

重签：

```bash
python3 -m xusi token new admin
```

## 2. 双边注册拓扑（推荐，`fd97652` 之后）

本机已经把对端 (`YktX3tUdGjs`) 写进 `etc/peers.toml`。**建议对端也把本机写进 `etc/peers.toml`**——`fd97652` 修了双边注册的 fan-in 回环问题。

```bash
# 本机侧已生效的 peer 名册
cat etc/peers.toml
# [[peers]]
# id = "YktX3tUdGjs"
# url = "http://82.157.131.225:8601"
# name = "VM-16-14-ubuntu"

# 对端建议同步：
# [[peers]]
# id = "61FyM_3Lazg"
# url = "http://81.70.43.157:8601"
# name = "VM-8-12-ubuntu"
```

双边注册后两个节点互见：每个 `/api/agents` 显示「本地 + 对方 local」共一层（不再递归 peers-of-peers）。

## 3. 双边注册后的实际能力

本机 admin 调 `GET /api/agents` 会 fan-in 对端的 4 个 agent（带 `_via: "YktX3tUdGjs"` 标记），对端同理能看到本机 agent。每个 agent 的读端点（status / capabilities / services / observe / tokens / backups）通过 `forward_to_peer` 透传 JWT 到对端验证后返回。

验证命令：

```bash
JWT='<admin JWT>'
curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:8601/api/agents | python3 -m json.tool
# 应该看到 _via: "YktX3tUdGjs" 的 4 行

curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:8601/api/cluster | python3 -m json.tool
# self + peers[] 各一
```

## 4. `local_only` 标志——双边注册能 work 的关键

### 历史问题（`e98bc10` 原始版本）

`/api/agents` 在集群模式下默认 fan-in 所有 peer。双边注册时：

```
api_agents_list -> _one(YktX3tUdGjs)
  -> fetch_json("/api/agents", timeout=5)
    -> httpx.get(peer.url + "/api/agents")
      -> 对端收到 → 又触发对端 _one(本机 id) → fetch_json("/api/agents")...
      -> 5s 后本机侧 ReadTimeout
    -> PeerUnreachable
  -> _one except 分支返回 []
```

`fetch_json` 默认 5s 超时（`xusi/xproxy.py`），peer 4 个 agent + journal 健康检查会让单次 fan-in 撑爆 5s。对称回环下每次请求都超时，`/api/agents` 返回 `[]`，但 journal 里能看到两边互相密集打 `/api/agents`。

### 修法（`fd97652`）

`api_agents_list` 加 `local_only: bool = False` query 参数；fan-in 中继时给 peer 传 `?local_only=1`，peer 端 handler 见此标志**只返回本地 agent、不二次 fan-out**。loop 在第一层就停。

行为矩阵：

| 调用方 | URL | 行为 |
|---|---|---|
| 客户端（WebUI / curl） | `GET /api/agents` | 默认 fan-in：`local + direct peers' local` |
| fan-in 中继（内部） | `GET /api/agents?local_only=1` | 只 local，不再 fan-out |
| 客户端（debug / 单机视角） | `GET /api/agents?local_only=1` | 只 local |

### 验证 `local_only` 工作正常

```bash
# 客户端视角：本机 0 agent + 对端 4 agent = 4
curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:8601/api/agents | jq length
# → 4

# 强制只看本地：本机 0 agent
curl -s -H "Authorization: Bearer $JWT" 'http://127.0.0.1:8601/api/agents?local_only=1' | jq length
# → 0

# 确认 fan-in 不再回环——journal 里 peer 打来的请求 URL 必须带 ?local_only=1
journalctl --user -u xusi --since "1 min ago" | grep '/api/agents'
# 应只看到：xxx.xxx.xxx.xxx:port - "GET /api/agents?local_only=1 HTTP/1.1" 200 OK
# 不应看到裸 "/api/agents"（peer 之间互打必须带 local_only=1）
```

### 部署要求

**双边都必须升级到 `fd97652` 或之后**——任一方是老版本（裸 `/api/agents`）就会触发回环。验证版本：

```bash
curl -s http://<对端>:8601/api/health | jq .version
# 当前是 "1.2.0"；具体 commit 通过 git log 核对
```

## 5. Phase 2 v1 范围限定（写路径未做）

来自 commit message 的明确声明：

| 已做 | 没做（v2 候选） |
|---|---|
| peer 注册表 + 5s 探活缓存 | 写路径：lifecycle / patch / mail / backup / token revoke |
| `/api/peers` CRUD + `/api/peers/probe` | `/svc` `/v1` `/ui` 跨节点 HTML 重写 |
| `/api/cluster` 真实化（带 latency） | peer 自动发现 |
| `/api/agents` fan-in | WebSocket 跨节点 |
| agent 读端点（status / capabilities / services / observe / tokens / backups）跨节点转发 | |
| `/px/{id}/...` 跨节点转发（观察台 + 任何前缀子路径） | |

> `/px/{id}/...` 已支持远端：peer 端 `prefix_proxy` 自己 inject agent token；HTML
> 中的 `/v1/*` 由 peer 在 HTML 重写时改成 `/px/{id}/v1/*`——浏览器仍在 dev 页面里继续触发
> `/px/...`，再被 dev 转发到 peer（递归）。所以 WebUI 上**点「观察台」即可打开对端 agent 的
> 完整观察台页面**，所有相对路径自动转发。鉴权只接受管理面 token（peer 端的 agent tokens.json
> 不在本机）。

**所以**：
- 在本机可以**列出**对端 agent、**读取**对端 agent 的 health / capabilities / observe log
- 可以**经本机打开对端 agent 的观察台**（`/px/{id}/ui/`），所有 `/v1/*` 子路径透明工作
- **不能在本机**对端 agent 做 `POST /api/agents/{id}/mailbox` 或启停 / 改参 / 投信 / 备份 / 撤销 token——这些请求**只在本机 agents 范围内生效**，对端 agent 需要登到对端 WebUI 操作

## 6. 排查 cheat sheet

### 验证 secret 一致

两边都 `grep secret etc/xusi.toml` 比对（注意：gitignored，本机本地）。

### 验证 handshake

```bash
curl -s http://<对端>:8601/api/peer/id
# 期望：{"id": "...", "name": "...", "role": "worker",
#        "version": "1.2.0", "url": "<对端外网入口>"}
```

`url` 字段必须是**外网入口**——不是内网 IP。

### 验证 JWT 跨节点 verify

```bash
JWT=$(python3 -m xusi token new admin | head -1)
# 上面只输出 token 这一行
curl -s -H "Authorization: Bearer $JWT" http://<对端>:8601/api/whoami
# 期望：{"label": "admin", "role": "admin", "agents": ["*"]}
# 失败：{"detail": "missing or invalid manager token"}
```

### 看 fan-in 是否回环

```bash
journalctl --user -u xusi --since "1 min ago" | grep '/api/agents'
# 正常（fd97652 之后）：所有 peer 打来的请求都带 ?local_only=1，无回环
# 回环（老版本双边注册）：每秒 10+ 条裸 "/api/agents" 互打 → 5s 超时
```

### 验证 `local_only` 行为

```bash
JWT='<admin JWT>'
# 客户端视角：本地 + 全部 peer 的 local（本机 0 + 对端 4 = 4）
curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:8601/api/agents | jq length

# 强制只看 local（本机 0 agent）
curl -s -H "Authorization: Bearer $JWT" 'http://127.0.0.1:8601/api/agents?local_only=1' | jq length

# peer 端 handler 也支持——对端也能用这个参数「只返自己」
curl -s -H "Authorization: Bearer $JWT" 'http://82.157.131.225:8601/api/agents?local_only=1' | jq length
```

### 清探活缓存（调试用）

```python
from xusi import peers
peers.clear_probe_cache()
```

## 7. 对端需要的最小操作清单

按顺序：

- [ ] 1. 核对 `[cluster].secret` 与本机一致
- [ ] 2. 设 `[node].public_url = "http://82.157.131.225:8601"`（外网入口）
- [ ] 3. **确认对端已升级到 `fd97652` 或之后**——`curl http://81.70.43.157:8601/api/health` 看 version / `git log -1` 看 commit
- [ ] 4. `systemctl --user restart xusi.service`
- [ ] 5. `python3 -m xusi token new admin`（重新签 JWT token）
- [ ] 6. 验证握手：`curl http://81.70.43.157:8601/api/peer/id`（应当从本机拿到 `61FyM_3Lazg`，url 是本机外网入口）
- [ ] 7. 验证跨节点 verify：用本机新签的 JWT 调对端 `/api/whoami`
- [ ] 8. **双边注册**：在本机 `etc/peers.toml` 加 `[[peers]] id="61FyM_3Lazg" url="http://81.70.43.157:8601"`；本机已经写好了对端
- [ ] 9. 验证 fan-in：`GET /api/agents` 应看到对端 4 个 agent（带 `_via="YktX3tUdGjs"`）；`?local_only=1` 只返本地
- [ ] 10. 建议 secret 轮换：`openssl rand -hex 32`，双方替换；轮换后重签所有 token

---

**维护者备注**：

- §1.1 的 secret 已在两次会话中明文出现，按泄露处理——完成对接后请双方用 §7 第 10 步轮换
- 文档 §2/§4 在 `fd97652` 之前曾以「单边注册推荐」为基线；之后改为「双边注册推荐」。后续 commit 若引入 list 缓存 / 写路径 / 自动发现等，需要再更新本文件 §0/§4/§5
- 本文档 188 行（`3c6e14d` 提交时的版本）+ §4 重写 + §3/§6/§7 同步更新 = 当前总 ~240 行
