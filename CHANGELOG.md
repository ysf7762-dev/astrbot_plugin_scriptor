# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.6] - 2026-05-06

### Fixed
- 修复配置文件加载兼容性：优先读取 `astrbot_plugin_scriptor_config.json`，回退兼容旧版 `Scriptor_config.json`
- 修复私聊判断逻辑：使用 `event.is_private_chat()` 官方方法，兼容 NapCat 等适配器返回异常 group_id 的情况
- 修复 admin_uids 设置后无法识别的问题（根因为配置文件加载失败，非 admin_uids 本身解析问题）
- 修复 whoami 命令在私聊环境下显示错误的问题（依赖私聊判断修复）

## [1.0.5] - 2026-05-04

### Fixed
- 修复插件数据目录路径不一致问题：统一使用 `data/plugin_data/astrbot_plugin_scriptor/`
- 修复 `main.py` 中 `StarTools.get_data_dir(self.name)` 路径与 WebUI 硬编码路径不一致导致数据不互通的问题

## [1.0.4] - 2026-05-03

### Fixed
- 修复 Web UI 构建文档中的错误路径引用：将 `web-vue` 更正为 `web`
- 修复 `web/api.py` 中未构建前端时的错误提示路径
- 修复 `main.py` 启动日志中的构建命令提示路径
- 修复 `web/README.md` 中的目录名称和项目结构图

## [1.0.3] - 2026-04-29

### Fixed
- 修复指令冲突问题：移除 EventsMixin 中的 @filter 事件装饰器，统一在 main.py 中注册
- 修复插件热重载后 Mixin handler 残留导致的双重注册问题

### Changed
- EventsMixin 中的所有 @filter 装饰器（event_message_type、on_llm_request、on_llm_response、on_llm_tool_respond、on_using_llm_tool、on_decorating_result）已移除
- 事件钩子统一通过 main.py 中的代理方法注册，确保 handler_module_path 与插件主模块一致

## [1.0.2] - 2026-04-29

### Added
- TODO 清单增强：支持截止时间（due_date）、提醒时间（reminder_time）和关联提醒 ID（linked_reminder_id）
- 周期性任务支持：TODO 支持设置重复执行任务
- 配置宽容验证：embedding_provider 和 rerank_provider 无效值自动回退为默认值
- 配置同步机制：AstrBot 配置修改后自动同步到 WebUI 和 shared_state
- install.bat 虚拟环境自动检测：优先使用 AstrBot venv 中的 Python 和 pip

### Changed
- 命令注册中心化：移除 mixins 中的 @filter.command 装饰器，统一在 main.py 中注册
- 依赖版本策略：移除 openai、psutil 的版本上限，与 AstrBot 核心版本保持一致
- install.bat 编码优化：转换为纯 ASCII 格式，避免 Windows 编码问题
- .gitignore 优化：将 graphify-out/ 加入忽略列表，防止二进制文件提交

### Fixed
- 修复指令冲突问题（56 个重复命令）
- 修复 embedding_provider 校验失败导致插件永久加载失败的问题
- 修复 AstrBot 配置修改后 WebUI 不更新的问题
- 修复 install.bat 中文字符导致的乱码和命令执行失败问题

## [1.0.1] - 2026-04-28

### Changed
- 更新插件元数据版本号为 1.0.1
- 添加 metadata.yaml 文件以支持 AstrBot 插件市场版本检测

### Fixed
- 修复插件市场版本号不更新的问题
- 修复 Git 标签与 metadata.yaml 版本不一致的问题

## [1.0.0] - 2026-04-27

### Added
- 初始版本发布
- 基于 Scriptor 体系的多角色跨群体 AI 智能管家记忆系统
- 跨平台身份聚合
- 群体记忆管理
- 跨群信息传递
- 主动式记忆管理
- 文件即记忆
- 三层 Heartbeat 机制（全局/群聊/个人）
- 记忆分类学习、身份管理、长期记忆检索
