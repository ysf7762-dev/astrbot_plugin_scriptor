# Scriptor 项目开发指南

本文档总结了 AstrBot 插件 Scriptor（灵笔司书）开发过程中的核心规范和最佳实践。

## 项目概述

- **项目名称**: Scriptor（灵笔司书）
- **插件目录**: `astrbot_plugin_scriptor`
- **数据目录**: `data/plugin_data/astrbot_plugin_scriptor/`
- **配置文件**: `data/config/astrbot_plugin_scriptor_config.json`

## 项目结构

```
astrbot_plugin_scriptor/
├── main.py                    # 插件主入口，所有 @filter 装饰器在此定义
├── metadata.yaml              # 插件元数据
├── pyproject.toml             # Python 项目配置
├── CHANGELOG.md               # 版本更新日志
├── core/                      # 核心模块
│   ├── memory_manager.py      # 记忆管理（只处理私聊）
│   ├── group_manager.py       # 群组管理（处理群聊）
│   ├── identity_manager.py    # 身份管理
│   └── ...
├── mixins/                    # Mixin 模块
│   ├── events_mixin.py        # 事件处理（无 @filter 装饰器）
│   ├── tools_mixin.py         # 工具注册
│   └── ...
├── tools/                     # 工具实现
├── web/                       # Web UI
└── tests/                     # 测试用例
```

## 开发规范

### 1. Mixin 模块规范

- **禁止**在 Mixin 模块中使用 `@filter` 装饰器
- Mixin 只实现业务逻辑，装饰器统一在 `main.py` 中定义
- 原因：AstrBot 热重载机制基于 `handler_module_path`，Mixin 路径不匹配会导致残留

```python
# ❌ 错误：Mixin 中使用 @filter 装饰器
class EventsMixin:
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def global_recorder(self, event):
        ...

# ✅ 正确：只在 main.py 中使用 @filter 装饰器
class ScriptorPlugin(Star, EventsMixin, ...):
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def global_recorder(self, event):
        return await EventsMixin.global_recorder(self, event)
```

### 2. 数据目录规范

- 统一使用 `astrbot_plugin_scriptor` 作为目录名
- 不要使用 `self.name` 或 `metadata.yaml` 中的 `name` 字段
- 配置路径：`data/config/astrbot_plugin_scriptor_config.json`
- 数据路径：`data/plugin_data/astrbot_plugin_scriptor/`

```python
# ❌ 错误：使用 self.name
self.data_dir = StarTools.get_data_dir(self.name)

# ✅ 正确：显式指定插件目录名
self.data_dir = StarTools.get_data_dir("astrbot_plugin_scriptor")
```

### 3. 依赖管理规范

- 对于稳定的库，只设置版本下限（`>=x.y.z`）
- 避免设置版本上限（`<x.y.z`），除非有明确的兼容性问题
- 定期测试与最新版本的兼容性

```toml
# ❌ 错误：设置版本上限
pandas = ">=2.0.0,<3.0.0"

# ✅ 正确：只设置下限
pandas = ">=2.0.0"
```

### 4. 职责分离规范

- `memory_manager`: 只处理私聊记忆和日记
- `group_manager`: 只处理群聊记忆和日记
- 避免多个组件写入同一资源

### 5. 版本发布流程

```bash
# 1. 更新版本号
# - metadata.yaml
# - pyproject.toml
# - main.py (@register 装饰器)

# 2. 更新 CHANGELOG.md

# 3. 提交所有更改
git add .
git commit -m "chore: update version to x.y.z"
git push origin main

# 4. 创建并推送 tag
git tag vx.y.z
git push origin vx.y.z
```

### 6. Git 提交规范

- **所有 commit message 必须使用中文**（类型前缀如 `feat:`、`fix:`、`chore:` 可保留英文，描述部分用中文）
- **每完成一个最小模块的改动就必须提交一次**，不要积攒多个改动再一起提交
- 最小模块 = 一个独立的功能点或修复（新增一个组件、修复一个 bug、增强一个函数、添加一组测试）
- 每次提交应保持代码可编译、测试可通过

---

**最后更新**: 2026-06-19
**维护者**: ysf7762-dev
