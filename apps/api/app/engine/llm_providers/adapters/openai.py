"""OpenAI / OpenAI 兼容 Provider"""

import json
from typing import Generator, Optional

from openai import OpenAI

from app.engine.llm_providers.base import LLMProvider
from app.engine.llm_providers.types import LLMRequest, LLMResponse, LLMStreamChunk


class OpenAIProvider(LLMProvider):
    """OpenAI / OpenAI 兼容适配器"""

    def __init__(self, api_key: str, base_url: Optional[str] = None):
        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)

    def complete(self, request: LLMRequest) -> LLMResponse:
        extra_params = request.extra_params or {}
        extra_body = extra_params.get("extra_body") or {"enable_thinking": False}
        extra_headers = extra_params.get("extra_headers")

        params = {
            "model": request.model_id,
            "messages": request.messages,
            "temperature": request.temperature,
            "extra_body": extra_body,
        }
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.response_format:
            params["response_format"] = request.response_format
        if request.tools:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if extra_headers:
            params["extra_headers"] = extra_headers

        response = self.client.chat.completions.create(**params)
        message = response.choices[0].message
        content = message.content or ""
        tool_calls = None
        if getattr(message, "tool_calls", None):
            tool_calls = []
            for tool_call in message.tool_calls:
                arguments = tool_call.function.arguments
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    pass
                tool_calls.append(
                    {
                        "id": getattr(tool_call, "id", None),
                        "name": tool_call.function.name,
                        "arguments": arguments,
                    }
                )
        return LLMResponse(
            content=content.strip(),
            tool_calls=tool_calls,
            raw=response,
            usage=getattr(response, "usage", None),
        )

    def stream(self, request: LLMRequest) -> Generator[LLMStreamChunk, None, None]:
        extra_params = request.extra_params or {}
        extra_body = extra_params.get("extra_body") or {"enable_thinking": False}
        extra_headers = extra_params.get("extra_headers")

        params = {
            "model": request.model_id,
            "messages": request.messages,
            "temperature": request.temperature,
            "stream": True,
            "extra_body": extra_body,
        }
        if request.max_tokens is not None:
            params["max_tokens"] = request.max_tokens
        if request.response_format:
            params["response_format"] = request.response_format
        if request.tools:
            params["tools"] = request.tools
        if request.tool_choice is not None:
            params["tool_choice"] = request.tool_choice
        if extra_headers:
            params["extra_headers"] = extra_headers

        response = self.client.chat.completions.create(**params)

        full_content = ""
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                delta = chunk.choices[0].delta.content
                full_content += delta
                yield LLMStreamChunk(delta=delta, full_content=full_content, raw=chunk)
