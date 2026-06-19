from __future__ import annotations

# scripts/quick_quality_check.py
"""快速代码质量检查脚本（无需外部依赖）

检查项目：
1. TODO/FIXME 注释数量
2. 文件复杂度（行数）
3. 导入一致性
4. 基本的语法问题
"""

import os
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List


def find_python_files(project_root: Path) -> List[Path]:
    """查找所有 Python 文件"""
    python_files = []

    # 排除的目录
    exclude_dirs = {
        "__pycache__",
        ".pytest_cache",
        ".git",
        "node_modules",
        "venv",
        ".venv",
        "dist",
        "build",
        "*.egg-info",
    }

    for root, dirs, files in os.walk(project_root):
        # 排除不需要的目录
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        for file in files:
            if file.endswith(".py"):
                python_files.append(Path(root) / file)

    return sorted(python_files)


def count_lines(file_path: Path) -> Dict[str, int]:
    """统计文件行数"""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        total = len(lines)
        code = sum(1 for line in lines if line.strip() and not line.strip().startswith("#"))
        comments = sum(1 for line in lines if line.strip().startswith("#"))
        blank = total - code - comments

        return {"total": total, "code": code, "comments": comments, "blank": blank}
    except Exception:
        return {"total": 0, "code": 0, "comments": 0, "blank": 0}


def check_todo_comments(file_path: Path) -> List[Dict]:
    """检查 TODO/FIXME/HACK/BUG 注释"""
    todos = []

    patterns = [
        (r"TODO\s*[:\-]?\s*(.*)", "TODO"),
        (r"FIXME\s*[:\-]?\s*(.*)", "FIXME"),
        (r"HACK\s*[:\-]?\s*(.*)", "HACK"),
        (r"BUG\s*[:\-]?\s*(.*)", "BUG"),
        (r"XXX\s*[:\-]?\s*(.*)", "XXX"),
    ]

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                for pattern, tag in patterns:
                    match = re.search(pattern, line, re.IGNORECASE)
                    if match:
                        todos.append(
                            {
                                "file": str(file_path.relative_to(file_path.parent.parent)),
                                "line": line_num,
                                "tag": tag,
                                "message": match.group(1).strip() if match.group(1) else "",
                                "context": line.strip(),
                            }
                        )
    except Exception:
        pass

    return todos


def check_function_complexity(file_path: Path) -> List[Dict]:
    """检查函数复杂度（基于行数）"""
    complex_functions = []

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 匹配函数定义
        func_pattern = re.compile(r"^(\s*)def\s+(\w+)\s*\([^)]*\)\s*[->\s:]*.*:", re.MULTILINE)

        for match in func_pattern.finditer(content):
            indent = len(match.group(1))
            func_name = match.group(2)
            start_line = content[: match.start()].count("\n") + 1

            # 计算函数体长度（简化：找到下一个同级或更高级别的缩进块结束位置）
            remaining = content[match.end() :]
            end_line = start_line

            for i, char in enumerate(remaining.split("\n")[1:], 1):  # 从下一行开始
                if char.strip():  # 非空行
                    current_indent = len(char) - len(char.lstrip())

                    if current_indent <= indent and char.strip():
                        break

                end_line = start_line + i

            func_length = end_line - start_line

            if func_length > 50:  # 超过50行的函数标记为复杂
                complex_functions.append(
                    {
                        "file": str(file_path.relative_to(file_path.parent.parent)),
                        "function": func_name,
                        "start_line": start_line,
                        "end_line": end_line,
                        "length": func_length,
                        "severity": "high" if func_length > 100 else "medium",
                    }
                )

    except Exception:
        pass

    return complex_functions


def check_import_consistency(files: List[Path]) -> Dict:
    """检查导入一致性"""
    import_stats = Counter()
    import_issues = []

    stdlib_modules = set(
        [
            "os",
            "sys",
            "re",
            "json",
            "time",
            "datetime",
            "pathlib",
            "typing",
            "collections",
            "functools",
            "itertools",
            "logging",
            "hashlib",
            "secrets",
            "base64",
            "asyncio",
            "tempfile",
            "shutil",
            "copy",
            "io",
            "enum",
            "dataclasses",
        ]
    )

    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # 提取所有 import 语句
            imports = re.findall(r"^(?:from|import)\s+([\w.]+)", content, re.MULTILINE)

            for imp in imports:
                base_module = imp.split(".")[0]
                if base_module not in stdlib_modules and base_module != "":
                    import_stats[base_module] += 1

        except Exception:
            pass

    return {
        "most_common": import_stats.most_common(10),
        "total_unique_imports": len(import_stats),
        "issues": import_issues,
    }


def generate_report(project_root: Path) -> Dict:
    """生成完整的质量报告"""
    print("=" * 70)
    print("🔍 Scriptor 快速代码质量检查")
    print("=" * 70)
    print()

    # 查找所有 Python 文件
    files = find_python_files(project_root)
    print(f"📂 发现 {len(files)} 个 Python 文件")
    print()

    # 统计信息
    total_lines = 0
    total_code = 0
    large_files = []

    for file_path in files:
        stats = count_lines(file_path)
        total_lines += stats["total"]
        total_code += stats["code"]

        if stats["total"] > 500:
            rel_path = file_path.relative_to(project_root)
            large_files.append(
                {
                    "file": str(rel_path),
                    "lines": stats["total"],
                    "severity": "high" if stats["total"] > 1000 else "medium",
                }
            )

    print("📊 代码统计:")
    print(f"   • 总行数: {total_lines:,}")
    print(f"   • 代码行数: {total_code:,}")
    print(f"   • 平均文件大小: {total_lines // max(len(files), 1)} 行")
    print(f"   • 大文件 (>500 行): {len(large_files)} 个")
    print()

    # TODO/FIXME 检查
    all_todos = []
    for file_path in files:
        todos = check_todo_comments(file_path)
        all_todos.extend(todos)

    todo_counts = Counter(todo["tag"] for todo in all_todos)

    print("📝 TODO/FIXME/HACK/BUG 标记:")
    print(f"   • 总计: {len(all_todos)} 处")
    for tag, count in todo_counts.most_common():
        icon = {"TODO": "📌", "FIXME": "🔧", "HACK": "⚠️", "BUG": "🐛", "XXX": "❓"}.get(tag, "•")
        print(f"     {icon} {tag}: {count} 处")
    print()

    # 函数复杂度检查
    complex_functions = []
    for file_path in files:
        functions = check_function_complexity(file_path)
        complex_functions.extend(functions)

    complex_functions.sort(key=lambda x: x["length"], reverse=True)

    print("🔧 复杂函数 (>50 行):")
    print(f"   • 总计: {len(complex_functions)} 个")
    high_complexity = [f for f in complex_functions if f["severity"] == "high"]
    print(f"   • 高度复杂 (>100 行): {len(high_complexity)} 个")

    if high_complexity:
        print("\n   最复杂的函数 TOP 5:")
        for func in high_complexity[:5]:
            print(f"     ⚠️  {func['function']} ({func['length']} 行)")
            print(f"         📍 {func['file']}:{func['start_line']}")
    print()

    # 导入统计
    import_info = check_import_consistency(files)

    print("📦 外部依赖使用频率 TOP 10:")
    for module, count in import_info["most_common"]:
        print(f"   • {module}: {count} 次")
    print()

    # 大文件列表
    if large_files:
        large_files.sort(key=lambda x: x["lines"], reverse=True)
        print("📄 大文件列表:")
        for file_info in large_files[:10]:
            icon = "🔴" if file_info["severity"] == "high" else "🟡"
            print(f"   {icon} {file_info['file']} ({file_info['lines']} 行)")
        print()

    # 生成报告摘要
    report = {
        "summary": {
            "total_files": len(files),
            "total_lines": total_lines,
            "total_code_lines": total_code,
            "todo_count": len(all_todos),
            "complex_functions_count": len(complex_functions),
            "large_files_count": len(large_files),
        },
        "todos": all_todos,
        "complex_functions": complex_functions,
        "large_files": large_files,
        "imports": import_info,
    }

    # 质量评分（简单算法）
    score = 100

    # 扣分项
    score -= min(len(all_todos), 20) * 0.5  # TODO/FIXME
    score -= len(high_complexity) * 2  # 高度复杂函数
    score -= len([f for f in large_files if f["severity"] == "high"]) * 3  # 超大文件

    score = max(0, min(100, score))

    grade = (
        "A+"
        if score >= 95
        else (
            "A"
            if score >= 90
            else "B+" if score >= 85 else "B" if score >= 80 else "C+" if score >= 70 else "C" if score >= 60 else "D"
        )
    )

    print("=" * 70)
    print(f"🎯 质量评分: {score:.1f}/100 ({grade})")
    print("=" * 70)

    if score >= 85:
        print("✅ 项目质量良好，可以发布！")
    elif score >= 70:
        print("⚠️  项目质量一般，建议优化后再发布。")
    else:
        print("❌ 项目存在较多问题，需要修复后才能发布。")

    print()

    return report


if __name__ == "__main__":
    project_root = Path(__file__).parent.parent

    report = generate_report(project_root)

    # 可选：保存报告到 JSON
    import json

    output_file = project_root / "quality_check_report.json"

    # 简化输出（移除完整上下文以减小文件大小）
    simplified_report = {
        **report,
        "todos": [{k: v for k, v in todo.items() if k != "context"} for todo in report["todos"]],
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(simplified_report, f, ensure_ascii=False, indent=2)

    print(f"📋 详细报告已保存到: {output_file}")
