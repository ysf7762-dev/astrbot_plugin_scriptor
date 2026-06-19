# core/archives/ingestor.py
"""
数据导入器 - 将文件导入到档案馆

功能：
1. 支持 Excel、CSV、TXT 格式
2. 自动检测编码和分隔符
3. 中文文件名处理（使用 hash）

注意：
- 此类负责数据导入逻辑
- 数据库路径由 ArchiveRouter 决定
"""

import hashlib
import os
import re
import sqlite3

import pandas as pd

try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

# 动态导入 ArchiveManager（支持子进程模式）
try:
    from .manager import ArchiveManager
except ImportError:
    # 子进程模式：使用绝对导入
    import importlib.util
    import sys
    from pathlib import Path

    plugin_dir = Path(__file__).parent.parent
    manager_file = plugin_dir / "archives" / "manager.py"

    if "manager" not in sys.modules:
        manager_spec = importlib.util.spec_from_file_location("manager", manager_file)
        manager_module = importlib.util.module_from_spec(manager_spec)
        sys.modules["manager"] = manager_module
        manager_spec.loader.exec_module(manager_module)

    ArchiveManager = sys.modules["manager"].ArchiveManager


class DataIngestor:
    """数据导入器"""

    def __init__(self, archive_manager: ArchiveManager = None, db_path: str = None):
        """
        初始化导入器

        Args:
            archive_manager: 档案管理器实例（可选）
            db_path: 数据库路径（可选，如果提供则创建 ArchiveManager）
        """
        if archive_manager:
            self.archive_manager = archive_manager
        elif db_path:
            self.archive_manager = ArchiveManager(db_path)
        else:
            raise ValueError("必须提供 archive_manager 或 db_path")

    @property
    def db_path(self) -> str:
        """获取当前数据库路径"""
        return self.archive_manager.db_path

    def _clean_table_name(self, name: str) -> str:
        """
        清理表名，确保符合 SQL 规范

        对于中文文件名，使用 hash 生成表名，避免大量下划线
        """
        has_chinese = any("\u4e00" <= c <= "\u9fff" for c in name)

        if has_chinese:
            name_hash = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
            clean_name = f"cn_{name_hash}"
        else:
            clean_name = re.sub(r"[^a-zA-Z0-9]", "_", name)
            clean_name = re.sub(r"_+", "_", clean_name).strip("_")
            if not clean_name:
                name_hash = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
                clean_name = name_hash

        return f"archive_{clean_name.lower()}"

    def _detect_delimiter(self, file_path: str, sample_lines: int = 10) -> str:
        """自动检测 TXT 文件的分隔符"""
        common_delimiters = ["\t", ",", ";", "|", " "]

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = [f.readline() for _ in range(sample_lines)]
        except Exception as e:
            logger.debug(f"[DataIngestor] 无法读取文件 {file_path}: {e}")
            return "\t"

        best_delimiter = "\t"
        best_score = 0

        for delim in common_delimiters:
            scores = []
            for line in lines:
                if line.strip():
                    count = line.count(delim)
                    scores.append(count)

            if scores:
                avg_score = sum(scores) / len(scores)
                consistency = min(scores) == max(scores) if len(scores) > 1 else True

                if consistency and avg_score > best_score:
                    best_score = avg_score
                    best_delimiter = delim

        return best_delimiter

    def _clean_numeric_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        清洗数值列，将带格式的数字字符串转换为数值类型

        例如：'21,000' -> 21000, '¥1,234.56' -> 1234.56
        """
        for col in df.columns:
            if df[col].dtype == object:
                sample = df[col].dropna().head(100)
                if len(sample) == 0:
                    continue

                numeric_pattern = r"^[¥￥$€£\s]*[\d,]+\.?\d*\s*$"
                numeric_count = sum(1 for v in sample if isinstance(v, str) and re.match(numeric_pattern, v.strip()))

                if numeric_count > len(sample) * 0.5:

                    def clean_number(val):
                        if val is None or not isinstance(val, str):
                            return val
                        cleaned = re.sub(r"[¥￥$€£,\s]", "", val.strip())
                        try:
                            if "." in cleaned:
                                return float(cleaned)
                            else:
                                return int(cleaned)
                        except ValueError:
                            return val

                    df[col] = df[col].apply(clean_number)

        return df

    def ingest_excel(
        self,
        file_path: str,
        sheet_name: str = None,
        display_name: str = None,
        description: str = "",
        delimiter: str = None,
        scope: str = "auto",
    ) -> tuple:
        """
        从 Excel、CSV 或 TXT 导入数据

        Args:
            file_path: 文件路径（支持 .xlsx, .xls, .csv, .txt）
            sheet_name: Excel 工作表名称（仅 Excel 有效，默认读取第一个工作表）
            display_name: 显示名称
            description: 描述
            delimiter: TXT/CSV 分隔符（None 自动检测）
            scope: 层级标记

        Returns:
            (table_name, row_count) 表名和行数
        """
        ext = os.path.splitext(file_path)[1].lower()

        if ext == ".txt":
            detected_delim = delimiter or self._detect_delimiter(file_path)

            df = None
            for encoding in ["utf-8", "gbk", "gb2312", "utf-8-sig", "latin-1"]:
                try:
                    df = pd.read_csv(file_path, sep=detected_delim, encoding=encoding)
                    if len(df.columns) > 1:
                        break
                except (UnicodeDecodeError, pd.errors.ParserError):
                    continue

            if df is None:
                df = pd.read_csv(file_path, sep=detected_delim, encoding="utf-8", errors="replace")

        elif ext == ".csv":
            df = None
            for encoding in ["utf-8", "gbk", "gb2312", "utf-8-sig"]:
                try:
                    df = pd.read_csv(file_path, encoding=encoding, sep=delimiter or ",")
                    break
                except UnicodeDecodeError:
                    continue

            if df is None:
                df = pd.read_csv(file_path, encoding="utf-8", errors="replace", sep=delimiter or ",")
        else:
            engine = "openpyxl" if ext == ".xlsx" else "xlrd"
            pandas_sheet = sheet_name if sheet_name else 0
            df = pd.read_excel(file_path, sheet_name=pandas_sheet, engine=engine)

        df = df.where(pd.notnull(df), None)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.loc[:, df.columns != ""]

        df = self._clean_numeric_columns(df)

        if df.empty or len(df.columns) == 0:
            raise ValueError("文件内容为空或无法解析")

        base_name = os.path.splitext(os.path.basename(file_path))[0]
        if sheet_name and ext not in [".csv", ".txt"]:
            base_name = f"{base_name}_{sheet_name}"
        table_name = self._clean_table_name(base_name)

        with sqlite3.connect(self.archive_manager.db_path) as conn:
            df.to_sql(table_name, conn, if_exists="replace", index=False)

        columns = {col: col for col in df.columns}
        self.archive_manager.register_table(
            table_name=table_name,
            display_name=display_name or base_name,
            columns=columns,
            row_count=len(df),
            description=description,
            scope=scope,
        )

        logger.info(f"[DataIngestor] 文件已导入: {file_path} -> {table_name} ({len(df)} 行)")
        return table_name, len(df)
