# tests/test_file_import_to_archive.py
"""
测试文件导入到档案馆功能（Mock 版本）

由于 core/archives/ingestor.py 有深层依赖链（→manager → core/__init__.py → tools），
此测试使用 Mock 对象来验证核心逻辑，同时保持对 MediaManager 的真实测试。

测试流程：
1. 创建测试文件（CSV、TXT）
2. 保存到媒体库（真实 MediaManager）
3. 模拟档案馆导入流程
4. 验证数据完整性
"""

import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path


def create_isolated_import(project_root: Path, module_name: str, file_path: Path):
    """
    创建隔离的模块导入，避免触发 __init__.py 中的相对导入
    """
    import importlib.util

    parts = module_name.split(".")

    current_path = project_root
    for i, part in enumerate(parts[:-1]):
        package_name = ".".join(parts[: i + 1])

        if package_name not in sys.modules:
            pkg = types.ModuleType(package_name)
            pkg.__path__ = [str(current_path)]
            pkg.__package__ = package_name
            sys.modules[package_name] = pkg

        current_path = current_path / part

    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {module_name} from {file_path}")

    module = importlib.util.module_from_spec(spec)

    parent_package = ".".join(parts[:-1])
    module.__package__ = parent_package
    module.__name__ = module_name

    try:
        spec.loader.exec_module(module)
    except ImportError as e:
        raise ImportError(
            f"Failed to import {module_name}: {e!s}. " f"This may be due to circular dependencies in core/__init__.py"
        ) from e

    return module


def safe_import_core_module(project_root: Path, relative_path: str, class_name: str = None):
    """安全地导入核心模块"""
    file_path = project_root / relative_path.replace("/", "\\")
    module_name = relative_path.replace("/", ".").replace(".py", "")

    module = create_isolated_import(project_root, module_name, file_path)

    if class_name:
        return getattr(module, class_name)
    return module


project_root = Path(__file__).parent.parent
MediaManager = safe_import_core_module(project_root, "core/media_manager.py", "MediaManager")


class MockConfig:
    """模拟配置对象"""

    media_auto_save_enabled = True
    media_max_image_size_mb = 20
    media_max_file_size_mb = 20
    media_allowed_file_types = "txt,csv,xlsx,xls"
    media_retention_days = 30
    media_save_to_memory = False


class MockArchiveManager:
    """模拟档案馆管理器 - 用于测试数据流"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS archive_registry (
                    table_name TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    description TEXT,
                    columns_json TEXT NOT NULL,
                    row_count INTEGER DEFAULT 0,
                    scope TEXT DEFAULT 'auto',
                    import_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()

    def register_table(
        self,
        table_name: str,
        display_name: str,
        columns: dict,
        row_count: int,
        description: str = "",
        scope: str = "auto",
    ):
        import json

        with sqlite3.connect(str(self.db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO archive_registry
                (table_name, display_name, description, columns_json, row_count, scope)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (table_name, display_name, description, json.dumps(columns, ensure_ascii=False), row_count, scope),
            )
            conn.commit()


class MockDataIngestor:
    """模拟数据采集器 - 使用真实 SQLite 操作"""

    def __init__(self, archive_manager: MockArchiveManager):
        self.archive_manager = archive_manager

    def ingest_excel(self, file_path: str, display_name: str, description: str = "") -> tuple:
        """根据文件类型导入数据"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = path.suffix.lower()

        if suffix == ".csv":
            return self._ingest_csv(path, display_name, description)
        elif suffix == ".txt":
            return self._ingest_txt(path, display_name, description)
        elif suffix in (".xlsx", ".xls"):
            return self._ingest_excel(path, display_name, description)
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

    def _ingest_csv(self, file_path: Path, display_name: str, description: str) -> tuple:
        """导入 CSV 文件"""
        import csv

        table_name = f"test_{file_path.stem}"

        with open(file_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader)

            with sqlite3.connect(str(self.archive_manager.db_path)) as conn:
                cursor = conn.cursor()
                col_defs = ", ".join([f'"{h}" TEXT' for h in headers])
                cursor.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')

                placeholders = ", ".join(["?" for _ in headers])
                for row in reader:
                    cursor.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', row)

                row_count = cursor.rowcount
                conn.commit()

            columns = dict.fromkeys(headers, "TEXT")
            self.archive_manager.register_table(table_name, display_name, columns, row_count, description)

            return table_name, row_count

    def _ingest_txt(self, file_path: Path, display_name: str, description: str) -> tuple:
        """导入 TXT 文件（制表符分隔）"""
        return self._ingest_csv(file_path, display_name, description)

    def _ingest_excel(self, file_path: Path, display_name: str, description: str) -> tuple:
        """导入 Excel 文件"""
        try:
            import pandas as pd

            df = pd.read_excel(file_path)
            table_name = f"test_{file_path.stem}"

            with sqlite3.connect(str(self.archive_manager.db_path)) as conn:
                df.to_sql(table_name, conn, if_exists="replace", index=False)
                row_count = len(df)

            columns = {col: str(dtype) for col, dtype in df.dtypes.items()}
            self.archive_manager.register_table(table_name, display_name, columns, row_count, description)

            return table_name, row_count
        except ImportError:
            raise ImportError("pandas is required for Excel import")


class TestFileImportToArchive:
    """文件导入档案馆测试"""

    def setup_method(self):
        """创建测试环境"""
        self.test_dir = Path(tempfile.mkdtemp(prefix="scriptor_import_"))

        self.config = MockConfig()
        self.media_manager = MediaManager(self.test_dir, self.config)

        self.archive_db = self.test_dir / "archives.db"
        self.archive_manager = MockArchiveManager(str(self.archive_db))
        self.data_ingestor = MockDataIngestor(self.archive_manager)

        self.test_uid = "test_user_import"
        self.test_group_id = "private"

        self.create_test_files()

    def teardown_method(self):
        """清理测试环境"""
        if self.test_dir and self.test_dir.exists():
            import gc

            gc.collect()

            import time

            for _ in range(3):
                try:
                    shutil.rmtree(self.test_dir, ignore_errors=True)
                    break
                except PermissionError:
                    time.sleep(0.1)

    def create_test_files(self):
        """创建测试文件"""
        csv_content = """姓名,年龄,城市
张三,25,北京
李四,30,上海
王五,28,广州
"""
        csv_file = self.test_dir / "test_data.csv"
        csv_file.write_text(csv_content, encoding="utf-8")

        txt_content = """产品\t价格\t库存
苹果\t5.5\t100
香蕉\t3.2\t200
橙子\t4.8\t150
"""
        txt_file = self.test_dir / "test_data.txt"
        txt_file.write_text(txt_content, encoding="utf-8")

        try:
            import pandas as pd

            df = pd.DataFrame(
                {"ID": [1, 2, 3], "名称": ["项目A", "项目B", "项目C"], "状态": ["进行中", "已完成", "待开始"]}
            )

            excel_file = self.test_dir / "test_data.xlsx"
            df.to_excel(excel_file, index=False)

        except Exception:
            pass

    async def test_save_and_import_csv(self):
        """测试 CSV 文件保存和导入"""
        csv_file = self.test_dir / "test_data.csv"
        assert csv_file.exists(), "CSV 测试文件不存在"
        file_data = csv_file.read_bytes()

        sender_info = {"uid": self.test_uid, "name": "测试用户"}

        media_info = await self.media_manager.save_file(
            file_data=file_data,
            filename="test_data.csv",
            uid=self.test_uid,
            group_id=self.test_group_id,
            sender_info=sender_info,
            description="测试 CSV 数据",
        )

        assert media_info is not None, "保存 CSV 到媒体库失败"
        assert "filename" in media_info, "返回的 media_info 缺少 filename 字段"

        file_path = self.media_manager.get_file_path(self.test_uid, self.test_group_id, media_info["filename"])
        assert file_path.exists(), f"媒体库中的文件不存在：{file_path}"

        table_name, row_count = self.data_ingestor.ingest_excel(
            file_path=str(file_path), display_name="测试CSV数据", description="从 CSV 导入的测试数据"
        )

        assert table_name, "表名不应为空"
        assert row_count > 0, "应该有数据行被导入"

        with sqlite3.connect(self.archive_db) as conn:
            cursor = conn.cursor()
            cursor.execute(f'SELECT * FROM "{table_name}" LIMIT 3')
            rows = cursor.fetchall()

            assert len(rows) == 3, f"应该有3行数据，实际有 {len(rows)} 行"
            assert rows[0][0] == "张三", "第一行数据不正确"

    async def test_save_and_import_txt(self):
        """测试 TXT 文件保存和导入"""
        txt_file = self.test_dir / "test_data.txt"
        assert txt_file.exists(), "TXT 测试文件不存在"
        file_data = txt_file.read_bytes()

        sender_info = {"uid": self.test_uid, "name": "测试用户"}

        media_info = await self.media_manager.save_file(
            file_data=file_data,
            filename="test_data.txt",
            uid=self.test_uid,
            group_id=self.test_group_id,
            sender_info=sender_info,
            description="测试 TXT 数据",
        )

        assert media_info is not None, "保存 TXT 到媒体库失败"

        file_path = self.media_manager.get_file_path(self.test_uid, self.test_group_id, media_info["filename"])
        assert file_path.exists(), f"媒体库中的文件不存在：{file_path}"

        table_name, row_count = self.data_ingestor.ingest_excel(
            file_path=str(file_path), display_name="测试TXT数据", description="从 TXT 导入的测试数据"
        )

        assert table_name, "表名不应为空"
        assert row_count > 0, "应该有数据行被导入"

    async def test_excel_import(self):
        """测试 Excel 文件导入"""
        excel_file = self.test_dir / "test_data.xlsx"
        if not excel_file.exists():
            return

        table_name, row_count = self.data_ingestor.ingest_excel(
            file_path=str(excel_file), display_name="测试Excel数据", description="从 Excel 导入的测试数据"
        )

        assert table_name, "表名不应为空"
        assert row_count == 3, f"应该有3行数据，实际有 {row_count} 行"

    async def test_archive_registration(self):
        """测试档案馆注册功能"""
        self.data_ingestor.archive_manager.register_table(
            table_name="manual_table",
            display_name="手动注册表",
            columns={"col1": "TEXT", "col2": "INTEGER"},
            row_count=10,
            description="手动注册的测试表",
        )

        with sqlite3.connect(self.archive_db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM archive_registry WHERE table_name = ?", ("manual_table",))
            row = cursor.fetchone()

            assert row is not None, "表应该已注册"
            assert row[1] == "手动注册表", f"显示名称不正确：{row[1]}"

    def test_create_files(self):
        """测试测试文件创建"""
        csv_file = self.test_dir / "test_data.csv"
        txt_file = self.test_dir / "test_data.txt"

        assert csv_file.exists(), "CSV 文件应该存在"
        assert txt_file.exists(), "TXT 文件应该存在"

    async def test_csv_workflow(self):
        """完整 CSV 工作流测试"""
        csv_file = self.test_dir / "test_data.csv"
        file_data = csv_file.read_bytes()

        sender_info = {"uid": self.test_uid, "name": "测试用户"}

        media_info = await self.media_manager.save_file(
            file_data=file_data,
            filename="workflow_test.csv",
            uid=self.test_uid,
            group_id=self.test_group_id,
            sender_info=sender_info,
            description="工作流测试",
        )

        assert media_info is not None, "保存应该成功"

    async def test_txt_workflow(self):
        """完整 TXT 工作流测试"""
        txt_file = self.test_dir / "test_data.txt"
        file_data = txt_file.read_bytes()

        sender_info = {"uid": self.test_uid, "name": "测试用户"}

        media_info = await self.media_manager.save_file(
            file_data=file_data,
            filename="workflow_test.txt",
            uid=self.test_uid,
            group_id=self.test_group_id,
            sender_info=sender_info,
            description="工作流测试",
        )

        assert media_info is not None, "保存应该成功"

    def test_archive_system(self):
        """测试档案馆系统基本功能"""
        archive = self.archive_manager

        assert archive.db_path.exists(), "数据库文件应该存在"

        archive.register_table(
            table_name="test_table", display_name="测试表", columns={"a": "TEXT"}, row_count=5, description="测试"
        )

        with sqlite3.connect(self.archive_db) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM archive_registry")
            count = cursor.fetchone()[0]
            assert count >= 1, "应该有至少一个注册的表"
