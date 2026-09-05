"""API 请求/响应模型（Pydantic）。

集中放在一处便于 schema 复查；每个路由文件按需 import。
"""
from pydantic import BaseModel, ConfigDict, Field


class RootEntry(BaseModel):
    """根智能体条目（[[roots]] 出生交割）：address/token 齐备才会被内核交割。"""
    address: str = Field(min_length=1, max_length=512, description="根智能体（目录服务）地址")
    token: str = Field(min_length=1, max_length=512, description="根签发的访问 token（或 env:变量名）")


class CreateAgentReq(BaseModel):
    name: str = Field("", max_length=64, description="显示名（可选：留空回落为 id agent-xxxx——"
                                                   "名字归 agent 自己，改参接口可改；id 一律 agent-xxxx）")
    mission: str = Field(min_length=1, description="长期使命（创建时渲染进 config.toml，此后由 agent 自治）")
    brains: list[str] = Field(min_length=1, description="大脑列表（首个为默认；故障转移只在与默认同档"
                                                   "（tier 相同）的大脑之间按此顺序循环，economy 档供智能体"
                                                   " llm_call 按档调用，不参与主循环轮换）")
    expose: bool = Field(False, description="true=监听 0.0.0.0 直接对外；默认 127.0.0.1 仅本机")
    port: int | None = Field(None, description="指定端口（缺省自动分配，自 8602 起）")
    budgets: dict | None = Field(None, description="预算 {max_rounds}（v2.7.5+ 内核只认 [limits] max_rounds；"
                                                "更早内核另认 max_seconds/max_context_tokens，随 source_version 渲染）")
    roots: list[RootEntry] | None = Field(None, max_length=8,
                                          description="根智能体（可选，v2.7.12+ 内核：首次启动一次性交割到 "
                                                      "workspace/playbook/根智能体.json，此后死键）")
    extra_config: str = Field("", max_length=8000,
                              description="附加配置（可选·高级）：自由 TOML 原样追加进出生 config.toml 末尾"
                                          "（[capabilities] 等内核可选段）；落盘前整体校验，坏 TOML 拒绝创建")
    note: str = Field("", description="备注")
    source_version: str = Field("", description="xuseek-v2 版本号（GET /api/versions）。缺省 = 仓库最新版"
                                                "（每 agent 自带私有副本，可单独迁移）。私有副本创建后不可改")
    runtime: str | None = Field(None, description="运行时：systemd（默认，系统进程）或 docker（容器，"
                                                  "host 网络；需内核 ≥ v2.7.19 与本机 docker 环境）。"
                                                  "缺省取 [manager].default_runtime；创建后可切换（停止 → 改参 → 启动）")


class PatchAgentReq(BaseModel):
    """可改字段：簿记层（name/note）、进程层（expose，需重启）、
    大脑（brains——手术式重渲染 config.toml 的 [brain] + [brains.*] 段，
    下次呼吸生效，不重启）。

    port 创建后固定（agent 对外联络 = ip+port，改端口等于换地址）；mission/budgets
    在创建后归 agent 自治——额外字段放行（extra="allow"），由 patch_agent 给出
    友好 400。
    """
    model_config = ConfigDict(extra="allow")
    name: str | None = None
    note: str | None = None
    expose: bool | None = None
    brains: list[str] | None = Field(None, description="大脑列表（首个为默认；故障转移只在与默认同档"
                                                        "（tier 相同）的大脑之间按此顺序循环；下次呼吸生效，不重启）")
    runtime: str | None = Field(None, description="切换运行时（systemd/docker）：须先停止 agent，"
                                                  "切换后不自动启动（停止 → 改参 → 启动）")


class MailReq(BaseModel):
    text: str = Field(min_length=1)


class HostsPutReq(BaseModel):
    """远端机器清单整表替换（每条目 name/host/user 必填；字段白名单见
    remote.HOST_FIELDS——password 先明文，文件 600）。"""
    hosts: list[dict] = Field(description="[[host]] 数组整表")


class RemoteRestoreReq(BaseModel):
    """远端恢复：from_path = 控制端本机备份包路径（通常来自 /api/remote/backups）。"""
    host: str = Field(min_length=1, description="目标机器（清单 name）")
    from_path: str = Field(min_length=1, description="备份 tar.gz 的控制端本机路径")
    new_id: str | None = Field(None, description="恢复后用新 id（避免冲突）")
    port: int | None = Field(None, description="恢复后端口（默认自动分配）")
    overwrite: bool = Field(False, description="覆盖同名已存在 agent")


class BackupReq(BaseModel):
    reason: str = Field("manual", description="备份原因（manual/pre-modify/...）；写进 meta")


class RestoreReq(BaseModel):
    from_path: str | None = Field(None, description="备份 tar.gz 本机路径（CLI 用）")
    key: str | None = Field(None, description="备份 key（WebUI 用：从 LocalBackend 取，免下载）")
    new_id: str | None = Field(None, description="恢复后用新 id（避免冲突；WebUI 克隆自动生成 agent-xxxx）")
    port: int | None = Field(None, description="恢复后端口（默认自动分配；listen host 由注册表 expose 推导）")
    overwrite: bool = Field(False, description="覆盖同名已存在 agent")
    brains: list[str] | None = Field(None, description="覆盖备份 meta.brains；克隆对话框用，"
                                                  "让用户从 xusi 大脑池显式选，而不是沿用 meta")
    note: str | None = Field(None, description="覆盖备份 meta.note；克隆对话框用，"
                                           "自动写'从备份克隆于 YYYY-MM-DD'")


class PatchNodeReq(BaseModel):
    """改名（仅 name 可改；id/role 走 toml，API 改不了）。"""
    name: str = Field(min_length=1, max_length=64, description="新显示名")
