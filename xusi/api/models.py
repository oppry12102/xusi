"""API 请求/响应模型（Pydantic）。

集中放在一处便于 schema 复查；每个路由文件按需 import。
"""
from pydantic import BaseModel, Field


class CreateAgentReq(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="显示名（生成 id 用）")
    mission: str = Field(min_length=1, description="长期使命")
    brains: list[str] = Field(min_length=1, description="大脑列表（首个为默认，顺序=故障转移序）")
    expose: bool = Field(False, description="true=监听 0.0.0.0 直接对外；默认 127.0.0.1 仅经反代")
    port: int | None = Field(None, description="指定端口（缺省自动分配，自 8601 起）")
    budgets: dict | None = Field(None, description="预算 {max_rounds, max_seconds, max_context_tokens}")
    note: str = Field("", description="备注")
    source_version: str = Field("", description="xuseek-v2 版本号（GET /api/versions）。缺省 = 仓库最新版"
                                                "（每 agent 自带私有副本，可单独迁移）；'main' = 共享主源码"
                                                "（保留值，过渡期后废弃）。私有副本创建后不可改")


class PatchAgentReq(BaseModel):
    name: str | None = None
    mission: str | None = None
    brains: list[str] | None = None
    budgets: dict | None = None
    expose: bool | None = None
    port: int | None = None
    note: str | None = None


class MailReq(BaseModel):
    text: str = Field(min_length=1)


class TokenNewReq(BaseModel):
    """agent 观察台 token 签发（管理面 token 走 [cluster].secret，没有独立模型）。"""
    label: str = ""


class ApiTokenNewReq(BaseModel):
    """反代入口凭证签发（api token）：admin-only；明文只在本次响应里返回一次。"""
    label: str = Field("", max_length=64, description="给人看的备注（哪个服务/客户端在用）")


class BackupReq(BaseModel):
    reason: str = Field("manual", description="备份原因（manual/pre-modify/...）；写进 meta")


class RestoreReq(BaseModel):
    from_path: str | None = Field(None, description="备份 tar.gz 本机路径（CLI 用）")
    key: str | None = Field(None, description="备份 key（WebUI 用：从 LocalBackend 取，免下载）")
    new_id: str | None = Field(None, description="恢复后用新 id（避免冲突）")
    port: int | None = Field(None, description="恢复后端口（默认自动分配）")
    host: str = Field("127.0.0.1", description="监听 host")
    overwrite: bool = Field(False, description="覆盖同名已存在 agent")
    brains: list[str] | None = Field(None, description="覆盖备份 meta.brains；克隆对话框用，"
                                                  "让用户从 xusi 大脑池显式选，而不是沿用 meta")
    note: str | None = Field(None, description="覆盖备份 meta.note；克隆对话框用，"
                                           "自动写'从备份克隆于 YYYY-MM-DD'")


class PatchNodeReq(BaseModel):
    """改名（仅 name 可改；id/role 走 toml，API 改不了）。"""
    name: str = Field(min_length=1, max_length=64, description="新显示名")


class AddPeerReq(BaseModel):
    """注册一个 peer；server 会立即探活 {peer.url}/api/peer/id 拿 id。

    集群互信前提：双方 [cluster].secret 一致——admin 自己负责把这个值同步到
    两端的 etc/xusi.toml，再来加 peer。"""
    url: str = Field(min_length=1, description="peer 管理面 url（如 http://10.0.16.15:8601）")
    name: str = Field("", description="显示名（缺省用 peer 自报）")


class AnnouncePeerReq(BaseModel):
    """集群内自收敛：另一台 xusi 加完 peer 后 fire-and-forget 通告过来。
    接收端 idempotent 入册（id 命中 + url 一致 = 跳过；id 命中 url 冲突 = 保留本地；
    id 未见 = 入册）。"""
    id: str = Field(min_length=1)
    url: str = Field(min_length=1)
    name: str = Field("")


class WelcomePeersReq(BaseModel):
    """迎新包：通告方把自己的全表（除 self 与新人）发给新人做一次性合并。

    与 announce 配对：announce 单条告诉别人"新人 X 来了"，welcome 是把
    当前全表塞给 X 让它自己 idempotent 合并。两次合并后集群对称。"""
    from_id: str = Field(min_length=1, description="通告方自己的 node_id（仅日志用）")
    peers: list[dict] = Field(default_factory=list,
                              description="[{id, url, name}, ...]；除 self 与被迎新者")
