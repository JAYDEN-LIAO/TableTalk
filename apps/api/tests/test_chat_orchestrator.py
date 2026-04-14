from app.services.excel_agent import (
    ANALYSIS_INTENT,
    CHAT_INTENT,
    PROCESSING_INTENT,
    ExcelAgent,
)


class DummyLLMClient:
    def call_llm(self, *args, **kwargs):
        raise AssertionError("LLM should not be called in these unit tests")


def test_normalize_decision_nulls_response_text_for_non_clarify():
    agent = ExcelAgent(DummyLLMClient())

    decision = agent._normalize_decision(
        {
            "tool_name": "processing_workflow",
            "intent": CHAT_INTENT,
            "response_text": "should be dropped",
            "reasoning": "processing request",
            "context": {},
        },
        ["file-1"],
    )

    assert decision["tool_name"] == "processing_workflow"
    assert decision["intent"] == PROCESSING_INTENT
    assert decision["response_text"] is None
    assert decision["context"]["context_type"] == PROCESSING_INTENT
    assert decision["context"]["agent_reasoning"] == "processing request"
    assert decision["tool_args"]["intent"] == PROCESSING_INTENT


def test_fallback_decision_asks_for_file_before_workflow():
    agent = ExcelAgent(DummyLLMClient())

    decision = agent._fallback_decision(query="帮我分析这个表", file_ids=[])

    assert decision["tool_name"] == "clarification_response"
    assert decision["intent"] == CHAT_INTENT
    assert "上传文件" in decision["response_text"]


def test_fallback_decision_routes_analysis_and_processing_requests():
    agent = ExcelAgent(DummyLLMClient())

    analysis = agent._fallback_decision(query="分析销售趋势", file_ids=["file-1"])
    processing = agent._fallback_decision(query="删除 Date 列", file_ids=["file-1"])

    assert analysis["tool_name"] == "analysis_workflow"
    assert analysis["intent"] == ANALYSIS_INTENT
    assert processing["tool_name"] == "processing_workflow"
    assert processing["intent"] == PROCESSING_INTENT
