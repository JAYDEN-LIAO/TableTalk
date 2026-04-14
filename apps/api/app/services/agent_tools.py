"""Tool registry and executor for agent-driven /chat flows."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncGenerator, Dict, List, Optional
from uuid import UUID

from sse_starlette.sse import ServerSentEvent

from app.services.chat_stream import stream_chat_response
from app.services.excel import get_files_by_ids_from_db, load_tables_from_files
from app.services.processor_stream import stream_excel_processing

CONVERSATION_RESPONSE_TOOL = "conversation_response"
CLARIFICATION_RESPONSE_TOOL = "clarification_response"
PROCESSING_WORKFLOW_TOOL = "processing_workflow"
ANALYSIS_WORKFLOW_TOOL = "analysis_workflow"


@dataclass(frozen=True)
class ToolRegistry:
    schemas: List[Dict]


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": CONVERSATION_RESPONSE_TOOL,
            "description": "Directly answer a general Excel or product question without running a workflow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["chat"]},
                    "response_text": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["intent", "response_text", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": CLARIFICATION_RESPONSE_TOOL,
            "description": "Ask the user a clarification question before any workflow execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["chat"]},
                    "response_text": {"type": "string"},
                    "context": {"type": "object"},
                },
                "required": ["intent", "response_text", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": PROCESSING_WORKFLOW_TOOL,
            "description": "Run the fixed processing workflow for transforming or exporting spreadsheet data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["processing"]},
                    "context": {"type": "object"},
                },
                "required": ["intent", "context"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": ANALYSIS_WORKFLOW_TOOL,
            "description": "Run the fixed analysis workflow for summarizing or analyzing spreadsheet data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": ["analysis"]},
                    "context": {"type": "object"},
                },
                "required": ["intent", "context"],
            },
        },
    },
]


class ToolExecutor:
    async def execute_for_agent(
        self,
        *,
        decision: Dict,
        query: str,
        user_id: UUID,
        thread_id: Optional[str],
        db,
        file_ids: List[str],
    ) -> Dict:
        final_payload = None
        step_summaries: List[Dict[str, str]] = []
        observed_thread_id = thread_id

        async for event in self.execute(
            decision=decision,
            query=query,
            user_id=user_id,
            thread_id=thread_id,
            db=db,
            file_ids=file_ids,
        ):
            payload = json.loads(event.data)
            final_payload = payload
            if isinstance(payload, dict) and payload.get("thread_id"):
                observed_thread_id = payload["thread_id"]
            if isinstance(payload, dict) and "step" in payload and "status" in payload:
                step_summaries.append(
                    {
                        "step": payload["step"],
                        "status": payload["status"],
                    }
                )

        return {
            "tool_name": decision["tool_name"],
            "status": "done",
            "thread_id": observed_thread_id,
            "steps": step_summaries,
            "output": final_payload.get("output") if isinstance(final_payload, dict) else final_payload,
        }

    async def execute(
        self,
        *,
        decision: Dict,
        query: str,
        user_id: UUID,
        thread_id: Optional[str],
        db,
        file_ids: List[str],
        emit_session: bool = True,
        persist_turn: bool = True,
    ) -> AsyncGenerator[ServerSentEvent, None]:
        tool_name = decision["tool_name"]

        if tool_name in {CONVERSATION_RESPONSE_TOOL, CLARIFICATION_RESPONSE_TOOL}:
            async for event in stream_chat_response(
                query=query,
                user_id=user_id,
                thread_id=thread_id,
                assistant_decision=decision,
                db=db,
                file_ids=file_ids,
                emit_session=emit_session,
                persist_turn=persist_turn,
            ):
                yield event
            return

        if tool_name in {PROCESSING_WORKFLOW_TOOL, ANALYSIS_WORKFLOW_TOOL}:
            async for event in self._execute_workflow_tool(
                decision=decision,
                query=query,
                user_id=user_id,
                db=db,
                file_ids=file_ids,
            ):
                yield event
            return

        raise ValueError(f"未知 agent tool: {tool_name}")

    async def _execute_workflow_tool(
        self,
        *,
        decision: Dict,
        query: str,
        user_id: UUID,
        db,
        file_ids: List[str],
    ) -> AsyncGenerator[ServerSentEvent, None]:
        async def load_tables():
            files = await get_files_by_ids_from_db(
                db,
                [UUID(fid) for fid in file_ids],
                user_id,
            )
            return await asyncio.to_thread(load_tables_from_files, files)

        async for event in stream_excel_processing(
            load_tables_fn=load_tables,
            query=query,
            stream_llm=True,
            export_path_prefix=f"users/{user_id}/outputs",
        ):
            yield event


def get_tool_registry() -> ToolRegistry:
    return ToolRegistry(schemas=TOOL_SCHEMAS)


def get_tool_executor() -> ToolExecutor:
    return ToolExecutor()
