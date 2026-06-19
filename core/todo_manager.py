from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from astrbot.api import logger


@dataclass
class TodoItem:
    """待办事项数据类

    扩展字段说明：
    - due_date: 任务截止时间（绝对时间）
    - reminder_time: 实际提醒时间（due_date - advance_minutes）
    - advance_minutes: 提前提醒分钟数（0 = 准点提醒）
    - cron_expression: 周期性任务的 Cron 表达式（空 = 一次性任务）
    - priority: 优先级 1(低)/2(中)/3(高)，默认 2
    - linked_reminder_id: 绑定的底层定时任务 ID，用于生命周期联动
    """

    id: int
    content: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    status: str = "pending"
    due_date: Optional[datetime] = None
    reminder_time: Optional[datetime] = None
    advance_minutes: int = 0
    cron_expression: str = ""
    priority: int = 2
    linked_reminder_id: str = ""

    def __post_init__(self):
        """确保数据兼容性：旧数据自动填充默认值"""
        if self.due_date is None:
            self.due_date = None
        if self.reminder_time is None:
            self.reminder_time = None
        if not self.cron_expression:
            self.cron_expression = ""
        if not self.linked_reminder_id:
            self.linked_reminder_id = ""

    def is_recurring(self) -> bool:
        """判断是否为周期性任务"""
        return bool(self.cron_expression)

    def is_overdue(self) -> bool:
        """判断是否已逾期"""
        if self.due_date and self.status == "pending":
            return datetime.now() > self.due_date
        return False

    def get_priority_label(self) -> str:
        """获取优先级标签"""
        labels = {1: "低", 2: "中", 3: "高"}
        return labels.get(self.priority, "中")


class TodoManager:
    """
    待办事项管理器

    核心职责：
    1. 解析结构化 Markdown TODO 文件
    2. 提供增删改查 (CRUD) 接口
    3. 维护排序规则（未完成正序，已完成倒序）
    4. 自动归档已完成项
    5. 支持时间属性、周期性任务、优先级
    """

    def __init__(self, data_dir: Path, scope: str = "personal"):
        self.data_dir = data_dir
        self.scope = scope
        self._next_id = 1
        self._cache: Optional[Dict[str, List[TodoItem]]] = None

    def _get_todo_file_path(self, uid: str) -> Path:
        """获取 TODO 主文件路径

        Args:
            uid: 用户 ID 或群组 ID

        Returns:
            文件路径
        """
        if self.scope == "personal":
            base_dir = self.data_dir / "profiles" / uid
            return base_dir / "P_TODO.md"
        else:
            group_id = uid.replace("group_", "")
            base_dir = self.data_dir / "groups" / group_id
            return base_dir / "G_TODO.md"

    def _get_archive_dir(self, uid: str) -> Path:
        """获取归档目录路径

        Args:
            uid: 用户 ID 或群组 ID

        Returns:
            归档目录路径
        """
        if self.scope == "personal":
            return self.data_dir / "profiles" / uid / "TODOed"
        else:
            group_id = uid.replace("group_", "")
            return self.data_dir / "groups" / group_id / "TODOed"

    def _get_archive_file_path(self, uid: str, year: int, month: int) -> Path:
        """获取按月的归档文件路径

        Args:
            uid: 用户 ID 或群组 ID
            year: 年份
            month: 月份

        Returns:
            归档文件路径（如 TODOed/P_TODO_2024-03.md）
        """
        archive_dir = self._get_archive_dir(uid)
        if self.scope == "personal":
            filename = f"P_TODO_{year}-{month:02d}.md"
        else:
            filename = f"G_TODO_{year}-{month:02d}.md"
        return archive_dir / filename

    def _parse_datetime(self, dt_str: Optional[str]) -> Optional[datetime]:
        """解析日期时间字符串"""
        if not dt_str:
            return None
        try:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            try:
                return datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                return None

    def _parse_todo_file(self, file_path: Path) -> Tuple[List[TodoItem], List[TodoItem]]:
        """解析 TODO Markdown 文件

        Returns:
            (pending_items, completed_items)
        """
        if not file_path.exists():
            return [], []

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning(f"[TodoManager] 读取 TODO 文件失败: {e}")
            return [], []

        pending_items = []
        completed_items = []
        current_id = 1

        lines = content.split("\n")
        current_section = None

        for line in lines:
            line = line.strip()

            if line.startswith("## 未完成"):
                current_section = "pending"
                continue
            elif line.startswith("## 已完成"):
                current_section = "completed"
                continue

            if current_section is None:
                continue

            match = re.match(r"^\s*-\s*\[(x| )\]\s*\[([^\]]+)\]\s*(.+?)(?:\s*\(完成于:\s*([^\)]+)\))?$", line)
            if not match:
                continue

            checkbox = match.group(1).strip()
            timestamp_str = match.group(2).strip() if match.group(2) else None
            content_part = match.group(3).strip() if match.group(3) else ""

            if not content_part:
                continue

            try:
                created_at = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                created_at = datetime.now()

            completed_at = None
            if match.group(4):
                try:
                    completed_at = datetime.strptime(match.group(4).strip(), "%Y-%m-%d %H:%M:%S")
                except (ValueError, TypeError):
                    completed_at = datetime.now()

            parsed_data = self._parse_extended_fields(content_part)

            item = TodoItem(
                id=current_id,
                content=parsed_data["content"],
                created_at=created_at,
                completed_at=completed_at,
                status="completed" if checkbox == "x" else "pending",
                due_date=parsed_data.get("due_date"),
                reminder_time=parsed_data.get("reminder_time"),
                advance_minutes=parsed_data.get("advance_minutes", 0),
                cron_expression=parsed_data.get("cron_expression", ""),
                priority=parsed_data.get("priority", 2),
                linked_reminder_id=parsed_data.get("linked_reminder_id", ""),
            )

            if checkbox == "x":
                completed_items.append(item)
            else:
                pending_items.append(item)

            current_id += 1

        return pending_items, completed_items

    def _parse_extended_fields(self, content: str) -> Dict[str, Any]:
        """解析扩展字段（从内容中提取元数据）

        格式示例：
        - [ ] [2024-01-01 10:00:00] 任务内容 | ⏰2024-01-02 14:00 | 🔔10min | 🔄daily | ⭐高 | 🔗abc123
        """
        result = {
            "content": content,
            "due_date": None,
            "reminder_time": None,
            "advance_minutes": 0,
            "cron_expression": "",
            "priority": 2,
            "linked_reminder_id": "",
        }

        parts = content.split("|")
        if len(parts) == 1:
            return result

        result["content"] = parts[0].strip()

        for part in parts[1:]:
            part = part.strip()

            if part.startswith("⏰") or part.startswith("截止:"):
                dt_str = part.replace("⏰", "").replace("截止:", "").strip()
                result["due_date"] = self._parse_datetime(dt_str)

            elif part.startswith("🔔") or part.startswith("提前:"):
                advance_str = part.replace("🔔", "").replace("提前:", "").strip()
                match = re.match(r"(\d+)\s*(min|分钟|h|小时)?", advance_str)
                if match:
                    amount = int(match.group(1))
                    unit = match.group(2) if match.group(2) else "min"
                    if unit in ("h", "小时"):
                        amount *= 60
                    result["advance_minutes"] = amount

            elif part.startswith("🔄") or part.startswith("周期:"):
                cron_str = part.replace("🔄", "").replace("周期:", "").strip()
                result["cron_expression"] = cron_str

            elif part.startswith("⭐") or part.startswith("优先级:"):
                priority_str = part.replace("⭐", "").replace("优先级:", "").strip()
                priority_map = {"高": 3, "中": 2, "低": 1, "high": 3, "medium": 2, "low": 1}
                result["priority"] = priority_map.get(priority_str, 2)

            elif part.startswith("🔗") or part.startswith("任务ID:"):
                result["linked_reminder_id"] = part.replace("🔗", "").replace("任务ID:", "").strip()

        if result["due_date"] and result["advance_minutes"] > 0:
            result["reminder_time"] = result["due_date"] - timedelta(minutes=result["advance_minutes"])
        elif result["due_date"]:
            result["reminder_time"] = result["due_date"]

        return result

    def _format_extended_fields(self, item: TodoItem) -> str:
        """格式化扩展字段为可读字符串"""
        parts = [item.content]

        if item.due_date:
            parts.append(f"⏰{item.due_date.strftime('%Y-%m-%d %H:%M')}")

        if item.advance_minutes > 0:
            if item.advance_minutes >= 60:
                hours = item.advance_minutes // 60
                parts.append(f"🔔{hours}h提前")
            else:
                parts.append(f"🔔{item.advance_minutes}min提前")

        if item.cron_expression:
            parts.append(f"🔄{item.cron_expression}")

        if item.priority != 2:
            priority_labels = {1: "低", 3: "高"}
            parts.append(f"⭐{priority_labels.get(item.priority, '中')}")

        if item.linked_reminder_id:
            parts.append(f"🔗{item.linked_reminder_id[:8]}")

        return " | ".join(parts)

    def _generate_markdown(self, pending_items: List[TodoItem], completed_items: List[TodoItem]) -> str:
        """生成结构化 Markdown 内容"""
        lines = ["# 个人待办清单" if self.scope == "personal" else "# 群组待办清单"]
        lines.append("")

        lines.append("## 未完成 (Pending)")
        lines.append("<!-- 待办事项将在此处显示 -->")
        lines.append("")

        def sort_key(item: TodoItem):
            priority_order = {3: 0, 2: 1, 1: 2}
            due_date_key = item.due_date.timestamp() if item.due_date else float("inf")
            return (priority_order.get(item.priority, 1), due_date_key, item.created_at)

        for item in sorted(pending_items, key=sort_key):
            timestamp = item.created_at.strftime("%Y-%m-%d %H:%M:%S")
            formatted_content = self._format_extended_fields(item)

            overdue_marker = " ⚠️逾期" if item.is_overdue() else ""
            recurring_marker = " 🔄" if item.is_recurring() else ""

            lines.append(f"- [ ] [{timestamp}] {formatted_content}{overdue_marker}{recurring_marker}")

        lines.append("")
        lines.append("## 已完成 (Completed)")
        lines.append("<!-- 已完成的待办事项将在此处显示 -->")
        lines.append("")

        for item in sorted(completed_items, key=lambda x: (x.completed_at or datetime.min), reverse=True):
            timestamp = item.created_at.strftime("%Y-%m-%d %H:%M:%S")
            completed_str = f" (完成于: {item.completed_at.strftime('%Y-%m-%d %H:%M:%S')})" if item.completed_at else ""
            lines.append(f"- [x] [{timestamp}] {item.content}{completed_str}")

        return "\n".join(lines)

    def get_hot_memory(self, uid: str) -> str:
        """获取热记忆内容（未完成 + 最近3天已完成）

        Returns:
            格式化的待办摘要文本
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        three_days_ago = datetime.now() - timedelta(days=3)
        recent_completed = [
            item for item in completed_items if item.completed_at and item.completed_at > three_days_ago
        ]

        lines = ["【当前待办状态】"]
        lines.append("")

        if pending_items:
            overdue_items = [item for item in pending_items if item.is_overdue()]
            high_priority = [item for item in pending_items if item.priority == 3 and not item.is_overdue()]
            normal_items = [item for item in pending_items if item.priority != 3 and not item.is_overdue()]

            if overdue_items:
                lines.append("**⚠️ 逾期任务：**")
                for item in overdue_items[:3]:
                    due_str = f"(截止: {item.due_date.strftime('%m-%d %H:%M')})" if item.due_date else ""
                    lines.append(f"  🔴 {item.content[:40]}{'...' if len(item.content) > 40 else ''} {due_str}")
                lines.append("")

            if high_priority:
                lines.append("**🔥 高优先级：**")
                for item in high_priority[:3]:
                    due_str = f"(截止: {item.due_date.strftime('%m-%d %H:%M')})" if item.due_date else ""
                    lines.append(f"  ⭐ {item.content[:40]}{'...' if len(item.content) > 40 else ''} {due_str}")
                lines.append("")

            if normal_items:
                lines.append("**📋 待办：**")
                for i, item in enumerate(normal_items[:5], 1):
                    due_str = f"(截止: {item.due_date.strftime('%m-%d %H:%M')})" if item.due_date else ""
                    recurring_str = " 🔄" if item.is_recurring() else ""
                    lines.append(
                        f"  {i}. {item.content[:40]}{'...' if len(item.content) > 40 else ''}{due_str}{recurring_str}"
                    )
        else:
            lines.append("**✅ 无未完成待办**")

        lines.append("")

        if recent_completed:
            lines.append("**✅ 最近完成（3天内）：**")
            for i, item in enumerate(sorted(recent_completed, key=lambda x: x.completed_at, reverse=True)[:3], 1):
                lines.append(f"  {i}. {item.content[:40]}{'...' if len(item.content) > 40 else ''}")
        else:
            lines.append("**无最近完成记录**")

        return "\n".join(lines)

    def get_overdue_items(self, uid: str) -> List[TodoItem]:
        """获取逾期的待办事项

        Args:
            uid: 用户 ID

        Returns:
            逾期的待办列表
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, _ = self._parse_todo_file(todo_file)
        return [item for item in pending_items if item.is_overdue()]

    def get_today_high_priority_items(self, uid: str) -> List[TodoItem]:
        """获取今日高优先级待办

        Args:
            uid: 用户 ID

        Returns:
            今日高优先级待办列表
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, _ = self._parse_todo_file(todo_file)

        today = datetime.now().date()
        result = []

        for item in pending_items:
            if item.priority == 3 and item.status == "pending":
                if item.due_date and item.due_date.date() == today:
                    result.append(item)

        return result

    def add_todo(
        self,
        uid: str,
        content: str,
        due_date: Optional[datetime] = None,
        advance_minutes: int = 0,
        cron_expression: str = "",
        priority: int = 2,
        linked_reminder_id: str = "",
    ) -> TodoItem:
        """添加新待办

        Args:
            uid: 用户 ID
            content: 待办内容
            due_date: 截止时间
            advance_minutes: 提前提醒分钟数
            cron_expression: 周期性任务 Cron 表达式
            priority: 优先级 1(低)/2(中)/3(高)
            linked_reminder_id: 关联的定时任务 ID

        Returns:
            新创建的 TodoItem
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        reminder_time = None
        if due_date:
            if advance_minutes > 0:
                reminder_time = due_date - timedelta(minutes=advance_minutes)
            else:
                reminder_time = due_date

        new_item = TodoItem(
            id=self._next_id,
            content=content,
            created_at=datetime.now(),
            completed_at=None,
            status="pending",
            due_date=due_date,
            reminder_time=reminder_time,
            advance_minutes=advance_minutes,
            cron_expression=cron_expression,
            priority=priority,
            linked_reminder_id=linked_reminder_id,
        )
        self._next_id += 1

        pending_items.append(new_item)
        markdown_content = self._generate_markdown(pending_items, completed_items)

        try:
            todo_file.write_text(markdown_content, encoding="utf-8")
            logger.info(f"[TodoManager] 已添加待办: {content[:30]}... (ID: {new_item.id})")

            self.archive_old_completed(uid)
        except Exception as e:
            logger.error(f"[TodoManager] 写入 TODO 文件失败: {e}")
            raise

        return new_item

    def complete_todo(self, uid: str, task_id: int, scheduler=None) -> Tuple[bool, Optional[TodoItem]]:
        """标记待办为已完成

        Args:
            uid: 用户 ID
            task_id: 待办 ID
            scheduler: 可选的 TaskScheduler 实例，用于联动删除定时任务

        Returns:
            (是否成功, 被完成的 TodoItem 或 None)
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        target_item = None
        for item in pending_items:
            if item.id == task_id:
                target_item = item
                break

        if not target_item:
            logger.warning(f"[TodoManager] 未找到待办 ID: {task_id}")
            return False, None

        if target_item.is_recurring():
            next_due_date = self._calculate_next_occurrence(target_item)
            if next_due_date:
                target_item.due_date = next_due_date
                if target_item.advance_minutes > 0:
                    target_item.reminder_time = next_due_date - timedelta(minutes=target_item.advance_minutes)
                else:
                    target_item.reminder_time = next_due_date

                if scheduler and target_item.linked_reminder_id:
                    try:
                        scheduler.remove_task(target_item.linked_reminder_id)
                        import time
                        import uuid

                        from ..core.scheduler import ScheduledTask

                        new_task = ScheduledTask(
                            task_id=str(uuid.uuid4()),
                            trigger_time=next_due_date.timestamp(),
                            content=f"TODO提醒: {target_item.content}",
                            task_type="once",
                            uid=uid,
                            group_id="" if self.scope == "personal" else uid,
                        )
                        scheduler.add_task(new_task)
                        target_item.linked_reminder_id = new_task.task_id
                        logger.info(f"[TodoManager] 周期任务已推延到: {next_due_date}")
                    except Exception as e:
                        logger.warning(f"[TodoManager] 更新周期任务定时器失败: {e}")

                markdown_content = self._generate_markdown(pending_items, completed_items)
                todo_file.write_text(markdown_content, encoding="utf-8")
                logger.info(f"[TodoManager] 周期任务已完成并推延: {target_item.content[:30]}...")
                return True, target_item
            else:
                logger.warning(f"[TodoManager] 无法计算周期任务的下次执行时间: {target_item.cron_expression}")

        if scheduler and target_item.linked_reminder_id:
            try:
                scheduler.remove_task(target_item.linked_reminder_id)
                logger.info(f"[TodoManager] 已删除关联的定时任务: {target_item.linked_reminder_id[:8]}")
            except Exception as e:
                logger.warning(f"[TodoManager] 删除关联定时任务失败: {e}")

        target_item.completed_at = datetime.now()
        target_item.status = "completed"

        pending_items = [item for item in pending_items if item.id != task_id]
        completed_items.insert(0, target_item)
        markdown_content = self._generate_markdown(pending_items, completed_items)

        try:
            todo_file.write_text(markdown_content, encoding="utf-8")
            logger.info(f"[TodoManager] 已完成待办: {target_item.content[:30]}... (ID: {task_id})")

            self.archive_old_completed(uid)
            return True, target_item
        except Exception as e:
            logger.error(f"[TodoManager] 写入 TODO 文件失败: {e}")
            return False, None

    def _calculate_next_occurrence(self, item: TodoItem) -> Optional[datetime]:
        """计算周期性任务的下次执行时间"""
        if not item.cron_expression or not item.due_date:
            return None

        try:
            from croniter import croniter

            base_time = datetime.now()
            cron = croniter(item.cron_expression, base_time)
            next_time = cron.get_next(datetime)
            return next_time
        except ImportError:
            logger.warning("[TodoManager] croniter 未安装，使用简单规则计算下次执行时间")
            return self._simple_next_occurrence(item)
        except Exception as e:
            logger.error(f"[TodoManager] 解析 Cron 表达式失败: {e}")
            return self._simple_next_occurrence(item)

    def _simple_next_occurrence(self, item: TodoItem) -> Optional[datetime]:
        """简单的下次执行时间计算（不依赖 croniter）"""
        cron = item.cron_expression.lower().strip()
        now = datetime.now()

        if cron in ("daily", "每天", "every day"):
            next_time = item.due_date + timedelta(days=1)
            while next_time <= now:
                next_time += timedelta(days=1)
            return next_time
        elif cron in ("weekly", "每周", "every week"):
            next_time = item.due_date + timedelta(weeks=1)
            while next_time <= now:
                next_time += timedelta(weeks=1)
            return next_time
        elif cron in ("monthly", "每月", "every month"):
            next_time = item.due_date + timedelta(days=30)
            while next_time <= now:
                next_time += timedelta(days=30)
            return next_time
        else:
            return item.due_date + timedelta(days=1)

    def update_todo(
        self,
        uid: str,
        task_id: int,
        new_content: str = None,
        due_date: Optional[datetime] = None,
        advance_minutes: int = None,
        cron_expression: str = None,
        priority: int = None,
        scheduler=None,
    ) -> Tuple[bool, Optional[TodoItem]]:
        """更新待办内容

        Args:
            uid: 用户 ID
            task_id: 待办 ID
            new_content: 新内容（可选）
            due_date: 新截止时间（可选）
            advance_minutes: 新提前提醒分钟数（可选）
            cron_expression: 新周期表达式（可选）
            priority: 新优先级（可选）
            scheduler: 可选的 TaskScheduler 实例

        Returns:
            (是否成功, 更新后的 TodoItem 或 None)
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        target_item = None
        for item in pending_items:
            if item.id == task_id:
                target_item = item
                break

        if not target_item:
            logger.warning(f"[TodoManager] 未找到待办 ID: {task_id}")
            return False, None

        old_linked_id = target_item.linked_reminder_id

        if new_content is not None:
            target_item.content = new_content
        if due_date is not None:
            target_item.due_date = due_date
            if advance_minutes is not None:
                target_item.advance_minutes = advance_minutes
            if target_item.advance_minutes > 0:
                target_item.reminder_time = due_date - timedelta(minutes=target_item.advance_minutes)
            else:
                target_item.reminder_time = due_date
        if advance_minutes is not None and due_date is None and target_item.due_date:
            target_item.advance_minutes = advance_minutes
            target_item.reminder_time = target_item.due_date - timedelta(minutes=advance_minutes)
        if cron_expression is not None:
            target_item.cron_expression = cron_expression
        if priority is not None:
            target_item.priority = priority

        if scheduler and old_linked_id:
            try:
                scheduler.remove_task(old_linked_id)
                logger.info(f"[TodoManager] 已删除旧的定时任务: {old_linked_id[:8]}")
            except Exception as e:
                logger.warning(f"[TodoManager] 删除旧定时任务失败: {e}")

        markdown_content = self._generate_markdown(pending_items, completed_items)

        try:
            todo_file.write_text(markdown_content, encoding="utf-8")
            logger.info(f"[TodoManager] 已更新待办: {target_item.content[:30]}... (ID: {task_id})")
            return True, target_item
        except Exception as e:
            logger.error(f"[TodoManager] 写入 TODO 文件失败: {e}")
            return False, None

    def delete_todo(self, uid: str, task_id: int, scheduler=None) -> Tuple[bool, Optional[TodoItem]]:
        """删除待办

        Args:
            uid: 用户 ID
            task_id: 待办 ID
            scheduler: 可选的 TaskScheduler 实例

        Returns:
            (是否成功, 被删除的 TodoItem 或 None)
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        target_item = None
        for item in pending_items:
            if item.id == task_id:
                target_item = item
                break

        if target_item and scheduler and target_item.linked_reminder_id:
            try:
                scheduler.remove_task(target_item.linked_reminder_id)
                logger.info(f"[TodoManager] 已删除关联的定时任务: {target_item.linked_reminder_id[:8]}")
            except Exception as e:
                logger.warning(f"[TodoManager] 删除关联定时任务失败: {e}")

        pending_items = [item for item in pending_items if item.id != task_id]
        completed_items = [item for item in completed_items if item.id != task_id]

        markdown_content = self._generate_markdown(pending_items, completed_items)

        try:
            todo_file.write_text(markdown_content, encoding="utf-8")
            logger.info(f"[TodoManager] 已删除待办 ID: {task_id}")
            return True, target_item
        except Exception as e:
            logger.error(f"[TodoManager] 写入 TODO 文件失败: {e}")
            return False, None

    def get_todo_by_id(self, uid: str, task_id: int) -> Optional[TodoItem]:
        """根据 ID 获取待办事项"""
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        for item in pending_items + completed_items:
            if item.id == task_id:
                return item
        return None

    def get_all_pending(self, uid: str) -> List[TodoItem]:
        """获取所有待办事项"""
        todo_file = self._get_todo_file_path(uid)
        pending_items, _ = self._parse_todo_file(todo_file)
        return pending_items

    def query_history(
        self,
        uid: str,
        time_range: Optional[str] = None,
        specific_date: Optional[str] = None,
        keyword: Optional[str] = None,
        status: str = "all",
        limit: int = 20,
    ) -> List[TodoItem]:
        """查询历史待办

        Args:
            uid: 用户 ID
            time_range: 时间范围，如 "2026-03" (某月), "2026" (某年)
            specific_date: 具体日期，如 "2026-03-05"
            keyword: 关键词搜索
            status: 状态过滤："completed", "pending", "all"
            limit: 返回条数限制

        Returns:
            匹配的待办列表
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        all_items = pending_items + completed_items
        filtered_items = []

        for item in all_items:
            if status == "pending" and item.status != "pending":
                continue
            if status == "completed" and item.status != "completed":
                continue

            if keyword and keyword.lower() not in item.content.lower():
                continue

            if specific_date:
                item_date = item.created_at.strftime("%Y-%m-%d")
                if item_date != specific_date:
                    continue
            elif time_range:
                if time_range == item.created_at.strftime("%Y-%m") or time_range == item.created_at.strftime("%Y"):
                    pass
                else:
                    continue

            filtered_items.append(item)

            if len(filtered_items) >= limit:
                break

        filtered_items.sort(key=lambda x: (x.completed_at or x.created_at), reverse=True)
        return filtered_items[:limit]

    def archive_old_completed(self, uid: str) -> int:
        """归档旧已完成项（按月切片归档）

        将完成时间超过 3 天的已完成项按月份分组，
        分别写入对应的归档文件（如 TODOed/P_TODO_2024-03.md）

        Returns:
            归档的条目数量
        """
        todo_file = self._get_todo_file_path(uid)
        pending_items, completed_items = self._parse_todo_file(todo_file)

        if not completed_items:
            return 0

        three_days_ago = datetime.now() - timedelta(days=3)
        to_archive = []
        remaining_completed = []

        for item in completed_items:
            if not item.completed_at:
                continue

            if item.completed_at < three_days_ago:
                to_archive.append(item)
            else:
                remaining_completed.append(item)

        if not to_archive:
            return 0

        from collections import defaultdict

        archive_by_month = defaultdict(list)
        for item in to_archive:
            if item.completed_at:
                year_month = (item.completed_at.year, item.completed_at.month)
                archive_by_month[year_month].append(item)

        total_archived = 0
        for (year, month), items in archive_by_month.items():
            archive_file = self._get_archive_file_path(uid, year, month)

            try:
                archive_dir = self._get_archive_dir(uid)
                if not archive_dir.exists():
                    archive_dir.mkdir(parents=True, exist_ok=True)

                existing_lines = []
                if archive_file.exists():
                    existing_content = archive_file.read_text(encoding="utf-8")
                    existing_lines = existing_content.split("\n")
                    if existing_lines and not existing_lines[-1]:
                        existing_lines = existing_lines[:-1]
                else:
                    archive_title = f"# {'个人' if self.scope == 'personal' else '群组'}待办归档 - {year}年{month}月"
                    existing_lines = [archive_title, "", "## 已完成（历史）", ""]

                for item in sorted(items, key=lambda x: x.completed_at or x.created_at):
                    timestamp = item.created_at.strftime("%Y-%m-%d %H:%M:%S")
                    completed_str = (
                        f" (完成于: {item.completed_at.strftime('%Y-%m-%d %H:%M:%S')})" if item.completed_at else ""
                    )
                    existing_lines.append(f"- [x] [{timestamp}] {item.content}{completed_str}")

                archive_file.write_text("\n".join(existing_lines) + "\n", encoding="utf-8")
                logger.info(f"[TodoManager] 已归档 {len(items)} 条待办到: {archive_file.name}")
                total_archived += len(items)
            except Exception as e:
                logger.error(f"[TodoManager] 归档到 {archive_file.name} 失败: {e}")

        markdown_content = self._generate_markdown(pending_items, remaining_completed)

        try:
            todo_file.write_text(markdown_content, encoding="utf-8")
            logger.info(f"[TodoManager] 主文件已更新，归档 {total_archived} 条")
            return total_archived
        except Exception as e:
            logger.error(f"[TodoManager] 更新主文件失败: {e}")
            return 0
