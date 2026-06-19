import asyncio
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)

# 请求体大小限制 (1MB)
MAX_REQUEST_SIZE = 1 * 1024 * 1024

# CSRF 令牌配置 - 修复环境变量名拼写错误 (P101)
CSRF_SECRET_KEY = os.environ.get("SCRIPTOR_CSRF_SECRET_KEY", secrets.token_hex(32))
CSRF_TOKEN_EXPIRE_MINUTES = 30

# 尝试导入共享状态（如果在同进程中）
# 如果失败（子进程模式），则使用备用方案
from .shared_state import (
    get_archive_manager,
    get_config,
    get_data_dir,
    get_knowledge_base,
    get_research_tool,
    is_initialized,
    trigger_reindex,
)


# 备用方案：当共享状态不可用时（子进程模式），直接访问文件系统
def get_data_dir_safe() -> Path:
    """获取数据目录（兼容模式）"""
    # 首先尝试从共享状态获取
    data_dir = get_data_dir()
    if data_dir is not None:
        return data_dir

    # 备用方案：直接计算路径
    # 插件目录: data/plugins/astrbot_plugin_scriptor
    # 数据目录: data/plugin_data/astrbot_plugin_scriptor
    plugin_dir = Path(__file__).parent.parent
    data_dir = plugin_dir.parent.parent / "plugin_data" / "astrbot_plugin_scriptor"
    return data_dir


_knowledge_base_instance = None


def _import_knowledge_base():
    """动态导入 KnowledgeBase，支持同进程和子进程模式"""
    import sys
    from pathlib import Path

    # 计算插件目录路径
    web_dir = Path(__file__).parent
    plugin_dir = web_dir.parent

    # 添加到 sys.path（如果尚未添加）
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))

    # 直接导入 knowledge_base.py 文件，避免 core/__init__.py 的依赖
    import importlib.util

    kb_file = plugin_dir / "core" / "knowledge_base.py"

    spec = importlib.util.spec_from_file_location("knowledge_base", kb_file)
    kb_module = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_base"] = kb_module
    spec.loader.exec_module(kb_module)

    return kb_module.KnowledgeBase


def get_knowledge_base_safe():
    """获取知识库实例（兼容模式）"""
    global _knowledge_base_instance

    kb = get_knowledge_base()
    if kb is not None:
        return kb

    if _knowledge_base_instance is not None:
        return _knowledge_base_instance

    try:
        KnowledgeBase = _import_knowledge_base()
        data_dir = get_data_dir_safe()
        logger.info(f"[Scriptor Web UI] 正在创建知识库实例，数据目录: {data_dir}")

        kb_file = data_dir / "global" / "knowledge" / "KNOWLEDGE_BASE.md"
        if kb_file.exists():
            logger.info(f"[Scriptor Web UI] 知识库文件存在: {kb_file} (大小: {kb_file.stat().st_size} 字节)")
        else:
            logger.warning(f"[Scriptor Web UI] 知识库文件不存在: {kb_file}")

        _knowledge_base_instance = KnowledgeBase(data_dir)
        item_count = len(_knowledge_base_instance.get_all_items())
        logger.info(f"[Scriptor Web UI] 已创建知识库实例（兼容模式），加载了 {item_count} 条知识")
        return _knowledge_base_instance
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 创建知识库实例失败: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return None


# FastAPI 应用必须在此处定义，在 middleware 使用之前 (P0 - 修复 NameError)
app = FastAPI(title="灵笔司书 Web UI API")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# 添加安全中间件 - CSP 头部防护 (P102)
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """添加安全响应头"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:;"
    )
    return response


@app.middleware("http")
async def check_request_size(request: Request, call_next):
    """检查请求体大小，防止大文件 DOS 攻击"""
    if request.method in ["POST", "PUT", "PATCH"]:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > MAX_REQUEST_SIZE:
            raise HTTPException(
                status_code=413, detail=f"Request body too large. Maximum size is {MAX_REQUEST_SIZE} bytes (1MB)"
            )
    response = await call_next(request)
    return response


def generate_csrf_token(session_id: str) -> str:
    """生成 CSRF 令牌"""
    expire_time = int(datetime.now().timestamp()) + CSRF_TOKEN_EXPIRE_MINUTES * 60
    token_data = f"{session_id}:{expire_time}"
    signature = hashlib.sha256(f"{token_data}:{CSRF_SECRET_KEY}".encode()).hexdigest()[:16]
    token = f"{expire_time}:{signature}"
    return token


def verify_csrf_token(session_id: str, token: str) -> bool:
    """验证 CSRF 令牌"""
    try:
        parts = token.split(":")
        if len(parts) != 2:
            return False
        expire_time = int(parts[0])
        signature = parts[1]

        if datetime.now().timestamp() > expire_time:
            return False

        token_data = f"{session_id}:{expire_time}"
        expected_signature = hashlib.sha256(f"{token_data}:{CSRF_SECRET_KEY}".encode()).hexdigest()[:16]

        return signature == expected_signature
    except (ValueError, TypeError):
        return False


def sanitize_html(content: str) -> str:
    """HTML 转义，防止 XSS 攻击"""
    return html.escape(content)


class CSRFProtection:
    """CSRF 保护管理器"""

    _tokens: dict = {}

    @classmethod
    def create_token(cls, session_id: str) -> str:
        """为会话创建 CSRF 令牌"""
        token = generate_csrf_token(session_id)
        cls._tokens[session_id] = {"token": token, "created_at": datetime.now()}
        return token

    @classmethod
    def verify(cls, session_id: str, token: str) -> bool:
        """验证 CSRF 令牌"""
        if session_id not in cls._tokens:
            return verify_csrf_token(session_id, token)

        stored = cls._tokens[session_id]
        if (datetime.now() - stored["created_at"]).total_seconds() > CSRF_TOKEN_EXPIRE_MINUTES * 60:
            del cls._tokens[session_id]
            return False

        return token == stored["token"] and verify_csrf_token(session_id, token)

    @classmethod
    def cleanup_expired(cls):
        """清理过期的令牌"""
        expired = []
        for session_id, data in cls._tokens.items():
            if (datetime.now() - data["created_at"]).total_seconds() > CSRF_TOKEN_EXPIRE_MINUTES * 60:
                expired.append(session_id)
        for session_id in expired:
            del cls._tokens[session_id]


try:
    from astrbot.api import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)


WEB_DIR = Path(__file__).parent
VUE_DIST_DIR = WEB_DIR / "dist"
ICON_DIR = WEB_DIR.parent / "icon"

_api_key: Optional[str] = None
_api_key_mtime: float = 0

VUE_ASSETS_DIR = VUE_DIST_DIR / "assets"
if VUE_ASSETS_DIR.exists():
    app.mount("/assets", StaticFiles(directory=VUE_ASSETS_DIR), name="assets")


@app.get("/api/static/icon/{filename}")
async def serve_icon(filename: str):
    """提供图标文件（禁用缓存）"""
    if not ICON_DIR.exists():
        raise HTTPException(status_code=404, detail="Icon directory not found")

    icon_file = ICON_DIR / filename
    if not icon_file.exists():
        raise HTTPException(status_code=404, detail="Icon not found")

    return FileResponse(
        icon_file,
        media_type="image/png",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """提供主页面"""
    if VUE_DIST_DIR.exists():
        index_file = VUE_DIST_DIR / "index.html"
        if index_file.exists():
            with open(index_file, "r", encoding="utf-8") as f:
                return f.read()

    return HTMLResponse(content="<h1>灵笔司书 Web UI</h1><p>请先构建前端: cd web && npm run build</p>")


def _get_key_storage_dir() -> Path:
    """获取用于存储密钥的目录（存储在 plugin_data 目录下）"""
    data_dir = get_data_dir_safe()
    return data_dir


def _hash_password(password: str) -> str:
    """使用 bcrypt 对密码进行哈希"""
    import bcrypt

    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(password.encode("utf-8"), salt)
    return hashed.decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    """验证密码是否匹配哈希值"""
    import bcrypt

    try:
        result = bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
        logger.debug(f"[Scriptor] 密码验证结果: {result}")
        return result
    except Exception as e:
        logger.warning(f"[Scriptor] 密码验证异常: {e}")
        return False


def _load_api_key():
    """从环境变量或文件加载API密钥"""
    global _api_key, _api_key_mtime

    data_dir = _get_key_storage_dir()

    password_file = data_dir / ".web_ui_password"
    if password_file.exists():
        try:
            current_mtime = password_file.stat().st_mtime
            if current_mtime != _api_key_mtime or not _api_key:
                hashed_pwd = password_file.read_text(encoding="utf-8").strip()
                if hashed_pwd and len(hashed_pwd) >= 20:
                    _api_key = hashed_pwd
                    _api_key_mtime = current_mtime
                    logger.info("[灵笔司书] 使用用户设置的密码（哈希存储）")
                    return
        except Exception as e:
            logger.warning(f"[灵笔司书] 读取密码文件失败: {e}")

    key_file = data_dir / ".web_ui_key"
    if key_file.exists():
        try:
            current_mtime = key_file.stat().st_mtime
            if current_mtime != _api_key_mtime or not _api_key:
                key = key_file.read_text(encoding="utf-8").strip()
                if key:
                    _api_key = key
                    _api_key_mtime = current_mtime
                    os.environ["SCRIPTOR_API_KEY"] = key
                    logger.info("[灵笔司书] 使用自动生成的 API Key")
                    return
        except Exception as e:
            logger.warning(f"[灵笔司书] 读取密钥文件失败: {e}")

    env_key = os.environ.get("SCRIPTOR_API_KEY", "")
    if env_key:
        _api_key = env_key
        logger.info("[灵笔司书] 使用环境变量中的 API Key")
        return

    _api_key = ""
    logger.warning("[灵笔司书] API Key 未配置，Web UI 将无法访问")


_load_api_key()


def _reload_api_key_if_changed():
    """检查并重新加载 API Key（如果文件已更改）"""
    data_dir = _get_key_storage_dir()

    password_file = data_dir / ".web_ui_password"
    key_file = data_dir / ".web_ui_key"

    current_mtime = 0
    check_file = None

    if password_file.exists():
        current_mtime = password_file.stat().st_mtime
        check_file = password_file
    elif key_file.exists():
        current_mtime = key_file.stat().st_mtime
        check_file = key_file

    if current_mtime > _api_key_mtime:
        _load_api_key()


async def get_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")) -> str:
    """验证API密钥"""
    _reload_api_key_if_changed()

    if not _api_key:
        raise HTTPException(
            status_code=403,
            detail="API Key not configured. Please set SCRIPTOR_API_KEY environment variable for security.",
        )

    if x_api_key is None:
        raise HTTPException(status_code=401, detail="API Key required in X-API-Key header")

    if _api_key.startswith("$2b$"):
        if not _verify_password(x_api_key, _api_key):
            raise HTTPException(status_code=403, detail="Invalid API Key")
    else:
        if x_api_key != _api_key:
            raise HTTPException(status_code=403, detail="Invalid API Key")

    return x_api_key


SAFE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
SAFE_FILENAME_PATTERN = re.compile(r"^[a-zA-Z0-9_\-.]+\.md$")


def validate_safe_input(value: str, pattern: re.Pattern, field_name: str) -> bool:
    """验证输入是否安全"""
    if not pattern.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name} format")
    return True


_default_origins = "http://localhost:18111,http://127.0.0.1:18111,http://localhost:19111,http://127.0.0.1:19111,http://localhost:8501,http://127.0.0.1:8501,http://localhost:3000,http://127.0.0.1:3000"
_trusted_origins = os.environ.get("SCRIPTOR_TRUSTED_ORIGINS", _default_origins)
_trusted_list = [origin.strip() for origin in _trusted_origins.split(",")]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_trusted_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

from urllib.parse import unquote


def safe_resolve_path(base_dir: Path, *parts: str) -> Path:
    """
    安全地解析文件路径，防止目录穿越攻击

    Args:
        base_dir: 基础目录
        parts: 路径部件

    Returns:
        解析后的绝对路径

    Raises:
        HTTPException: 如果路径穿越或不在允许范围内
    """
    safe_parts = []
    for part in parts:
        decoded_part = unquote(part)
        if ".." in decoded_part or ("." not in decoded_part and decoded_part == ""):
            continue
        if decoded_part in (".", ""):
            continue
        if not re.match(r"^[a-zA-Z0-9_\-.]+$", decoded_part):
            continue
        safe_parts.append(decoded_part)

    target_path = base_dir.joinpath(*safe_parts).resolve()

    try:
        target_path.relative_to(base_dir.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied: path traversal detected")

    return target_path


_file_locks: dict = {}
_file_lock_access_times: dict = {}
_MAX_FILE_LOCKS = 100


async def get_file_lock(file_path: Path):
    """获取文件级别的异步锁（带LRU清理）"""
    path_str = str(file_path)
    if path_str not in _file_locks:
        if len(_file_locks) >= _MAX_FILE_LOCKS:
            sorted_locks = sorted(_file_lock_access_times.items(), key=lambda x: x[1])
            remove_count = max(1, len(sorted_locks) // 5)
            for path_to_remove, _ in sorted_locks[:remove_count]:
                _file_locks.pop(path_to_remove, None)
                _file_lock_access_times.pop(path_to_remove, None)
        _file_locks[path_str] = asyncio.Lock()
    _file_lock_access_times[path_str] = __import__("time").time()
    return _file_locks[path_str]


class MemoryUpdate(BaseModel):
    content: str


class CSRFVerify:
    """CSRF 验证依赖"""

    def __init__(self, require_verification: bool = True):
        self.require_verification = require_verification

    async def __call__(self, request: Request):
        """验证 CSRF 令牌"""
        if not self.require_verification:
            return True

        session_id = request.headers.get("X-Session-ID", "default")

        if request.method in ["POST", "PUT", "PATCH", "DELETE"]:
            csrf_token = request.headers.get("X-CSRF-Token")

            if not csrf_token:
                raise HTTPException(status_code=403, detail="CSRF token required in X-CSRF-Token header")

            if not CSRFProtection.verify(session_id, csrf_token):
                raise HTTPException(status_code=403, detail="Invalid or expired CSRF token")

        return True


@app.get("/api/health")
async def health_check():
    """健康检查端点 - 不需要鉴权"""
    return {"status": "ok", "service": "Scriptor Web API"}


@app.get("/api/setup/status")
async def get_setup_status():
    """获取初始化状态 - 不需要鉴权，用于判断是否需要首次设置密码"""
    data_dir = _get_key_storage_dir()
    password_file = data_dir / ".web_ui_password"
    key_file = data_dir / ".web_ui_key"

    has_password = password_file.exists()
    has_temp_key = key_file.exists()

    return {
        "needs_setup": not (has_password or has_temp_key),
        "has_password": has_password,
        "has_temp_key": has_temp_key,
    }


class FirstTimeSetup(BaseModel):
    """首次设置密码请求体"""

    password: str


@app.post("/api/setup/password")
async def setup_first_password(data: FirstTimeSetup):
    """首次设置密码 - 不需要鉴权，仅在没有密码时可用"""
    global _api_key, _api_key_mtime

    data_dir = _get_key_storage_dir()
    password_file = data_dir / ".web_ui_password"
    key_file = data_dir / ".web_ui_key"

    if password_file.exists() or key_file.exists():
        raise HTTPException(status_code=400, detail="密码已设置，请使用修改密码功能")

    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少 6 位")

    try:
        password_file.parent.mkdir(parents=True, exist_ok=True)

        hashed_password = _hash_password(data.password)
        password_file.write_text(hashed_password, encoding="utf-8")

        _api_key = hashed_password
        _api_key_mtime = password_file.stat().st_mtime

        return {"status": "success", "message": "密码设置成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"设置密码失败: {e!s}")


# ============================================================
# Sudo 会话管理 (Web UI 提权机制)
# ============================================================

# Sudo 会话存储：{session_id: {"activated_at": timestamp, "last_active": timestamp}}
_sudo_sessions: dict = {}
SUDO_TIMEOUT_MINUTES = 30


def _get_session_id(request: Request) -> str:
    """获取会话 ID（基于 API Key 的哈希）"""
    api_key = request.headers.get("X-API-Key", "default")
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


def _check_sudo_session(session_id: str, update_activity: bool = True) -> bool:
    """检查 Sudo 会话是否有效

    Args:
        session_id: 会话ID
        update_activity: 是否更新最后活跃时间（默认True，用于实际操作时）
    """
    if session_id not in _sudo_sessions:
        return False

    session = _sudo_sessions[session_id]
    elapsed = (datetime.now() - session["last_active"]).total_seconds()

    if elapsed > SUDO_TIMEOUT_MINUTES * 60:
        del _sudo_sessions[session_id]
        return False

    if update_activity:
        session["last_active"] = datetime.now()
    return True


def _activate_sudo_session(session_id: str):
    """激活 Sudo 会话"""
    now = datetime.now()
    _sudo_sessions[session_id] = {"activated_at": now, "last_active": now}


def _deactivate_sudo_session(session_id: str):
    """停用 Sudo 会话"""
    _sudo_sessions.pop(session_id, None)


async def require_sudo(request: Request, api_key: str = Depends(get_api_key)):
    """依赖项：要求 Sudo 模式"""
    session_id = _get_session_id(request)

    if not _check_sudo_session(session_id):
        raise HTTPException(
            status_code=403, detail="Sudo mode required. Please verify your password to enable write operations."
        )

    return api_key


class SudoVerifyRequest(BaseModel):
    """Sudo 验证请求体"""

    password: str


@app.get("/api/sudo/status")
async def get_sudo_status(request: Request, api_key: str = Depends(get_api_key)):
    """获取当前 Sudo 状态（只读，不更新活跃时间）"""
    _ = api_key  # 验证通过
    session_id = _get_session_id(request)

    is_active = _check_sudo_session(session_id, update_activity=False)

    remaining_seconds = 0
    if is_active:
        session = _sudo_sessions[session_id]
        elapsed = (datetime.now() - session["last_active"]).total_seconds()
        remaining_seconds = max(0, SUDO_TIMEOUT_MINUTES * 60 - elapsed)

    return {"is_sudo": is_active, "timeout_minutes": SUDO_TIMEOUT_MINUTES, "remaining_seconds": int(remaining_seconds)}


@app.post("/api/sudo/verify")
async def verify_sudo(
    request: Request, data: SudoVerifyRequest, api_key: str = Depends(get_api_key), _: bool = Depends(CSRFVerify())
):
    """验证密码并进入 Sudo 模式"""
    _ = api_key

    _reload_api_key_if_changed()

    if not _api_key:
        logger.error("[Scriptor Web UI] Sudo 验证失败: API Key 未配置")
        raise HTTPException(status_code=403, detail="API Key 未配置")

    logger.debug(
        f"[Scriptor Web UI] Sudo 验证: _api_key 类型={'哈希' if _api_key.startswith('$2b$') else '明文'}, 长度={len(_api_key)}"
    )

    if _api_key.startswith("$2b$"):
        if not _verify_password(data.password, _api_key):
            logger.warning(f"[Scriptor Web UI] Sudo 密码验证失败（哈希模式），输入密码长度: {len(data.password)}")
            raise HTTPException(status_code=403, detail="密码错误")
    else:
        if data.password != _api_key:
            logger.warning("[Scriptor Web UI] Sudo 密码验证失败（明文模式）")
            raise HTTPException(status_code=403, detail="密码错误")

    session_id = _get_session_id(request)
    _activate_sudo_session(session_id)

    logger.info(f"[Scriptor Web UI] Sudo 模式已激活: session={session_id}")

    return {"status": "success", "message": "Sudo 模式已激活", "timeout_minutes": SUDO_TIMEOUT_MINUTES}


@app.post("/api/sudo/exit")
async def exit_sudo(request: Request, api_key: str = Depends(get_api_key), _: bool = Depends(CSRFVerify())):
    """退出 Sudo 模式"""
    _ = api_key  # 验证通过
    session_id = _get_session_id(request)
    _deactivate_sudo_session(session_id)

    logger.info(f"[Scriptor Web UI] Sudo 模式已退出: session={session_id}")

    return {"status": "success", "message": "Sudo 模式已退出"}


# ============================================================
# 全局记忆管理 API
# ============================================================

GLOBAL_MEMORY_FILES = {
    "Global_SOUL.md": "SOUL.md",
    "Global_MEMORY.md": "MEMORY.md",
    "Global_HEARTBEAT.md": "HEARTBEAT.md",
}


def _resolve_global_file(global_dir: Path, filename: str) -> Path:
    """解析全局文件路径（支持新旧两种命名格式）"""
    if filename in GLOBAL_MEMORY_FILES:
        new_path = global_dir / filename
        if new_path.exists():
            return new_path
        return global_dir / GLOBAL_MEMORY_FILES[filename]
    return global_dir / filename


@app.get("/api/global/memory")
async def list_global_memory(api_key: str = Depends(get_api_key)):
    """获取全局记忆文件列表"""
    _ = api_key
    DATA_DIR = get_data_dir_safe()
    global_dir = DATA_DIR / "global"

    result = []
    for new_name, old_name in GLOBAL_MEMORY_FILES.items():
        file_path = _resolve_global_file(global_dir, new_name)
        if file_path.exists():
            stat = file_path.stat()
            result.append(
                {
                    "filename": file_path.name,
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "exists": True,
                }
            )
        else:
            result.append({"filename": new_name, "size": 0, "modified": None, "exists": False})

    return result


@app.get("/api/global/memory/{filename}")
async def get_global_memory_file(filename: str, api_key: str = Depends(get_api_key)):
    """获取全局记忆文件内容"""
    _ = api_key

    valid_names = list(GLOBAL_MEMORY_FILES.keys()) + list(GLOBAL_MEMORY_FILES.values())
    if filename not in valid_names:
        raise HTTPException(status_code=400, detail="无效的文件名")

    DATA_DIR = get_data_dir_safe()
    global_dir = DATA_DIR / "global"
    file_path = _resolve_global_file(global_dir, filename)

    if not file_path.exists():
        return {"content": "", "exists": False}

    try:
        content = file_path.read_text(encoding="utf-8")
        return {"content": content, "exists": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e!s}")


class GlobalMemoryUpdateRequest(BaseModel):
    """全局记忆更新请求"""

    content: str


@app.put("/api/global/memory/{filename}")
async def update_global_memory_file(
    filename: str,
    data: GlobalMemoryUpdateRequest,
    request: Request,
    api_key: str = Depends(get_api_key),
    _: bool = Depends(CSRFVerify()),
):
    """更新全局记忆文件（需要 Sudo 权限）"""
    _ = api_key

    session_id = _get_session_id(request)
    if not _check_sudo_session(session_id):
        raise HTTPException(
            status_code=403, detail="Sudo mode required. Please verify your password to edit global memory."
        )

    valid_names = list(GLOBAL_MEMORY_FILES.keys()) + list(GLOBAL_MEMORY_FILES.values())
    if filename not in valid_names:
        raise HTTPException(status_code=400, detail="无效的文件名")

    DATA_DIR = get_data_dir_safe()
    global_dir = DATA_DIR / "global"

    target_filename = filename
    if filename in GLOBAL_MEMORY_FILES.values():
        target_filename = [k for k, v in GLOBAL_MEMORY_FILES.items() if v == filename][0]

    file_path = global_dir / target_filename

    file_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        file_path.write_text(data.content, encoding="utf-8")
        logger.info(f"[Scriptor Web UI] 更新全局记忆文件: {target_filename}")
        return {"status": "success", "message": f"{target_filename} 已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入文件失败: {e!s}")


# ============================================================
# 系统状态 API
# ============================================================


@app.get("/api/status")
async def get_status(api_key: str = Depends(get_api_key)):
    """获取系统状态"""
    _ = api_key  # 验证通过
    DATA_DIR = get_data_dir_safe()
    profiles_count = len(list((DATA_DIR / "profiles").glob("*"))) if (DATA_DIR / "profiles").exists() else 0
    groups_count = len(list((DATA_DIR / "groups").glob("*"))) if (DATA_DIR / "groups").exists() else 0

    total_memory_files = 0
    if (DATA_DIR / "profiles").exists():
        for p_dir in (DATA_DIR / "profiles").iterdir():
            if p_dir.is_dir() and (p_dir / "memory").exists():
                total_memory_files += len(list((p_dir / "memory").glob("*.md")))

    if (DATA_DIR / "groups").exists():
        for g_dir in (DATA_DIR / "groups").iterdir():
            if g_dir.is_dir() and (g_dir / "memory").exists():
                total_memory_files += len(list((g_dir / "memory").glob("*.md")))

    key_dir = _get_key_storage_dir()
    password_file = key_dir / ".web_ui_password"
    key_file = key_dir / ".web_ui_key"

    initialized = is_initialized() if is_initialized() else DATA_DIR.exists()

    return {
        "status": "running",
        "initialized": initialized,
        "data_dir": str(DATA_DIR),
        "profiles_count": profiles_count,
        "groups_count": groups_count,
        "total_memory_files": total_memory_files,
        "timestamp": datetime.now().isoformat(),
        "debug": {
            "api_key_loaded": bool(_api_key),
            "api_key_is_hashed": _api_key.startswith("$2b$") if _api_key else False,
            "key_dir": str(key_dir),
            "password_file_exists": password_file.exists(),
            "key_file_exists": key_file.exists(),
            "env_key_set": bool(os.environ.get("SCRIPTOR_API_KEY", "")),
        },
    }


@app.get("/api/profiles", dependencies=[Depends(get_api_key)])
async def list_profiles():
    """获取所有用户画像列表"""
    DATA_DIR = get_data_dir_safe()
    profiles_dir = DATA_DIR / "profiles"
    if not profiles_dir.exists():
        return []

    profiles = []
    for p in profiles_dir.iterdir():
        if p.is_dir():
            uid = p.name.replace("user_", "") if p.name.startswith("user_") else p.name
            profile_info = {"uid": uid}

            profile_file = p / "PROFILE.md"
            if profile_file.exists():
                try:
                    content = profile_file.read_text(encoding="utf-8")
                    lines = content.strip().split("\n")
                    if lines:
                        profile_info["name"] = lines[0].strip("# ")
                except (IOError, OSError) as e:
                    logger.warning(f"[灵笔司书] 读取用户画像文件失败: {e}")

            profiles.append(profile_info)
    return profiles


@app.get("/api/profiles/{uid}", dependencies=[Depends(get_api_key)])
async def get_profile_detail(uid: str):
    """获取用户详细信息"""
    DATA_DIR = get_data_dir_safe()
    profile_dir = DATA_DIR / "profiles" / uid
    if not profile_dir.exists():
        raise HTTPException(status_code=404, detail="Profile not found")

    detail = {"uid": uid, "files": {}}

    personal_files = [
        ("Personal_PROFILE.md", "PROFILE.md"),
        ("Personal_MEMORY.md", "MEMORY.md"),
        ("Personal_SOUL.md", "SOUL.md"),
        ("AGENTS.md", "AGENTS.md"),
        ("P_SOP.md", "P_SOP.md"),
    ]

    for new_name, old_name in personal_files:
        file_path = profile_dir / new_name
        if not file_path.exists():
            file_path = profile_dir / old_name
        if file_path.exists():
            detail["files"][file_path.name] = file_path.read_text(encoding="utf-8")

    return detail


@app.get("/api/profiles/{uid}/memory", dependencies=[Depends(get_api_key)])
async def get_profile_memory(uid: str):
    """获取指定用户的记忆文件列表"""
    DATA_DIR = get_data_dir_safe()
    memory_dir = DATA_DIR / "profiles" / uid / "memory"
    if not memory_dir.exists():
        return []

    memories = []
    for m in memory_dir.glob("*.md"):
        stat = m.stat()
        memories.append(
            {
                "filename": m.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "modified_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return sorted(memories, key=lambda x: x["modified"], reverse=True)


@app.get("/api/profiles/{uid}/memory/{filename}", dependencies=[Depends(get_api_key)])
async def read_memory_file(uid: str, filename: str):
    """读取具体的记忆文件内容"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "profiles" / uid / "memory" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return {"content": file_path.read_text(encoding="utf-8")}


@app.put("/api/profiles/{uid}/memory/{filename}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def update_memory_file(uid: str, filename: str, update: MemoryUpdate):
    """更新具体的记忆文件内容"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "profiles" / uid / "memory" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    lock = await get_file_lock(file_path)
    async with lock:
        file_path.write_text(update.content, encoding="utf-8")

    try:
        asyncio.create_task(trigger_reindex())
    except (RuntimeError, asyncio.RuntimeError) as e:
        logger.warning(f"[灵笔司书] 触发索引失败: {e}")

    return {"status": "success", "reindex_triggered": True}


@app.delete("/api/profiles/{uid}/memory/{filename}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def delete_profile_memory_file(uid: str, filename: str):
    """删除用户的记忆文件"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "profiles" / uid / "memory" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.unlink()
        asyncio.create_task(trigger_reindex())
    except (RuntimeError, asyncio.RuntimeError) as e:
        logger.warning(f"[灵笔司书] 触发索引失败: {e}")

    return {"status": "success", "message": "File deleted"}


@app.get("/api/groups", dependencies=[Depends(get_api_key)])
async def list_groups():
    """获取所有群体列表"""
    DATA_DIR = get_data_dir_safe()
    groups_dir = DATA_DIR / "groups"
    if not groups_dir.exists():
        return []

    groups = []
    for g in groups_dir.iterdir():
        if g.is_dir():
            group_id = g.name.replace("group_", "") if g.name.startswith("group_") else g.name
            group_info = {"group_id": group_id}

            group_file = g / "GROUP.md"
            if group_file.exists():
                try:
                    content = group_file.read_text(encoding="utf-8")
                    lines = content.strip().split("\n")
                    if lines:
                        group_info["name"] = lines[0].strip("# ")
                except (IOError, OSError) as e:
                    logger.warning(f"[灵笔司书] 读取群体文件失败: {e}")

            groups.append(group_info)
    return groups


@app.get("/api/groups/{group_id}", dependencies=[Depends(get_api_key)])
async def get_group_detail(group_id: str):
    """获取群体详细信息"""
    DATA_DIR = get_data_dir_safe()
    group_dir = DATA_DIR / "groups" / group_id
    if not group_dir.exists():
        raise HTTPException(status_code=404, detail="Group not found")

    detail = {"group_id": group_id, "files": {}}

    group_files = [("GROUP.md", "GROUP.md"), ("Group_MEMORY.md", "MEMORY.md"), ("MEMBERS.md", "MEMBERS.md")]

    for new_name, old_name in group_files:
        file_path = group_dir / new_name
        if not file_path.exists():
            file_path = group_dir / old_name
        if file_path.exists():
            detail["files"][file_path.name] = file_path.read_text(encoding="utf-8")

    return detail


@app.get("/api/groups/{group_id}/memory", dependencies=[Depends(get_api_key)])
async def get_group_memory(group_id: str):
    """获取指定群体的记忆文件列表"""
    DATA_DIR = get_data_dir_safe()
    memory_dir = DATA_DIR / "groups" / group_id / "memory"
    if not memory_dir.exists():
        return []

    memories = []
    for m in memory_dir.glob("*.md"):
        stat = m.stat()
        memories.append(
            {
                "filename": m.name,
                "size": stat.st_size,
                "modified": stat.st_mtime,
                "modified_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return sorted(memories, key=lambda x: x["modified"], reverse=True)


@app.get("/api/groups/{group_id}/memory/{filename}", dependencies=[Depends(get_api_key)])
async def read_group_memory_file(group_id: str, filename: str):
    """读取群体的记忆文件内容"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "groups" / group_id / "memory" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    return {"content": file_path.read_text(encoding="utf-8")}


@app.put("/api/groups/{group_id}/memory/{filename}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def update_group_memory_file(group_id: str, filename: str, update: MemoryUpdate):
    """更新群体的记忆文件内容"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "groups" / group_id / "memory" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    lock = await get_file_lock(file_path)
    async with lock:
        file_path.write_text(update.content, encoding="utf-8")

    try:
        asyncio.create_task(trigger_reindex())
    except (RuntimeError, asyncio.RuntimeError) as e:
        logger.warning(f"[灵笔司书] 触发索引失败: {e}")

    return {"status": "success", "reindex_triggered": True}


@app.delete("/api/groups/{group_id}/memory/{filename}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def delete_group_memory_file(group_id: str, filename: str):
    """删除群体的记忆文件"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "groups" / group_id / "memory" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")

    try:
        file_path.unlink()
        asyncio.create_task(trigger_reindex())
    except (RuntimeError, asyncio.RuntimeError) as e:
        logger.warning(f"[灵笔司书] 触发索引失败: {e}")

    return {"status": "success", "message": "File deleted"}


@app.get("/api/ledger", dependencies=[Depends(get_api_key)])
async def list_ledger_sessions():
    """获取对话总账会话列表"""
    DATA_DIR = get_data_dir_safe()
    ledger_dir = DATA_DIR / "ledger"
    if not ledger_dir.exists():
        return []

    sessions = []
    for f in ledger_dir.glob("*.json"):
        stat = f.stat()
        sessions.append(
            {
                "filename": f.name,
                "modified": stat.st_mtime,
                "modified_str": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return sorted(sessions, key=lambda x: x["modified"], reverse=True)


@app.get("/api/ledger/{filename}", dependencies=[Depends(get_api_key)])
async def get_ledger_session(filename: str):
    """获取对话总账会话详情"""
    DATA_DIR = get_data_dir_safe()
    file_path = DATA_DIR / "ledger" / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {"messages": data}
    except (json.JSONDecodeError, IOError, OSError) as e:
        logger.error(f"[灵笔司书] 读取会话文件失败: {e}")
        raise HTTPException(status_code=500, detail="Failed to read session")


@app.post("/api/maintenance/reindex", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
@limiter.limit("5/minute")
async def trigger_reindex_endpoint(request: Request):
    """手动触发重新索引"""
    try:
        success = await trigger_reindex()
        return {"status": "success" if success else "failed", "triggered": True}
    except (RuntimeError, asyncio.RuntimeError) as e:
        logger.error(f"[灵笔司书] 重新索引失败: {e}")
        raise HTTPException(status_code=500, detail="Reindex failed")


@app.post("/api/csrf/token")
async def create_csrf_token(request: Request):
    """创建 CSRF 令牌"""
    session_id = request.headers.get("X-Session-ID", "default")
    token = CSRFProtection.create_token(session_id)
    return {"csrf_token": token, "expires_in": CSRF_TOKEN_EXPIRE_MINUTES * 60}


@app.get("/api/csrf/token")
async def get_csrf_token(request: Request):
    """获取 CSRF 令牌 (GET 请求不验证)"""
    session_id = request.headers.get("X-Session-ID", "default")
    token = CSRFProtection.create_token(session_id)
    return {"csrf_token": token, "expires_in": CSRF_TOKEN_EXPIRE_MINUTES * 60}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=18111)


class KnowledgeItemCreate(BaseModel):
    title: str
    content: str
    knowledge_type: str = "fact"
    tags: list[str] = []
    category: str = ""
    is_active: bool = True


@app.get("/api/knowledge", dependencies=[Depends(get_api_key)])
async def list_knowledge():
    """获取知识库所有条目"""
    kb = get_knowledge_base_safe()
    if kb is None:
        logger.warning("[Scriptor Web UI] 知识库实例不可用，返回空列表")
        return []

    items = kb.get_all_items()
    logger.info(f"[Scriptor Web UI] 返回知识库列表: {len(items)} 条")
    return [
        {
            "id": item.id,
            "title": item.title,
            "content": item.content,
            "knowledge_type": item.knowledge_type.value,
            "tags": item.tags,
            "category": item.category,
            "is_active": item.is_active,
            "source": item.source,
            "useful_count": item.useful_count,
            "useful_score": item.useful_score,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }
        for item in items
    ]


@app.post("/api/knowledge", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def create_knowledge(item_data: KnowledgeItemCreate, request: Request):
    """创建知识库条目"""
    logger.info(f"[Scriptor Web UI] 收到添加知识请求: title={item_data.title}, type={item_data.knowledge_type}")

    kb = get_knowledge_base_safe()
    if kb is None:
        logger.error("[Scriptor Web UI] 知识库未初始化，无法添加条目")
        raise HTTPException(status_code=503, detail="Knowledge base not initialized. Check server logs for details.")

    # 动态导入 KnowledgeItem 和 KnowledgeType（使用相同的导入方式）
    import importlib.util
    import sys
    from pathlib import Path

    web_dir = Path(__file__).parent
    plugin_dir = web_dir.parent
    kb_file = plugin_dir / "core" / "knowledge_base.py"

    spec = importlib.util.spec_from_file_location("knowledge_base", kb_file)
    kb_module = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_base"] = kb_module
    spec.loader.exec_module(kb_module)

    KnowledgeItem = kb_module.KnowledgeItem
    KnowledgeType = kb_module.KnowledgeType

    type_map = {
        "fact": KnowledgeType.FACT,
        "skill": KnowledgeType.SKILL,
        "preference": KnowledgeType.PREFERENCE,
        "rule": KnowledgeType.RULE,
        "experience": KnowledgeType.EXPERIENCE,
        "reference": KnowledgeType.REFERENCE,
    }

    kb_type = type_map.get(item_data.knowledge_type.lower(), KnowledgeType.FACT)

    try:
        item = KnowledgeItem.create(
            title=item_data.title,
            content=item_data.content,
            knowledge_type=kb_type,
            tags=item_data.tags,
            category=item_data.category,
            is_active=item_data.is_active,
            source="Web UI",
        )

        success = kb.add_item(item)
        if not success:
            logger.error(f"[Scriptor Web UI] 添加知识条目失败: {item.title}")
            raise HTTPException(status_code=500, detail="Failed to add knowledge item")

        logger.info(f"[Scriptor Web UI] 成功添加知识条目: {item.title} (ID: {item.id})")
        return {"status": "success", "id": item.id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 添加知识条目异常: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to add knowledge item: {e!s}")


@app.get("/api/knowledge/{item_id}", dependencies=[Depends(get_api_key)])
async def get_knowledge_item(item_id: str):
    """获取单个知识库条目详情"""
    kb = get_knowledge_base_safe()
    if kb is None:
        raise HTTPException(status_code=503, detail="Knowledge base not initialized")

    item = kb.get_item(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Knowledge item not found")

    return {
        "id": item.id,
        "title": item.title,
        "content": item.content,
        "knowledge_type": item.knowledge_type.value,
        "tags": item.tags,
        "category": item.category,
        "is_active": item.is_active,
        "source": item.source,
        "useful_count": item.useful_count,
        "useful_score": item.useful_score,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


class KnowledgeItemUpdate(BaseModel):
    """知识库条目更新请求"""

    title: str
    content: str
    knowledge_type: str = "fact"
    tags: list[str] = []
    category: str = ""
    is_active: bool = True


@app.put("/api/knowledge/{item_id}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def update_knowledge(item_id: str, item_data: KnowledgeItemUpdate, request: Request):
    """更新知识库条目（需要 Sudo 权限）"""
    session_id = _get_session_id(request)
    if not _check_sudo_session(session_id):
        raise HTTPException(
            status_code=403, detail="Sudo mode required. Please verify your password to edit knowledge items."
        )

    kb = get_knowledge_base_safe()
    if kb is None:
        raise HTTPException(status_code=503, detail="Knowledge base not initialized")

    # 动态导入 KnowledgeType
    import importlib.util
    import sys
    from pathlib import Path

    web_dir = Path(__file__).parent
    plugin_dir = web_dir.parent
    kb_file = plugin_dir / "core" / "knowledge_base.py"

    spec = importlib.util.spec_from_file_location("knowledge_base", kb_file)
    kb_module = importlib.util.module_from_spec(spec)
    sys.modules["knowledge_base"] = kb_module
    spec.loader.exec_module(kb_module)

    KnowledgeType = kb_module.KnowledgeType

    type_map = {
        "fact": KnowledgeType.FACT,
        "skill": KnowledgeType.SKILL,
        "preference": KnowledgeType.PREFERENCE,
        "rule": KnowledgeType.RULE,
        "experience": KnowledgeType.EXPERIENCE,
        "reference": KnowledgeType.REFERENCE,
    }

    kb_type = type_map.get(item_data.knowledge_type.lower(), KnowledgeType.FACT)

    success = kb.update_item(
        item_id,
        title=item_data.title,
        content=item_data.content,
        knowledge_type=kb_type,
        tags=item_data.tags,
        category=item_data.category,
        is_active=item_data.is_active,
    )

    if not success:
        raise HTTPException(status_code=404, detail="Knowledge item not found")

    logger.info(f"[Scriptor Web UI] 成功更新知识条目: {item_data.title} (ID: {item_id})")
    return {"status": "success", "id": item_id}


@app.delete("/api/knowledge/{item_id}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def delete_knowledge(item_id: str, request: Request):
    """删除知识库条目（需要 Sudo 权限）"""
    session_id = _get_session_id(request)
    if not _check_sudo_session(session_id):
        raise HTTPException(
            status_code=403, detail="Sudo mode required. Please verify your password to delete knowledge items."
        )

    kb = get_knowledge_base_safe()
    if kb is None:
        raise HTTPException(status_code=503, detail="Knowledge base not initialized")

    success = kb.delete_item(item_id)
    if not success:
        raise HTTPException(status_code=404, detail="Knowledge item not found")

    logger.info(f"[Scriptor Web UI] 成功删除知识条目: {item_id}")
    return {"status": "success"}


@app.get("/api/knowledge/stats", dependencies=[Depends(get_api_key)])
async def get_knowledge_stats():
    """获取知识库统计"""
    kb = get_knowledge_base_safe()
    if kb is None:
        raise HTTPException(status_code=503, detail="Knowledge base not initialized")

    return kb.get_stats()


@app.get("/api/research/tasks", dependencies=[Depends(get_api_key)])
async def list_research_tasks():
    """获取所有研究任务"""
    rt = get_research_tool()
    if rt is None:
        return []

    tasks = rt.get_all_tasks()
    return [
        {
            "id": task.id,
            "topic": task.topic,
            "depth": task.depth.value,
            "status": task.status.value,
            "current_round": task.current_round,
            "max_rounds": task.max_rounds,
            "notes_count": len(task.notes),
            "extracted_knowledge_count": len(task.extracted_knowledge),
            "created_at": task.created_at,
            "completed_at": task.completed_at,
        }
        for task in tasks
    ]


@app.get("/api/debug/stats", dependencies=[Depends(get_api_key)])
async def get_debug_stats():
    """获取调试统计信息（所有模块）"""
    stats = {}

    kb = get_knowledge_base_safe()
    if kb:
        stats["knowledge_base"] = kb.get_stats()

    from ..core.message_sanitizer import get_sanitizer

    sanitizer = get_sanitizer()
    if sanitizer:
        stats["message_sanitizer"] = sanitizer.get_stats()

    from ..core.message_buffering import get_message_buffer

    buffer = get_message_buffer()
    if buffer:
        stats["message_buffer"] = buffer.get_stats()

    from ..core.tool_decoration import get_tool_decorator

    decorator = get_tool_decorator()
    if decorator:
        stats["tool_decorator"] = decorator.get_stats()

    from ..core.session_locks import get_session_lock_manager

    lock_manager = get_session_lock_manager()
    if lock_manager:
        stats["session_locks"] = lock_manager.get_stats()

    return stats


class ExportRequest(BaseModel):
    type: str = "all"
    format: str = "json"


@app.post("/api/export", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
@limiter.limit("10/minute")
async def export_data(request: Request, request_data: ExportRequest):
    """导出数据"""
    DATA_DIR = get_data_dir_safe()
    export_data = {}

    if request_data.type in ["all", "knowledge"]:
        kb = get_knowledge_base_safe()
        if kb:
            items = kb.get_all_items()
            export_data["knowledge"] = [
                {
                    "title": item.title,
                    "content": item.content,
                    "knowledge_type": item.knowledge_type.value,
                    "tags": item.tags,
                    "category": item.category,
                    "is_active": item.is_active,
                    "source": item.source,
                }
                for item in items
            ]

    if request_data.format == "json":
        return export_data
    else:
        raise HTTPException(status_code=400, detail="Unsupported format")


@app.get("/api/config", dependencies=[Depends(get_api_key)])
async def get_config_endpoint():
    """获取当前配置"""
    cfg = get_config()
    if cfg:
        return cfg.to_dict(include_sensitive=True)

    # 备用方案：从磁盘读取配置
    try:
        data_dir = get_data_dir_safe()
        config_file = data_dir / "config.json"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"[Scriptor Web UI] 从磁盘读取配置失败: {e}")

    # 备用方案2：返回默认配置
    try:
        from ..core.config_pydantic import ScriptorConfigPydantic

        default_config = ScriptorConfigPydantic()
        return default_config.to_dict(include_sensitive=True)
    except Exception as e:
        logger.debug(f"[Scriptor Web UI] 加载默认配置失败: {e}")

    # 如果还是没有，返回一个空字典而不是报错
    return {"status": "unavailable", "message": "Config only available in main process"}


class ConfigUpdate(BaseModel):
    """配置更新请求体"""

    config: dict


class PasswordUpdate(BaseModel):
    """密码更新请求体"""

    current_password: str
    password: str


@app.put("/api/config", dependencies=[Depends(get_api_key)])
async def update_config_endpoint(request: Request, data: ConfigUpdate):
    """更新配置（双向同步到 AstrBot 配置文件，保存后重启生效）"""
    try:
        data_dir = get_data_dir_safe()

        # 保存到 Scriptor 配置文件
        scriptor_config_file = data_dir / "config.json"
        scriptor_config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(scriptor_config_file, "w", encoding="utf-8") as f:
            json.dump(data.config, f, ensure_ascii=False, indent=2)

        # 同时同步到 AstrBot 配置文件
        astrbot_config_dir = data_dir.parent.parent / "config" / "astrbot_plugin_scriptor_config.json"
        astrbot_config_file = astrbot_config_dir / "astrbot_plugin_scriptor_config.json"
        astrbot_config_file.parent.mkdir(parents=True, exist_ok=True)

        # 将嵌套配置转换为扁平配置
        flat_config = _convert_nested_to_flat(data.config)

        with open(astrbot_config_file, "w", encoding="utf-8") as f:
            json.dump(flat_config, f, ensure_ascii=False, indent=2)

        logger.info(f"[Scriptor WebUI] 配置已同步到 AstrBot 配置文件: {astrbot_config_file}")

        return {"status": "success", "message": "配置已保存，重启 AstrBot 后生效"}
    except Exception as e:
        logger.error(f"[Scriptor WebUI] 保存配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存配置失败: {e!s}")


def _convert_nested_to_flat(nested_config: dict, parent_key: str = "") -> dict:
    """将嵌套配置转换为扁平配置（用于 AstrBot 配置文件）"""
    flat_config = {}

    for key, value in nested_config.items():
        full_key = f"{parent_key}{key}" if parent_key else key

        if isinstance(value, dict):
            # 递归处理嵌套字典
            flat_config.update(_convert_nested_to_flat(value, full_key + "_"))
        else:
            flat_config[full_key] = value

    return flat_config


@app.put("/api/password", dependencies=[Depends(get_api_key)])
async def update_password_endpoint(request: Request, data: PasswordUpdate):
    """更新 Web UI 登录密码（使用 bcrypt 哈希存储）

    需要验证当前密码才能修改
    """
    global _api_key, _api_key_mtime

    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少 6 位")

    if _api_key.startswith("$2b$"):
        if not _verify_password(data.current_password, _api_key):
            raise HTTPException(status_code=401, detail="当前密码错误")
    else:
        if data.current_password != _api_key:
            raise HTTPException(status_code=401, detail="当前密码错误")

    try:
        data_dir = _get_key_storage_dir()
        password_file = data_dir / ".web_ui_password"

        password_file.parent.mkdir(parents=True, exist_ok=True)

        hashed_password = _hash_password(data.password)
        password_file.write_text(hashed_password, encoding="utf-8")

        _api_key = hashed_password
        _api_key_mtime = password_file.stat().st_mtime

        return {"status": "success", "message": "密码已修改，立即生效"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"保存密码失败: {e!s}")


@app.get("/api/metrics", dependencies=[Depends(get_api_key)])
@limiter.limit("30/minute")
async def get_metrics(request: Request):
    """获取系统性能指标"""
    import psutil

    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    # 模拟一些历史数据，或者如果需要真实历史数据，可以从数据库读取
    # 这里为了简单，返回当前值
    return {
        "cpu_percent": cpu_percent,
        "memory_percent": memory.percent,
        "disk_percent": disk.percent,
        "history": [cpu_percent],  # 简化的历史数据
    }


@app.get("/api/performance/stats", dependencies=[Depends(get_api_key)])
@limiter.limit("30/minute")
async def get_performance_stats(request: Request):
    """获取性能与成本统计数据"""
    DATA_DIR = get_data_dir_safe()

    db_size_mb = 0.0
    try:
        db_path = DATA_DIR / "chroma_db"
        if db_path.exists():
            total_size = sum(f.stat().st_size for f in db_path.rglob("*") if f.is_file())
            db_size_mb = total_size / (1024 * 1024)
    except (OSError, PermissionError) as e:
        logger.warning(f"[灵笔司书] 计算数据库大小失败: {e}")

    memory_usage_mb = 0.0
    try:
        import psutil

        process = psutil.Process(os.getpid())
        memory_usage_mb = process.memory_info().rss / (1024 * 1024)
    except (ImportError, psutil.NoSuchProcess) as e:
        logger.warning(f"[灵笔司书] 获取内存使用失败: {e}")

    return {
        "cpu_percent": round(psutil.cpu_percent(interval=0.1), 2),
        "memory_usage_mb": round(memory_usage_mb, 2),
        "db_size_mb": round(db_size_mb, 2),
    }


# --- 司书档案馆 (Archives) 路由 ---


@app.get("/api/archives", dependencies=[Depends(get_api_key)])
async def list_archives():
    """获取所有已导入的档案列表（支持三级架构）"""

    try:
        data_dir = get_data_dir_safe()
        logger.info(f"[Scriptor Web UI] 档案列表请求 - 数据目录: {data_dir}")

        if not data_dir or not data_dir.exists():
            logger.error(f"[Scriptor Web UI] 数据目录不存在: {data_dir}")
            return []

        archives = _get_all_archives_direct(data_dir)
        logger.info(f"[Scriptor Web UI] 获取到 {len(archives)} 条档案")

        return archives
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 获取档案列表失败: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return []


def _get_all_archives_direct(data_dir: Path):
    """直接从文件系统读取档案列表（备用方案）"""
    import sqlite3

    result = []

    def _read_archives_from_db(db_path: Path, scope: str, scope_label: str, target_id: str = None):
        if not db_path.exists():
            return []
        try:
            with sqlite3.connect(str(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM archive_registry")
                rows = cursor.fetchall()
                archives = []
                for row in rows:
                    item = dict(row)
                    item["scope"] = scope
                    item["scope_label"] = scope_label
                    if target_id:
                        item["target_id"] = target_id
                    item["db_path"] = str(db_path)
                    import_time = item.get("import_time")
                    if import_time:
                        item["import_time"] = str(import_time)
                    archives.append(item)
                return archives
        except Exception as e:
            logger.warning(f"[Scriptor] 读取档案库失败 {db_path}: {e}")
            return []

    global_db = data_dir / "global" / "archives.db"
    result.extend(_read_archives_from_db(global_db, "global", "全局"))

    groups_dir = data_dir / "groups"
    if groups_dir.exists():
        for group_dir in groups_dir.iterdir():
            if group_dir.is_dir() and "group_" in group_dir.name:
                # 支持任意前缀的群组目录：*_group_xxx（如 QQ_group_123, Telegram_group_456）
                # 提取 group_ 后面的部分作为 group_id
                parts = group_dir.name.split("group_", 1)
                if len(parts) == 2:
                    group_id = parts[1]
                else:
                    continue
                group_db = group_dir / "archives.db"
                result.extend(_read_archives_from_db(group_db, "group", f"群组 {group_id}", group_id))

    profiles_dir = data_dir / "profiles"
    if profiles_dir.exists():
        for profile_dir in profiles_dir.iterdir():
            if profile_dir.is_dir() and "user_" in profile_dir.name:
                # 支持任意前缀的用户目录：*_user_xxx（如 QQ_user_123, Telegram_user_456）
                # 提取 user_ 后面的部分作为 uid
                parts = profile_dir.name.split("user_", 1)
                if len(parts) == 2:
                    uid = parts[1]
                else:
                    continue
                personal_db = profile_dir / "archives.db"
                result.extend(_read_archives_from_db(personal_db, "personal", f"个人 {uid}", uid))

    return result


@app.get("/api/archives/{table_name}/preview")
async def preview_archive(
    table_name: str,
    scope: str = "global",
    target_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    api_key: str = Depends(get_api_key),
):
    """预览档案内容"""
    import sqlite3

    _ = api_key
    data_dir = get_data_dir_safe()

    # 根据 scope 确定数据库路径（支持 *_group_* 和 *_user_* 格式）
    if scope == "global":
        db_path = data_dir / "global" / "archives.db"
    elif scope == "group":
        if not target_id:
            raise HTTPException(status_code=400, detail="群组档案需要提供 target_id")
        # 查找群组目录
        groups_dir = data_dir / "groups"
        db_path = None
        target_id_str = str(target_id)
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    if len(parts) == 2 and (parts[1] == target_id_str or target_id_str in group_dir.name):
                        db_path = group_dir / "archives.db"
                        break
                    if group_dir.name == target_id_str:
                        db_path = group_dir / "archives.db"
                        break
        if not db_path:
            # 如果 target_id 已经包含 group_，直接使用；否则添加前缀
            if "group_" in target_id_str:
                db_path = groups_dir / target_id_str / "archives.db"
            else:
                db_path = groups_dir / f"group_{target_id_str}" / "archives.db"
    elif scope == "personal":
        if not target_id:
            raise HTTPException(status_code=400, detail="个人档案需要提供 target_id")
        # 查找用户目录
        profiles_dir = data_dir / "profiles"
        db_path = None
        target_id_str = str(target_id)
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    if len(parts) == 2 and (parts[1] == target_id_str or target_id_str in profile_dir.name):
                        db_path = profile_dir / "archives.db"
                        break
                    if profile_dir.name == target_id_str:
                        db_path = profile_dir / "archives.db"
                        break
        if not db_path:
            # 如果 target_id 已经包含 user_，直接使用；否则添加前缀
            if "user_" in target_id_str:
                db_path = profiles_dir / target_id_str / "archives.db"
            else:
                db_path = profiles_dir / f"user_{target_id_str}" / "archives.db"
    else:
        raise HTTPException(status_code=400, detail=f"未知的 scope: {scope}")

    if not db_path.exists():
        raise HTTPException(status_code=404, detail=f"档案库不存在: {db_path}")

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # 获取总行数
            cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            total_count = cursor.fetchone()[0]

            # 获取数据
            cursor.execute(f'SELECT * FROM "{table_name}" LIMIT ? OFFSET ?', (limit, offset))
            rows = cursor.fetchall()

            # 获取列名
            columns = list(rows[0].keys()) if rows else []

            # 转换为字典列表
            data = [dict(row) for row in rows]

            return {
                "table_name": table_name,
                "columns": columns,
                "data": data,
                "total_count": total_count,
                "limit": limit,
                "offset": offset,
                "has_more": (offset + limit) < total_count,
            }
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 预览档案失败: {e}")
        raise HTTPException(status_code=500, detail=f"预览失败: {e!s}")


@app.post("/api/archives/upload", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def upload_archive(
    request: Request,
    file: UploadFile = File(...),
    display_name: Optional[str] = Form(None),
    description: Optional[str] = Form(""),
    sheet_name: Optional[str] = Form(None),
    delimiter: Optional[str] = Form(None),
    scope: str = Form("personal"),
    target_id: Optional[str] = Form(None),
):
    """上传并导入 Excel/CSV/TXT 档案"""
    if scope == "global":
        if not _check_sudo_session(_get_session_id(request)):
            raise HTTPException(status_code=403, detail="导入到全局档案库需要 Sudo 权限")

    data_dir = get_data_dir_safe()

    if scope == "global":
        db_path = data_dir / "global" / "archives.db"
    elif scope == "group":
        if not target_id:
            raise HTTPException(status_code=400, detail="群组档案需要提供 target_id")
        db_path = data_dir / "groups" / target_id / "archives.db"
    elif scope == "personal":
        if target_id:
            db_path = data_dir / "profiles" / f"user_{target_id}" / "archives.db"
        else:
            db_path = data_dir / "profiles" / "archives.db"
    else:
        raise HTTPException(status_code=400, detail=f"未知的 scope: {scope}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in [".xlsx", ".xls", ".csv", ".txt"]:
        raise HTTPException(status_code=400, detail="Only .xlsx, .xls, .csv, and .txt files are supported")

    temp_path = get_data_dir_safe() / f"temp_{file.filename}"
    try:
        with open(temp_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)

        # 动态导入 DataIngestor（支持子进程模式）
        import importlib.util
        import sys

        web_dir = Path(__file__).parent
        plugin_dir = web_dir.parent

        # 先导入 ArchiveManager（被依赖模块）
        manager_file = plugin_dir / "core" / "archives" / "manager.py"
        manager_spec = importlib.util.spec_from_file_location("manager", manager_file)
        manager_module = importlib.util.module_from_spec(manager_spec)
        sys.modules["manager"] = manager_module
        manager_spec.loader.exec_module(manager_module)

        # 再导入 ingestor（依赖 manager）
        ingestor_file = plugin_dir / "core" / "archives" / "ingestor.py"
        spec = importlib.util.spec_from_file_location("ingestor", ingestor_file)
        ingestor_module = importlib.util.module_from_spec(spec)
        sys.modules["ingestor"] = ingestor_module

        # 手动注入依赖（避免相对导入）
        ingestor_module.ArchiveManager = manager_module.ArchiveManager

        spec.loader.exec_module(ingestor_module)

        DataIngestor = ingestor_module.DataIngestor
        ingestor = DataIngestor(db_path=str(db_path))

        table_name, row_count = ingestor.ingest_excel(
            str(temp_path),
            sheet_name=sheet_name,
            display_name=display_name,
            description=description,
            delimiter=delimiter,
            scope=scope,
        )

        scope_names = {"global": "全局", "group": "群组", "personal": "个人"}
        logger.info(f"[Scriptor Web UI] 已导入档案: {table_name} ({row_count} 行) 到 {scope_names.get(scope, scope)}")
        return {
            "status": "success",
            "table_name": table_name,
            "row_count": row_count,
            "message": f"成功导入 {row_count} 条数据到{scope_names.get(scope, scope)}档案库",
        }
    except Exception as e:
        logger.error(f"[灵笔司书] 导入档案失败: {e}")
        raise HTTPException(status_code=500, detail=f"Import failed: {e!s}")
    finally:
        if temp_path.exists():
            os.remove(temp_path)


@app.delete("/api/archives/{table_name}", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def delete_archive(table_name: str, request: Request):
    """删除指定的档案表"""
    # 从 query params 获取 scope 和 target_id
    scope = request.query_params.get("scope", "global")
    target_id = request.query_params.get("target_id")

    data_dir = get_data_dir_safe()

    # 根据 scope 和 target_id 构建正确的数据库路径
    if scope == "global":
        db_path = data_dir / "global" / "archives.db"
    elif scope == "group":
        if not target_id:
            raise HTTPException(status_code=400, detail="群组档案需要提供 target_id")
        # 需要查找实际的群组目录（支持 *_group_* 格式）
        groups_dir = data_dir / "groups"
        db_path = None
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    if len(parts) == 2 and parts[1] == target_id:
                        db_path = group_dir / "archives.db"
                        break
        if not db_path or not db_path.exists():
            raise HTTPException(status_code=404, detail="群组档案库不存在")
    elif scope == "personal":
        if not target_id:
            raise HTTPException(status_code=400, detail="个人档案需要提供 target_id")
        # 需要查找实际的用户目录（支持 *_user_* 格式）
        profiles_dir = data_dir / "profiles"
        db_path = None
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    if len(parts) == 2 and parts[1] == target_id:
                        db_path = profile_dir / "archives.db"
                        break
        if not db_path or not db_path.exists():
            raise HTTPException(status_code=404, detail="个人档案库不存在")
    else:
        raise HTTPException(status_code=400, detail=f"未知的 scope: {scope}")

    # 动态导入 ArchiveManager
    import importlib.util
    import sys

    web_dir = Path(__file__).parent
    plugin_dir = web_dir.parent
    manager_file = plugin_dir / "core" / "archives" / "manager.py"

    spec = importlib.util.spec_from_file_location("manager", manager_file)
    manager_module = importlib.util.module_from_spec(spec)
    sys.modules["manager"] = manager_module
    spec.loader.exec_module(manager_module)

    ArchiveManager = manager_module.ArchiveManager
    am = ArchiveManager(db_path=str(db_path))

    try:
        am.unregister_table(table_name)
        logger.info(f"[Scriptor Web UI] 已删除档案：{table_name} (scope={scope}, target_id={target_id})")
        return {"status": "success", "message": f"表 {table_name} 已删除"}
    except Exception as e:
        logger.error(f"[灵笔司书] 删除档案失败：{e}")
        raise HTTPException(status_code=500, detail=f"Delete failed: {e!s}")


class ArchiveRenameRequest(BaseModel):
    """档案重命名请求"""

    new_display_name: str


@app.put("/api/archives/{table_name}/rename", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def rename_archive(table_name: str, request: Request, data: ArchiveRenameRequest):
    """重命名档案（需要Sudo权限）"""
    session_id = _get_session_id(request)
    if not _check_sudo_session(session_id):
        raise HTTPException(status_code=403, detail="Sudo mode required for renaming archives")

    scope = request.query_params.get("scope", "global")
    target_id = request.query_params.get("target_id")

    data_dir = get_data_dir_safe()

    # 根据 scope 确定数据库路径（支持 *_group_* 和 *_user_* 格式）
    if scope == "global":
        db_path = data_dir / "global" / "archives.db"
    elif scope == "group":
        if not target_id:
            raise HTTPException(status_code=400, detail="群组档案需要提供 target_id")
        # 查找群组目录
        groups_dir = data_dir / "groups"
        db_path = None
        target_id_str = str(target_id)
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    if len(parts) == 2 and (parts[1] == target_id_str or target_id_str in group_dir.name):
                        db_path = group_dir / "archives.db"
                        break
                    if group_dir.name == target_id_str:
                        db_path = group_dir / "archives.db"
                        break
        if not db_path:
            if "group_" in target_id_str:
                db_path = groups_dir / target_id_str / "archives.db"
            else:
                db_path = groups_dir / f"group_{target_id_str}" / "archives.db"
    elif scope == "personal":
        if not target_id:
            raise HTTPException(status_code=400, detail="个人档案需要提供 target_id")
        # 查找用户目录
        profiles_dir = data_dir / "profiles"
        db_path = None
        target_id_str = str(target_id)
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    if len(parts) == 2 and (parts[1] == target_id_str or target_id_str in profile_dir.name):
                        db_path = profile_dir / "archives.db"
                        break
                    if profile_dir.name == target_id_str:
                        db_path = profile_dir / "archives.db"
                        break
        if not db_path:
            if "user_" in target_id_str:
                db_path = profiles_dir / target_id_str / "archives.db"
            else:
                db_path = profiles_dir / f"user_{target_id_str}" / "archives.db"
    else:
        raise HTTPException(status_code=400, detail=f"未知的 scope: {scope}")

    if not db_path.exists():
        raise HTTPException(status_code=404, detail="档案库不存在")

    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE archive_registry SET display_name = ? WHERE table_name = ?", (data.new_display_name, table_name)
            )
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail=f"档案表 {table_name} 不存在")
            conn.commit()

        logger.info(f"[Scriptor Web UI] 已重命名档案: {table_name} -> {data.new_display_name}")
        return {"status": "success", "message": f"已重命名为 {data.new_display_name}"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 重命名档案失败: {e}")
        raise HTTPException(status_code=500, detail=f"重命名失败: {e!s}")


class ArchiveMoveRequest(BaseModel):
    """档案移动请求"""

    target_scope: str
    target_id: Optional[str] = None


@app.post("/api/archives/{table_name}/move", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def move_archive(table_name: str, request: Request, data: ArchiveMoveRequest):
    """移动档案到其他层级（需要Sudo权限）"""

    session_id = _get_session_id(request)
    if not _check_sudo_session(session_id):
        raise HTTPException(status_code=403, detail="Sudo mode required for moving archives")

    source_scope = request.query_params.get("scope", "global")
    source_target_id = request.query_params.get("target_id")

    data_dir = get_data_dir_safe()

    # 构建源数据库路径（支持 *_group_* 和 *_user_* 格式）
    if source_scope == "global":
        source_db_path = data_dir / "global" / "archives.db"
    elif source_scope == "group":
        if not source_target_id:
            raise HTTPException(status_code=400, detail="源群组档案需要提供 target_id")
        # 查找实际的群组目录
        groups_dir = data_dir / "groups"
        source_db_path = None
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    if len(parts) == 2 and parts[1] == source_target_id:
                        source_db_path = group_dir / "archives.db"
                        break
        if not source_db_path or not source_db_path.exists():
            raise HTTPException(status_code=404, detail="源群组档案库不存在")
    elif source_scope == "personal":
        if not source_target_id:
            raise HTTPException(status_code=400, detail="源个人档案需要提供 target_id")
        # 查找实际的用户目录
        profiles_dir = data_dir / "profiles"
        source_db_path = None
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    if len(parts) == 2 and parts[1] == source_target_id:
                        source_db_path = profile_dir / "archives.db"
                        break
        if not source_db_path or not source_db_path.exists():
            raise HTTPException(status_code=404, detail="源个人档案库不存在")
    else:
        raise HTTPException(status_code=400, detail=f"未知的源 scope: {source_scope}")

    # 构建目标数据库路径（支持 *_group_* 和 *_user_* 格式）
    if data.target_scope == "global":
        target_db_path = data_dir / "global" / "archives.db"
    elif data.target_scope == "group":
        if not data.target_id:
            raise HTTPException(status_code=400, detail="目标群组档案需要提供 target_id")
        # 查找或创建目标群组目录
        groups_dir = data_dir / "groups"
        target_db_path = None
        target_id_str = str(data.target_id)
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    # 匹配：parts[1] == target_id（数字）或目录名包含 target_id（完整名称）
                    if len(parts) == 2 and (
                        parts[1] == target_id_str or target_id_str in group_dir.name or target_id_str == parts[1]
                    ):
                        target_db_path = group_dir / "archives.db"
                        break
                    # 也检查目录名是否完全匹配 target_id
                    if group_dir.name == target_id_str:
                        target_db_path = group_dir / "archives.db"
                        break
        if not target_db_path:
            # 如果 target_id 已经包含 group_，直接使用；否则添加前缀
            if "group_" in target_id_str:
                target_db_path = groups_dir / target_id_str / "archives.db"
            else:
                target_db_path = groups_dir / f"group_{target_id_str}" / "archives.db"
        target_db_path.parent.mkdir(parents=True, exist_ok=True)
    elif data.target_scope == "personal":
        if not data.target_id:
            raise HTTPException(status_code=400, detail="目标个人档案需要提供 target_id")
        # 查找或创建目标用户目录
        profiles_dir = data_dir / "profiles"
        target_db_path = None
        target_id_str = str(data.target_id)
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    # 匹配：parts[1] == target_id（数字）或目录名包含 target_id（完整名称）
                    if len(parts) == 2 and (
                        parts[1] == target_id_str or target_id_str in profile_dir.name or target_id_str == parts[1]
                    ):
                        target_db_path = profile_dir / "archives.db"
                        break
                    # 也检查目录名是否完全匹配 target_id
                    if profile_dir.name == target_id_str:
                        target_db_path = profile_dir / "archives.db"
                        break
        if not target_db_path:
            # 如果 target_id 已经包含 user_，直接使用；否则添加前缀
            if "user_" in target_id_str:
                target_db_path = profiles_dir / target_id_str / "archives.db"
            else:
                target_db_path = profiles_dir / f"user_{target_id_str}" / "archives.db"
        target_db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        raise HTTPException(status_code=400, detail=f"未知的目标 scope: {data.target_scope}")

    if not source_db_path.exists():
        raise HTTPException(status_code=404, detail="源档案库不存在")

    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sqlite3.connect(str(source_db_path)) as source_conn:
            source_conn.row_factory = sqlite3.Row
            source_cursor = source_conn.cursor()

            source_cursor.execute("SELECT * FROM archive_registry WHERE table_name = ?", (table_name,))
            registry_row = source_cursor.fetchone()
            if not registry_row:
                raise HTTPException(status_code=404, detail=f"档案表 {table_name} 不存在")

            source_cursor.execute(f"SELECT * FROM {table_name}")
            all_rows = source_cursor.fetchall()
            columns = list(all_rows[0].keys()) if all_rows else []

        # 第1步：先写入目标数据库（确保目标可写）
        with sqlite3.connect(str(target_db_path)) as target_conn:
            target_cursor = target_conn.cursor()

            target_cursor.execute("""
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

            if all_rows:
                col_defs = ", ".join([f'"{col}" TEXT' for col in columns])
                target_cursor.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')

                placeholders = ", ".join(["?" for _ in columns])
                for row in all_rows:
                    target_cursor.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', tuple(row))

            target_cursor.execute(
                """
                INSERT OR REPLACE INTO archive_registry 
                (table_name, display_name, description, columns_json, row_count, scope)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    table_name,
                    registry_row["display_name"],
                    registry_row["description"],
                    registry_row["columns_json"],
                    registry_row["row_count"],
                    data.target_scope,
                ),
            )
            target_conn.commit()

        # 第2步：目标写入成功后，再删除源数据
        with sqlite3.connect(str(source_db_path)) as source_conn:
            source_cursor = source_conn.cursor()
            source_cursor.execute(f"DROP TABLE IF EXISTS {table_name}")
            source_cursor.execute("DELETE FROM archive_registry WHERE table_name = ?", (table_name,))
            source_conn.commit()

        scope_names = {"global": "全局", "group": "群组", "personal": "个人"}
        logger.info(f"[Scriptor Web UI] 已移动档案: {table_name} 从 {source_scope} 到 {data.target_scope}")
        return {
            "status": "success",
            "message": f"已将档案移动到{scope_names.get(data.target_scope, data.target_scope)}档案库",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 移动档案失败: {e}")
        raise HTTPException(status_code=500, detail=f"移动失败: {e!s}")


@app.post("/api/archives/{table_name}/copy", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def copy_archive(table_name: str, request: Request, data: ArchiveMoveRequest):
    """复制档案到其他层级"""

    source_scope = request.query_params.get("scope", "global")
    source_target_id = request.query_params.get("target_id")

    data_dir = get_data_dir_safe()

    # 构建源数据库路径（支持 *_group_* 和 *_user_* 格式）
    if source_scope == "global":
        source_db_path = data_dir / "global" / "archives.db"
    elif source_scope == "group":
        if not source_target_id:
            raise HTTPException(status_code=400, detail="源群组档案需要提供 target_id")
        groups_dir = data_dir / "groups"
        source_db_path = None
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    if len(parts) == 2 and parts[1] == source_target_id:
                        source_db_path = group_dir / "archives.db"
                        break
        if not source_db_path or not source_db_path.exists():
            raise HTTPException(status_code=404, detail="源群组档案库不存在")
    elif source_scope == "personal":
        if not source_target_id:
            raise HTTPException(status_code=400, detail="源个人档案需要提供 target_id")
        profiles_dir = data_dir / "profiles"
        source_db_path = None
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    if len(parts) == 2 and parts[1] == source_target_id:
                        source_db_path = profile_dir / "archives.db"
                        break
        if not source_db_path or not source_db_path.exists():
            raise HTTPException(status_code=404, detail="源个人档案库不存在")
    else:
        raise HTTPException(status_code=400, detail=f"未知的源 scope: {source_scope}")

    # 构建目标数据库路径（支持 *_group_* 和 *_user_* 格式）
    if data.target_scope == "global":
        target_db_path = data_dir / "global" / "archives.db"
    elif data.target_scope == "group":
        if not data.target_id:
            raise HTTPException(status_code=400, detail="目标群组档案需要提供 target_id")
        groups_dir = data_dir / "groups"
        target_db_path = None
        target_id_str = str(data.target_id)
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    # 匹配：parts[1] == target_id（数字）或目录名包含 target_id（完整名称）
                    if len(parts) == 2 and (
                        parts[1] == target_id_str or target_id_str in group_dir.name or target_id_str == parts[1]
                    ):
                        target_db_path = group_dir / "archives.db"
                        break
                    # 也检查目录名是否完全匹配 target_id
                    if group_dir.name == target_id_str:
                        target_db_path = group_dir / "archives.db"
                        break
        if not target_db_path:
            # 如果 target_id 已经包含 group_，直接使用；否则添加前缀
            if "group_" in target_id_str:
                target_db_path = groups_dir / target_id_str / "archives.db"
            else:
                target_db_path = groups_dir / f"group_{target_id_str}" / "archives.db"
        target_db_path.parent.mkdir(parents=True, exist_ok=True)
    elif data.target_scope == "personal":
        if not data.target_id:
            raise HTTPException(status_code=400, detail="目标个人档案需要提供 target_id")
        profiles_dir = data_dir / "profiles"
        target_db_path = None
        target_id_str = str(data.target_id)
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    # 匹配：parts[1] == target_id（数字）或目录名包含 target_id（完整名称）
                    if len(parts) == 2 and (
                        parts[1] == target_id_str or target_id_str in profile_dir.name or target_id_str == parts[1]
                    ):
                        target_db_path = profile_dir / "archives.db"
                        break
                    # 也检查目录名是否完全匹配 target_id
                    if profile_dir.name == target_id_str:
                        target_db_path = profile_dir / "archives.db"
                        break
        if not target_db_path:
            # 如果 target_id 已经包含 user_，直接使用；否则添加前缀
            if "user_" in target_id_str:
                target_db_path = profiles_dir / target_id_str / "archives.db"
            else:
                target_db_path = profiles_dir / f"user_{target_id_str}" / "archives.db"
        target_db_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        raise HTTPException(status_code=400, detail=f"未知的目标 scope: {data.target_scope}")

    if not source_db_path.exists():
        raise HTTPException(status_code=404, detail="源档案库不存在")

    target_db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with sqlite3.connect(str(source_db_path)) as source_conn:
            source_conn.row_factory = sqlite3.Row
            source_cursor = source_conn.cursor()

            source_cursor.execute("SELECT * FROM archive_registry WHERE table_name = ?", (table_name,))
            registry_row = source_cursor.fetchone()
            if not registry_row:
                raise HTTPException(status_code=404, detail=f"档案表 {table_name} 不存在")

            source_cursor.execute(f"SELECT * FROM {table_name}")
            all_rows = source_cursor.fetchall()
            columns = list(all_rows[0].keys()) if all_rows else []

        with sqlite3.connect(str(target_db_path)) as target_conn:
            target_cursor = target_conn.cursor()

            target_cursor.execute("""
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

            if all_rows:
                col_defs = ", ".join([f'"{col}" TEXT' for col in columns])
                target_cursor.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({col_defs})')

                placeholders = ", ".join(["?" for _ in columns])
                for row in all_rows:
                    target_cursor.execute(f'INSERT INTO "{table_name}" VALUES ({placeholders})', tuple(row))

            target_cursor.execute(
                """
                INSERT OR REPLACE INTO archive_registry 
                (table_name, display_name, description, columns_json, row_count, scope)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    table_name,
                    registry_row["display_name"],
                    registry_row["description"],
                    registry_row["columns_json"],
                    registry_row["row_count"],
                    data.target_scope,
                ),
            )
            target_conn.commit()

        scope_names = {"global": "全局", "group": "群组", "personal": "个人"}
        logger.info(f"[Scriptor Web UI] 已复制档案: {table_name} 从 {source_scope} 到 {data.target_scope}")
        return {
            "status": "success",
            "message": f"已将档案复制到{scope_names.get(data.target_scope, data.target_scope)}档案库",
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 复制档案失败: {e}")
        raise HTTPException(status_code=500, detail=f"复制失败: {e!s}")


@app.get("/api/archives/{table_name}/export")
async def export_archive(
    table_name: str,
    scope: str = "global",
    target_id: Optional[str] = None,
    format: str = "json",
    limit: int = 10000,
    api_key: str = Depends(get_api_key),
):
    """导出档案数据"""
    _ = api_key
    data_dir = get_data_dir_safe()

    # 根据 scope 确定数据库路径（支持 *_group_* 和 *_user_* 格式）
    if scope == "global":
        db_path = data_dir / "global" / "archives.db"
    elif scope == "group":
        if not target_id:
            raise HTTPException(status_code=400, detail="群组档案需要提供 target_id")
        # 查找群组目录
        groups_dir = data_dir / "groups"
        db_path = None
        target_id_str = str(target_id)
        if groups_dir.exists():
            for group_dir in groups_dir.iterdir():
                if group_dir.is_dir() and "group_" in group_dir.name:
                    parts = group_dir.name.split("group_", 1)
                    if len(parts) == 2 and (parts[1] == target_id_str or target_id_str in group_dir.name):
                        db_path = group_dir / "archives.db"
                        break
                    if group_dir.name == target_id_str:
                        db_path = group_dir / "archives.db"
                        break
        if not db_path:
            # 如果 target_id 已经包含 group_，直接使用；否则添加前缀
            if "group_" in target_id_str:
                db_path = groups_dir / target_id_str / "archives.db"
            else:
                db_path = groups_dir / f"group_{target_id_str}" / "archives.db"
    elif scope == "personal":
        if not target_id:
            raise HTTPException(status_code=400, detail="个人档案需要提供 target_id")
        # 查找用户目录
        profiles_dir = data_dir / "profiles"
        db_path = None
        target_id_str = str(target_id)
        if profiles_dir.exists():
            for profile_dir in profiles_dir.iterdir():
                if profile_dir.is_dir() and "user_" in profile_dir.name:
                    parts = profile_dir.name.split("user_", 1)
                    if len(parts) == 2 and (parts[1] == target_id_str or target_id_str in profile_dir.name):
                        db_path = profile_dir / "archives.db"
                        break
                    if profile_dir.name == target_id_str:
                        db_path = profile_dir / "archives.db"
                        break
        if not db_path:
            # 如果 target_id 已经包含 user_，直接使用；否则添加前缀
            if "user_" in target_id_str:
                db_path = profiles_dir / target_id_str / "archives.db"
            else:
                db_path = profiles_dir / f"user_{target_id_str}" / "archives.db"
    else:
        raise HTTPException(status_code=400, detail=f"未知的 scope: {scope}")

    if not db_path.exists():
        raise HTTPException(status_code=404, detail="档案库不存在")

    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            total_count = cursor.fetchone()[0]

            cursor.execute(f'SELECT * FROM "{table_name}" LIMIT ?', (limit,))
            rows = cursor.fetchall()
            columns = list(rows[0].keys()) if rows else []
            data = [dict(row) for row in rows]

        if format == "csv":
            import csv
            import io

            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=columns)
            writer.writeheader()
            writer.writerows(data)

            from fastapi.responses import Response

            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="{table_name}.csv"'},
            )
        else:
            import json

            from fastapi.responses import Response

            return Response(
                content=json.dumps(
                    {
                        "table_name": table_name,
                        "columns": columns,
                        "total_count": total_count,
                        "exported_count": len(data),
                        "data": data,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{table_name}.json"'},
            )
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 导出档案失败: {e}")
        raise HTTPException(status_code=500, detail=f"导出失败: {e!s}")


@app.get("/api/archives/{table_name}/schema", dependencies=[Depends(get_api_key)])
async def get_archive_schema(table_name: str):
    """获取档案表的结构信息"""
    am = get_archive_manager()
    if am is None:
        raise HTTPException(status_code=503, detail="Archive manager not initialized")

    try:
        schema = am.get_table_schema(table_name)
        if schema is None:
            raise HTTPException(status_code=404, detail="Table not found")
        return schema
    except Exception as e:
        logger.error(f"[灵笔司书] 获取档案结构失败: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get schema: {e!s}")


# --- 维护功能路由 ---


@app.get("/api/logs", dependencies=[Depends(get_api_key)])
@limiter.limit("10/minute")
async def get_logs(request: Request, lines: int = 100, source: str = "all"):
    """获取系统日志

    Args:
        lines: 返回的日志行数
        source: 日志来源 (all, api, frontend, astrbot)
    """
    DATA_DIR = get_data_dir_safe()
    results = {}

    # 1. 插件 API 错误日志
    if source in ["all", "api"]:
        api_log = DATA_DIR / "web_api_error.log"
        if api_log.exists():
            try:
                with open(api_log, "r", encoding="utf-8", errors="ignore") as f:
                    all_lines = f.readlines()
                    recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    results["api"] = {
                        "content": "".join(recent),
                        "total_lines": len(all_lines),
                        "returned_lines": len(recent),
                    }
            except Exception as e:
                results["api"] = {"error": str(e)}
        else:
            results["api"] = {"content": "API 日志文件不存在", "total_lines": 0}

    # 2. 前端错误日志
    if source in ["all", "frontend"]:
        frontend_log = DATA_DIR / "web_frontend_error.log"
        if frontend_log.exists():
            try:
                with open(frontend_log, "r", encoding="utf-8", errors="ignore") as f:
                    all_lines = f.readlines()
                    recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
                    results["frontend"] = {
                        "content": "".join(recent),
                        "total_lines": len(all_lines),
                        "returned_lines": len(recent),
                    }
            except Exception as e:
                results["frontend"] = {"error": str(e)}
        else:
            results["frontend"] = {"content": "前端日志文件不存在", "total_lines": 0}

    # 3. AstrBot 主日志（如果启用了文件日志）
    if source in ["all", "astrbot"]:
        astrbot_log_paths = [
            Path(__file__).parent.parent.parent.parent / "logs" / "astrbot.log",
            DATA_DIR.parent.parent / "logs" / "astrbot.log",
        ]

        found = False
        for log_path in astrbot_log_paths:
            if log_path.exists():
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        all_lines = f.readlines()
                        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
                        results["astrbot"] = {
                            "content": "".join(recent),
                            "total_lines": len(all_lines),
                            "returned_lines": len(recent),
                            "path": str(log_path),
                        }
                        found = True
                        break
                except Exception as e:
                    results["astrbot"] = {"error": str(e)}
                    found = True
                    break

        if not found:
            results["astrbot"] = {
                "content": "AstrBot 主日志未启用文件记录。\n请在 AstrBot 配置中启用: log_file_enable = true",
                "total_lines": 0,
                "hint": "AstrBot 默认将日志输出到控制台，如需文件日志请在配置中启用 log_file_enable",
            }

    return {"source": source, "results": results, "timestamp": datetime.now().isoformat()}


@app.post("/api/maintenance/backup", dependencies=[Depends(get_api_key)])
@limiter.limit("3/minute")
async def create_backup(request: Request):
    """创建数据备份"""
    import shutil

    DATA_DIR = get_data_dir_safe()
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"backup_{timestamp}"
    backup_path = backup_dir / backup_name

    try:
        # 创建临时目录
        temp_backup = DATA_DIR / f"temp_backup_{timestamp}"
        temp_backup.mkdir(parents=True, exist_ok=True)

        # 复制需要备份的目录
        dirs_to_backup = ["profiles", "groups", "knowledge", "ledger"]
        files_to_backup = ["config.json"]

        copied_count = 0
        for dir_name in dirs_to_backup:
            src = DATA_DIR / dir_name
            if src.exists():
                dst = temp_backup / dir_name
                shutil.copytree(src, dst)
                copied_count += 1

        for file_name in files_to_backup:
            src = DATA_DIR / file_name
            if src.exists():
                dst = temp_backup / file_name
                shutil.copy2(src, dst)
                copied_count += 1

        # 压缩备份
        shutil.make_archive(str(backup_path), "zip", temp_backup)

        # 清理临时目录
        shutil.rmtree(temp_backup)

        # 清理旧备份（保留最近7个）
        backups = sorted(backup_dir.glob("backup_*.zip"), reverse=True)
        for old_backup in backups[7:]:
            old_backup.unlink()

        backup_size = (backup_dir / f"{backup_name}.zip").stat().st_size / (1024 * 1024)

        return {
            "status": "success",
            "backup_name": f"{backup_name}.zip",
            "backup_size_mb": round(backup_size, 2),
            "items_backed_up": copied_count,
            "backup_path": str(backup_dir / f"{backup_name}.zip"),
        }
    except Exception as e:
        logger.error(f"[灵笔司书] 创建备份失败: {e}")
        raise HTTPException(status_code=500, detail=f"备份失败: {e!s}")


@app.post("/api/maintenance/cleanup", dependencies=[Depends(get_api_key)])
@limiter.limit("3/minute")
async def cleanup_temp_files(request: Request):
    """清理临时文件"""
    import shutil

    DATA_DIR = get_data_dir_safe()

    cleaned_items = []
    cleaned_size = 0

    try:
        # 清理临时文件
        temp_patterns = ["temp_*", "*.tmp", "*.bak"]
        for pattern in temp_patterns:
            for item in DATA_DIR.glob(pattern):
                try:
                    if item.is_file():
                        cleaned_size += item.stat().st_size
                        item.unlink()
                        cleaned_items.append(str(item.name))
                    elif item.is_dir():
                        size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file())
                        cleaned_size += size
                        shutil.rmtree(item)
                        cleaned_items.append(str(item.name))
                except Exception as e:
                    logger.warning(f"[灵笔司书] 清理 {item} 失败: {e}")

        # 清理过期的备份（超过7天）
        backup_dir = DATA_DIR / "backups"
        if backup_dir.exists():
            retention_days = 7
            cutoff = datetime.now() - timedelta(days=retention_days)
            for backup in backup_dir.glob("backup_*.zip"):
                try:
                    mtime = datetime.fromtimestamp(backup.stat().st_mtime)
                    if mtime < cutoff:
                        cleaned_size += backup.stat().st_size
                        backup.unlink()
                        cleaned_items.append(f"过期备份: {backup.name}")
                except Exception as e:
                    logger.warning(f"[灵笔司书] 清理备份 {backup} 失败: {e}")

        return {
            "status": "success",
            "cleaned_items": cleaned_items,
            "cleaned_count": len(cleaned_items),
            "cleaned_size_mb": round(cleaned_size / (1024 * 1024), 2),
        }
    except Exception as e:
        logger.error(f"[灵笔司书] 清理临时文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"清理失败: {e!s}")


@app.get("/api/maintenance/backups", dependencies=[Depends(get_api_key)])
async def list_backups():
    """列出所有备份"""
    DATA_DIR = get_data_dir_safe()
    backup_dir = DATA_DIR / "backups"

    if not backup_dir.exists():
        return []

    backups = []
    for backup in backup_dir.glob("backup_*.zip"):
        stat = backup.stat()
        backups.append(
            {
                "name": backup.name,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    return sorted(backups, key=lambda x: x["created"], reverse=True)


@app.get("/api/config/validate", dependencies=[Depends(get_api_key)])
@limiter.limit("10/minute")
async def validate_config(request: Request):
    """验证配置文件的有效性和完整性"""
    try:
        DATA_DIR = get_data_dir_safe()
        config = get_config()

        if not config:
            return {"valid": False, "errors": ["无法加载配置"], "warnings": [], "info": []}

        errors = []
        warnings = []
        info = []

        # 验证必填配置项
        required_fields = {
            "admin_uids": "管理员 UID 列表",
            "embedding_enabled": "Embedding 开关",
            "web_ui_enabled": "Web UI 开关",
        }

        for field, description in required_fields.items():
            if not hasattr(config, field):
                errors.append(f"缺少必要配置: {description} ({field})")
            elif getattr(config, field) is None:
                warnings.append(f"配置项为空: {description} ({field})")

        # 验证 Embedding 配置
        if getattr(config, "embedding_enabled", False):
            embedding_model = getattr(config, "embedding_model", None)
            if not embedding_model:
                errors.append("已启用 Embedding 但未指定模型名称")

            # 检查模型文件是否存在（如果是本地模型）
            if embedding_model and not embedding_model.startswith("http"):
                model_path = Path(embedding_model)
                if not model_path.exists():
                    warnings.append(f"Embedding 模型文件不存在: {embedding_model}")

        # 验证 API Key 配置
        api_key_file = DATA_DIR / ".api_key"
        if api_key_file.exists():
            info.append("✅ API Key 已配置")
        else:
            warnings.append("⚠️ 未设置 API Key（建议在生产环境设置）")

        # 验证 Web UI 密码
        password_file = DATA_DIR / ".web_ui_password"
        if password_file.exists():
            info.append("✅ Web UI 密码已设置")
        else:
            warnings.append("⚠️ 未设置 Web UI 密码（任何人可访问）")

        # 验证数据目录结构
        expected_dirs = ["profiles", "groups", "global"]
        for dir_name in expected_dirs:
            dir_path = DATA_DIR / dir_name
            if dir_path.exists():
                file_count = len(list(dir_path.rglob("*")))
                info.append(f"📁 {dir_name}/ 目录存在 ({file_count} 个文件)")
            else:
                warnings.append(f"⚠️ 数据目录不存在: {dir_name}/")

        # 验证向量数据库
        chroma_dir = DATA_DIR / "chroma_db"
        if chroma_dir.exists():
            chroma_size = sum(f.stat().st_size for f in chroma_dir.rglob("*") if f.is_file())
            chroma_size_mb = chroma_size / (1024 * 1024)
            info.append(f"📊 ChromaDB 存在 ({chroma_size_mb:.2f} MB)")
        elif getattr(config, "embedding_enabled", False):
            warnings.append("⚠️ 已启用 Embedding 但 ChromaDB 不存在")

        # 检查磁盘空间
        import shutil

        total, used, free = shutil.disk_usage(DATA_DIR)
        free_gb = free / (1024**3)
        free_percent = (free / total) * 100

        if free_gb < 1:  # 少于 1GB
            errors.append(f"❌ 磁盘空间不足: 仅剩 {free_gb:.2f} GB")
        elif free_gb < 5:  # 少于 5GB
            warnings.append(f"⚠️ 磁盘空间较低: 剩余 {free_gb:.2f} GB")
        else:
            info.append(f"💾 磁盘空间充足: {free_gb:.2f} GB 可用 ({free_percent:.1f}%)")

        is_valid = len(errors) == 0

        result = {
            "valid": is_valid,
            "error_count": len(errors),
            "warning_count": len(warnings),
            "info_count": len(info),
            "errors": errors,
            "warnings": warnings,
            "info": info,
        }

        status_code = 200 if is_valid else 400

        return JSONResponse(content=result, status_code=status_code)

    except Exception as e:
        logger.error(f"[Scriptor Web UI] 配置验证失败: {e}")
        raise HTTPException(status_code=500, detail=f"配置验证失败: {e!s}")


@app.get("/api/system/diagnostics", dependencies=[Depends(get_api_key)])
@limiter.limit("10/minute")
async def get_system_diagnostics(request: Request):
    """获取系统诊断信息（用于调试）"""
    try:
        import platform
        import time

        import psutil

        # 系统信息
        system_info = {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
            "process_id": os.getpid(),
            "working_directory": str(Path.cwd()),
        }

        # 资源使用情况
        process = psutil.Process(os.getpid())
        memory_info = process.memory_info()

        resource_info = {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory_rss_mb": round(memory_info.rss / (1024 * 1024), 2),
            "memory_vms_mb": round(memory_info.vms / (1024 * 1024), 2),
            "memory_percent": round(process.memory_percent(), 2),
            "num_threads": process.num_threads(),
            "num_handles": process.num_handles() if hasattr(process, "num_handles") else 0,
            "create_time": datetime.fromtimestamp(process.create_time()).isoformat(),
            "uptime_seconds": time.time() - process.create_time(),
        }

        # 文件系统信息
        DATA_DIR = get_data_dir_safe()
        disk_info = {}

        if DATA_DIR and DATA_DIR.exists():
            usage = shutil.disk_usage(DATA_DIR)
            disk_info = {
                "total_gb": round(usage.total / (1024**3), 2),
                "used_gb": round(usage.used / (1024**3), 2),
                "free_gb": round(usage.free / (1024**3), 2),
                "used_percent": round((usage.used / usage.total) * 100, 2),
            }

            # 统计数据目录大小
            def get_dir_size(path):
                total_size = 0
                for dirpath, _, filenames in os.walk(path):
                    for filename in filenames:
                        filepath = os.path.join(dirpath, filename)
                        try:
                            total_size += os.path.getsize(filepath)
                        except OSError:
                            pass
                return total_size

            data_size_bytes = get_dir_size(DATA_DIR)
            data_size_mb = data_size_bytes / (1024 * 1024)
            disk_info["data_dir_size_mb"] = round(data_size_mb, 2)

        # 网络连接信息
        network_info = {}
        try:
            net_io = psutil.net_io_counters()
            network_info = {
                "bytes_sent": net_io.bytes_sent,
                "bytes_recv": net_io.bytes_recv,
                "packets_sent": net_io.packets_sent,
                "packets_recv": net_io.packets_recv,
            }
        except Exception as e:
            network_info["error"] = str(e)

        # 加载的模块信息
        module_info = {
            "fastapi_installed": True,
            "uvicorn_installed": True,
            "psutil_installed": True,
            "chromadb_available": False,
            "sentence_transformers_available": False,
        }

        try:
            import chromadb

            module_info["chromadb_available"] = True
        except ImportError:
            pass

        try:
            import sentence_transformers

            module_info["sentence_transformers_available"] = True
        except ImportError:
            pass

        return {
            "timestamp": datetime.now().isoformat(),
            "system": system_info,
            "resources": resource_info,
            "disk": disk_info,
            "network": network_info,
            "modules": module_info,
        }

    except ImportError as e:
        logger.error(f"[Scriptor Web UI] 缺少依赖模块: {e}")
        raise HTTPException(status_code=500, detail=f"缺少必要的诊断模块: {e!s}")
    except Exception as e:
        logger.error(f"[Scriptor Web UI] 获取系统诊断失败: {e}")
        raise HTTPException(status_code=500, detail=f"诊断失败: {e!s}")


@app.get("/api/performance/history", dependencies=[Depends(get_api_key)])
@limiter.limit("30/minute")
async def get_performance_history(request: Request, hours: int = 24):
    """获取历史性能数据（最近 N 小时）"""
    try:

        # 使用内存中的历史数据（如果存在）
        # 注意：这是一个简化的实现，生产环境应使用时序数据库或文件存储

        # 返回模拟的历史数据点
        history_points = []
        now = datetime.now()

        # 每 5 分钟一个数据点
        interval_minutes = 5
        num_points = (hours * 60) // interval_minutes

        for i in range(num_points):
            point_time = now - timedelta(minutes=i * interval_minutes)

            # 模拟 CPU 和内存使用率（实际应从监控系统获取）
            import random

            cpu_base = 20 + random.uniform(-10, 30)
            mem_base = 40 + random.uniform(-10, 20)

            # 添加一些周期性波动
            hour_factor = 1 + 0.3 * ((point_time.hour % 12) / 12 - 0.5)

            history_points.append(
                {
                    "timestamp": point_time.isoformat(),
                    "cpu_percent": max(0, min(100, cpu_base * hour_factor)),
                    "memory_percent": max(0, min(100, mem_base * hour_factor)),
                    "request_count": int(random.gauss(10, 3)),
                    "avg_response_time_ms": random.uniform(50, 200),
                }
            )

        return {
            "period_hours": hours,
            "interval_minutes": interval_minutes,
            "points": list(reversed(history_points)),
            "summary": {
                "avg_cpu": sum(p["cpu_percent"] for p in history_points) / len(history_points),
                "avg_memory": sum(p["memory_percent"] for p in history_points) / len(history_points),
                "total_requests": sum(p["request_count"] for p in history_points),
                "peak_cpu": max(p["cpu_percent"] for p in history_points),
                "peak_memory": max(p["memory_percent"] for p in history_points),
            },
        }

    except Exception as e:
        logger.error(f"[Scriptor Web UI] 获取性能历史失败: {e}")
        raise HTTPException(status_code=500, detail=f"获取性能历史失败: {e!s}")


@app.post("/api/maintenance/optimize", dependencies=[Depends(get_api_key), Depends(CSRFVerify())])
async def run_optimization_tasks(request: Request):
    """执行优化任务（清理缓存、重建索引等）"""
    try:
        DATA_DIR = get_data_dir_safe()
        results = []

        # 任务 1: 清理 Python 缓存文件
        cache_cleaned = 0
        for pattern in ["**/__pycache__", "**/*.pyc"]:
            for item in DATA_DIR.rglob(pattern):
                if item.is_dir():
                    import shutil

                    shutil.rmtree(item)
                    cache_cleaned += 1
                elif item.is_file():
                    item.unlink()
                    cache_cleaned += 1

        results.append(
            {"task": "clean_python_cache", "status": "success", "detail": f"清理了 {cache_cleaned} 个缓存文件/目录"}
        )

        # 任务 2: 触发重新索引
        try:
            trigger_reindex()
            results.append({"task": "reindex", "status": "success", "detail": "已触发重新索引"})
        except Exception as e:
            results.append({"task": "reindex", "status": "warning", "detail": f"重新索引触发失败: {e!s}"})

        # 任务 3: 清理过期会话锁
        try:
            session_locks_dir = DATA_DIR / ".session_locks"
            if session_locks_dir.exists():
                lock_files = list(session_locks_dir.glob("*.lock"))
                cleaned_locks = 0

                cutoff = datetime.now() - timedelta(hours=24)
                for lock_file in lock_files:
                    mtime = datetime.fromtimestamp(lock_file.stat().st_mtime)
                    if mtime < cutoff:
                        lock_file.unlink()
                        cleaned_locks += 1

                results.append(
                    {
                        "task": "cleanup_session_locks",
                        "status": "success",
                        "detail": f"清理了 {cleaned_locks} 个过期会话锁",
                    }
                )
            else:
                results.append({"task": "cleanup_session_locks", "status": "skipped", "detail": "无会话锁目录"})
        except Exception as e:
            results.append({"task": "cleanup_session_locks", "status": "warning", "detail": f"清理会话锁失败: {e!s}"})

        success_count = sum(1 for r in results if r["status"] == "success")

        return {
            "status": "completed",
            "total_tasks": len(results),
            "successful": success_count,
            "results": results,
            "executed_at": datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"[Scriptor Web UI] 执行优化任务失败: {e}")
        raise HTTPException(status_code=500, detail=f"优化任务执行失败: {e!s}")


@app.get("/{path:path}", response_class=HTMLResponse)
async def serve_spa(path: str):
    """SPA 路由回退 - 所有非 API/静态文件路由返回 index.html"""
    if path.startswith("api/") or path.startswith("assets/"):
        raise HTTPException(status_code=404, detail="Not found")

    if VUE_DIST_DIR.exists():
        index_file = VUE_DIST_DIR / "index.html"
        if index_file.exists():
            with open(index_file, "r", encoding="utf-8") as f:
                return f.read()

    raise HTTPException(status_code=404, detail="Page not found")
