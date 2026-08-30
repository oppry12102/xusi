"""API 请求/响应模型（Pydantic）。

集中放在一处便于 schema 复查；每个路由文件按需 import。
"""
from pydantic import BaseModel, ConfigDict, Field


class CreateAgentReq(BaseModel):
    name: str = Field(min_length=1, max_length=64, description="显示名（生成 id 用）")
    mission: str = Field(min_length=1, description="长期使命（创建时渲染进 config.toml，此后由 agent 自治）")
    brains: list[str] = Field(min_length=1, description="大脑列表（首个为默认，顺序=故障转移序）")
    expose: bool = Field(False, description="true=监听 0.0.0.0 直接对外；默认 127.0.0.1 仅本机")
    port: int | None = Field(None, description="指定端口（缺省自动分配，自 8602 起）")
    budgets: dict | None = Field(None, description="预算 {max_rounds}（v2.7.5+ 内核只认 [limits] max_rounds；"
                                                "更早内核另认 max_seconds/max_context_tokens，随 source_version 渲染）")
    note: str = Field("", description="备注")
    source_version: str = Field("", description="xuseek-v2 版本号（GET /api/versions）。缺省 = 仓库最新版"
                                                "（每 agent 自带私有副本，可单独迁移）。私有副本创建后不可改")


class PatchAgentReq(BaseModel):
    """可改字段只有簿记层（name/note）与进程层（port/expose）。

    mission/brains/budgets 在创建后归 agent 自治——额外字段放行（extra="allow"），
    由 patch_agent 给出「请投信让 agent 自己修改 config.toml」的友好 400。
    """
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    note: str | None = None
    expose: bool | None = None
    port: int | None = None


class MailReq(BaseModel):
    text: str = Field(min_length=1)


class BackupReq(BaseModel):
    reason: str = Field("manual", description="备份原因（manual/pre-modify/...）；写进 meta")


class RestoreReq(BaseModel):
    from_path: str | None = Field(None, description="备份 tar.gz 本机路径（CLI 用）")
    key: str | None = Field(None, description="备份 key（WebUI 用：从 LocalBackend 取，免下载）")
    new_id: str | None = Field(None, description="恢复后用新 id（避免冲突）")
    port: int | None = Field(None, description="恢复后端口（默认自动分配；listen host 由注册表 expose 推导）")
    overwrite: bool = Field(False, description="覆盖同名已存在 agent")
    brains: list[str] | None = Field(None, description="覆盖备份 meta.brains；克隆对话框用，"
                                                  "让用户从 xusi 大脑池显式选，而不是沿用 meta")
    note: str | None = Field(None, description="覆盖备份 meta.note；克隆对话框用，"
                                           "自动写'从备份克隆于 YYYY-MM-DD'")


class PatchNodeReq(BaseModel):
    """改名（仅 name 可改；id/role 走 toml，API 改不了）。"""
    name: str = Field(min_length=1, max_length=64, description="新显示名")
