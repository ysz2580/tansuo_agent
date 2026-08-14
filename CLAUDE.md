# 项目约定

## Git 推送策略

本仓库配置了代理 `http.proxy = http://127.0.0.1:7897`（仓库级配置，勿删）。
注意 `.git/config` 里 `[http]` 与 `[https]` **各有一条** proxy，两条都要清。
代理软件时而上游断连，表现为 `schannel: failed to receive handshake,
SSL/TLS connection failed`。因此推送时**两条路都要试**：

1. 先按仓库配置走代理：`git push`
2. 失败则直连兜底（一次性覆盖，不改仓库配置）：
   `git -c http.proxy= -c https.proxy= push`

不要在失败时反复重试同一条路超过 2 次；先换另一条路，再考虑诊断
（`Test-NetConnection 127.0.0.1 -Port 7897` 查代理端口，
`Invoke-WebRequest https://github.com -Proxy ...` 查代理上游，
`curl.exe -v --connect-timeout 10 https://github.com` 查直连 TCP 层）。
