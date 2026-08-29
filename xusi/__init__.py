"""墟司（xusi）—— 墟寻（xuseek-v2）智能体的管理面。

一个自洽目录管理多个自主体：创建/启停/暂停/删除，经**管理邮箱**投信/收信，
维护 agent 名录与互联标注（agent 自发行互联 token，经邮箱发布，xusi 只做
公告板——见 mailroom.py）。

与 agent 之间**只有一条通道**：管理邮箱（投信 mailbox.jsonl / 收信 outbox.jsonl）。
其余界面全部取消：不 import xuseek 代码、不调 xuseek CLI、不 HTTP 观察
（/v1/*）、不反代（/px /svc）、不改写 config.toml（仅创建时渲染一次出生配置）、
不代签任何 token。systemd 进程/信号是宿主职责，不算通信。
"""
__version__ = "2.0.0"
