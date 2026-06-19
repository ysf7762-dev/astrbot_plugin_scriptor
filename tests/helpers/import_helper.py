# tests/helpers/import_helper.py
"""
测试导入辅助工具

解决测试环境中的复杂依赖链和相对导入问题
"""

import sys
import types
from pathlib import Path


def create_isolated_import(project_root: Path, module_name: str, file_path: Path):
    """
    创建隔离的模块导入，避免触发 __init__.py 中的相对导入

    Args:
        project_root: 项目根目录
        module_name: 模块名称（如 "core.media_manager"）
        file_path: 目标 .py 文件路径

    Returns:
        导入的模块对象
    """
    import importlib.util

    # 创建包命名空间（避免相对导入失败）
    parts = module_name.split(".")

    # 确保所有父包都存在
    current_path = project_root
    for i, part in enumerate(parts[:-1]):
        package_name = ".".join(parts[: i + 1])

        if package_name not in sys.modules:
            # 创建虚拟包
            pkg = types.ModuleType(package_name)
            pkg.__path__ = [str(current_path)]
            pkg.__package__ = package_name
            sys.modules[package_name] = pkg

        # 移动到子目录
        current_path = current_path / part

    # 导入目标模块
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {module_name} from {file_path}")

    module = importlib.util.module_from_spec(spec)

    # 设置包上下文
    parent_package = ".".join(parts[:-1])
    module.__package__ = parent_package
    module.__name__ = module_name

    # 执行模块代码
    try:
        spec.loader.exec_module(module)
    except ImportError as e:
        # 如果仍然遇到相对导入问题，提供更清晰的错误信息
        raise ImportError(
            f"Failed to import {module_name}: {e!s}. " f"This may be due to circular dependencies in core/__init__.py"
        ) from e

    return module


def safe_import_core_module(project_root: Path, relative_path: str, class_name: str = None):
    """
    安全地导入核心模块

    Args:
        project_root: 项目根目录
        relative_path: 相对于项目根的路径（如 "core/media_manager.py"）
        class_name: 要提取的类名（可选）

    Returns:
        模块或类
    """
    file_path = project_root / relative_path.replace("/", "\\")
    module_name = relative_path.replace("/", ".").replace(".py", "")

    module = create_isolated_import(project_root, module_name, file_path)

    if class_name:
        return getattr(module, class_name)
    return module
