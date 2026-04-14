import pytest

from app.core.sse import sse
from app.services.agent_tools import ToolExecutor


@pytest.mark.anyio
async def test_execute_for_agent_collects_observation(monkeypatch):
    executor = ToolExecutor()

    async def fake_execute(**kwargs):
        yield sse({"step": "load", "status": "running"})
        yield sse({"step": "complete", "status": "done", "output": {"success": True}})

    monkeypatch.setattr(executor, "execute", fake_execute)

    observation = await executor.execute_for_agent(
        decision={"tool_name": "processing_workflow", "tool_args": {"intent": "processing"}},
        query="删除 Date 列",
        user_id=None,
        thread_id=None,
        db=None,
        file_ids=[],
    )

    assert observation["tool_name"] == "processing_workflow"
    assert observation["status"] == "done"
    assert observation["steps"] == [
        {"step": "load", "status": "running"},
        {"step": "complete", "status": "done"},
    ]
    assert observation["output"] == {"success": True}
