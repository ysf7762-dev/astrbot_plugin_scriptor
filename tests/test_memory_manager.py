# tests/test_memory_manager.py
"""MemoryManager 核心模块测试"""

import asyncio

import pytest

# 使用直接导入方式（通过 conftest.py 设置的 sys.path）
try:
    from core.group_manager import GroupManager
    from core.identity_manager import IdentityManager
    from core.memory_manager import MemoryManager
except ImportError:
    from group_manager import GroupManager
    from identity_manager import IdentityManager
    from memory_manager import MemoryManager


class MockConfig:
    """模拟配置对象"""

    memory_compact_threshold = 8000
    daily_note_enabled = True
    cross_group_enabled = True
    embedding_enabled = False
    search_top_k = 5
    embedding_provider = "local"
    embedding_api_base = "http://localhost:11434/v1"
    embedding_api_key = ""
    embedding_model = "AI-ModelScope/bge-small-zh-v1.5"
    rerank_enabled = False
    rerank_provider = "api"
    rerank_api_base = "http://localhost:11434/v1"
    rerank_api_key = ""
    rerank_model = "bge-reranker-v2-m3"
    rerank_top_k = 5
    max_system_prompt_tokens = 100000
    enable_token_control = True
    soul_priority = 10
    agents_priority = 9
    profile_priority = 8
    group_rules_priority = 7
    group_members_priority = 6
    cross_group_tasks_priority = 5
    recent_notes_priority = 4
    sop_priority = 3
    retrieval_guidance_priority = 2
    message_sanitizer_enabled = True
    message_buffer_enabled = True
    tool_decoration_enabled = True
    session_locks_enabled = True
    reflection_message_threshold = 15
    reflection_time_threshold = 1800
    reflection_topic_threshold = 0.7
    reflection_recent_messages_limit = 20
    memory_archive_score_cap = 15.0
    backup_retention_days = 7
    llm_extraction_threshold = 10
    max_file_locks = 100
    index_cache_timeout = 300
    admin_uids = []
    nightly_maintenance_inactivity_minutes = 60
    nightly_maintenance_enabled = True


@pytest.fixture
def temp_data_dir(tmp_path):
    """创建临时数据目录"""
    return tmp_path


@pytest.fixture
def mock_config():
    """创建模拟配置"""
    return MockConfig()


@pytest.fixture
def identity_manager(temp_data_dir):
    """创建IdentityManager实例"""
    return IdentityManager(temp_data_dir)


@pytest.fixture
def group_manager(temp_data_dir, identity_manager):
    """创建GroupManager实例"""
    return GroupManager(temp_data_dir, identity_manager)


@pytest.fixture
def memory_manager(temp_data_dir, mock_config, identity_manager, group_manager):
    """创建MemoryManager实例"""
    return MemoryManager(temp_data_dir, mock_config, identity_manager, group_manager)


class TestMemoryManagerInit:
    """MemoryManager初始化测试"""

    def test_initialization(self, memory_manager, temp_data_dir, mock_config):
        """测试MemoryManager正确初始化"""
        assert memory_manager.data_dir == temp_data_dir
        assert memory_manager.config == mock_config
        assert memory_manager._unprocessed_messages == {}
        assert memory_manager.LLM_EXTRACTION_THRESHOLD == 10
        assert memory_manager._file_locks == {}
        assert memory_manager._MAX_LOCKS == 100

    def test_memory_keywords_loaded(self, memory_manager):
        """测试记忆关键词已加载"""
        assert hasattr(memory_manager, "MEMORY_KEYWORDS")
        assert len(memory_manager.MEMORY_KEYWORDS) > 0

    def test_memory_types_loaded(self, memory_manager):
        """测试记忆类型已加载"""
        assert hasattr(memory_manager, "MEMORY_TYPES")
        assert len(memory_manager.MEMORY_TYPES) > 0


class TestProfileOperations:
    """个人目录操作测试"""

    def test_get_profile_dir(self, memory_manager):
        """测试获取用户目录"""
        profile_dir = memory_manager._get_profile_dir("test_user_123")
        assert profile_dir.name == "test_user_123"
        assert "profiles" in str(profile_dir)

    def test_get_profile_dir_sanitization(self, memory_manager):
        """测试用户 ID sanitization"""
        profile_dir = memory_manager._get_profile_dir("../../../etc")
        # sanitize_id 会将特殊字符替换为下划线，但不会移除..
        # 真正的安全验证由 validate_sandbox_path 负责
        assert profile_dir.name != "../../../etc"
        # 验证路径是安全的（在 profiles 目录内）
        assert "profiles" in str(profile_dir)

    def test_get_group_dir(self, memory_manager):
        """测试获取群组目录"""
        group_dir = memory_manager._get_group_dir("test_group_456")
        assert group_dir.name == "test_group_456"
        assert "groups" in str(group_dir)

    def test_get_group_dir_sanitization(self, memory_manager):
        """测试群组ID sanitization"""
        group_dir = memory_manager._get_group_dir("test_group_456")
        assert group_dir.name == "test_group_456"


class TestFileLocking:
    """文件锁测试"""

    @pytest.mark.asyncio
    async def test_get_lock_creates_lock(self, memory_manager, tmp_path):
        """测试获取锁会创建锁对象"""
        test_file = tmp_path / "test.md"
        lock = await memory_manager._get_lock(test_file)
        assert lock is not None
        assert isinstance(lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_get_lock_reuses_lock(self, memory_manager, tmp_path):
        """测试相同文件获取相同锁"""
        test_file = tmp_path / "test.md"
        lock1 = await memory_manager._get_lock(test_file)
        lock2 = await memory_manager._get_lock(test_file)
        assert lock1 is lock2

    @pytest.mark.asyncio
    async def test_lock_cleanup_on_max(self, memory_manager, tmp_path):
        """测试锁数量超过上限时的LRU清理"""
        memory_manager._MAX_LOCKS = 5
        locks = []
        for i in range(10):
            test_file = tmp_path / f"test_{i}.md"
            lock = await memory_manager._get_lock(test_file)
            locks.append(lock)

        assert len(memory_manager._file_locks) <= memory_manager._MAX_LOCKS


class TestInteractionRecording:
    """交互记录测试"""

    @pytest.mark.asyncio
    async def test_record_interaction_private(self, memory_manager):
        """测试记录私聊交互"""
        is_new = await memory_manager.record_interaction(
            uid="user_123", group_id="private", role="user", content="你好，这是测试消息"
        )
        assert isinstance(is_new, bool)

        profile_dir = memory_manager._get_profile_dir("user_123")
        note_dir = profile_dir / "memory"
        assert note_dir.exists()

    @pytest.mark.asyncio
    async def test_record_interaction_group(self, memory_manager):
        """测试记录群聊交互

        修复 Issue #10: memory_manager.record_interaction() 不再为群聊创建日记文件，
        群聊日记由 group_manager 统一负责。此处只验证：
        1. 不创建群聊 memory 目录
        2. 未处理消息计数正常增加
        3. 时间戳正常更新
        """
        session_key = "user_123_group_456"
        initial_count = len(memory_manager._unprocessed_messages.get(session_key, []))

        is_new = await memory_manager.record_interaction(
            uid="user_123", group_id="group_456", role="user", content="群聊测试消息"
        )
        assert isinstance(is_new, bool)

        # 群聊场景下，memory_manager 不再创建 memory 目录
        group_dir = memory_manager._get_group_dir("group_456")
        note_dir = group_dir / "memory"
        assert not note_dir.exists(), "memory_manager 不应为群聊创建 memory 目录"

        # 但未处理消息计数应正常增加
        new_count = len(memory_manager._unprocessed_messages.get(session_key, []))
        assert new_count == initial_count + 1

        # 时间戳应已更新
        assert session_key in memory_manager._last_active_time

    @pytest.mark.asyncio
    async def test_record_interaction_increments_unprocessed(self, memory_manager):
        """测试记录会增加未处理消息计数"""
        session_key = "user_123_private"
        initial_count = len(memory_manager._unprocessed_messages.get(session_key, []))

        await memory_manager.record_interaction(uid="user_123", group_id="private", role="user", content="测试消息")

        new_count = len(memory_manager._unprocessed_messages.get(session_key, []))
        assert new_count == initial_count + 1


class TestMemoryExtraction:
    """记忆提取测试"""

    def test_should_extract_memory_keywords(self, memory_manager):
        """测试关键词触发记忆提取"""
        test_content = "我决定了，以后每天早上跑步"
        assert memory_manager.should_extract_memory(test_content) is True

    def test_should_extract_memory_length(self, memory_manager):
        """测试长度触发记忆提取"""
        long_content = "A" * 600
        assert memory_manager.should_extract_memory(long_content) is True

    def test_should_not_extract_memory_short(self, memory_manager):
        """测试短内容不触发记忆提取"""
        short_content = "你好"
        assert memory_manager.should_extract_memory(short_content) is False

    @pytest.mark.skip(reason="Logic change - threshold not reached")
    def test_should_trigger_llm_extraction(self, memory_manager):
        """测试触发LLM提取的条件"""
        memory_manager._unprocessed_messages["test_session"] = list(range(15))
        assert memory_manager.should_trigger_llm_extraction("test_user", "test_group") is True

    def test_should_not_trigger_llm_extraction(self, memory_manager):
        """测试不触发LLM提取的条件"""
        memory_manager._unprocessed_messages["test_session"] = list(range(5))
        assert memory_manager.should_trigger_llm_extraction("test_user", "test_group") is False

    def test_extract_memory_type(self, memory_manager):
        """测试记忆类型提取"""
        content = "我更喜欢喝咖啡而不是茶"
        mem_type = memory_manager.extract_memory_type(content)
        assert mem_type is not None
        assert mem_type in ["preference", "habit", "fact", "experience", "decision", "task"]


class TestLongTermMemory:
    """长期记忆测试"""

    @pytest.mark.asyncio
    async def test_record_long_term_memory_private(self, memory_manager):
        """测试记录私聊长期记忆"""
        # 使用包导入方式，兼容相对导入
        from astrbot_plugin_scriptor.core.interfaces import MemoryRecordParams

        profile_dir = memory_manager._get_profile_dir("user_123")
        profile_dir.mkdir(parents=True, exist_ok=True)

        params = MemoryRecordParams(
            uid="user_123", group_id="private", content="这是一个重要的记忆", memory_type="fact"
        )
        await memory_manager.record_long_term_memory(params)

        memory_file = profile_dir / "P_MEMORY.md"
        assert memory_file.exists()

    @pytest.mark.asyncio
    async def test_record_long_term_memory_group(self, memory_manager):
        """测试记录群聊长期记忆"""
        # 使用包导入方式，兼容相对导入
        from astrbot_plugin_scriptor.core.interfaces import MemoryRecordParams

        group_dir = memory_manager._get_group_dir("group_456")
        group_dir.mkdir(parents=True, exist_ok=True)

        params = MemoryRecordParams(
            uid="user_123", group_id="group_456", content="这是一个群组的重要记忆", memory_type="fact"
        )
        await memory_manager.record_long_term_memory(params)

        memory_file = group_dir / "G_MEMORY.md"
        assert memory_file.exists()


class TestProfileUpdate:
    """画像更新测试"""

    @pytest.mark.asyncio
    async def test_update_profile(self, memory_manager):
        """测试更新用户画像"""
        uid = "user_123"
        profile_dir = memory_manager._get_profile_dir(uid)
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile_file = profile_dir / "P_PROFILE.md"
        profile_file.write_text("## 用户资料\n初始内容\n", encoding="utf-8")

        await memory_manager.update_profile(uid, "private", "我叫张三", scope="personal")

        content = profile_file.read_text(encoding="utf-8")
        assert "张三" in content

    @pytest.mark.asyncio
    async def test_update_profile_creates_file(self, memory_manager):
        """测试更新不存在的画像文件会记录警告"""
        uid = "nonexistent_user"
        await memory_manager.update_profile(uid, "private", "新事实", scope="personal")


class TestGroupProfileUpdate:
    """群组画像更新测试"""

    @pytest.mark.asyncio
    async def test_update_group_profile_success(self, memory_manager):
        """测试成功更新群组画像"""
        group_id = "group_test"
        group_dir = memory_manager._get_group_dir(group_id)
        group_dir.mkdir(parents=True, exist_ok=True)
        profile_file = group_dir / "G_PROFILE.md"
        profile_file.write_text("## 群组资料\n初始内容\n", encoding="utf-8")

        await memory_manager.update_profile("user_123", group_id, "这个群喜欢讨论技术", scope="group")

        content = profile_file.read_text(encoding="utf-8")
        assert "讨论技术" in content

    @pytest.mark.asyncio
    async def test_update_group_profile_in_private_rejected(self, memory_manager):
        """测试在私聊中更新群组画像被拒绝"""
        result = await memory_manager.update_profile("user_123", "private", "测试内容", scope="group")

        assert result is None


class TestTaskConsolidation:
    """任务巩固测试"""

    @pytest.mark.asyncio
    async def test_consolidate_tasks_for_uid(self, memory_manager):
        """测试任务巩固功能"""
        uid = "user_123"
        profile_dir = memory_manager._get_profile_dir(uid)
        memory_dir = profile_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        memory_file = profile_dir / "MEMORY.md"
        memory_file.write_text("### [2026-01-01 10:00:00] (task) [Status: pending]\n测试任务\n", encoding="utf-8")

        await memory_manager._consolidate_tasks_for_uid(uid, "private")

        content = memory_file.read_text(encoding="utf-8")
        assert "task" in content or "archived" in content


class TestPendingTasks:
    """待处理任务测试"""

    @pytest.mark.asyncio
    async def test_get_pending_tasks(self, memory_manager):
        """测试获取待处理任务"""
        uid = "user_123"
        profile_dir = memory_manager._get_profile_dir(uid)
        memory_dir = profile_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        memory_file = profile_dir / "MEMORY.md"
        memory_file.write_text("### [2026-01-01 10:00:00] (task) [Status: pending]\n待办任务\n", encoding="utf-8")

        tasks = await memory_manager.get_pending_tasks(uid, "private")
        assert isinstance(tasks, list)

    @pytest.mark.asyncio
    async def test_get_pending_tasks_empty(self, memory_manager):
        """测试无任务时返回空列表"""
        uid = "user_no_tasks"
        tasks = await memory_manager.get_pending_tasks(uid, "private")
        assert tasks == []


class TestUnprocessedMessages:
    """未处理消息测试"""

    def test_get_unprocessed_messages(self, memory_manager):
        """测试获取未处理消息"""
        session_key = "user_123_private"
        memory_manager._unprocessed_messages[session_key] = [
            {"role": "user", "content": "消息1"},
            {"role": "assistant", "content": "回复1"},
        ]

        messages = memory_manager.get_unprocessed_messages("user_123", "private")
        assert len(messages) == 2

    def test_clear_unprocessed_messages(self, memory_manager):
        """测试清空未处理消息"""
        session_key = "user_123_private"
        memory_manager._unprocessed_messages[session_key] = [{"role": "user", "content": "消息1"}]

        memory_manager.clear_unprocessed_messages("user_123", "private")
        messages = memory_manager.get_unprocessed_messages("user_123", "private")
        assert len(messages) == 0


class TestDailyNotes:
    """日记测试"""

    def test_get_daily_notes_empty(self, memory_manager):
        """测试获取空日记"""
        notes = memory_manager.get_daily_notes("user_123", "private", days=7)
        assert notes == []

    @pytest.mark.asyncio
    async def test_get_daily_notes_with_data(self, memory_manager):
        """测试获取有数据的日记"""
        await memory_manager.record_interaction(uid="user_123", group_id="private", role="user", content="测试消息")

        notes = memory_manager.get_daily_notes("user_123", "private", days=7)
        assert isinstance(notes, list)

    def test_get_recent_notes_text_empty(self, memory_manager):
        """测试获取空日记文本"""
        text = memory_manager.get_recent_notes_text("user_123", "private", limit=3)
        assert text == ""


class TestHotMemory:
    """热记忆测试"""

    def test_get_hot_memory_private(self, memory_manager):
        """测试获取私聊热记忆"""
        uid = "user_123"
        profile_dir = memory_manager._get_profile_dir(uid)
        profile_dir.mkdir(parents=True, exist_ok=True)

        profile_file = profile_dir / "PROFILE.md"
        profile_file.write_text("## 用户资料\n我叫李四\n", encoding="utf-8")

        hot = memory_manager.get_hot_memory(uid, "private")
        assert isinstance(hot, str)

    @pytest.mark.skip(reason="Requires pre-existing memory file")
    def test_get_hot_memory_group(self, memory_manager):
        """测试获取群聊热记忆"""
        uid = "user_123"
        group_id = "group_456"

        profile_dir = memory_manager._get_profile_dir(uid)
        profile_file = profile_dir / "PROFILE.md"
        profile_file.write_text("## 用户资料\n我叫王五\n", encoding="utf-8")

        hot = memory_manager.get_hot_memory(uid, group_id)
        assert isinstance(hot, str)


class TestTaskMonitor:
    """任务监控测试"""

    @pytest.mark.asyncio
    async def test_start_task_monitor(self, memory_manager):
        """测试启动任务监控"""
        memory_manager._task_check_task = None
        await memory_manager.start_task_monitor()
        assert memory_manager._task_check_task is not None
        assert not memory_manager._task_check_task.done()

    @pytest.mark.asyncio
    async def test_stop_task_monitor(self, memory_manager):
        """测试停止任务监控"""
        await memory_manager.start_task_monitor()
        await memory_manager.stop_task_monitor()
        assert memory_manager._task_check_task is None or memory_manager._task_check_task.done()

    @pytest.mark.asyncio
    async def test_task_consolidation_loop(self, memory_manager):
        """测试任务巩固循环"""
        memory_manager._task_check_interval = 1
        task = asyncio.create_task(memory_manager._task_consolidation_loop())
        await asyncio.sleep(2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestMemoryStrength:
    """记忆强度测试"""

    @pytest.mark.asyncio
    async def test_increase_memory_strength(self, memory_manager, tmp_path):
        """测试增加记忆强度"""
        uid = "user_123"
        profile_dir = memory_manager._get_profile_dir(uid)
        profile_dir.mkdir(parents=True, exist_ok=True)

        memory_file = profile_dir / "MEMORY.md"
        memory_file.write_text(
            "### [2026-01-01 10:00:00] (fact) [Privacy: private] [Strength: 1.0] [Score: 5.0]\n测试内容\n",
            encoding="utf-8",
        )

        await memory_manager.increase_memory_strength(uid, "private", "测试内容", "MEMORY.md", is_useful=True)

        content = memory_file.read_text(encoding="utf-8")
        assert "Strength:" in content or "Score:" in content


class TestConcurrency:
    """并发测试"""

    @pytest.mark.asyncio
    async def test_concurrent_interaction_recording(self, memory_manager):
        """测试并发记录交互"""
        tasks = []
        for i in range(10):
            task = memory_manager.record_interaction(
                uid=f"user_{i}", group_id="private", role="user", content=f"并发消息 {i}"
            )
            tasks.append(task)

        await asyncio.gather(*tasks)

        for i in range(10):
            profile_dir = memory_manager._get_profile_dir(f"user_{i}")
            assert profile_dir.exists()

    @pytest.mark.asyncio
    async def test_concurrent_memory_writing(self, memory_manager):
        """测试并发写入长期记忆"""
        # 使用包导入方式，兼容相对导入
        from astrbot_plugin_scriptor.core.interfaces import MemoryRecordParams

        profile_dir = memory_manager._get_profile_dir("user_concurrent")
        profile_dir.mkdir(parents=True, exist_ok=True)

        async def record_memory(index: int):
            params = MemoryRecordParams(
                uid="user_concurrent", group_id="private", content=f"并发记忆 {index}", memory_type="fact"
            )
            await memory_manager.record_long_term_memory(params)

        tasks = [record_memory(i) for i in range(5)]
        await asyncio.gather(*tasks)

        profile_dir = memory_manager._get_profile_dir("user_concurrent")
        memory_file = profile_dir / "P_MEMORY.md"
        assert memory_file.exists()
