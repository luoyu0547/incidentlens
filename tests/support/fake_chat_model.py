"""Scripted fake chat model for testing agent graphs.

The ScriptedChatModel returns pre-configured AIMessages in sequence,
allowing deterministic testing of agent behavior without network calls.
"""

from __future__ import annotations

from typing import Any, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


class ScriptedChatModel(BaseChatModel):
    """A fake chat model that returns pre-scripted AIMessages.

    Each call returns the next AIMessage in the ``responses`` list.
    Raises ``AssertionError`` if responses are exhausted.

    Tracks which tools were bound via ``bind_tools`` for assertions.
    """

    responses: list[AIMessage]
    cursor: int = 0
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "incidentlens-scripted-test-model"

    def bind_tools(
        self,
        tools: Sequence[Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> ScriptedChatModel:
        """Record bound tool names and return self."""
        self.bound_tool_names = [
            tool.name if hasattr(tool, "name") else tool["function"]["name"]
            for tool in tools
        ]
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Return the next scripted response."""
        if self.cursor >= len(self.responses):
            raise AssertionError("scripted model exhausted")
        message = self.responses[self.cursor]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=message)])
