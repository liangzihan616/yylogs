# 打包与分发（仅说明）

本仓库不含源码与构建脚本，仅说明分发形态：

- **安装包**：`YYLOGS_Setup_<version>_x64.exe`，内置运行依赖，目标机器无需安装 Python/Qt；未签名，Windows 可能提示未知发布者。
- **单文件 EXE**：`yylogs_<version>.exe`，用于本机快速启动；其采集能力依赖本机开发环境，**不建议单独分发**。
- **数据目录**：`%LOCALAPPDATA%/YYLOGS`，升级与卸载默认保留用户记录。
