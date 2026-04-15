"""ExcelAgent: native-tool-call-first agent for spreadsheet help."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import ServerSentEvent

from app.api.deps import get_llm_client
from app.core.sse import sse
from app.engine.step_tracker import StepTracker
from app.persistence import TurnRepository
from app.services.agent_tools import (
    ANALYSIS_WORKFLOW_TOOL,
    CLARIFICATION_RESPONSE_TOOL,
    CONVERSATION_RESPONSE_TOOL,
    PROCESSING_WORKFLOW_TOOL,
    get_tool_registry,
)
from app.services.excel import get_files_by_ids_from_db, load_tables_from_files
from app.services.thread import generate_thread_title

logger = logging.getLogger(__name__)

CHAT_INTENT = "chat"
ANALYSIS_INTENT = "analysis"
PROCESSING_INTENT = "processing"


class ExcelAgent:
    """Top-level agent entrypoint for /chat."""

    def __init__(self, llm_client):
        self.llm_client = llm_client

    async def run(
        self,
        *,
        query: str,
        file_ids: List[str],
        thread_id: Optional[str],
        db_session: Optional[AsyncSession],
        user_id: Optional[UUID],
        tool_executor: Optional[Any] = None,
    ) -> Dict[str, Any]:
        history_messages = await self._load_history_messages(thread_id, db_session)
        file_schema_lines = await self._extract_schema_info(file_ids, db_session, user_id)

        try:
            decision = await self._run_loop(
                query=query,
                history_messages=history_messages,
                file_schema_lines=file_schema_lines,
                file_ids=file_ids,
                tool_executor=tool_executor,
                db_session=db_session,
                user_id=user_id,
                thread_id=thread_id,
            )
            if decision:
                return decision
        except Exception as exc:
            logger.warning("ExcelAgent native tool loop failed: %s", exc)

        return self._native_tool_call_failure_decision(file_ids=file_ids)

    async def run_stream(
        self,
        *,
        query: str,
        file_ids: List[str],
        thread_id: Optional[str],
        db_session: Optional[AsyncSession],
        user_id: Optional[UUID],
        tool_executor: Any,
    ) -> AsyncGenerator[ServerSentEvent, None]:
        history_messages = await self._load_history_messages(thread_id, db_session)
        file_schema_lines = await self._extract_schema_info(file_ids, db_session, user_id)
        messages = history_messages + [{"role": "user", "content": query}]
        turn_context = await self._ensure_turn_context(
            thread_id=thread_id,
            db_session=db_session,
            user_id=user_id,
            query=query,
            file_ids=file_ids,
        )
        current_thread_id = turn_context["thread_id"]
        current_turn_id = turn_context["turn_id"]
        records: List[Dict[str, Any]] = []
        emit_session = False
        session_emitted = False

        try:
            while True:
                decision = self._choose_decision(
                    query=query,
                    file_schema_lines=file_schema_lines,
                    file_ids=file_ids,
                    messages=messages,
                )
                if not decision:
                    decision = self._native_tool_call_failure_decision(file_ids=file_ids)

                if current_thread_id and not session_emitted:
                    yield sse(turn_context["session_payload"], event="session")
                    session_emitted = True

                await self._record_tool_call(
                    decision=decision,
                    records=records,
                    repo=turn_context["repo"],
                    turn_id=current_turn_id,
                )

                if decision["tool_name"] in {CONVERSATION_RESPONSE_TOOL, CLARIFICATION_RESPONSE_TOOL}:
                    async for event in tool_executor.execute(
                        decision=decision,
                        query=query,
                        user_id=user_id,
                        thread_id=current_thread_id,
                        db=db_session,
                        file_ids=file_ids,
                        emit_session=emit_session,
                        persist_turn=True,
                        existing_turn_id=current_turn_id,
                    ):
                        yield event
                        await self._record_event_on_turn(
                            event=event,
                            records=records,
                            repo=turn_context["repo"],
                            turn_id=current_turn_id,
                            tool_name=decision["tool_name"],
                        )
                    # Note: turn persistence is handled by stream_chat_response
                    # via _save_conversation_turn (persist_turn=True, existing_turn_id=current_turn_id).
                    # Do NOT overwrite response_text here - _save_conversation_turn already
                    # stored the correct response from chat_service.chat_stream().
                    if turn_context["repo"] is not None and current_turn_id is not None and current_thread_id is not None:
                        await turn_context["repo"].update_turn_intent_type(current_turn_id, decision.get("intent", CHAT_INTENT))
                        await turn_context["repo"].finalize_turn(current_turn_id, UUID(current_thread_id), "completed")
                        await turn_context["repo"].commit()
                    return

                final_payload = None
                step_summaries: List[Dict[str, str]] = []
                async for event in tool_executor.execute(
                    decision=decision,
                    query=query,
                    user_id=user_id,
                    thread_id=current_thread_id,
                    db=db_session,
                    file_ids=file_ids,
                    emit_session=emit_session,
                    persist_turn=False,
                ):
                    yield event
                    payload = json.loads(event.data)
                    final_payload = payload
                    if isinstance(payload, dict) and "step" in payload and "status" in payload:
                        step_summaries.append({"step": payload["step"], "status": payload["status"]})
                    await self._record_event_on_turn(
                        event=event,
                        records=records,
                        repo=turn_context["repo"],
                        turn_id=current_turn_id,
                        tool_name=decision["tool_name"],
                    )

                observation = {
                    "tool_name": decision["tool_name"],
                    "status": "done",
                    "steps": step_summaries,
                    "output": final_payload.get("output") if isinstance(final_payload, dict) else final_payload,
                }
                messages.append({"role": "tool", "content": self._format_tool_observation(observation)})
        except Exception as exc:
            logger.warning("ExcelAgent stream loop failed: %s", exc)
            fallback = self._native_tool_call_failure_decision(file_ids=file_ids)
            async for event in tool_executor.execute(
                decision=fallback,
                query=query,
                user_id=user_id,
                thread_id=current_thread_id,
                db=db_session,
                file_ids=file_ids,
                emit_session=False,
                persist_turn=False,
            ):
                yield event
                await self._record_event_on_turn(
                    event=event,
                    records=records,
                    repo=turn_context["repo"],
                    turn_id=current_turn_id,
                    tool_name=fallback["tool_name"],
                )
            if turn_context["repo"] is not None and current_turn_id is not None and current_thread_id is not None:
                await turn_context["repo"].update_turn_response_text(current_turn_id, fallback.get("response_text", ""))
                await turn_context["repo"].finalize_turn(current_turn_id, UUID(current_thread_id), "failed", has_error=True)
                await turn_context["repo"].commit()

    async def _run_loop(
        self,
        *,
        query: str,
        history_messages: List[Dict[str, str]],
        file_schema_lines: List[str],
        file_ids: List[str],
        tool_executor: Optional[Any] = None,
        db_session: Optional[AsyncSession] = None,
        user_id: Optional[UUID] = None,
        thread_id: Optional[str] = None,
        messages_override: Optional[List[Dict[str, str]]] = None,
    ) -> Optional[Dict[str, Any]]:
        if not hasattr(self.llm_client, "call_llm_with_tools"):
            return None

        registry = get_tool_registry()
        system_prompt = self._build_system_prompt(
            has_files=bool(file_ids),
            file_schema_lines=file_schema_lines,
        )
        messages = messages_override or (history_messages + [{"role": "user", "content": query}])

        while True:
            response = self.llm_client.call_llm_with_tools(
                "chat",
                system_prompt=system_prompt,
                messages=messages,
                tools=registry.schemas,
                tool_choice="required",
            )
            if not response.tool_calls:
                return None

            first_call = response.tool_calls[0]
            payload = dict(first_call.get("arguments") or {})
            payload["tool_name"] = first_call["name"]
            decision = self._normalize_decision(payload, file_ids)

            if decision["tool_name"] in {CONVERSATION_RESPONSE_TOOL, CLARIFICATION_RESPONSE_TOOL}:
                return decision

            if tool_executor is None:
                return decision

            observation = await tool_executor.execute_for_agent(
                decision=decision,
                query=query,
                user_id=user_id,
                thread_id=thread_id,
                db=db_session,
                file_ids=file_ids,
            )
            messages.append(
                {
                    "role": "tool",
                    "content": self._format_tool_observation(observation),
                }
            )

    def _format_tool_observation(self, observation: Dict[str, Any]) -> str:
        tool_name = observation.get("tool_name", "unknown_tool")
        status = observation.get("status", "unknown")
        output = observation.get("output")
        return f"工具 {tool_name} 执行状态: {status}; 结果: {output}"

    def _choose_decision(
        self,
        *,
        query: str,
        file_schema_lines: List[str],
        file_ids: List[str],
        messages: List[Dict[str, str]],
    ) -> Optional[Dict[str, Any]]:
        if not hasattr(self.llm_client, "call_llm_with_tools"):
            return None

        registry = get_tool_registry()
        system_prompt = self._build_system_prompt(
            has_files=bool(file_ids),
            file_schema_lines=file_schema_lines,
        )
        response = self.llm_client.call_llm_with_tools(
            "chat",
            system_prompt=system_prompt,
            messages=messages,
            tools=registry.schemas,
            tool_choice="required",
        )
        if not response.tool_calls:
            return None

        first_call = response.tool_calls[0]
        payload = dict(first_call.get("arguments") or {})
        payload["tool_name"] = first_call["name"]
        return self._normalize_decision(payload, file_ids)

    async def _load_history_messages(
        self,
        thread_id: Optional[str],
        db_session: Optional[AsyncSession],
    ) -> List[Dict[str, str]]:
        if not thread_id or not db_session:
            return []

        try:
            repo = TurnRepository(db_session)
            recent_turns = await repo.get_thread_turns(UUID(thread_id), with_files=True)
        except Exception as exc:
            logger.warning("加载历史对话失败: %s", exc)
            return []

        messages: List[Dict[str, str]] = []
        for turn in reversed(recent_turns):
            if turn.user_query:
                messages.append({"role": "user", "content": turn.user_query})
            if turn.response_text:
                messages.append({"role": "assistant", "content": turn.response_text})
        return messages

    async def _ensure_turn_context(
        self,
        *,
        thread_id: Optional[str],
        db_session: Optional[AsyncSession],
        user_id: Optional[UUID],
        query: str,
        file_ids: List[str],
    ) -> Dict[str, Any]:
        if not db_session or not user_id:
            return {
                "thread_id": thread_id,
                "turn_id": None,
                "session_payload": {"thread_id": thread_id, "intent": CHAT_INTENT},
                "repo": None,
            }

        repo = TurnRepository(db_session)
        is_new_thread = False
        title = None
        thread_uuid: UUID
        if thread_id:
            thread_uuid = UUID(thread_id)
        else:
            try:
                title = await asyncio.to_thread(generate_thread_title, query, self.llm_client)
            except Exception:
                title = query[:30] or "Excel Agent"
            thread = await repo.create_thread(user_id, title or "Excel Agent")
            thread_uuid = thread.id
            is_new_thread = True

        turn_number = await repo.get_next_turn_number(thread_uuid)
        turn = await repo.create_turn(
            thread_id=thread_uuid,
            turn_number=turn_number,
            user_query=query,
            intent_type="agent",
        )
        if file_ids:
            await repo.link_files_to_turn(turn.id, [UUID(fid) for fid in file_ids], user_id)
        await repo.mark_processing(turn.id, StepTracker())
        await repo.commit()
        return {
            "thread_id": str(thread_uuid),
            "turn_id": turn.id,
            "session_payload": {
                "thread_id": str(thread_uuid),
                "turn_id": str(turn.id),
                "title": title,
                "is_new_thread": is_new_thread,
            },
            "repo": repo,
        }

    async def _record_event_on_turn(
        self,
        *,
        event: ServerSentEvent,
        records: List[Dict[str, Any]],
        repo: Optional[TurnRepository],
        turn_id: Optional[UUID],
        tool_name: str,
    ) -> None:
        if repo is None or turn_id is None:
            return
        payload = json.loads(event.data)
        step = payload.get("step")
        status = payload.get("status")
        if not step or step == "complete" or not status:
            return
        stage_id = payload.get("stage_id")
        record = None
        for existing in reversed(records):
            if existing.get("type") != "tool_result":
                continue
            if stage_id and existing.get("stage_id") == stage_id:
                record = existing
                break
            if not stage_id and existing.get("step") == step and existing.get("tool_name") == tool_name:
                record = existing
                break

        if status == "running" or record is None:
            record = {
                "id": str(uuid4()),
                "type": "tool_result",
                "tool_name": tool_name,
                "step": step,
                "stage_id": stage_id,
                "status": status,
                "started_at": payload.get("started_at") or self._now(),
            }
            if status == "streaming":
                record["streaming_content"] = payload.get("delta", "")
            elif status == "done":
                record["output"] = payload.get("output")
                record["completed_at"] = payload.get("completed_at") or self._now()
            elif status == "error":
                record["error"] = payload.get("error")
                record["completed_at"] = payload.get("completed_at") or self._now()
            records.append(record)
            await repo.replace_turn_steps(turn_id, records)
            return

        record["status"] = status
        if status == "streaming":
            existing = record.get("streaming_content", "")
            record["streaming_content"] = existing + (payload.get("delta", "") or "")
        elif status == "done":
            record["output"] = payload.get("output")
            record["completed_at"] = payload.get("completed_at") or self._now()
            record.pop("streaming_content", None)
        elif status == "error":
            record["error"] = payload.get("error")
            record["completed_at"] = payload.get("completed_at") or self._now()
            record.pop("streaming_content", None)
        await repo.replace_turn_steps(turn_id, records)

    async def _record_tool_call(
        self,
        *,
        decision: Dict[str, Any],
        records: List[Dict[str, Any]],
        repo: Optional[TurnRepository],
        turn_id: Optional[UUID],
    ) -> None:
        if repo is None or turn_id is None:
            return
        records.append(
            {
                "id": str(uuid4()),
                "type": "tool_call",
                "tool_name": decision["tool_name"],
                "arguments": decision.get("tool_args", {}),
                "created_at": self._now(),
            }
        )
        await repo.replace_turn_steps(turn_id, records)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _extract_schema_info(
        self,
        file_ids: List[str],
        db_session: Optional[AsyncSession],
        user_id: Optional[UUID],
    ) -> List[str]:
        if not file_ids or not db_session or not user_id:
            return []

        try:
            uuid_file_ids = [UUID(fid) if isinstance(fid, str) else fid for fid in file_ids]
            files = await get_files_by_ids_from_db(db_session, uuid_file_ids, user_id)
            file_collection = await asyncio.to_thread(load_tables_from_files, files)
        except Exception as exc:
            logger.warning("提取文件 schema 失败: %s", exc)
            return []

        schema_lines: List[str] = []
        for excel_file in file_collection:
            for sheet_name in excel_file.get_sheet_names():
                table = excel_file.get_sheet(sheet_name)
                schema_lines.append(
                    f"- 文件 [{excel_file.filename}] 表 [{sheet_name}] 列: {', '.join(table.get_columns())}"
                )
        return schema_lines

    def _build_system_prompt(
        self,
        *,
        has_files: bool,
        file_schema_lines: List[str],
    ) -> str:
        schema_text = "\n".join(file_schema_lines) if file_schema_lines else "无可用文件 schema"
        file_hint = "用户当前带有文件，可调用 workflow 工具。" if has_files else "用户当前没有可用文件。"
        return f"""你是 ExcelAgent，负责在 /chat 入口下循环思考并选择一个合适的工具。

当前阶段只允许选择一个最合适的工具，不要输出普通文本。
如果你选择 {CONVERSATION_RESPONSE_TOOL} 或 {CLARIFICATION_RESPONSE_TOOL}，必须在 `response_text` 中直接给出要返回给用户的话。

工具选择原则:
1. 一般产品问答、Excel 知识问答、无需操作文件时，选 {CONVERSATION_RESPONSE_TOOL}
2. 需求不完整、上下文不足、需要用户补充时，选 {CLARIFICATION_RESPONSE_TOOL}
3. 用户要修改、转换、筛选、排序、计算、导出文件时，选 {PROCESSING_WORKFLOW_TOOL}
4. 用户要总结、统计、分析、洞察、报告文件数据时，选 {ANALYSIS_WORKFLOW_TOOL}
5. 如果用户显然要处理或分析文件，但当前没有文件，优先选 {CLARIFICATION_RESPONSE_TOOL} 并提示上传文件

当前文件状态:
{file_hint}

文件 schema:
{schema_text}
"""

    def _native_tool_call_failure_decision(self, *, file_ids: List[str]) -> Dict[str, Any]:
        return self._build_decision(
            tool_name=CLARIFICATION_RESPONSE_TOOL,
            intent=CHAT_INTENT,
            file_ids=file_ids,
            response_text="我这次没有稳定拿到工具调用结果。请换一种表述再试一次，如果涉及文件处理或分析，也可以补充更明确的目标。",
            reasoning="native tool calling unavailable",
        )

    def _normalize_decision(
        self,
        payload: Dict[str, Any],
        file_ids: List[str],
    ) -> Dict[str, Any]:
        tool_name = payload.get("tool_name") or CONVERSATION_RESPONSE_TOOL
        if tool_name not in {
            CONVERSATION_RESPONSE_TOOL,
            CLARIFICATION_RESPONSE_TOOL,
            PROCESSING_WORKFLOW_TOOL,
            ANALYSIS_WORKFLOW_TOOL,
        }:
            tool_name = CONVERSATION_RESPONSE_TOOL

        intent = payload.get("intent") or CHAT_INTENT
        if tool_name == PROCESSING_WORKFLOW_TOOL:
            intent = PROCESSING_INTENT
        elif tool_name == ANALYSIS_WORKFLOW_TOOL:
            intent = ANALYSIS_INTENT
        elif tool_name in {CONVERSATION_RESPONSE_TOOL, CLARIFICATION_RESPONSE_TOOL}:
            intent = CHAT_INTENT

        response_text = payload.get("response_text")
        if tool_name not in {CLARIFICATION_RESPONSE_TOOL, CONVERSATION_RESPONSE_TOOL}:
            response_text = None

        context = payload.get("context") or {}
        context.setdefault("context_type", intent)
        if payload.get("reasoning"):
            context["agent_reasoning"] = payload["reasoning"]

        return self._build_decision(
            tool_name=tool_name,
            intent=intent,
            file_ids=file_ids,
            response_text=response_text,
            reasoning=payload.get("reasoning", ""),
            context=context,
        )

    def _fallback_decision(self, *, query: str, file_ids: List[str]) -> Dict[str, Any]:
        query_stripped = query.strip()
        lower_query = query_stripped.lower()
        analysis_keywords = ["分析", "统计", "总结", "趋势", "洞察", "报告"]
        processing_keywords = ["筛选", "过滤", "排序", "计算", "新增", "添加", "删除", "导出", "修改", "转换"]
        ambiguous_queries = {"是", "不是", "对", "不对", "这个", "那个", "它", "继续", "然后"}

        if (not file_ids) and any(keyword in query_stripped for keyword in analysis_keywords + processing_keywords):
            return self._build_decision(
                tool_name=CLARIFICATION_RESPONSE_TOOL,
                intent=CHAT_INTENT,
                file_ids=file_ids,
                response_text="要处理或分析 Excel 文件的话，先上传文件并告诉我你想怎么操作。",
                reasoning="需要文件才能继续 workflow 工具。",
            )

        if query_stripped in ambiguous_queries or len(query_stripped) <= 2:
            return self._build_decision(
                tool_name=CLARIFICATION_RESPONSE_TOOL,
                intent=CHAT_INTENT,
                file_ids=file_ids,
                response_text="我还缺一点关键信息。你是想直接提问，还是想让我对某个 Excel 文件做处理或分析？",
                reasoning="用户请求过短，信息不足。",
            )

        if any(keyword in query_stripped for keyword in analysis_keywords):
            return self._build_decision(
                tool_name=ANALYSIS_WORKFLOW_TOOL,
                intent=ANALYSIS_INTENT,
                file_ids=file_ids,
                reasoning="命中分析类请求。",
            )

        if any(keyword in query_stripped for keyword in processing_keywords):
            return self._build_decision(
                tool_name=PROCESSING_WORKFLOW_TOOL,
                intent=PROCESSING_INTENT,
                file_ids=file_ids,
                reasoning="命中处理类请求。",
            )

        return self._build_decision(
            tool_name=CONVERSATION_RESPONSE_TOOL,
            intent=CHAT_INTENT,
            file_ids=file_ids,
            reasoning=f"默认直答。query={lower_query[:40]}",
        )

    def _build_decision(
        self,
        *,
        tool_name: str,
        intent: str,
        file_ids: List[str],
        response_text: Optional[str] = None,
        reasoning: str = "",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        normalized_context = dict(context or {})
        normalized_context.setdefault("context_type", intent)
        if reasoning:
            normalized_context.setdefault("agent_reasoning", reasoning)

        return {
            "tool_name": tool_name,
            "intent": intent,
            "response_text": response_text,
            "reasoning": reasoning,
            "context": normalized_context,
            "tool_args": {
                "intent": intent,
                "context": normalized_context,
            },
            "file_ids": file_ids,
        }


async def get_excel_agent(db_session: Optional[AsyncSession] = None) -> ExcelAgent:
    llm_client = await get_llm_client(db_session)
    return ExcelAgent(llm_client)
