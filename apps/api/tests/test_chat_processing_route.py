import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import chat as chat_route
from app.core.sse import sse


class FakeExcelAgent:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.decision

    async def run_stream(self, **kwargs):
        self.calls.append(kwargs)
        tool_executor = kwargs["tool_executor"]
        async for event in tool_executor.execute(
            decision=self.decision,
            query=kwargs["query"],
            user_id=kwargs["user_id"],
            thread_id=kwargs["thread_id"],
            db=kwargs["db_session"],
            file_ids=kwargs["file_ids"],
        ):
            yield event


class FakeStreamingExcelAgent:
    def __init__(self, events):
        self.events = events
        self.calls = []

    async def run_stream(self, **kwargs):
        self.calls.append(kwargs)
        for event in self.events:
            yield sse(event)


class FakeToolDispatcher:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        yield sse({"step": "complete", "status": "done", "output": kwargs["decision"]["tool_name"]})


class FakeResponseExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        decision = kwargs["decision"]
        yield sse({"step": "chat", "status": "done", "output": decision["response_text"]})
        yield sse({"step": "complete", "status": "done"})


def _collect_sse_json(response):
    events = []
    for line in response.iter_lines():
        if not line:
            continue
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _build_app():
    app = FastAPI()
    app.include_router(chat_route.router)
    return app


def test_chat_route_uses_excel_agent_for_processing_workflow(monkeypatch):
    dispatcher = FakeToolDispatcher()
    decision = {
        "tool_name": "processing_workflow",
        "intent": "processing",
        "response_text": None,
        "file_ids": [str(uuid4())],
        "context": {"context_type": "processing"},
        "tool_args": {"intent": "processing", "context": {"context_type": "processing"}},
    }
    agent = FakeExcelAgent(decision)

    async def fake_get_excel_agent(db):
        return agent

    async def fake_get_current_user():
        return SimpleNamespace(id=uuid4())

    async def fake_get_db():
        yield object()

    app = _build_app()
    app.dependency_overrides[chat_route.get_current_user] = fake_get_current_user
    app.dependency_overrides[chat_route.get_db] = fake_get_db

    monkeypatch.setattr(chat_route, "get_excel_agent", fake_get_excel_agent)
    monkeypatch.setattr(chat_route, "get_tool_executor", lambda: dispatcher)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/chat",
            json={"query": "删除某列", "file_ids": [], "thread_id": None},
        ) as response:
            assert response.status_code == 200
            events = _collect_sse_json(response)

    captured = dispatcher.calls[0]
    assert agent.calls[0]["tool_executor"] is dispatcher
    assert captured["decision"]["tool_name"] == "processing_workflow"
    assert captured["query"] == "删除某列"
    assert captured["decision"]["tool_args"]["intent"] == "processing"
    assert events[-1]["output"] == "processing_workflow"


def test_chat_route_uses_excel_agent_for_direct_response(monkeypatch):
    dispatcher = FakeToolDispatcher()
    decision = {
        "tool_name": "conversation_response",
        "intent": "chat",
        "response_text": None,
        "file_ids": [],
        "context": {"context_type": "chat"},
        "tool_args": {"intent": "chat", "context": {"context_type": "chat"}},
    }
    agent = FakeExcelAgent(decision)

    async def fake_get_excel_agent(db):
        return agent

    async def fake_get_current_user():
        return SimpleNamespace(id=uuid4())

    async def fake_get_db():
        yield object()

    app = _build_app()
    app.dependency_overrides[chat_route.get_current_user] = fake_get_current_user
    app.dependency_overrides[chat_route.get_db] = fake_get_db

    monkeypatch.setattr(chat_route, "get_excel_agent", fake_get_excel_agent)
    monkeypatch.setattr(chat_route, "get_tool_executor", lambda: dispatcher)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/chat",
            json={"query": "这个产品能做什么？", "file_ids": [], "thread_id": None},
        ) as response:
            assert response.status_code == 200
            events = _collect_sse_json(response)

    captured = dispatcher.calls[0]
    assert agent.calls[0]["tool_executor"] is dispatcher
    assert captured["decision"] == decision
    assert captured["query"] == "这个产品能做什么？"
    assert events[-1]["output"] == "conversation_response"


def test_chat_route_streams_agent_final_response_text(monkeypatch):
    executor = FakeResponseExecutor()
    decision = {
        "tool_name": "conversation_response",
        "intent": "chat",
        "response_text": "我已经处理完成，并总结好了结果。",
        "file_ids": [],
        "context": {"context_type": "chat"},
        "tool_args": {"intent": "chat", "context": {"context_type": "chat"}},
    }
    agent = FakeExcelAgent(decision)

    async def fake_get_excel_agent(db):
        return agent

    async def fake_get_current_user():
        return SimpleNamespace(id=uuid4())

    async def fake_get_db():
        yield object()

    app = _build_app()
    app.dependency_overrides[chat_route.get_current_user] = fake_get_current_user
    app.dependency_overrides[chat_route.get_db] = fake_get_db

    monkeypatch.setattr(chat_route, "get_excel_agent", fake_get_excel_agent)
    monkeypatch.setattr(chat_route, "get_tool_executor", lambda: executor)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/chat",
            json={"query": "做完后告诉我", "file_ids": [], "thread_id": None},
        ) as response:
            assert response.status_code == 200
            events = _collect_sse_json(response)

    outputs = [event.get("output") for event in events if event.get("step") == "chat" and event.get("status") == "done"]
    assert "我已经处理完成，并总结好了结果。" in outputs


@pytest.mark.anyio
async def test_excel_agent_reuses_agent_owned_thread_id_for_followup_response():
    from app.services.excel_agent import ExcelAgent

    class TwoStepLLM:
        def __init__(self):
            self.calls = 0

        def call_llm_with_tools(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(
                    tool_calls=[
                        {
                            "name": "processing_workflow",
                            "arguments": {
                                "intent": "processing",
                                "context": {"context_type": "processing"},
                            },
                        }
                    ],
                    content="",
                )
            return SimpleNamespace(
                tool_calls=[
                    {
                        "name": "conversation_response",
                        "arguments": {
                            "intent": "chat",
                            "response_text": "处理完成",
                            "context": {"context_type": "chat"},
                        },
                    }
                ],
                content="",
            )

    class WorkflowThenResponseExecutor:
        def __init__(self):
            self.calls = []

        async def execute(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["decision"]["tool_name"] == "processing_workflow":
                yield sse({"step": "execute", "status": "done", "output": {"success": True}})
                return
            yield sse({"step": "chat", "status": "done", "output": kwargs["decision"]["response_text"]})
            yield sse({"step": "complete", "status": "done"})

    class FakeRepo:
        def __init__(self, db):
            self.db = db

        async def create_thread(self, user_id, title):
            return SimpleNamespace(id=uuid4())

        async def get_next_turn_number(self, thread_id):
            return 1

        async def create_turn(self, thread_id, turn_number, user_query, intent_type=None):
            return SimpleNamespace(id=uuid4())

        async def link_files_to_turn(self, turn_id, file_ids, user_id):
            return file_ids

        async def mark_processing(self, turn_id, tracker):
            return None

        async def save_steps(self, turn_id, tracker):
            return None

        async def replace_turn_steps(self, turn_id, steps):
            return None

        async def update_turn_response_text(self, turn_id, response_text):
            return None

        async def update_turn_intent_type(self, turn_id, intent_type):
            return None

        async def finalize_turn(self, turn_id, thread_id, status, has_error=False):
            return None

        async def commit(self):
            return None

    async def fake_commit():
        return None

    agent = ExcelAgent(TwoStepLLM())
    executor = WorkflowThenResponseExecutor()

    import app.services.excel_agent as excel_agent_module
    original_repo = excel_agent_module.TurnRepository
    excel_agent_module.TurnRepository = FakeRepo
    fake_db = SimpleNamespace(commit=fake_commit)

    events = []
    try:
        async for event in agent.run_stream(
            query="删完后告诉我",
            file_ids=[],
            thread_id=None,
            db_session=fake_db,
            user_id=uuid4(),
            tool_executor=executor,
        ):
            events.append(json.loads(event.data))
    finally:
        excel_agent_module.TurnRepository = original_repo

    thread_id = executor.calls[0]["thread_id"]
    assert executor.calls[1]["thread_id"] == thread_id
    assert any(event.get("thread_id") == thread_id for event in events)
    assert any(event.get("output") == "处理完成" for event in events)


@pytest.mark.anyio
async def test_excel_agent_does_not_persist_chat_step_into_turn_steps():
    from app.services.excel_agent import ExcelAgent

    class RepoRecorder:
        def __init__(self):
            self.saved_steps = []

        async def replace_turn_steps(self, turn_id, steps):
            self.saved_steps = steps

    agent = ExcelAgent(SimpleNamespace())
    repo = RepoRecorder()
    records = []

    await agent._record_event_on_turn(
        event=sse({"step": "chat", "status": "done", "output": "最终总结"}),
        records=records,
        repo=repo,
        turn_id=uuid4(),
        tool_name="conversation_response",
    )

    assert repo.saved_steps == []


@pytest.mark.anyio
async def test_excel_agent_persists_tool_call_and_tool_result_records():
    from app.services.excel_agent import ExcelAgent

    class RepoRecorder:
        def __init__(self):
            self.saved_steps = []

        async def replace_turn_steps(self, turn_id, steps):
            self.saved_steps = steps

    agent = ExcelAgent(SimpleNamespace())
    repo = RepoRecorder()
    records = []
    turn_id = uuid4()

    await agent._record_tool_call(
        decision={
            "tool_name": "processing_workflow",
            "tool_args": {"intent": "processing"},
        },
        records=records,
        repo=repo,
        turn_id=turn_id,
    )
    await agent._record_event_on_turn(
        event=sse({"step": "execute", "status": "done", "output": {"success": True}}),
        records=records,
        repo=repo,
        turn_id=turn_id,
        tool_name="processing_workflow",
    )

    assert repo.saved_steps[0]["type"] == "tool_call"
    assert repo.saved_steps[0]["tool_name"] == "processing_workflow"
    assert repo.saved_steps[1]["type"] == "tool_result"
    assert repo.saved_steps[1]["step"] == "execute"


def test_chat_route_streams_workflow_observation_and_final_agent_reply(monkeypatch):
    agent = FakeStreamingExcelAgent(
        [
            {"thread_id": "thread-1", "intent": "processing"},
            {"step": "load", "status": "running"},
            {"step": "execute", "status": "done", "output": {"success": True}},
            {"step": "chat", "status": "done", "output": "已经处理完成，并把结果总结给你。"},
            {"step": "complete", "status": "done"},
        ]
    )

    async def fake_get_excel_agent(db):
        return agent

    async def fake_get_current_user():
        return SimpleNamespace(id=uuid4())

    async def fake_get_db():
        yield object()

    app = _build_app()
    app.dependency_overrides[chat_route.get_current_user] = fake_get_current_user
    app.dependency_overrides[chat_route.get_db] = fake_get_db

    monkeypatch.setattr(chat_route, "get_excel_agent", fake_get_excel_agent)

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/chat",
            json={"query": "删完告诉我结果", "file_ids": [], "thread_id": None},
        ) as response:
            assert response.status_code == 200
            events = _collect_sse_json(response)

    assert agent.calls, "route should delegate streaming to ExcelAgent"
    assert any(event.get("step") == "load" for event in events)
    assert any(event.get("step") == "execute" for event in events)
    assert any(event.get("output") == "已经处理完成，并把结果总结给你。" for event in events)


def test_chat_route_module_no_longer_references_intent_service():
    source = chat_route.__file__
    with open(source, "r", encoding="utf-8") as handle:
        content = handle.read()

    assert "get_intent_service" not in content
    assert "intent_service" not in content


def test_legacy_intent_service_module_removed():
    service_path = Path(chat_route.__file__).resolve().parents[2] / "services" / "intent_service.py"
    assert not service_path.exists()


def test_legacy_intent_classifier_module_removed():
    classifier_path = Path(chat_route.__file__).resolve().parents[2] / "engine" / "intent_classifier.py"
    assert not classifier_path.exists()


def test_legacy_chat_clarify_endpoint_removed():
    app = _build_app()

    with TestClient(app) as client:
        response = client.post(
            "/chat/clarify",
            json={"original_intent_result": {}, "user_response": "继续", "thread_id": None},
        )

    assert response.status_code == 404


def test_legacy_chat_intents_endpoint_removed():
    app = _build_app()

    with TestClient(app) as client:
        response = client.get("/chat/intents")

    assert response.status_code == 404
