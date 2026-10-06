# Security Policy · 安全策略

## 报告漏洞

如果你发现安全问题（本地数据泄露、路径穿越、更新签名绕过、
任意代码执行等），请通过 GitHub
[Security Advisories](https://github.com/MetalRiver/zhizhang/security/advisories/new)
私密报告，不要在公开 issue 中披露细节。

## 范围

- 桌面应用与本地后端（serve.py / ledger.py / src-tauri）
- 更新通道与签名验证
- 本机数据文件权限

## 不属于本项目的安全边界

- 本项目**本地优先、无云端组件**；你机器上的数据安全由你的
  磁盘加密与账户安全决定。
- 第三方 AI 工具（Codex、WorkBuddy、Trae 等）自身产生的数据文件
  不在本项目攻击面内。

## 签名

更新包使用 minisign 签名，客户端内嵌公钥验证。签名验证失败的更新包
会被无条件拒绝安装。请勿绕过该机制。

## 加密通信

如需私下沟通且不适合使用 GitHub Advisory，可在 GitHub issue 中请求
建立安全联系渠道。
