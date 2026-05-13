"""ExcelAgent: native-tool-call-first agent for spreadsheet help.

v2: Agent 熔断与自校正 Guardrails
  - max_iterations 硬上限熔断
  - Token 预算追踪
  - 停滞检测自校正（连续相同 tool → 主动追问）
  - 结构化 Tool Observation
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Set
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import ServerSentEvent

from app.api.deps import get_llm_client
from app.core.sse import sse
from app.engine.step_tracker import StepTracker
from app.engine.token_counter import get_token_counter
from app.engine.context_builder import create_context_builder
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

# --- Agent Guardrail 常量 ---
MAX_ITERATIONS = 5               # 单 Turn 最大 LLM 调用轮数
MAX_TOKENS_PER_TURN = 8000       # 单 Turn 累计 token 预算上限
STAGNATION_SIMILARITY = 0.7      # 停滞检测：连续两次决策的 args key 重叠阈值


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
        # 合并历史文件与当前请求文件，避免后续 turn 丢失文件上下文
        historical_file_ids = await self._get_thread_file_ids(thread_id, db_session)
        all_file_ids = list({*(str(fid) for fid in file_ids), *historical_file_ids})
        file_schema_lines = await self._extract_schema_info(all_file_ids, db_session, user_id)
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

        # --- v2: Agent Guardrail 状态 ---
        iteration = 0
        cumulative_tokens = 0
        last_decision: Optional[Dict[str, Any]] = None
        guardrail_events: List[Dict[str, Any]] = []
        token_counter = get_token_counter()
        context_builder = create_context_builder()

        try:
            while True:
                iteration += 1

                # --- Guardrail 1: 硬上限熔断 ---
                if iteration > MAX_ITERATIONS:
                    guardrail_events.append({
                        "type": "max_iterations",
                        "iteration": iteration,
                        "reason": f"超过最大迭代次数 {MAX_ITERATIONS}",
                    })
                    logger.warning(
                        "Agent 熔断: 超出迭代上限 %d (turn=%s)",
                        MAX_ITERATIONS, current_turn_id,
                    )
                    break

                # --- Guardrail 2: Token 预算检查 + 上下文压缩 ---
                current_input_tokens = token_counter.count_messages(messages)
                if current_input_tokens > MAX_TOKENS_PER_TURN * 0.8:
                    logger.info(
                        "Token 预算紧张: %d/%d, 触发上下文压缩",
                        current_input_tokens, MAX_TOKENS_PER_TURN,
                    )
                    messages, file_schema_lines, budget_report = (
                        context_builder.compact_context_if_needed(
                            messages, file_schema_lines,
                            max_input_tokens=MAX_TOKENS_PER_TURN,
                            query=query,
                        )
                    )
                    guardrail_events.append({
                        "type": "context_compacted",
                        "iteration": iteration,
                        "budget_report": budget_report,
                    })

                if cumulative_tokens > MAX_TOKENS_PER_TURN:
                    guardrail_events.append({
                        "type": "token_budget_exceeded",
                        "iteration": iteration,
                        "cumulative_tokens": cumulative_tokens,
                        "budget": MAX_TOKENS_PER_TURN,
                    })
                    logger.warning(
                        "Agent 熔断: Token 预算耗尽 %d/%d (turn=%s)",
                        cumulative_tokens, MAX_TOKENS_PER_TURN, current_turn_id,
                    )
                    break

                decision = self._choose_decision(
                    query=query,
                    file_schema_lines=file_schema_lines,
                    file_ids=all_file_ids,
                    messages=messages,
                )
                if not decision:
                    decision = self._native_tool_call_failure_decision(file_ids=all_file_ids)

                # 追踪决策 token 消耗（估算，含 input + 输出预留）
                decision_tokens = token_counter.count_messages(messages) + 1024
                cumulative_tokens += decision_tokens

                # --- Guardrail 3: 停滞自校正 ---
                if last_decision is not None:
                    stagnation = self._detect_stagnation(last_decision, decision)
                    if stagnation:
                        guardrail_events.append({
                            "type": "stagnation_detected",
                            "iteration": iteration,
                            "previous_tool": last_decision["tool_name"],
                            "current_tool": decision["tool_name"],
                            "similarity": stagnation,
                        })
                        logger.warning(
                            "Agent 停滞检测: %s → %s (相似度=%.2f), 转为追问",
                            last_decision["tool_name"], decision["tool_name"], stagnation,
                        )
                        decision = self._build_stagnation_clarification(
                            tool_name=decision["tool_name"],
                            file_ids=all_file_ids,
                        )
                        # 先保存 guardrail 事件（在 commit 前 flush），再执行追问返回
                        if turn_context["repo"] is not None and current_turn_id is not None:
                            await self._save_guardrail_events(
                                turn_context["repo"], current_turn_id, guardrail_events
                            )
                        async for event in tool_executor.execute(
                            decision=decision,
                            query=query,
                            user_id=user_id,
                            thread_id=current_thread_id,
                            db=db_session,
                            file_ids=all_file_ids,
                            emit_session=emit_session,
                            persist_turn=True,
                            existing_turn_id=current_turn_id,
                        ):
                            yield event
                        return

                last_decision = decision

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
                        file_ids=all_file_ids,
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
                            user_id=user_id,
                        )
                    if turn_context["repo"] is not None and current_turn_id is not None and current_thread_id is not None:
                        await turn_context["repo"].update_turn_intent_type(current_turn_id, decision.get("intent", CHAT_INTENT))
                        await self._save_guardrail_events(turn_context["repo"], current_turn_id, guardrail_events)
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
                    file_ids=all_file_ids,
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
                        user_id=user_id,
                    )

                # v2: 结构化 Tool Observation
                observation = self._format_tool_observation_v2(
                    decision=decision,
                    final_payload=final_payload,
                    step_summaries=step_summaries,
                    file_ids=all_file_ids,
                    budget_remaining=MAX_TOKENS_PER_TURN - cumulative_tokens,
                )
                messages.append({"role": "tool", "content": observation})

            # 循环结束（被熔断中断）→ 降级为追问
            fallback = self._build_guardrail_clarification(
                guardrail_events=guardrail_events,
                file_ids=all_file_ids,
            )
            # 先保存 guardrail 事件（在 persist_turn commit 前 flush）
            if turn_context["repo"] is not None and current_turn_id is not None:
                await self._save_guardrail_events(
                    turn_context["repo"], current_turn_id, guardrail_events
                )
            async for event in tool_executor.execute(
                decision=fallback,
                query=query,
                user_id=user_id,
                thread_id=current_thread_id,
                db=db_session,
                file_ids=all_file_ids,
                emit_session=False,
                persist_turn=True,
                existing_turn_id=current_turn_id,
            ):
                yield event
            if turn_context["repo"] is not None and current_turn_id is not None:
                await turn_context["repo"].finalize_turn(
                    current_turn_id, UUID(current_thread_id), "completed", has_error=False
                )
                await turn_context["repo"].commit()

        except Exception as exc:
            logger.warning("ExcelAgent stream loop failed: %s", exc)
            guardrail_events.append({
                "type": "exception",
                "error": str(exc),
            })
            fallback = self._native_tool_call_failure_decision(file_ids=all_file_ids)
            async for event in tool_executor.execute(
                decision=fallback,
                query=query,
                user_id=user_id,
                thread_id=current_thread_id,
                db=db_session,
                file_ids=all_file_ids,
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
                    user_id=user_id,
                )
            if turn_context["repo"] is not None and current_turn_id is not None and current_thread_id is not None:
                await self._save_guardrail_events(
                    turn_context["repo"], current_turn_id, guardrail_events
                )
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

    # ------------------------------------------------------------------
    # v2: Agent 熔断与自校正
    # ------------------------------------------------------------------

    def _detect_stagnation(
        self,
        previous: Dict[str, Any],
        current: Dict[str, Any],
    ) -> float:
        """检测两次决策是否停滞（相同 tool + 相似 args）。

        Returns:
            相似度 (0~1)，>= STAGNATION_SIMILARITY 视为停滞。
        """
        prev_tool = previous.get("tool_name", "")
        curr_tool = current.get("tool_name", "")

        # 不同 tool → 不可能是停滞
        if prev_tool != curr_tool:
            return 0.0

        # 对话类工具不会停滞（直接结束）
        if prev_tool in {CONVERSATION_RESPONSE_TOOL, CLARIFICATION_RESPONSE_TOOL}:
            return 0.0

        # 比较 arguments 的相似度
        prev_args = previous.get("tool_args", {})
        curr_args = current.get("tool_args", {})
        similarity = self._compute_args_similarity(prev_args, curr_args)

        return similarity

    @staticmethod
    def _compute_args_similarity(
        args_a: Dict[str, Any],
        args_b: Dict[str, Any],
    ) -> float:
        """计算两组 args 的 Jaccard 相似度（基于 key 集合 + 值的 hash）。"""
        def _arg_signature(args: Dict[str, Any]) -> Set[str]:
            sig: Set[str] = set()
            for k, v in args.items():
                if isinstance(v, dict):
                    sig.add(f"{k}:{json.dumps(v, sort_keys=True, default=str)}")
                elif isinstance(v, list):
                    sig.add(f"{k}:{'_'.join(sorted(str(x) for x in v))}")
                else:
                    sig.add(f"{k}:{v}")
            return sig

        sig_a = _arg_signature(args_a)
        sig_b = _arg_signature(args_b)

        if not sig_a and not sig_b:
            return 1.0  # 都为空 → 完全相似
        if not sig_a or not sig_b:
            return 0.0

        intersection = len(sig_a & sig_b)
        union = len(sig_a | sig_b)
        return intersection / union if union > 0 else 0.0

    def _build_stagnation_clarification(
        self,
        *,
        tool_name: str,
        file_ids: List[str],
    ) -> Dict[str, Any]:
        """停滞检测后构建追问决策，引导用户提供更明确的需求。"""
        tool_hint = {
            PROCESSING_WORKFLOW_TOOL: "数据处理",
            ANALYSIS_WORKFLOW_TOOL: "数据分析",
        }.get(tool_name, "操作")

        return self._build_decision(
            tool_name=CLARIFICATION_RESPONSE_TOOL,
            intent=CHAT_INTENT,
            file_ids=file_ids,
            response_text=(
                f"我刚才尝试了{tool_hint}，但检测到连续相同的操作请求。"
                f"为了避免不必要的消耗，请帮我确认一下："
                f"你希望我在当前文件上做哪些具体的操作？"
                f"可以描述得更具体一些（如要筛选哪些条件、计算哪个列等）。"
            ),
            reasoning="stagnation detected, asking for clarification",
        )

    def _build_guardrail_clarification(
        self,
        *,
        guardrail_events: List[Dict[str, Any]],
        file_ids: List[str],
    ) -> Dict[str, Any]:
        """熔断触发后构建降级追问决策。"""
        event_types = [e.get("type", "unknown") for e in guardrail_events]
        reasons = ", ".join(event_types)

        return self._build_decision(
            tool_name=CLARIFICATION_RESPONSE_TOOL,
            intent=CHAT_INTENT,
            file_ids=file_ids,
            response_text=(
                f"我处理这个请求时遇到了一些限制（{reasons}）。"
                f"为了帮你更好地完成，能否换一种方式描述需求？"
                f"或者分步骤告诉我你想怎么做。"
            ),
            reasoning=f"guardrail triggered: {reasons}",
        )

    async def _save_guardrail_events(
        self,
        repo: Any,
        turn_id: UUID,
        events: List[Dict[str, Any]],
    ) -> None:
        """将熔断事件写入 Turn 的 context_snapshot.guardrails 中。"""
        if not events or repo is None or turn_id is None:
            return
        try:
            await repo.update_context_snapshot(
                turn_id, {"guardrails": events}
            )
        except Exception:
            logger.debug("保存 guardrail 事件失败（非关键）")

    # ------------------------------------------------------------------
    # v2: 结构化 Tool Observation（模块 4）
    # ------------------------------------------------------------------

    def _format_tool_observation_v2(
        self,
        *,
        decision: Dict[str, Any],
        final_payload: Any,
        step_summaries: List[Dict[str, str]],
        file_ids: List[str],
        budget_remaining: int,
    ) -> str:
        """构建结构化 Tool Observation（JSON 格式），替代旧版纯文本。

        包含: status, summary, file_changes, variables, errors。
        受 token 预算约束，超出时自动裁剪。
        """
        tool_name = decision.get("tool_name", "unknown_tool")
        all_done = all(s.get("status") == "done" for s in step_summaries)
        has_errors = any(s.get("status") == "error" for s in step_summaries)

        # 解析 workflow 输出
        file_changes: List[Dict[str, Any]] = []
        variables: Dict[str, Any] = {}
        error_list: List[str] = []
        summary = ""

        if isinstance(final_payload, dict):
            output = final_payload.get("output", {}) or {}
            if isinstance(output, dict):
                # 从 export 步骤提取文件变化
                output_files = output.get("output_files", [])
                if output_files:
                    for f in output_files:
                        file_changes.append({
                            "filename": f.get("filename", ""),
                            "url_present": bool(f.get("url")),
                        })

                # 从 execute 步骤提取变量
                if "variables" in output:
                    variables = output["variables"]

                # 提取错误
                errors_raw = output.get("errors", [])
                if errors_raw:
                    error_list = (
                        list(errors_raw)
                        if isinstance(errors_raw, list)
                        else [str(errors_raw)]
                    )

        # 生成摘要
        if has_errors:
            summary = f"工作流 {tool_name} 执行遇到 {len(error_list)} 个错误"
        elif all_done:
            changed = len(file_changes)
            if changed:
                summary = f"工作流 {tool_name} 完成，修改了 {changed} 个文件"
            else:
                summary = f"工作流 {tool_name} 完成，无文件变更"
        else:
            summary = f"工作流 {tool_name} 部分完成"

        # 构建结构化 observation
        observation = json.dumps(
            {
                "tool": tool_name,
                "status": "error" if has_errors else ("success" if all_done else "partial"),
                "summary": summary,
                "file_changes": file_changes[:5],
                "variables": variables,
                "errors": error_list[:3],
            },
            ensure_ascii=False,
            default=str,
        )

        # Token 预算感知：如果 observation 太大，裁剪
        token_counter = get_token_counter()
        obs_tokens = token_counter.count(observation)
        max_obs_tokens = max(300, budget_remaining // 4)

        if obs_tokens > max_obs_tokens:
            # 简化为最小 observation
            observation = json.dumps(
                {
                    "tool": tool_name,
                    "status": "error" if has_errors else "success",
                    "summary": summary,
                    "file_changes_count": len(file_changes),
                    "errors_count": len(error_list),
                },
                ensure_ascii=False,
            )

        return observation

    # ------------------------------------------------------------------
    # 旧版 observation（保留向后兼容）
    # ------------------------------------------------------------------

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

    async def _get_thread_file_ids(
        self,
        thread_id: Optional[str],
        db_session: Optional[AsyncSession],
    ) -> List[str]:
        """获取线程历史中所有关联文件的 ID（含 output 文件）"""
        if not thread_id or not db_session:
            return []
        try:
            repo = TurnRepository(db_session)
            recent_turns = await repo.get_thread_turns(UUID(thread_id), with_files=True)
        except Exception as exc:
            logger.warning("加载线程文件失败: %s", exc)
            return []
        file_ids: List[str] = []
        seen: set[str] = set()
        for turn in recent_turns:
            for f in turn.files:
                fid = str(f.id)
                if fid not in seen:
                    seen.add(fid)
                    file_ids.append(fid)
        return file_ids

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
        user_id: Optional[UUID] = None,
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
                # 当 export 步骤完成时，保存输出文件到数据库
                if step == "export" and user_id is not None:
                    export_output = payload.get("output")
                    if export_output and isinstance(export_output, dict):
                        output_files = export_output.get("output_files", [])
                        if output_files:
                            files_to_save = []
                            for item in output_files:
                                url = item.get("url", "")
                                # 从 URL 提取 object_name（格式: /{minio_base}/{bucket}/{object_name}）
                                object_name = url.split("/", 3)[-1] if url else ""
                                files_to_save.append({
                                    "filename": item.get("filename", "output.xlsx"),
                                    "object_name": object_name,
                                    "file_size": item.get("file_size", 0),
                                    "md5": item.get("md5", "0" * 32),
                                })
                            if files_to_save:
                                await repo.save_output_files_to_turn(turn_id, files_to_save, user_id)
                                await repo.commit()
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
            if step == "export" and user_id is not None:
                export_output = payload.get("output")
                if export_output and isinstance(export_output, dict):
                    output_files = export_output.get("output_files", [])
                    if output_files:
                        files_to_save = []
                        for item in output_files:
                            url = item.get("url", "")
                            object_name = url.split("/", 3)[-1] if url else ""
                            files_to_save.append({
                                "filename": item.get("filename", "output.xlsx"),
                                "object_name": object_name,
                                "file_size": item.get("file_size", 0),
                                "md5": item.get("md5", "0" * 32),
                            })
                        if files_to_save:
                            await repo.save_output_files_to_turn(turn_id, files_to_save, user_id)
                            await repo.commit()
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
