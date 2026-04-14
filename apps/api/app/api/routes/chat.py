"""聊天路由 - 统一入口端点"""

import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.api.deps import get_current_user, get_db
from app.core.sse import sse
from app.models.user import User
from app.services.agent_tools import get_tool_executor
from app.services.excel_agent import get_excel_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat")


# ============ Request/Response Models ============


class ChatRequest(BaseModel):
    """聊天请求"""

    query: str = Field(..., description="用户查询的自然语言描述")
    file_ids: List[str] = Field(
        default_factory=list,
        description="上传文件返回的 file_id 列表（UUID 字符串），支持多个文件",
    )
    thread_id: Optional[str] = Field(None, description="线程 ID（可选，用于继续会话）")


# ============ Error Codes ============


class ChatErrorCode:
    """错误码常量"""

    INVALID_FILE_IDS = "INVALID_FILE_IDS"


# ============ API Endpoints ============


@router.post("")
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    统一入口端点 (SSE 流式返回)

    接收用户请求，由统一 Excel 助手决定：
    1. 直接回答
    2. 提出澄清
    3. 调用 processing workflow
    4. 调用 analysis workflow
    """

    async def stream():
        try:
            # === 验证并转换 file_ids ===
            file_ids: List[str] = []
            if request.file_ids:
                try:
                    file_ids = [str(UUID(fid)) for fid in request.file_ids]
                except ValueError as e:
                    yield sse(
                        {
                            "code": ChatErrorCode.INVALID_FILE_IDS,
                            "message": f"无效的 file_id 格式: {e}",
                        },
                        event="error",
                    )
                    return

            executor = get_tool_executor()
            agent = await get_excel_agent(db)
            async for event in agent.run_stream(
                query=request.query,
                file_ids=file_ids,
                thread_id=request.thread_id,
                db_session=db,
                user_id=current_user.id,
                tool_executor=executor,
            ):
                yield event

        except Exception as e:
            logger.error(f"处理请求流失败: {e}", exc_info=True)
            yield sse({"message": f"处理失败: {str(e)}"}, event="error")

    return EventSourceResponse(stream())
