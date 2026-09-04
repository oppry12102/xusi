"""墟司（xusi）—— 墟寻（xuseek-v2）智能体的管理面。

一个自洽目录管理多个自主体：创建/启停/暂停/删除，经**管理邮箱**投信/收信。
彻底本地化管理：互联由 xuseek 内核自己完成（根智能体 + [[roots]] 出生交割，
见内核 docs/interconnect.md），xusi 不参与、不设公告板。

与 agent 之间**只有一条写通道**：管理邮箱（投信 mailbox.jsonl / 收信 outbox.jsonl）。
其余界面：不 import xuseek 代码、不调 xuseek CLI、不反代（/px /svc）、
不改写 config.toml（仅创建时渲染一次出生配置）；HTTP 观察仅两条只读 GET
（/v1/events、/v1/status，详情页用；观察 token 缺失时自动签发一枚写进
data/webui_tokens.json），会话索引读磁盘 sessions.jsonl。systemd 进程/信号
是宿主职责，不算通信。
"""
__version__ = "2.3.0"
