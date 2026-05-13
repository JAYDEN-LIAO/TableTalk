"""上下文构建器 - 为LLM提示词格式化上下文

负责将上下文数据格式化为适合LLM处理的文本格式。
根据意图类型使用不同的模板和格式化策略。

v2: 引入 tiktoken 精确计数、三级压缩策略、Schema 按需注入。
"""

import logging
from typing import Dict, List, Optional, Any, Set
from uuid import UUID

from app.engine.token_counter import get_token_counter

logger = logging.getLogger(__name__)

CHAT_INTENT = "chat"
ANALYSIS_INTENT = "analysis"
PROCESSING_INTENT = "processing"


class ContextBuilder:
    """
    上下文构建器
    
    职责：为LLM提示词格式化上下文，根据意图类型使用特定模板。
    
    关键方法：
        build_prompt_context(): 构建完整的提示词上下文
        _build_chat_context(): 聊天上下文模板
        _build_analysis_context(): 分析上下文模板
        _build_processing_context(): 处理上下文模板
    """
    
    def __init__(self, max_history_turns: int = 5, max_tokens: int = 2000):
        """
        初始化上下文构建器
        
        Args:
            max_history_turns: 最大历史轮次数
            max_tokens: 最大令牌数（用于长度控制）
        """
        self.max_history_turns = max_history_turns
        self.max_tokens = max_tokens
    
    def build_prompt_context(
        self,
        intent_type: str,
        context_data: Dict[str, Any],
        current_query: str,
        current_files: List[Dict[str, Any]]
    ) -> str:
        """
        构建提示词上下文
        
        Args:
            intent_type: 意图类型
            context_data: 上下文数据（来自ContextService）
            current_query: 当前用户查询
            current_files: 当前文件列表
            
        Returns:
            格式化后的上下文文本
        """
        try:
            # 根据意图类型选择构建方法
            if intent_type == CHAT_INTENT:
                return self._build_chat_prompt_context(
                    context_data, current_query, current_files
                )
            elif intent_type == ANALYSIS_INTENT:
                return self._build_analysis_prompt_context(
                    context_data, current_query, current_files
                )
            elif intent_type == PROCESSING_INTENT:
                return self._build_processing_prompt_context(
                    context_data, current_query, current_files
                )
            else:
                return self._build_default_prompt_context(
                    context_data, current_query, current_files
                )
                
        except Exception as e:
            logger.error(f"构建提示词上下文失败: {e}", exc_info=True)
            return self._build_fallback_context(current_query, current_files)
    
    def _build_chat_prompt_context(
        self,
        context_data: Dict[str, Any],
        current_query: str,
        current_files: List[Dict[str, Any]]
    ) -> str:
        """
        构建聊天提示词上下文
        
        模板：对话历史、话题延续、情感分析
        """
        formatted = "## 对话上下文\n\n"
        
        # 添加对话历史
        conversation_history = context_data.get("conversation_history", [])
        if conversation_history:
            formatted += "### 历史对话记录\n\n"
            
            # 限制历史记录数量
            recent_history = conversation_history[-self.max_history_turns*2:]  # 每轮有用户和助手两条消息
            
            for msg in recent_history:
                role = "用户" if msg.get("role") == "user" else "助手"
                content = msg.get("content", "")
                timestamp = msg.get("timestamp", "")
                
                time_str = f" ({timestamp})" if timestamp else ""
                formatted += f"{role}{time_str}: {content}\n\n"
            
            formatted += f"共 {len(conversation_history)//2} 轮对话，显示最近 {len(recent_history)//2} 轮。\n\n"
        else:
            formatted += "这是新对话，没有历史记录。\n\n"
        
        # 添加话题连续性分析
        topic_continuity = context_data.get("topic_continuity", True)
        if not topic_continuity and conversation_history:
            formatted += "### 话题分析\n"
            formatted += "检测到话题可能已切换，请根据当前查询提供相关回应。\n\n"
        
        # 添加上下文元数据
        history_count = context_data.get("history_count", 0)
        if history_count > 0:
            formatted += f"### 上下文摘要\n"
            formatted += f"- 对话线程: {history_count} 个历史轮次\n"
            formatted += f"- 当前意图: 聊天对话\n"
        
        # 添加当前查询
        formatted += f"\n### 当前查询\n{current_query}\n"
        
        return self._truncate_to_token_limit(formatted)
    
    def _build_analysis_prompt_context(
        self,
        context_data: Dict[str, Any],
        current_query: str,
        current_files: List[Dict[str, Any]]
    ) -> str:
        """
        构建分析提示词上下文
        
        模板：文件信息、历史分析记录、数据洞察
        """
        formatted = "## 数据分析上下文\n\n"
        
        # 添加当前文件信息
        if current_files:
            formatted += "### 当前分析文件\n\n"
            for file in current_files:
                file_id = file.get("id", "未知")
                file_name = file.get("name", "未命名")
                file_type = file.get("type", "未知")
                file_size = file.get("size", 0)
                
                size_str = f"{file_size/1024:.1f}KB" if file_size < 1024*1024 else f"{file_size/(1024*1024):.1f}MB"
                formatted += f"- 文件: {file_name} ({file_type}, {size_str})\n"
            formatted += "\n"
        
        # 添加历史分析记录
        previous_analyses = context_data.get("previous_analyses", [])
        if previous_analyses:
            formatted += "### 历史分析记录\n\n"
            
            # 限制显示数量
            recent_analyses = previous_analyses[-min(3, len(previous_analyses)):]
            
            for i, analysis in enumerate(recent_analyses, 1):
                turn_number = analysis.get("turn_number", "未知")
                user_query = analysis.get("user_query", "")[:100]
                created_at = analysis.get("created_at", "")
                
                formatted += f"{i}. 第 {turn_number} 轮分析:\n"
                formatted += f"   查询: {user_query}...\n"
                
                # 添加分析洞察（如果有）
                insights = analysis.get("insights")
                if insights:
                    if isinstance(insights, list):
                        formatted += f"   关键洞察: {len(insights)} 条\n"
                    elif isinstance(insights, dict):
                        formatted += f"   分析结果: 包含 {len(insights)} 个维度\n"
                
                formatted += f"   时间: {created_at[:19] if created_at else '未知'}\n\n"
            
            if len(previous_analyses) > len(recent_analyses):
                formatted += f"（还有 {len(previous_analyses) - len(recent_analyses)} 条更早的分析记录）\n\n"
        
        # 添加数据洞察
        data_insights = context_data.get("data_insights", [])
        if data_insights:
            formatted += "### 历史数据洞察\n\n"
            
            # 限制显示数量
            recent_insights = data_insights[-min(5, len(data_insights)):]
            
            for i, insight in enumerate(recent_insights, 1):
                step = insight.get("step", "未知步骤")
                result = insight.get("result", "")
                has_data = insight.get("has_data", False)
                
                if result:
                    result_preview = result[:80] + "..." if len(result) > 80 else result
                    formatted += f"{i}. {step}: {result_preview}\n"
                elif has_data:
                    formatted += f"{i}. {step}: 已生成数据结果\n"
            
            if len(data_insights) > len(recent_insights):
                formatted += f"（还有 {len(data_insights) - len(recent_insights)} 条更早的洞察）\n\n"
        
        # 添加文件分析历史
        file_analysis_history = context_data.get("file_analysis_history", [])
        if file_analysis_history and current_files:
            # 找出当前文件的历史分析记录
            current_file_ids = {file.get("id") for file in current_files}
            relevant_history = [
                h for h in file_analysis_history 
                if h.get("file_id") in current_file_ids
            ]
            
            if relevant_history:
                formatted += "### 当前文件分析历史\n\n"
                for history in relevant_history[:3]:  # 最多显示3条
                    file_name = history.get("file_name", "未知文件")
                    analysis_turn = history.get("analysis_turn", "未知")
                    analysis_time = history.get("analysis_time", "")
                    
                    time_str = analysis_time[:19] if analysis_time else "未知时间"
                    formatted += f"- {file_name}: 第 {analysis_turn} 轮分析 ({time_str})\n"
                formatted += "\n"
        
        # 添加上下文元数据
        formatted += f"### 分析上下文摘要\n"
        formatted += f"- 历史分析次数: {len(previous_analyses)}\n"
        formatted += f"- 数据洞察数量: {len(data_insights)}\n"
        formatted += f"- 当前分析文件数: {len(current_files)}\n\n"
        
        # 添加当前查询
        formatted += f"### 当前分析请求\n{current_query}\n"
        
        return self._truncate_to_token_limit(formatted)
    
    def _build_processing_prompt_context(
        self,
        context_data: Dict[str, Any],
        current_query: str,
        current_files: List[Dict[str, Any]]
    ) -> str:
        """
        构建处理提示词上下文
        
        模板：输入文件、数据状态、可用输出文件、操作历史
        """
        formatted = "## 数据处理上下文\n\n"
        
        # 添加当前文件信息
        if current_files:
            formatted += "### 输入文件\n\n"
            for file in current_files:
                file_id = file.get("id", "未知")
                file_name = file.get("name", "未命名")
                file_type = file.get("type", "未知")
                file_size = file.get("size", 0)
                
                size_str = f"{file_size/1024:.1f}KB" if file_size < 1024*1024 else f"{file_size/(1024*1024):.1f}MB"
                formatted += f"- {file_name} ({file_type}, {size_str})\n"
            formatted += "\n"
        
        # 添加数据状态
        data_state = context_data.get("data_state", "unknown")
        data_state_map = {
            "processed": "已处理",
            "error": "存在错误",
            "in_progress": "处理中",
            "unknown": "未知状态"
        }
        formatted += f"### 数据状态\n{data_state_map.get(data_state, data_state)}\n\n"
        
        # 添加操作历史
        operation_history = context_data.get("operation_history", [])
        if operation_history:
            formatted += "### 最近操作记录\n\n"
            
            # 限制显示数量
            recent_operations = operation_history[-min(3, len(operation_history)):]
            
            for i, operation in enumerate(recent_operations, 1):
                turn_number = operation.get("turn_number", "未知")
                user_query = operation.get("user_query", "")[:80]
                status = operation.get("status", "未知")
                operations_list = operation.get("operations", [])
                
                status_map = {
                    "completed": "完成",
                    "failed": "失败",
                    "processing": "处理中",
                    "pending": "等待中"
                }
                status_str = status_map.get(status, status)
                
                formatted += f"{i}. 第 {turn_number} 轮操作 ({status_str}):\n"
                formatted += f"   请求: {user_query}...\n"
                
                # 显示操作详情
                if operations_list:
                    formatted += f"   执行步骤: "
                    step_names = [op.get("step", "") for op in operations_list if op.get("step")]
                    formatted += ", ".join(step_names) + "\n"
                
                formatted += "\n"
            
            if len(operation_history) > len(recent_operations):
                formatted += f"（还有 {len(operation_history) - len(recent_operations)} 条更早的操作记录）\n\n"
        
        # 添加可用文件
        available_files = context_data.get("available_files", [])
        if available_files:
            # 区分输入文件和输出文件
            input_files = [f for f in available_files if not f.get("is_output", False)]
            output_files = [f for f in available_files if f.get("is_output", False)]
            
            if output_files:
                formatted += "### 可用输出文件\n\n"
                for file in output_files[:5]:  # 最多显示5个输出文件
                    file_name = file.get("name", "未命名")
                    source_turn = file.get("source_turn", "未知")
                    file_type = file.get("type", "未知")
                    
                    formatted += f"- {file_name} ({file_type}, 来自第 {source_turn} 轮)\n"
                formatted += "\n"
            
            if input_files and len(input_files) > len(current_files):
                formatted += "### 其他可用输入文件\n\n"
                # 排除当前已选择的文件
                current_file_ids = {file.get("id") for file in current_files}
                other_input_files = [f for f in input_files if f.get("id") not in current_file_ids]
                
                for file in other_input_files[:3]:  # 最多显示3个
                    file_name = file.get("name", "未命名")
                    source_turn = file.get("source_turn", "未知")
                    
                    formatted += f"- {file_name} (来自第 {source_turn} 轮)\n"
                formatted += "\n"
        
        # 添加文件依赖
        file_dependencies = context_data.get("file_dependencies", [])
        if file_dependencies:
            formatted += "### 文件依赖关系\n\n"
            for dep in file_dependencies[:3]:  # 最多显示3个依赖
                file_id = dep.get("file_id", "未知")
                depends_on_turn = dep.get("depends_on_turn", "未知")
                dependency_type = dep.get("dependency_type", "未知")
                
                formatted += f"- 文件 {file_id[:8]}... 依赖于第 {depends_on_turn[:8]}... 轮 ({dependency_type})\n"
            formatted += "\n"
        
        # 添加上下文元数据
        formatted += f"### 处理上下文摘要\n"
        formatted += f"- 操作历史记录: {len(operation_history)} 条\n"
        formatted += f"- 可用文件总数: {len(available_files)} 个\n"
        formatted += f"- 文件依赖关系: {len(file_dependencies)} 个\n\n"
        
        # 添加当前查询
        formatted += f"### 当前处理请求\n{current_query}\n"
        
        return self._truncate_to_token_limit(formatted)
    
    def _build_default_prompt_context(
        self,
        context_data: Dict[str, Any],
        current_query: str,
        current_files: List[Dict[str, Any]]
    ) -> str:
        """
        构建默认提示词上下文
        """
        formatted = "## 上下文信息\n\n"
        
        # 添加上下文摘要
        history_summary = context_data.get("history_summary", {})
        if history_summary:
            total_turns = history_summary.get("total_turns", 0)
            recent_intents = history_summary.get("recent_intents", [])
            last_turn_time = history_summary.get("last_turn_time")
            
            formatted += f"### 对话历史摘要\n"
            formatted += f"- 总轮次数: {total_turns}\n"
            if recent_intents:
                formatted += f"- 最近意图: {', '.join(recent_intents)}\n"
            if last_turn_time:
                formatted += f"- 最后轮次时间: {last_turn_time[:19]}\n"
            formatted += "\n"
        
        # 添加当前文件信息
        if current_files:
            formatted += f"### 当前文件\n"
            for file in current_files:
                file_name = file.get("name", "未命名")
                formatted += f"- {file_name}\n"
            formatted += "\n"
        
        # 添加当前查询
        formatted += f"### 当前请求\n{current_query}\n"
        
        return self._truncate_to_token_limit(formatted)
    
    def _build_fallback_context(
        self,
        current_query: str,
        current_files: List[Dict[str, Any]]
    ) -> str:
        """
        构建降级上下文（当主要构建失败时使用）
        """
        formatted = "## 上下文信息（简化版）\n\n"
        
        formatted += f"### 当前请求\n{current_query}\n\n"
        
        if current_files:
            formatted += f"### 相关文件\n"
            for file in current_files:
                file_name = file.get("name", "未命名")
                formatted += f"- {file_name}\n"
        
        return formatted
    
    # ------------------------------------------------------------------
    # Token 管理（v2: tiktoken 精确计数）
    # ------------------------------------------------------------------

    def _truncate_to_token_limit(self, text: str) -> str:
        """将文本截断到 token 限制（使用 tiktoken 精确计数）。"""
        counter = get_token_counter()
        current_tokens = counter.count(text)

        if current_tokens <= self.max_tokens:
            return text

        # 按比例估算需裁剪的字符数，避免逐字符重编码
        ratio = self.max_tokens / max(current_tokens, 1)
        target_chars = max(1, int(len(text) * ratio * 0.9))

        truncated = text[:target_chars]
        # 尝试在语义边界处截断
        for boundary in ["\n\n", "\n", ". ", "。", "；"]:
            last_boundary = truncated.rfind(boundary)
            if last_boundary > target_chars * 0.7:
                truncated = truncated[: last_boundary + len(boundary)]
                break

        truncated += (
            f"\n\n[上下文已截断: {current_tokens} → {counter.count(truncated)} tokens"
            f", 限制 {self.max_tokens}]"
        )
        return truncated

    def estimate_token_count(self, text: str) -> int:
        """使用 tiktoken 精确计算文本的 token 数量。"""
        return get_token_counter().count(text)

    def count_messages_tokens(self, messages: List[Dict[str, str]]) -> int:
        """计算消息列表的精确 token 数。"""
        return get_token_counter().count_messages(messages)

    # ------------------------------------------------------------------
    # 上下文压缩（v2: 三级压缩策略）
    # ------------------------------------------------------------------

    def compact_history_messages(
        self,
        messages: List[Dict[str, str]],
        *,
        budget_tokens: int,
        query: str = "",
    ) -> List[Dict[str, str]]:
        """在 token 预算内压缩历史消息。

        三级策略:
            L1 — 最近 2 对 user/assistant 完整保留
            L2 — 更早的消息合并为一条摘要消息
            L3 — 超出预算时裁剪 L2 摘要

        Args:
            messages: 完整的历史消息列表
            budget_tokens: 分配给历史的 token 预算
            query: 当前查询（用于保留语义相关性）

        Returns:
            压缩后的消息列表（可能包含摘要消息）
        """
        counter = get_token_counter()
        total = counter.count_messages(messages)

        if total <= budget_tokens:
            return messages

        # 分离最近 2 轮和更早的消息
        recent_pairs = 2
        recent_count = min(recent_pairs * 2, len(messages))
        recent = messages[-recent_count:] if recent_count > 0 else []
        older = messages[:-recent_count] if len(messages) > recent_count else []

        recent_tokens = counter.count_messages(recent)
        remaining_budget = budget_tokens - recent_tokens

        if not older or remaining_budget <= 0:
            # 连最近消息都超出预算，做硬截断
            return self._hard_truncate_messages(recent, budget_tokens)

        # 对更早的消息生成结构化摘要
        summary = self._summarize_older_messages(older)
        summary_msg = {
            "role": "system",
            "content": f"[历史对话摘要] {summary}",
        }
        summary_tokens = counter.count(summary_msg["content"]) + 4

        if summary_tokens > remaining_budget:
            # 摘要本身超出预算，裁剪摘要
            ratio = remaining_budget / max(summary_tokens, 1)
            truncate_chars = int(len(summary_msg["content"]) * ratio * 0.9)
            summary_msg["content"] = (
                summary_msg["content"][:truncate_chars] + "...[摘要已裁剪]"
            )

        return [summary_msg] + recent

    def _hard_truncate_messages(
        self,
        messages: List[Dict[str, str]],
        budget_tokens: int,
    ) -> List[Dict[str, str]]:
        """硬截断消息列表到 token 预算内（从最早的消息开始丢弃）。"""
        counter = get_token_counter()
        result: List[Dict[str, str]] = []
        used = 0

        for msg in reversed(messages):
            msg_tokens = counter.count(msg.get("content", "")) + 4
            if used + msg_tokens > budget_tokens:
                break
            result.insert(0, msg)
            used += msg_tokens

        return result

    @staticmethod
    def _summarize_older_messages(messages: List[Dict[str, str]]) -> str:
        """对更早的消息生成结构化摘要（轻量级，基于模式提取）。

        注意：完整的 LLM 摘要需要异步调用，这里提供轻量级的模式提取。
        对于真正的 LLM 摘要，由调用方在外部异步处理。
        """
        if not messages:
            return "无历史对话。"

        parts: List[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                # 截断用户消息用于摘要
                short = content[:120] + "..." if len(content) > 120 else content
                parts.append(f"用户: {short}")
            elif role == "assistant":
                short = content[:200] + "..." if len(content) > 200 else content
                parts.append(f"助手: {short}")
            elif role == "tool":
                # 工具调用结果只保留关键信息
                parts.append("[工具执行结果]")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Schema 按需注入（v2）
    # ------------------------------------------------------------------

    def filter_schema_for_query(
        self,
        schema_lines: List[str],
        query: str,
    ) -> List[str]:
        """根据用户查询过滤 schema，仅保留相关列。

        策略：将 query 分词，检查每个 schema 行中的列名是否包含
        query 中的关键词。匹配到的整行保留，未匹配的仅保留文件名和列数统计。

        Args:
            schema_lines: 原始 schema 行列表
            query: 用户查询文本

        Returns:
            过滤后的 schema 行列表
        """
        if not query or not schema_lines:
            return schema_lines

        # 从 query 提取关键词（过滤停用词和短词）
        keywords = self._extract_keywords(query)
        if not keywords:
            return schema_lines

        filtered: List[str] = []
        for line in schema_lines:
            # 检查该行是否包含 query 关键词
            line_lower = line.lower()
            relevant = any(kw.lower() in line_lower for kw in keywords)
            if relevant:
                filtered.append(line)
            else:
                # 只保留文件/表名 + 列数统计
                compact = self._compact_schema_line(line)
                if compact and compact not in filtered:
                    filtered.append(compact)

        if not filtered:
            return schema_lines  # 回退：全量保留

        return filtered

    @staticmethod
    def _extract_keywords(query: str) -> List[str]:
        """从查询中提取有意义的关键词。"""
        # 常见停用词
        stop_words = {
            "的", "了", "在", "是", "我", "你", "他", "她", "它",
            "这", "那", "和", "与", "或", "对", "不", "要", "请",
            "把", "被", "让", "从", "到", "用", "为", "给", "帮",
            "个", "些", "一个", "这个", "那个", "什么", "怎么",
            "the", "a", "an", "is", "are", "in", "of", "to", "for",
        }

        # 简单分词（基于空格、标点符号）
        import re
        tokens = re.split(r"[\s,，。！？、；：""''（）\(\)\[\]{}]+", query.lower())
        keywords = [
            t for t in tokens
            if len(t) >= 2 and t not in stop_words
        ]
        return keywords[:10]  # 最多 10 个关键词

    @staticmethod
    def _compact_schema_line(line: str) -> str:
        """压缩单行 schema：去掉详细列名，只保留文件名/表名和列数。"""
        # 格式: "- 文件 [xxx] 表 [Sheet1] 列: col1, col2, col3, ..."
        # 压缩为: "- 文件 [xxx] 表 [Sheet1] (N 列)"
        import re

        match = re.match(
            r"(- 文件 \[(.+?)\] 表 \[(.+?)\] 列: )(.+)", line
        )
        if match:
            prefix = f"- 文件 [{match.group(2)}] 表 [{match.group(3)}]"
            columns = [c.strip() for c in match.group(4).split(",")]
            return f"{prefix} ({len(columns)} 列)"
        return line

    # ------------------------------------------------------------------
    # 上下文预算感知（v2）
    # ------------------------------------------------------------------

    def compact_context_if_needed(
        self,
        messages: List[Dict[str, str]],
        schema_lines: List[str],
        *,
        max_input_tokens: int = 8000,
        query: str = "",
    ) -> tuple[List[Dict[str, str]], List[str], Dict[str, int]]:
        """在 Agent 循环每次迭代前检查 token 预算，不足时自动压缩。

        Returns:
            (压缩后的 messages, 压缩后的 schema_lines, 预算报告)
        """
        counter = get_token_counter()
        schema_text = "\n".join(schema_lines) if schema_lines else ""
        schema_tokens = counter.count(schema_text)

        msgs_tokens = counter.count_messages(messages)
        total = msgs_tokens + schema_tokens

        budget_report = {
            "messages_tokens": msgs_tokens,
            "schema_tokens": schema_tokens,
            "total_tokens": total,
            "budget": max_input_tokens,
            "compacted": False,
        }

        if total <= max_input_tokens:
            return messages, schema_lines, budget_report

        # 超出预算：先压缩 schema，再压缩消息
        budget_report["compacted"] = True

        # Step 1: Schema 按需过滤
        compacted_schema = self.filter_schema_for_query(schema_lines, query)

        # Step 2: 为历史消息分配预算（优先保证 schema + 当前 query）
        schema_text_new = "\n".join(compacted_schema)
        new_schema_tokens = counter.count(schema_text_new)
        history_budget = max(
            1000, max_input_tokens - new_schema_tokens - 2000
        )

        compacted_msgs = self.compact_history_messages(
            messages, budget_tokens=history_budget, query=query
        )

        budget_report["messages_tokens_after"] = counter.count_messages(
            compacted_msgs
        )
        budget_report["schema_tokens_after"] = new_schema_tokens

        return compacted_msgs, compacted_schema, budget_report


# 工厂函数
def create_context_builder(
    max_history_turns: int = 5,
    max_tokens: int = 2000
) -> ContextBuilder:
    """创建上下文构建器实例"""
    return ContextBuilder(
        max_history_turns=max_history_turns,
        max_tokens=max_tokens
    )
