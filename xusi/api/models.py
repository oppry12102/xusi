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
    label: str = ""


class TokenMgrNewReq(BaseModel):
    """管理面 token 签发（仅 admin 可调）。

    rotate=True 时：先 revoke 同 role 的所有 PLAIN，再签发新的——用户层面始终只
    看见一把 active token；旧的被换掉就立刻作废。"""
    label: str = ""
    role: str = Field("user", description="admin 或 user")
    agents: list[str] | None = Field(None, description="user 范围（admin 无需）")
    rotate: bool = Field(False, description="签发前先 revoke 同 role 的所有 PLAIN")


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
    """注册一个 peer；server 会立即探活 {peer.url}/api/peer/id 拿 id。"""
    url: str = Field(min_length=1, description="peer 管理面 url（如 http://10.0.16.15:8601）")
    name: str = Field("", description="显示名（缺省用 peer 自报）")


class IssueInvitationReq(BaseModel):
    """签发一行引导脚本用的邀请 token（Phase 2 v1.1）。"""
    name: str = Field("", description="建议的新节点名（写入 [node].name 与 peer.name）")


class RedeemInvitationReq(BaseModel):
    """新机器装好后回调：消费 token + 注册到 issuer 的 peer 名册。"""
    token: str = Field(min_length=1, description="issue 时返回的 JWT（再次传入以确保只有持 token 者能 redeem）")
    url: str = Field(min_length=1, description="新机器自己的公开 URL（须 issuer 端可访问）")
