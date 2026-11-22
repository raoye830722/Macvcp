# Macvcp

为 macOS（Apple Silicon）用户打造的 VCP 改造与适配仓库。

## 上游项目（True Upstream）

VCP（Variable & Command Protocol）——「给 AI 一个持续存在的世界」的 AI 中间层系统。

| 仓库 | 地址 | 作者 |
|------|------|------|
| VCPToolBox（后端） | https://github.com/lioensky/VCPToolBox | lioensky |
| VCPChat（前端） | https://github.com/lioensky/VCPChat | lioensky |

本仓库全部能力源自上述上游项目，请前往上游获取完整文档与原始支持。

## 本仓库的定位

帮助 macOS 用户实现 VCP 的 macOS 改造，包括但不限于：

- **插件适配**：manifest 入口 python → python3、darwin-arm64 二进制补齐（CodeSearcher / ProjectForge AST 索引器等）
- **执行器改造**：PowerShellExecutor 的 zsh / osascript 深度适配、MacPowerShell（真 pwsh 7）通道
- **本地搜索**：以 Spotlight（mdfind）替代 Windows Everything 实现本地文件秒搜
- **路径与安全**：FileOperator 白名单 macOS 路径化
- **桌面操控**：ScreenPilot 的 darwin 通道（osascript / AppleScript）
- **语音克隆**：gsv.cpp 引擎 Vulkan→Metal 编译适配 + VCPChat 协议适配层

> ⚠️ 本仓库为 macOS 改造版，可能与上游存在行为差异；生产使用请以上游为准。

## 仓库结构

| 文件夹 | 内容 | 来源 |
|--------|------|------|
| `macpowershell/` | 真 pwsh 7.6.3 语义执行器 | VCPChat 插件（已回流上游 PR #356） |
| `vcpeverything/` | mdfind 替代 Everything 的本地秒搜 | VCPChat 插件 LocalSearchController |
| `screenpilot/` | Computer Use 引擎 + darwin 四通道 | VCPChat 插件 ScreenPilot |
| `gsv-voice/` | 语音克隆 Metal 补丁 + 协议适配层 | KakaruHayate/gsv.cpp + VCPChat gsv-service |
