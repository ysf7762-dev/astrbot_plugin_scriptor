"""
AstrBot 框架契约静态检查测试

本测试模块用于验证 ScriptorPlugin 插件的架构是否符合 AstrBot 框架的隐式契约：
1. 命令代理方法必须有 @filter.command() 装饰器（框架只扫描主类）
2. 工具方法在 Mixin 中定义，通过 _rebind_mixin_tool_handlers() 重新绑定 handler
3. 事件代理方法必须有对应的装饰器（框架只扫描主类）
4. super() 调用的方法名必须正确

重要说明：
- AstrBot 框架在绑定工具 handler 时检查 handler.__module__ == plugin_module_path
- Mixin 类的方法来自不同的模块，导致绑定条件不满足
- 解决方案：在 __init__ 中调用 _rebind_mixin_tool_handlers() 手动重新绑定

这些测试不运行任何业务逻辑，纯粹使用 Python 的 inspect 机制进行静态扫描。
"""

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class MethodInfo:
    """方法信息"""

    name: str
    parameters: List[Tuple[str, Any, Any]]  # (name, annotation, default)
    has_command_decorator: bool = False
    has_llm_tool_decorator: bool = False
    has_event_decorator: bool = False
    decorator_value: Optional[str] = None
    is_async: bool = False
    is_generator: bool = False
    source_file: Optional[str] = None
    line_number: int = 0


@dataclass
class MixinInfo:
    """Mixin 类信息"""

    name: str
    commands: Dict[str, MethodInfo] = field(default_factory=dict)
    tools: Dict[str, MethodInfo] = field(default_factory=dict)
    events: Dict[str, MethodInfo] = field(default_factory=dict)


class AstrBotContractChecker:
    """AstrBot 框架契约检查器"""

    def __init__(self, plugin_root: Path):
        self.plugin_root = plugin_root
        self.main_py_path = plugin_root / "main.py"
        self.mixins_dir = plugin_root / "mixins"

        self.main_class_name = "ScriptorPlugin"
        self.mixin_names = [
            "HelpersMixin",
            "IdentityMixin",
            "MemoryMixin",
            "LearningMixin",
            "KnowledgeMixin",
            "EventsMixin",
            "ToolsMixin",
            "CommandsMixin",
        ]

        self._main_methods: Dict[str, MethodInfo] = {}
        self._mixin_methods: Dict[str, MixinInfo] = {}

    def analyze(self) -> Dict[str, Any]:
        """执行完整分析"""
        self._analyze_main_class()
        self._analyze_mixins()

        return {
            "command_decorator_check": self._check_command_decorators(),
            "tool_decorator_check": self._check_tool_decorators(),
            "event_decorator_check": self._check_event_decorators(),
            "super_call_check": self._check_super_calls(),
        }

    def _analyze_main_class(self):
        """分析主类中的方法"""
        source_code = self.main_py_path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == self.main_class_name:
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef):
                        method_info = self._extract_method_info(item, source_code)
                        self._main_methods[method_info.name] = method_info

    def _analyze_mixins(self):
        """分析所有 Mixin 类"""
        for mixin_file in self.mixins_dir.glob("*_mixin.py"):
            self._analyze_mixin_file(mixin_file)

    def _analyze_mixin_file(self, mixin_path: Path):
        """分析单个 Mixin 文件"""
        source_code = mixin_path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                mixin_info = MixinInfo(name=node.name)

                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef):
                        method_info = self._extract_method_info(item, source_code)
                        method_info.source_file = str(mixin_path)

                        if method_info.has_command_decorator:
                            mixin_info.commands[method_info.name] = method_info
                        elif method_info.has_llm_tool_decorator:
                            mixin_info.tools[method_info.name] = method_info
                        elif method_info.has_event_decorator:
                            mixin_info.events[method_info.name] = method_info

                self._mixin_methods[mixin_info.name] = mixin_info

    def _extract_method_info(self, func_node: ast.AsyncFunctionDef, source_code: str) -> MethodInfo:
        """从 AST 节点提取方法信息"""
        method_name = func_node.name

        parameters = []
        for arg in func_node.args.args:
            param_name = arg.arg
            annotation = self._get_annotation_string(arg.annotation)
            parameters.append((param_name, annotation, None))

        defaults = func_node.args.defaults
        for i, default in enumerate(defaults):
            idx = len(func_node.args.args) - len(defaults) + i
            if idx < len(parameters):
                param_name, annotation, _ = parameters[idx]
                default_value = self._get_default_value(default)
                parameters[idx] = (param_name, annotation, default_value)

        has_command = False
        has_llm_tool = False
        has_event = False
        decorator_value = None

        for decorator in func_node.decorator_list:
            dec_str = self._get_decorator_string(decorator, source_code)

            if "@filter.command" in dec_str:
                has_command = True
                match = re.search(r'@filter\.command\(["\']([^"\']+)["\']\)', dec_str)
                if match:
                    decorator_value = match.group(1)
            elif "@filter.llm_tool" in dec_str:
                has_llm_tool = True
            elif "@filter.event_message_type" in dec_str or "@filter.on_" in dec_str:
                has_event = True

        is_generator = False
        for node in ast.walk(func_node):
            if isinstance(node, ast.AsyncFor):
                if isinstance(node.iter, ast.Call):
                    if hasattr(node.iter, "func") and isinstance(node.iter.func, ast.Attribute):
                        if node.iter.func.attr == "super":
                            is_generator = True
            if isinstance(node, ast.Yield):
                is_generator = True

        return MethodInfo(
            name=method_name,
            parameters=parameters,
            has_command_decorator=has_command,
            has_llm_tool_decorator=has_llm_tool,
            has_event_decorator=has_event,
            decorator_value=decorator_value,
            is_async=True,
            is_generator=is_generator,
            line_number=func_node.lineno,
        )

    def _get_annotation_string(self, annotation) -> str:
        """获取类型注解字符串"""
        if annotation is None:
            return ""
        if isinstance(annotation, ast.Name):
            return annotation.id
        if isinstance(annotation, ast.Attribute):
            return f"{self._get_annotation_string(annotation.value)}.{annotation.attr}"
        if isinstance(annotation, ast.Subscript):
            return f"{self._get_annotation_string(annotation.value)}[...]"
        return ""

    def _get_default_value(self, default) -> str:
        """获取默认值字符串"""
        if isinstance(default, ast.Constant):
            return repr(default.value)
        if isinstance(default, ast.Name):
            return default.id
        if isinstance(default, ast.Attribute):
            return f"{default.value.id}.{default.attr}" if isinstance(default.value, ast.Name) else ""
        return ""

    def _get_decorator_string(self, decorator, source_code: str) -> str:
        """获取装饰器字符串"""
        if isinstance(decorator, ast.Call):
            return f"@{ast.unparse(decorator.func)}"
        if isinstance(decorator, ast.Attribute):
            return f"@{ast.unparse(decorator)}"
        return ""

    def _check_command_decorators(self) -> Dict[str, Any]:
        """检查命令代理方法的装饰器

        架构说明：
        - main.py 中的代理方法必须有 @filter.command() 装饰器
        - Mixin 中的方法只保留实现，不包含 @filter.command() 装饰器
          （这是为了避免指令冲突，同一个指令被注册两次）
        """
        errors = []
        warnings = []

        # 收集所有 Mixin 中的方法（通过扫描 Mixin 文件中的所有 async def）
        all_mixin_methods = set()
        for mixin_info in self._mixin_methods.values():
            # 将 CamelCase 转换为 snake_case: LearningMixin -> learning_mixin
            import re

            snake_name = re.sub(r"(?<!^)(?=[A-Z])", "_", mixin_info.name).lower()
            mixin_file = self.plugin_root / "mixins" / f"{snake_name}.py"
            if mixin_file.exists():
                source = mixin_file.read_text(encoding="utf-8")
                mixin_tree = ast.parse(source)
                for node in ast.walk(mixin_tree):
                    if isinstance(node, ast.AsyncFunctionDef):
                        if node.name.startswith("cmd_"):
                            all_mixin_methods.add(node.name)

        super_call_map = self._build_super_call_map()

        # 检查 main.py 中的命令代理方法（只检查有 @filter.command 装饰器的方法）
        for method_name, main_method in self._main_methods.items():
            if main_method.has_command_decorator:
                called_method = super_call_map.get(method_name)
                if called_method:
                    if called_method not in all_mixin_methods:
                        warnings.append(
                            f"main.py:{main_method.line_number} - 命令方法 '{method_name}' "
                            f"调用的 super().{called_method}() 不是有效的 Mixin 方法"
                        )
                else:
                    # 检查是否有直接的方法体（不是 super() 调用）
                    expected_mixin_name = f"cmd_{method_name}"
                    if expected_mixin_name not in all_mixin_methods:
                        matching_methods = [
                            m for m in all_mixin_methods if m.endswith(f"_{method_name}") or m == method_name
                        ]
                        if not matching_methods:
                            warnings.append(
                                f"main.py:{main_method.line_number} - 命令方法 '{method_name}' "
                                f"没有对应的 Mixin 实现 (期望: {expected_mixin_name})"
                            )

        # 检查 Mixin 中的 cmd_ 方法在 main.py 中有对应的代理方法
        for mixin_method_name in all_mixin_methods:
            found = False
            for proxy_name, called_name in super_call_map.items():
                if called_name == mixin_method_name:
                    found = True
                    # 只检查有 @filter.command 装饰器的代理方法
                    if proxy_name in self._main_methods:
                        if self._main_methods[proxy_name].has_command_decorator:
                            # 命令代理方法有 @filter.command，检查通过
                            pass
                        elif self._main_methods[proxy_name].has_event_decorator:
                            # 事件方法，不应该检查 @filter.command
                            pass
                        else:
                            # 既不是命令也不是事件，可能是错误
                            warnings.append(
                                f"main.py:{self._main_methods[proxy_name].line_number} - "
                                f"代理方法 '{proxy_name}' 既没有 @filter.command 也没有事件装饰器"
                            )
                    else:
                        errors.append(f"main.py - Mixin 方法 '{mixin_method_name}' 的代理方法 '{proxy_name}' 不存在")
                    break

            if not found:
                proxy_name = mixin_method_name.replace("cmd_", "")
                if proxy_name in self._main_methods:
                    if self._main_methods[proxy_name].has_command_decorator:
                        continue  # 有命令装饰器，检查通过
                    elif self._main_methods[proxy_name].has_event_decorator:
                        continue  # 有事件装饰器，也检查通过（可能是事件代理）

                errors.append(f"Mixin 方法 '{mixin_method_name}' 在 main.py 中缺少代理方法")

        return {
            "passed": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    def _build_super_call_map(self) -> Dict[str, str]:
        """构建方法名到 super() 调用方法名的映射"""
        result = {}

        source_code = self.main_py_path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                method_name = node.name

                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Attribute):
                            if isinstance(child.func.value, ast.Call):
                                if isinstance(child.func.value.func, ast.Name):
                                    if child.func.value.func.id == "super":
                                        called_method = child.func.attr
                                        result[method_name] = called_method
                                        break

        return result

    def _check_tool_decorators(self) -> Dict[str, Any]:
        """检查工具方法配置"""
        errors = []
        warnings = []

        all_mixin_tools = {}
        for mixin_info in self._mixin_methods.values():
            all_mixin_tools.update(mixin_info.tools)

        for method_name, main_method in self._main_methods.items():
            if main_method.has_llm_tool_decorator:
                errors.append(
                    f"main.py:{main_method.line_number} - "
                    f"方法 '{method_name}' 不应有 @filter.llm_tool() 装饰器 "
                    f"(框架会从 Mixin 中扫描，工具方法不需要代理)"
                )

        for method_name, main_method in list(self._main_methods.items()):
            if method_name in all_mixin_tools:
                warnings.append(
                    f"main.py:{main_method.line_number} - "
                    f"方法 '{method_name}' 与 Mixin 工具同名，会覆盖 Mixin 方法。"
                    f"建议删除此代理方法，让框架直接调用 Mixin 中的工具。"
                )

        return {
            "passed": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    def _check_event_decorators(self) -> Dict[str, Any]:
        """检查事件代理方法的装饰器"""
        errors = []
        warnings = []

        all_mixin_events = {}
        for mixin_info in self._mixin_methods.values():
            all_mixin_events.update(mixin_info.events)

        super_call_map = self._build_super_call_map()

        for mixin_method_name, mixin_method in all_mixin_events.items():
            found = False
            for proxy_name, called_name in super_call_map.items():
                if called_name == mixin_method_name:
                    found = True
                    if proxy_name not in self._main_methods:
                        errors.append(f"main.py - Mixin 事件 '{mixin_method_name}' 的代理方法 '{proxy_name}' 不存在")
                    elif not self._main_methods[proxy_name].has_event_decorator:
                        errors.append(
                            f"main.py:{self._main_methods[proxy_name].line_number} - "
                            f"代理方法 '{proxy_name}' 缺少事件装饰器 (如 @filter.event_message_type)"
                        )
                    break

            if not found:
                if mixin_method_name in self._main_methods:
                    if not self._main_methods[mixin_method_name].has_event_decorator:
                        errors.append(
                            f"main.py:{self._main_methods[mixin_method_name].line_number} - "
                            f"代理方法 '{mixin_method_name}' 缺少事件装饰器"
                        )

        return {
            "passed": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
        }

    def _check_signatures(self) -> Dict[str, Any]:
        """检查代理方法签名与 Mixin 一致"""
        errors = []

        all_mixin_methods = {}
        for mixin_info in self._mixin_methods.values():
            all_mixin_methods.update(mixin_info.commands)
            all_mixin_methods.update(mixin_info.tools)
            all_mixin_methods.update(mixin_info.events)

        for method_name, main_method in self._main_methods.items():
            expected_mixin_name = f"cmd_{method_name}"
            if expected_mixin_name not in all_mixin_methods:
                expected_mixin_name = method_name
            if expected_mixin_name not in all_mixin_methods:
                for m in all_mixin_methods:
                    if m.endswith(f"_{method_name}") or m == f"{method_name}_wrapper":
                        expected_mixin_name = m
                        break

            if expected_mixin_name in all_mixin_methods:
                mixin_method = all_mixin_methods[expected_mixin_name]

                main_params = [(n, a) for n, a, d in main_method.parameters if n != "self"]
                mixin_params = [(n, a) for n, a, d in mixin_method.parameters if n != "self"]

                if len(main_params) != len(mixin_params):
                    errors.append(
                        f"main.py:{main_method.line_number} - "
                        f"代理方法 '{method_name}' 参数数量 ({len(main_params)}) "
                        f"与 Mixin 方法 '{expected_mixin_name}' ({len(mixin_params)}) 不一致"
                    )

        return {
            "passed": len(errors) == 0,
            "errors": errors,
        }

    def _check_super_calls(self) -> Dict[str, Any]:
        """检查 super() 调用的方法名"""
        errors = []

        source_code = self.main_py_path.read_text(encoding="utf-8")
        tree = ast.parse(source_code)

        # 收集所有 Mixin 中的方法（通过扫描 Mixin 文件）
        all_mixin_methods = set()
        for mixin_info in self._mixin_methods.values():
            # 将 CamelCase 转换为 snake_case: LearningMixin -> learning_mixin
            import re

            snake_name = re.sub(r"(?<!^)(?=[A-Z])", "_", mixin_info.name).lower()
            mixin_file = self.plugin_root / "mixins" / f"{snake_name}.py"
            if mixin_file.exists():
                mixin_source = mixin_file.read_text(encoding="utf-8")
                mixin_tree = ast.parse(mixin_source)
                for node in ast.walk(mixin_tree):
                    if isinstance(node, ast.AsyncFunctionDef):
                        all_mixin_methods.add(node.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef):
                method_name = node.name

                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        if isinstance(child.func, ast.Attribute):
                            if isinstance(child.func.value, ast.Call):
                                if isinstance(child.func.value.func, ast.Name):
                                    if child.func.value.func.id == "super":
                                        called_method = child.func.attr

                                        expected_names = [
                                            f"cmd_{method_name}",
                                            method_name,
                                            f"{method_name}_wrapper",
                                        ]

                                        if (
                                            called_method not in expected_names
                                            and called_method not in all_mixin_methods
                                        ):
                                            errors.append(
                                                f"main.py:{node.lineno} - "
                                                f"方法 '{method_name}' 调用了 super().{called_method}()，"
                                                f"但这不是有效的 Mixin 方法名"
                                            )

        return {
            "passed": len(errors) == 0,
            "errors": errors,
        }


def test_command_decorators():
    """测试：所有命令代理方法必须有 @filter.command() 装饰器"""
    plugin_root = Path(__file__).parent.parent
    checker = AstrBotContractChecker(plugin_root)
    result = checker.analyze()

    check_result = result["command_decorator_check"]

    if not check_result["passed"]:
        error_msg = "\n".join(check_result["errors"])
        assert False, f"命令装饰器检查失败:\n{error_msg}"

    if check_result["warnings"]:
        print("\n警告:")
        for w in check_result["warnings"]:
            print(f"  - {w}")


def test_tool_decorators():
    """测试：工具方法不需要代理，且不应有冗余装饰器"""
    plugin_root = Path(__file__).parent.parent
    checker = AstrBotContractChecker(plugin_root)
    result = checker.analyze()

    check_result = result["tool_decorator_check"]

    if not check_result["passed"]:
        error_msg = "\n".join(check_result["errors"])
        assert False, f"工具装饰器检查失败:\n{error_msg}"

    if check_result["warnings"]:
        print("\n警告:")
        for w in check_result["warnings"]:
            print(f"  - {w}")


def test_event_decorators():
    """测试：所有事件代理方法必须有对应的事件装饰器"""
    plugin_root = Path(__file__).parent.parent
    checker = AstrBotContractChecker(plugin_root)
    result = checker.analyze()

    check_result = result["event_decorator_check"]

    if not check_result["passed"]:
        error_msg = "\n".join(check_result["errors"])
        assert False, f"事件装饰器检查失败:\n{error_msg}"


def test_super_calls():
    """测试：super() 调用的方法名必须正确"""
    plugin_root = Path(__file__).parent.parent
    checker = AstrBotContractChecker(plugin_root)
    result = checker.analyze()

    check_result = result["super_call_check"]

    if not check_result["passed"]:
        error_msg = "\n".join(check_result["errors"])
        assert False, f"super() 调用检查失败:\n{error_msg}"


def test_all_checks_summary():
    """测试：输出完整的检查报告"""
    plugin_root = Path(__file__).parent.parent
    checker = AstrBotContractChecker(plugin_root)
    result = checker.analyze()

    print("\n" + "=" * 60)
    print("AstrBot 框架契约静态检查报告")
    print("=" * 60)

    all_passed = True

    for check_name, check_result in result.items():
        status = "✅ 通过" if check_result["passed"] else "❌ 失败"
        print(f"\n{check_name}: {status}")

        if check_result.get("errors"):
            print("  错误:")
            for err in check_result["errors"]:
                print(f"    - {err}")

        if check_result.get("warnings"):
            print("  警告:")
            for warn in check_result["warnings"]:
                print(f"    - {warn}")

        if not check_result["passed"]:
            all_passed = False

    print("\n" + "=" * 60)
    if all_passed:
        print("🎉 所有检查通过！插件架构符合 AstrBot 框架契约。")
    else:
        print("⚠️ 存在检查失败项，请修复上述问题。")
    print("=" * 60)

    assert all_passed, "部分检查未通过，请查看上述报告"


if __name__ == "__main__":
    test_all_checks_summary()
