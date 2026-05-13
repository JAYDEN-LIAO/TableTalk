"""生成+验证复合阶段

v2: LLM 生成自校正
  - 错误分类器（COLUMN_NOT_FOUND / SYNTAX_ERROR / LOGIC_ERROR）
  - 针对性修复提示
  - 渐进式修复策略
"""
import re
import json
import logging
from enum import Enum
from typing import Any, Dict, Generator, List, Optional, Tuple, TYPE_CHECKING

from tablo.types import ProcessStage, EventType, ProcessEvent, ProcessConfig
from tablo.stages.base import Stage
from tablo.stages.errors import StageError

if TYPE_CHECKING:
    from tablo.models import FileCollection
    from typing import Any as LLMClient

logger = logging.getLogger(__name__)


# --- v2: 错误分类 ---

class ErrorCategory(Enum):
    """验证错误的分类。"""
    COLUMN_NOT_FOUND = "column_not_found"    # 列名/文件名不存在
    SYNTAX_ERROR = "syntax_error"            # JSON 格式/结构错误
    LOGIC_ERROR = "logic_error"              # 函数参数不匹配、引用链断裂等
    UNKNOWN = "unknown"


def classify_validation_errors(
    errors: List[str],
    available_columns: Optional[List[str]] = None,
) -> Dict[ErrorCategory, List[str]]:
    """将验证错误按类型分组，用于生成针对性修复提示。

    Args:
        errors: 验证错误消息列表
        available_columns: 可用的列名列表（用于判断列名错误）

    Returns:
        {ErrorCategory: [error_messages]}
    """
    classified: Dict[ErrorCategory, List[str]] = {
        ErrorCategory.COLUMN_NOT_FOUND: [],
        ErrorCategory.SYNTAX_ERROR: [],
        ErrorCategory.LOGIC_ERROR: [],
        ErrorCategory.UNKNOWN: [],
    }

    col_patterns = [
        "列名", "column", "列 ", "表中不存在", "找不到", "not found",
        "不存在", "没有这个", "未定义", "unknown column",
    ]
    syntax_patterns = [
        "JSON", "json", "格式错误", "解析", "parse", "syntax",
        "缺少", "缺少必填", "required", "类型错误", "expected",
        "不是有效的", "invalid",
    ]
    logic_patterns = [
        "参数", "argument", "必须", "不能", "冲突", "引用",
        "不匹配", "circular", "循环", "交叉", "表名",
    ]

    for err in errors:
        err_lower = err.lower()

        # 检查是否命中列名模式
        if any(p.lower() in err_lower for p in col_patterns):
            # 进一步检查：错误中是否提到了具体的列名
            if available_columns:
                mentioned_col = any(
                    col.lower() in err_lower for col in available_columns
                )
                if mentioned_col:
                    classified[ErrorCategory.COLUMN_NOT_FOUND].append(err)
                    continue
            else:
                classified[ErrorCategory.COLUMN_NOT_FOUND].append(err)
                continue

        # 检查语法错误
        if any(p.lower() in err_lower for p in syntax_patterns):
            classified[ErrorCategory.SYNTAX_ERROR].append(err)
            continue

        # 检查逻辑错误
        if any(p.lower() in err_lower for p in logic_patterns):
            classified[ErrorCategory.LOGIC_ERROR].append(err)
            continue

        classified[ErrorCategory.UNKNOWN].append(err)

    return classified


def build_targeted_retry_hint(
    classified: Dict[ErrorCategory, List[str]],
    available_columns: Optional[List[str]] = None,
    retry_count: int = 1,
) -> str:
    """根据错误分类生成针对性修复提示。

    Args:
        classified: 分类后的错误
        available_columns: 可用列名列表
        retry_count: 当前重试次数（影响提示详细程度）

    Returns:
        针对性的修复提示文本
    """
    hints: List[str] = []

    if classified[ErrorCategory.COLUMN_NOT_FOUND]:
        errors = classified[ErrorCategory.COLUMN_NOT_FOUND]
        hint = "## 列名/表名错误\n\n"
        hint += f"发现 {len(errors)} 个列名或表名相关的错误。\n"
        if available_columns:
            hint += f"**可用的列名包括**: {', '.join(available_columns[:30])}"
            if len(available_columns) > 30:
                hint += f" (等 {len(available_columns)} 列)"
            hint += "\n"
        hint += "**修复方向**: 请逐一核对操作中的 file_id、table（sheet名）、column 字段，"
        hint += "确保它们与上方 schema 中的名称完全一致（区分大小写）。\n"
        if retry_count >= 2:
            hint += "如果某个列名确实不存在，考虑用相近的列名替代，或去掉该操作。\n"
        hints.append(hint)

    if classified[ErrorCategory.SYNTAX_ERROR]:
        errors = classified[ErrorCategory.SYNTAX_ERROR]
        hint = "## JSON 格式/结构错误\n\n"
        hint += f"发现 {len(errors)} 个格式或结构相关的错误。\n"
        hint += "**修复方向**:\n"
        hint += "1. 确保 JSON 语法正确（引号配对、逗号不遗漏、括号匹配）\n"
        hint += "2. 每个操作必须包含 type, description, file_id, table 字段\n"
        hint += "3. 不同操作类型的 required 字段不同，请对照操作规范检查\n"
        if retry_count >= 2:
            hint += "4. 如果某个操作类型不确定字段，简化操作为更基础的类型\n"
        hints.append(hint)

    if classified[ErrorCategory.LOGIC_ERROR]:
        errors = classified[ErrorCategory.LOGIC_ERROR]
        hint = "## 逻辑/参数错误\n\n"
        hint += f"发现 {len(errors)} 个逻辑或参数相关的错误。\n"
        hint += "**修复方向**:\n"
        hint += "1. 检查函数参数数量和类型是否匹配（如 VLOOKUP 需要 4 个参数）\n"
        hint += "2. 确认跨表引用格式为 file_id.sheet_name.column_name（三段式）\n"
        hint += "3. 检查变量引用的定义顺序（先 aggregate/create_variable 再引用）\n"
        if retry_count >= 2:
            hint += "4. 考虑将复杂操作拆分为多个简单操作\n"
        hints.append(hint)

    if classified[ErrorCategory.UNKNOWN]:
        errors = classified[ErrorCategory.UNKNOWN]
        hint = "## 其他错误\n\n"
        hint += f"发现 {len(errors)} 个未分类的错误。\n"
        for err in errors[:3]:
            hint += f"- {err}\n"
        hint += "**修复方向**: 请根据具体错误信息修正，重点关注字段名和数据格式。\n"
        hints.append(hint)

    # 组装最终提示
    severity = "初次" if retry_count == 1 else ("再次" if retry_count == 2 else "最后一次")
    prefix = f"## {severity}重试修复指引\n\n"
    return prefix + "\n---\n".join(hints)


class GenerateValidateStage(Stage):
    """
    生成+验证复合阶段

    内部封装了 generate + validate 的重试循环，但仍然 yield 两种阶段的事件，
    使得前端可以区分生成和验证的状态。

    流程：
    1. 调用 LLM 生成操作 JSON (yield generate 事件)
    2. 解析验证操作 (yield validate 事件)
    3. 如果验证失败且未超过重试次数，带错误信息重新生成

    输入:
        - tables: 表集合
        - query: 用户查询
        - context["analyze"]: 分析结果（可选）
        - context["processing_context"]: 外部编排层准备好的增强上下文（可选）

    输出:
        {
            "operations": {...},       # 解析后的操作字典
            "operations_json": "...",  # 原始 JSON 字符串
            "parsed_operations": [...], # 解析后的 Operation 对象列表
            "validation_errors": [...], # 验证错误（如果有）
        }
    """

    # 主阶段标识（用于 context key）
    stage = ProcessStage.GENERATE

    def __init__(self, llm_client: "LLMClient"):
        self.llm_client = llm_client

    def run(
        self,
        tables: "FileCollection",
        query: str,
        config: ProcessConfig,
        context: dict,
    ) -> Generator[ProcessEvent, None, Any]:
        """执行生成+验证流程"""

        # 使用增强的 schema（包含类型和样本数据）
        schemas = tables.get_schemas_with_samples(sample_count=3)
        analysis = context.get("analyze", {}).get("content", "")
        file_sheets = self._build_file_sheets(tables)

        processing_context = context.get("processing_context", "")

        retry_count = 0
        max_retries = config.max_validation_retries

        # 用于重试时传递错误信息
        previous_errors: Optional[List[str]] = None
        previous_json: Optional[str] = None
        targeted_hint: Optional[str] = None  # v2: 针对性修复提示

        # 收集可用列名供错误分类使用
        available_columns = self._collect_all_columns(tables)

        # 最终输出
        operations_dict = {}
        operations_json = ""
        parsed_operations = []
        validation_errors = []

        while True:
            # ========== 1. 生成阶段 ==========
            try:
                operations_json, operations_dict = yield from self._run_generate(
                    query, analysis, schemas, config,
                    previous_errors=previous_errors,
                    previous_json=previous_json,
                    processing_context=processing_context,
                    targeted_hint=targeted_hint,
                )
            except StageError:
                raise
            except Exception as e:
                # 防御性编程：捕获未预期的异常
                error_msg = f"生成操作失败: {e}"
                yield self._create_event(
                    ProcessStage.GENERATE, EventType.STAGE_ERROR,
                    stage_id=self._generate_stage_id(), error=error_msg
                )
                raise StageError(error_msg) from e

            # ========== 2. 验证阶段 ==========
            try:
                parsed_operations, validation_errors = yield from self._run_validate(
                    operations_json, file_sheets
                )
            except StageError:
                raise
            except Exception as e:
                # 防御性编程：捕获未预期的异常
                error_msg = f"验证失败: {e}"
                yield self._create_event(
                    ProcessStage.VALIDATE, EventType.STAGE_ERROR,
                    stage_id=self._generate_stage_id(), error=error_msg
                )
                raise StageError(error_msg) from e

            # ========== 3. 检查是否需要重试 ==========
            if not validation_errors:
                # 验证通过，跳出循环
                break

            retry_count += 1

            if retry_count > max_retries:
                # 超过最大重试次数
                logger.warning(
                    f"验证失败，已达到最大重试次数 ({max_retries})，继续执行"
                )
                break

            # --- v2: 错误分类 + 针对性提示 ---
            classified = classify_validation_errors(validation_errors, available_columns)
            targeted_hint = build_targeted_retry_hint(
                classified, available_columns, retry_count=retry_count
            )
            logger.info(
                f"验证失败，准备重试 ({retry_count}/{max_retries})，"
                f"错误分类: col={len(classified[ErrorCategory.COLUMN_NOT_FOUND])} "
                f"syntax={len(classified[ErrorCategory.SYNTAX_ERROR])} "
                f"logic={len(classified[ErrorCategory.LOGIC_ERROR])}"
            )
            previous_errors = validation_errors
            previous_json = operations_json

        # 返回最终输出
        output = {
            "operations": operations_dict,
            "operations_json": operations_json,
            "parsed_operations": parsed_operations,
            "validation_errors": validation_errors,
        }
        return output

    def _run_generate(
        self,
        query: str,
        analysis: str,
        schemas: dict,
        config: ProcessConfig,
        previous_errors: Optional[List[str]] = None,
        previous_json: Optional[str] = None,
        processing_context: str = "",
        targeted_hint: Optional[str] = None,
    ) -> Generator[ProcessEvent, None, tuple]:
        """
        运行生成子阶段

        Args:
            targeted_hint: v2 针对性修复提示（错误分类后的具体指引）

        Returns:
            (operations_json, operations_dict)
        """
        # 为此次生成子阶段生成唯一 ID
        stage_id = self._generate_stage_id()

        yield self._create_event(ProcessStage.GENERATE, EventType.STAGE_START, stage_id=stage_id)

        try:
            # 构建增强的用户查询，包含处理上下文
            enhanced_query = query
            if processing_context:
                enhanced_query = f"{processing_context}\n\n当前请求：{query}"

            if config.stream_llm:
                operations_json = ""
                accumulated_text = ""

                for delta, full_content in self.llm_client.generate_operations_stream(
                    enhanced_query, analysis, schemas,
                    previous_errors=previous_errors,
                    previous_json=previous_json,
                    targeted_hint=targeted_hint,
                ):
                    if delta:
                        accumulated_text += delta

                    if full_content and full_content.strip():
                        operations_json = full_content

                    yield self._event_stream(delta, stage_id)

                if not operations_json.strip():
                    operations_json = accumulated_text

                # 清理 JSON 响应
                operations_json = self._clean_json_response(operations_json)
            else:
                # 非流式调用
                operations_json = self.llm_client.generate_operations(
                    enhanced_query, analysis, schemas,
                    previous_errors=previous_errors,
                    previous_json=previous_json,
                    targeted_hint=targeted_hint,
                )

            # 解析 JSON
            try:
                operations_dict = json.loads(operations_json)
            except json.JSONDecodeError as e:
                raise StageError(f"JSON 解析失败: {e}") from e

            yield self._create_event(
                ProcessStage.GENERATE, EventType.STAGE_DONE,
                stage_id=stage_id, output=operations_dict  # operations_dict 本身就是 {"operations": [...]}
            )

            return operations_json, operations_dict

        except StageError:
            raise
        except Exception as e:
            error_msg = f"生成操作失败: {e}"
            yield self._create_event(
                ProcessStage.GENERATE, EventType.STAGE_ERROR,
                stage_id=stage_id, error=error_msg
            )
            raise StageError(error_msg) from e

    def _run_validate(
        self,
        operations_json: str,
        file_sheets: Dict[str, List[str]],
    ) -> Generator[ProcessEvent, None, tuple]:
        """
        运行验证子阶段

        Returns:
            (parsed_operations, errors)
        """
        # 为此次验证子阶段生成唯一 ID
        stage_id = self._generate_stage_id()

        yield self._create_event(ProcessStage.VALIDATE, EventType.STAGE_START, stage_id=stage_id)

        try:
            from tablo.parser import parse_and_validate

            parsed_operations, errors = parse_and_validate(operations_json, file_sheets)

            yield self._create_event(
                ProcessStage.VALIDATE, EventType.STAGE_DONE,
                stage_id=stage_id,
                output={
                    "valid": len(errors) == 0,
                    "operation_count": len(parsed_operations),
                    "errors": errors if errors else None,
                }
            )

            return parsed_operations, errors

        except Exception as e:
            error_msg = f"验证失败: {e}"
            logger.exception(error_msg)
            yield self._create_event(
                ProcessStage.VALIDATE, EventType.STAGE_ERROR,
                stage_id=stage_id, error=error_msg
            )
            raise StageError(error_msg) from e

    def _create_event(
        self,
        stage: ProcessStage,
        event_type: EventType,
        stage_id: str = None,
        output: Any = None,
        delta: str = None,
        error: str = None,
    ) -> ProcessEvent:
        """创建指定阶段的事件"""
        return ProcessEvent(
            stage=stage,
            event_type=event_type,
            stage_id=stage_id,
            output=output,
            delta=delta,
            error=error,
        )

    @staticmethod
    def _collect_all_columns(tables: "FileCollection") -> Optional[List[str]]:
        """收集所有可用的列名（用于错误分类时判断列名错误）。"""
        try:
            columns: List[str] = []
            for file_id in tables.get_file_ids():
                excel_file = tables.get_file(file_id)
                for sheet_name in excel_file.get_sheet_names():
                    table = excel_file.get_sheet(sheet_name)
                    columns.extend(table.get_columns())
            return list(set(columns))
        except Exception:
            return None

    def _build_file_sheets(self, tables: "FileCollection") -> Dict[str, List[str]]:
        """构建 file_id -> sheet_names 映射"""
        file_sheets = {}
        for file_id in tables.get_file_ids():
            excel_file = tables.get_file(file_id)
            file_sheets[file_id] = excel_file.get_sheet_names()
        return file_sheets

    def _clean_json_response(self, content: str) -> str:
        """清理 LLM 响应中可能存在的思考过程、XML 标签和 markdown 标记"""
        content = content.strip()

        match = re.search(r"<SELGETABEL_EXCEL_IR_OUTPUT>\s*(.*?)\s*</SELGETABEL_EXCEL_IR_OUTPUT>", content, re.DOTALL | re.IGNORECASE)
        target_str = match.group(1) if match else content

        start_idx = target_str.find('{')
        end_idx = target_str.rfind('}')
        if start_idx != -1 and end_idx != -1 and end_idx >= start_idx:
            clean_str = target_str[start_idx:end_idx+1]
        else:
            clean_str = target_str.strip()

        return clean_str
