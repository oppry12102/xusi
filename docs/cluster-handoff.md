# 集群互联对接说明（两节点 Phase 2 v1 实战记录）

> 写给对端 xusi 维护者：本文档记录的是本机 (`61FyM_3Lazg`, 入口
> `http://81.70.43.157:8601`) 与对端 (`YktX3tUdGjs`, 入口
> `http://82.157.131.225:8601`) 在 `commit e98bc10` (Phase 2 v1) 上对接
> 时的全过程、踩坑、当前状态与已知未解决问题。请按下面的检查清单核对。

## 0. TL;DR

- 两边都设了同一个 `[cluster].secret`（HS256 共享密钥），token 走 JWT 跨节点 verify 通过
- 握手 (`/api/peer/id`) 通：latency ~30–700ms 不等
- **单边注册可工作**；**双边注册会触发 fan-in 回环 + 5s 超时**，见 §4
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
- secret 改了之后：**本机现存的所有 token 立即失效**（Phase 1.1 的按输入形态分流就是为了让 secret 轮换正确）。需要重新 `python3 -m xusi token new admin --role admin`

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
python3 -m xusi token new admin --role admin
```

## 2. 单边注册就能工作（推荐拓扑）

本机已经把对端 (`YktX3tUdGjs`) 写进 `etc/peers.toml`，握手 30ms 通。**强烈建议对端不要把本机也写进 `etc/peers.toml`**——理由见 §4。

```bash
# 本机侧已生效的 peer 名册
cat etc/peers.toml
# [[peers]]
# id = "YktX3tUdGjs"
# url = "http://82.157.131.225:8601"
# name = "VM-16-14-ubuntu"

# 对端不需要加 peer，除非有特殊理由
```

## 3. 单边注册后的实际能力

本机 admin 调 `GET /api/agents` 会 fan-in 对端的 4 个 agent（带 `_via: "YktX3tUdGjs"` 标记），每个 agent 的读端点（status / capabilities / services / observe / tokens / backups）通过 `forward_to_peer` 透传 JWT 到对端验证后返回。

验证命令：

```bash
JWT='<本机 admin JWT>'
curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:8601/api/agents | python3 -m json.tool
# 应该看到 _via: "YktX3tUdGjs" 的 4 行

curl -s -H "Authorization: Bearer $JWT" http://127.0.0.1:8601/api/cluster | python3 -m json.tool
# self + peers[] 各一
```

## 4. ⚠ 双边注册会触发 fan-in 回环（不要做）

### 现象

- `fetch_json` 默认 5s 超时（`xusi/xproxy.py`）
- 对端 4 个 agent × 每次 `/api/agents` 内部 fan-out（journal 健康检查 / 状态轮询）会撑到 5s 以上
- 双边注册时：本机 fan-in 对端 → 对端 fan-in 本机 → 形成对称回环，每个 `/api/agents` 内部都在打对方

实测（双边注册状态）：

```
api_agents_list -> _one(YktX3tUdGjs)
  -> fetch_json("/api/agents", timeout=5)
    -> httpx.get(peer.url + "/api/agents")
      -> 对端收到 → 又触发对端 _one(本机 id) → fetch_json("/api/agents")...
      -> 5s 后本机侧 ReadTimeout
    -> PeerUnreachable
  -> _one except 分支返回 []
api_agents_list 返回 []
```

`api_agents_list` 把 `PeerUnreachable` / `PeerHttpError` 都吞掉（设计意图：单 peer 挂掉不让 list 整体 502），**所以 UI 上看到的是「本地 + 对端」共 0 agent**，但日志（`journalctl --user -u xusi -n 100`）能看到两边互相密集打 `/api/agents`。

### 解决方向（任一）

- **A. 单边注册（推荐，当前已生效）**：本机加对端、对端不加本机。代价：对端 `/api/agents` 看不到本机身份（但本机 0 agent，没东西可看）
- **B. 调高 `fetch_json` 超时**：`xproxy.fetch_json` 默认 5s 改成 30s+，让 fan-in 有时间穿透。问题：双边注册仍然有 N×N 放大，长尾延迟可观
- **C. 加本地 list 缓存**：`/api/agents` 结果在 N 秒内复用，避免每次请求穿透到 peer。Phase 2 v2 候选
- **D. fan-in 时跳过自身的 peer id**：当前 `api_agents_list` 已经做了 `p['id'] != self_id` 过滤，但**本机 id 不在 peer 名册时**过滤不了对端 fan-in 回来的本机（双向注册时对端 fan-in 包含本机 id，但**对端的 fan-in 列表里不应该有本机**，因为本机 id 不会在对端 `etc/peers.toml` 的 `[[peers]]` 里——除非对方把本机也加进去了）

**结论**：A 是 v1 唯一干净解。如果对端非要双向注册，至少需要 B + 砍掉回环路径。

## 5. Phase 2 v1 范围限定（写路径未做）

来自 commit message 的明确声明：

| 已做 | 没做（v2 候选） |
|---|---|
| peer 注册表 + 5s 探活缓存 | 写路径：lifecycle / patch / mail / backup / token revoke |
| `/api/peers` CRUD + `/api/peers/probe` | `/px` `/svc` `/v1` `/ui` 跨节点 HTML 重写 |
| `/api/cluster` 真实化（带 latency） | peer 自动发现 |
| `/api/agents` fan-in | WebSocket 跨节点 |
| agent 读端点（status / capabilities / services / observe / tokens / backups）跨节点转发 | |

**所以**：
- 在本机可以**列出**对端 agent、**读取**对端 agent 的 health / capabilities / observe log
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
JWT=$(python3 -m xusi token new admin --role admin | head -1)
# 上面只输出 token 这一行
curl -s -H "Authorization: Bearer $JWT" http://<对端>:8601/api/whoami
# 期望：{"label": "admin", "role": "admin", "agents": ["*"]}
# 失败：{"detail": "missing or invalid manager token"}
```

### 看 fan-in 是否回环

```bash
journalctl --user -u xusi --since "5 min ago" -f | grep "/api/agents"
# 单边注册：每秒 0–1 条（健康检查 / WebUI 轮询）
# 双边回环：每秒 10+ 条（双向 fan-in 互打）
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
- [ ] 3. `systemctl --user restart xusi.service`
- [ ] 4. `python3 -m xusi token new admin --role admin`（重新签 JWT token）
- [ ] 5. 验证握手：`curl http://81.70.43.157:8601/api/peer/id`（应当从本机拿到 `61FyM_3Lazg`，url 是本机外网入口）
- [ ] 6. 验证跨节点 verify：用本机新签的 JWT 调对端 `/api/whoami`
- [ ] 7. **不要**在本机 `etc/peers.toml` 加本机（避免双边 fan-in 回环）
- [ ] 8. 建议 secret 轮换：`openssl rand -hex 32`，双方替换；轮换后重签所有 token

---

**维护者备注**：上面 §1.1 的 secret 已在会话中明文出现，按泄露处理——完成对接后请双方同步替换。
