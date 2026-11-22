# MacPowerShell — macOS 真 PowerShell 7 执行器

VCPChat 分布式插件，macOS 原生 PowerShell 通道。

## 上游
- 来源：VCPChat `VCPDistributedServer/Plugin/MacPowerShell`
- 已通过 PR #356 回流上游 lioensky/VCPChat

## macOS 改造点
- 底层 spawn 本机 pwsh 7.6.3（`brew install powershell`），非 bash 伪装
- 对象管道与 PS 语法完整可用
- errors / terminatingError / truncated 三字段真值回传（bash 版恒 0/false）
- requireAdmin 经 osascript 提权弹窗执行

## 使用
放入 VCPChat `VCPDistributedServer/Plugin/` 重启生效。config.env 为禁词/权限行为配置，无凭证。
