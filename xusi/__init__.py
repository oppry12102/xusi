"""墟司（xusi）—— 墟寻（xuseek-v2）智能体的管理面。

一个自洽目录管理多个自主体：创建/启停/暂停/改参/观察/删除，签发 token，
单一对外端口反代所有 agent 的观察台。

与 agent 之间只有三条通道，绝不 import xuseek 代码：
进程与信号（systemd）、只读 HTTP GET、文件（config.toml / mailbox / token 文件）。
"""
__version__ = "1.2.0"
