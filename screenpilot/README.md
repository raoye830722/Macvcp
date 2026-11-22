# ScreenPilot — Computer Use 引擎（含 darwin 适配）

面向分布式设备的常驻 Computer Use 执行引擎，本地版含深度 macOS darwin 通道。

## 上游
- 来源：VCPChat `VCPDistributedServer/Plugin/ScreenPilot`
- 上游原版面向 Windows（win32 UIA / 截屏 / 窗口管理）

## macOS 改造点（约 2074 行 darwin 适配）
- darwin_commands.py：指令层 osascript / AppleScript 通道
- uia_darwin.py：macOS Accessibility 自动化树
- host_darwin.py：截屏与窗口感知 darwin 实现
- windows_darwin.py：窗口管理 darwin 通道

## 使用
放入 VCPChat `VCPDistributedServer/Plugin/` 重启生效。依赖：系统 Python 3 + pyobjc，Accessibility 权限需系统设置授权。config.env 为行为配置（VISION_API_KEY 留空即不启用视觉模型），无凭证。
