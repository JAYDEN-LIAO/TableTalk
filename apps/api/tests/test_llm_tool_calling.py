import pytest
from types import SimpleNamespace

from app.engine.llm_providers.adapters.openai import OpenAIProvider
from app.engine.llm_providers.types import LLMRequest
from app.services.excel_agent import ExcelAgent


class NativeToolLLMClient:
    def __init__(self):
        self.calls = []

    def call_llm_with_tools(self, stage, system_prompt, messages, tools, tool_choice):
        self.calls.append(
            {
                "stage": stage,
                "system_prompt": system_prompt,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        return SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "name": "processing_workflow",
                    "arguments": {
                        "intent": "processing",
                        "context": {"context_type": "processing"},
                    },
                }
            ],
        )


class EmptyToolCallLLMClient:
    def __init__(self):
        self.json_called = False

    def call_llm_with_tools(self, stage, system_prompt, messages, tools, tool_choice):
        return SimpleNamespace(content="", tool_calls=[])

    def call_llm(self, *args, **kwargs):
        self.json_called = True
        raise AssertionError("Legacy JSON fallback should not be used")


@pytest.mark.anyio
async def test_excel_agent_prefers_native_tool_calling():
    agent = ExcelAgent(NativeToolLLMClient())

    decision = await agent._run_loop(
        query="删除 Date 列",
        history_messages=[],
        file_schema_lines=["- 文件 [orders.xlsx] 表 [Sheet1] 列: Date, Amount"],
        file_ids=["file-1"],
    )

    assert decision["tool_name"] == "processing_workflow"
    assert decision["tool_args"]["intent"] == "processing"


@pytest.mark.anyio
async def test_excel_agent_requires_native_tool_calls_for_tool_decisions():
    agent = ExcelAgent(EmptyToolCallLLMClient())

    decision = await agent._run_loop(
        query="删除 Date 列",
        history_messages=[],
        file_schema_lines=["- 文件 [orders.xlsx] 表 [Sheet1] 列: Date, Amount"],
        file_ids=["file-1"],
    )

    assert decision is None


@pytest.mark.anyio
async def test_excel_agent_does_not_fallback_to_json_tool_invocation():
    llm_client = EmptyToolCallLLMClient()
    agent = ExcelAgent(llm_client)

    decision = await agent.run(
        query="删除 Date 列",
        file_ids=["file-1"],
        thread_id=None,
        db_session=None,
        user_id=None,
    )

    assert decision["tool_name"] == "clarification_response"
    assert llm_client.json_called is False


def test_openai_provider_complete_passes_tools_and_parses_tool_calls():
    captured = {}

    class FakeCompletions:
        def create(self, **params):
            captured.update(params)
            message = SimpleNamespace(
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_1",
                        function=SimpleNamespace(
                            name="conversation_response",
                            arguments='{"intent":"chat","context":{"context_type":"chat"}}',
                        ),
                    )
                ],
            )
            choice = SimpleNamespace(message=message)
            return SimpleNamespace(choices=[choice], usage=None)

    provider = OpenAIProvider(api_key="test-key")
    provider.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

    response = provider.complete(
        LLMRequest(
            model_id="gpt-test",
            messages=[{"role": "user", "content": "hi"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "conversation_response",
                        "description": "Directly answer the user",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            tool_choice="required",
        )
    )

    assert captured["tools"][0]["function"]["name"] == "conversation_response"
    assert captured["tool_choice"] == "required"
    assert response.tool_calls[0]["name"] == "conversation_response"


class LoopingLLMClient:
    def __init__(self):
        self.calls = 0

    def call_llm_with_tools(self, stage, system_prompt, messages, tools, tool_choice):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "name": "processing_workflow",
                        "arguments": {
                            "intent": "processing",
                            "context": {"context_type": "processing"},
                        },
                    }
                ],
            )
        return SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "name": "conversation_response",
                    "arguments": {
                        "intent": "chat",
                        "response_text": "已删除 Date 列，并确认处理成功。",
                        "context": {"context_type": "chat"},
                    },
                }
            ],
        )


class FakeExecutor:
    def __init__(self):
        self.decisions = []

    async def execute_for_agent(self, *, decision, **kwargs):
        self.decisions.append(decision)
        return {
            "tool_name": decision["tool_name"],
            "status": "done",
            "output": {"success": True},
        }


@pytest.mark.anyio
async def test_excel_agent_can_take_second_decision_after_tool_observation():
    agent = ExcelAgent(LoopingLLMClient())
    executor = FakeExecutor()

    decision = await agent.run(
        query="删除 Date 列然后告诉我做完了什么",
        file_ids=["file-1"],
        thread_id=None,
        db_session=None,
        user_id=None,
        tool_executor=executor,
    )

    assert len(executor.decisions) == 1
    assert executor.decisions[0]["tool_name"] == "processing_workflow"
    assert decision["tool_name"] == "conversation_response"
    assert decision["response_text"] == "已删除 Date 列，并确认处理成功。"
