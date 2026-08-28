"""Provider adapter for OpenAI-compatible chat-completions APIs."""

from __future__ import annotations

import asyncio
import html
import json
import re

from incidentlens_control_plane.investigation.model_transport import (
    ModelTransportError,
    OpenAICompatibleConfig,
    OpenAICompatibleTransport,
)
from incidentlens_control_plane.investigation.prompt import (
    PromptContext,
    SystemPromptBuilder,
)
from incidentlens_control_plane.investigation.provider import (
    AgentTurnResult,
    ConversationRequest,
    ModelProvider,
    ProviderError,
    ProviderOutputFormatError,
    ToolSchema,
)
from incidentlens_control_plane.investigation.skills import SkillRegistry
from incidentlens_control_plane.investigation.types import (
    MessageRole,
    ProviderUsage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    TranscriptMessage,
)


class OpenAICompatibleProvider(ModelProvider):
    """Send bounded turns to a provider without granting it execution access."""

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        transport: OpenAICompatibleTransport,
        *,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._skill_registry = skill_registry or SkillRegistry()

    async def generate_turn(self, request: ConversationRequest) -> AgentTurnResult:
        payload = {
            "model": self._config.model,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": _system_prompt(
                        request, skill_registry=self._skill_registry
                    ),
                },
                *_context_attachments(request),
                *(_message_payload(message) for message in request.messages),
            ],
            "tools": [_tool_payload(schema) for schema in request.tool_schemas],
        }
        try:
            response = await asyncio.to_thread(
                self._transport.chat_completions, payload
            )
        except ModelTransportError as exc:
            raise ProviderError(exc.message, retryable=exc.retryable) from exc
        try:
            message = response["choices"][0]["message"]
            result_payload = _result_payload_from_message(message)
            _normalise_optional_fields(result_payload)
            _remove_unknown_top_level_tool_arguments(
                result_payload, request.tool_schemas
            )
            if _needs_initial_registry_check(request, result_payload):
                # Some OpenAI-compatible models answer the strict JSON prompt
                # with an empty object despite receiving function schemas. Keep
                # the run evidence-first by making the mandatory first
                # observation explicit; subsequent turns remain model-driven.
                result_payload["tool_requests"] = [{
                    "tool_call_id": "auto_registry_info",
                    "tool_name": "registry_info",
                    "arguments": {},
                }]
            usage = response.get("usage", {})
            result_payload["usage"] = ProviderUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                output_bytes=len(str(message.get("content") or "").encode("utf-8")),
            ).model_dump()
            return AgentTurnResult.model_validate(result_payload)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Keep the diagnostic bounded and free of request/response bodies:
            # callers need the schema reason to distinguish a provider-format
            # issue from a transport error, but raw model text may be sensitive.
            raise ProviderOutputFormatError(
                f"OpenAI-compatible API 返回的结构化调查回合无效：{str(exc)[:500]}",
            ) from exc


def _needs_initial_registry_check(
    request: ConversationRequest, payload: dict[str, object]
) -> bool:
    """Return whether an empty first turn must gather registry evidence."""
    if (
        payload.get("tool_requests")
        or payload.get("hypotheses")
        or payload.get("conclusions")
        or payload.get("stop_signal")
    ):
        return False
    if not any(schema.tool_name == "registry_info" for schema in request.tool_schemas):
        return False
    saw_remote_request = False
    saw_tool_result = False
    for message in request.messages:
        for block in message.blocks:
            if isinstance(block, ToolResultBlock):
                saw_tool_result = True
            elif isinstance(block, TextBlock) and any(
                word in block.text.lower()
                for word in ("服务器", "远程", "remote", "server", "日志", "log")
            ):
                saw_remote_request = True
    return saw_remote_request and not saw_tool_result

def _message_payload(message: TranscriptMessage) -> dict[str, object]:
    if message.role is MessageRole.ASSISTANT:
        content = _assistant_content(message.blocks)
        return {
            "role": "assistant",
            "content": json.dumps(content, ensure_ascii=False),
        }
    content = _user_content(message.blocks)
    return {"role": "user", "content": json.dumps(content, ensure_ascii=False)}


def _assistant_content(blocks: tuple) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {
                    "type": "tool_use",
                    "id": block.tool_call_id,
                    "name": block.tool_name,
                    "input": block.arguments,
                }
            )
    return content


def _user_content(blocks: tuple) -> list[dict[str, object]]:
    content: list[dict[str, object]] = []
    for block in blocks:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolResultBlock):
            item: dict[str, object] = {
                "type": "tool_result",
                "tool_call_id": block.tool_call_id,
                "content": block.content,
            }
            if block.evidence_ids:
                item["evidence_ids"] = block.evidence_ids
            content.append(item)
    return content


def _context_attachments(request: ConversationRequest) -> tuple[dict[str, str], ...]:
    """Render the bounded checkpoint/investigation context as a leading message.

    The transcript carries the back-and-forth; the checkpoint and investigation
    snapshot still need to be visible every turn so the model can stay in
    bounds.  The child ``task_prompt`` (when present) rides along.
    """
    context: dict[str, object] = {
        "checkpoint": request.checkpoint.model_dump(mode="json"),
        "investigation": request.investigation.model_dump(mode="json"),
    }
    if request.task_prompt is not None:
        context["task_prompt"] = request.task_prompt
    return (
        {
            "role": "user",
            "content": json.dumps(context, ensure_ascii=False),
        },
    )


_PROMPT_BUILDER = SystemPromptBuilder()


def _system_prompt(
    request: ConversationRequest, *, skill_registry: SkillRegistry | None = None
) -> str:
    """Return stable, model-directed agent guidance — never a round schedule.

    The prompt is assembled by ``SystemPromptBuilder`` from the request data:
    the tools actually registered for this run (``request.tool_schemas``), the
    run scope (``request.checkpoint.scope.scope``), whether this is a child run
    (a ``task_prompt`` is present), whether the project memory is available
    (``request.memory_present``) and the registry catalog.  The registry is the
    provider's in-memory ``SkillRegistry``; a default empty registry advertises
    no skills.  The builder caches equal contexts, so repeated turns with the
    same capabilities reuse the same bounded prompt string.

    No round/stage schedule is ever encoded: the model decides how to
    investigate, delegate, compact, repair, verify and stop.  The child
    ``task_prompt`` (when present) rides along in the context attachment and
    tells a child to finish its own narrow task instead of re-solving the whole
    incident.
    """
    registry = skill_registry or SkillRegistry()
    context = PromptContext(
        tool_names=tuple(schema.tool_name for schema in request.tool_schemas),
        scope=request.checkpoint.scope.scope,
        is_child=request.task_prompt is not None,
        memory_present=bool(getattr(request, "memory_present", False)),
        skill_catalog=registry.catalog(),
    )
    return _PROMPT_BUILDER.build(context)


def _tool_payload(schema: ToolSchema) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": schema.tool_name,
            "description": schema.description,
            "parameters": schema.parameters_json_schema,
        },
    }


def _strip_fence(content: object) -> str:
    if not isinstance(content, str):
        raise ValueError("message.content must be a string")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        return text.split("\n", 1)[1].rsplit("\n", 1)[0]
    return text


_DSML_INVOKE_RE = re.compile(
    r'<｜｜DSML｜｜invoke\s+name="(?P<name>[^"]+)">(?P<body>.*?)</｜｜DSML｜｜invoke>',
    re.DOTALL,
)
_DSML_PARAMETER_RE = re.compile(
    r'<｜｜DSML｜｜parameter\s+(?P<attrs>[^>]*)>(?P<value>.*?)</｜｜DSML｜｜parameter>',
    re.DOTALL,
)
_DSML_NAME_RE = re.compile(r'name="(?P<name>[^"]+)"')
_HYPOTHESIS_PROPOSAL_FIELDS = frozenset(
    {"summary", "facts", "inferences", "unknowns", "evidence_ids"}
)


def _result_payload_from_content(content: object) -> dict[str, object]:
    """Decode JSON output plus DeepSeek's appended DSML tool invocation.

    DeepSeek v4-flash may return a valid JSON result followed by a DSML tool
    block when Chat Completions receives both ``response_format`` and tools.
    DSML is not executable: this function only converts the declared name and
    arguments into the existing proposal shape, which is validated downstream.
    """
    text = _strip_fence(content)
    dsml_only = _deepseek_dsml_tool_requests(text)
    if dsml_only is not None:
        return {
            "tool_requests": dsml_only,
            "hypotheses": [],
            "conclusions": [],
            "child_delegation": None,
            "stop_signal": None,
            "usage": {},
        }
    decoder = json.JSONDecoder()
    payload, end = decoder.raw_decode(text)
    # Some otherwise OpenAI-compatible providers wrap a single JSON result in
    # an array when tools are present.  It remains one declarative provider
    # turn, so unwrap only that precise harmless shape; multiple items are
    # never merged or interpreted as extra operations.
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]
    # v4-flash can also serialize several declarative ``tool_use`` objects as
    # the top-level JSON array.  Treat that narrow, all-tool shape like the
    # normal ``tool_requests`` field; do not merge arbitrary result objects.
    if isinstance(payload, list) and payload:
        tool_items = [
            item
            for item in payload
            if isinstance(item, dict)
            and (
                item.get("type") == "tool_use"
                or (
                    isinstance(item.get("name"), str)
                    and any(key in item for key in ("input", "arguments", "parameters"))
                )
            )
        ]
        text_items = [
            item
            for item in payload
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if tool_items and len(tool_items) + len(text_items) == len(payload):
            payload = {
                "tool_requests": tool_items,
                "hypotheses": [],
                "conclusions": [],
                "child_delegation": None,
                "stop_signal": None,
                "usage": {},
            }
    if not isinstance(payload, dict):
        if isinstance(payload, list):
            item_shapes = [
                (
                    sorted(str(key) for key in item)[:6]
                    if isinstance(item, dict)
                    else type(item).__name__
                )
                for item in payload[:4]
            ]
            raise ValueError(
                "provider result must be an object "
                f"(top-level list length={len(payload)}, item_shapes={item_shapes!r})"
            )
        raise ValueError(f"provider result must be an object (got {type(payload).__name__})")
    suffix = text[end:].strip()
    if not suffix:
        return payload
    tool_requests = _deepseek_dsml_tool_requests(suffix)
    if tool_requests is None:
        raise ValueError("unexpected content after provider JSON result")
    if payload.get("tool_requests"):
        raise ValueError("provider returned both JSON and DSML tool requests")
    payload["tool_requests"] = tool_requests
    return payload


def _result_payload_from_message(message: object) -> dict[str, object]:
    """Decode content or standard OpenAI function-call fields from one message."""
    if not isinstance(message, dict):
        raise ValueError("choice.message must be an object")
    standard_calls = message.get("tool_calls")
    content = message.get("content")
    if isinstance(standard_calls, list) and standard_calls:
        if isinstance(content, str) and content.strip():
            payload = _result_payload_from_content(content)
            if payload.get("tool_requests"):
                raise ValueError("provider returned content and standard tool requests")
        elif content in (None, ""):
            payload = {
                "tool_requests": [],
                "hypotheses": [],
                "conclusions": [],
                "child_delegation": None,
                "stop_signal": None,
                "usage": {},
            }
        else:
            raise ValueError("message.content must be text or null")
        payload["tool_requests"] = [_standard_tool_request(item) for item in standard_calls]
        return payload
    return _result_payload_from_content(content)


def _standard_tool_request(item: object) -> dict[str, object]:
    """Convert one OpenAI function call into a proposal, never an execution."""
    if not isinstance(item, dict):
        raise ValueError("standard tool call must be an object")
    function = item.get("function")
    if not isinstance(function, dict):
        raise ValueError("standard tool call is missing function")
    name = function.get("name")
    raw_arguments = function.get("arguments")
    if not isinstance(name, str) or not isinstance(raw_arguments, str):
        raise ValueError("standard tool call must have name and JSON arguments")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise ValueError("standard tool arguments are invalid JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("standard tool arguments must be an object")
    tool_call_id = item.get("id")
    if not isinstance(tool_call_id, str):
        raise ValueError("standard tool call is missing id")
    return {
        "tool_call_id": tool_call_id,
        "tool_name": name,
        "arguments": arguments,
    }


def _deepseek_dsml_tool_requests(suffix: str) -> list[dict[str, object]] | None:
    """Parse DeepSeek's narrow DSML tool block, or reject unrelated suffixes."""
    if not (
        suffix.startswith("<｜｜DSML｜｜tool_calls>")
        and suffix.endswith("</｜｜DSML｜｜tool_calls>")
    ):
        return None
    requests: list[dict[str, object]] = []
    for index, invoke in enumerate(_DSML_INVOKE_RE.finditer(suffix), start=1):
        arguments: dict[str, object] = {}
        for parameter in _DSML_PARAMETER_RE.finditer(invoke.group("body")):
            name_match = _DSML_NAME_RE.search(parameter.group("attrs"))
            if name_match is None:
                raise ValueError("DSML parameter is missing its name")
            arguments[name_match.group("name")] = html.unescape(
                parameter.group("value").strip()
            )
        requests.append(
            {
                "tool_call_id": f"deepseek-dsml-{index}",
                "tool_name": invoke.group("name"),
                "arguments": arguments,
            }
        )
    if not requests:
        raise ValueError("DSML tool block has no invocations")
    return requests


def _remove_unknown_top_level_tool_arguments(
    payload: object, schemas: tuple[ToolSchema, ...]
) -> None:
    """Drop provider-added top-level keys before strict runtime validation.

    This applies only when a tool schema explicitly forbids additional
    properties. Required fields, nested values, scope, policy and approvals
    remain unchanged and are still checked by ``ProviderOutputValidator``.
    """
    if not isinstance(payload, dict):
        return
    requests = payload.get("tool_requests")
    if not isinstance(requests, list):
        return
    by_name = {schema.tool_name: schema.parameters_json_schema for schema in schemas}
    for request in requests:
        if not isinstance(request, dict):
            continue
        schema = by_name.get(request.get("tool_name"))
        arguments = request.get("arguments")
        if (
            not isinstance(schema, dict)
            or schema.get("additionalProperties") is not False
            or not isinstance(schema.get("properties"), dict)
            or not isinstance(arguments, dict)
        ):
            continue
        allowed = schema["properties"].keys()
        request["arguments"] = {
            key: value for key, value in arguments.items() if key in allowed
        }


def _normalise_optional_fields(payload: object) -> None:
    """只规范 model 常见的空值表示，不补造任何操作或外部事实。"""
    if not isinstance(payload, dict):
        raise ValueError("provider result must be an object")
    # DeepSeek may emit one OpenAI-style tool-use object as the whole content
    # instead of putting it under ``tool_requests``.  Wrap exactly that one
    # declarative proposal; the ordinary request validator still owns tool
    # allowlisting, arguments, scope, policy, approval and execution.
    if (
        "tool_requests" not in payload
        and (
            payload.get("type") == "tool_use"
            or (
                isinstance(payload.get("name"), str)
                and any(key in payload for key in ("input", "arguments", "parameters"))
            )
        )
    ):
        tool_request = dict(payload)
        payload.clear()
        payload.update(
            {
                "tool_requests": [tool_request],
                "hypotheses": [],
                "conclusions": [],
                "child_delegation": None,
                "stop_signal": None,
            }
        )
    if payload.get("child_delegation") == []:
        payload["child_delegation"] = None
    tool_requests = payload.get("tool_requests")
    if isinstance(tool_requests, list):
        for tool_request in tool_requests:
            if not isinstance(tool_request, dict):
                continue
            if "arguments" not in tool_request and isinstance(
                tool_request.get("parameters"), dict
            ):
                # OpenAI-style tools are commonly documented with
                # ``parameters``; internally our proposal contract calls this
                # payload ``arguments``.  This is a name-only conversion: the
                # downstream schema/scope validator still validates every key.
                tool_request["arguments"] = tool_request.pop("parameters")
            if "tool_call_id" not in tool_request and isinstance(tool_request.get("id"), str):
                tool_request["tool_call_id"] = tool_request.pop("id")
            if "tool_name" not in tool_request and isinstance(tool_request.get("name"), str):
                tool_request["tool_name"] = tool_request.pop("name")
            if "arguments" not in tool_request and isinstance(tool_request.get("input"), dict):
                tool_request["arguments"] = tool_request.pop("input")
            if tool_request.get("type") == "tool_use":
                tool_request.pop("type")
            arguments = tool_request.get("arguments")
            if tool_request.get("tool_name") == "delegate_child" and isinstance(
                arguments, dict
            ):
                child_scope = arguments.get("scope")
                if isinstance(child_scope, dict) and child_scope.get("scope") == "host":
                    # These identities are invalid for a host-scoped AgentScope.
                    # Some compatible models copy them from the parent service;
                    # dropping only the schema-incompatible optional keys keeps
                    # the requested target and path bounds unchanged.
                    child_scope.pop("service_name", None)
                    child_scope.pop("container_name", None)
            if tool_request.get("tool_name") == "todo_write" and isinstance(arguments, dict):
                todos = arguments.get("todos")
                if isinstance(todos, str):
                    try:
                        decoded_todos = json.loads(todos)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(decoded_todos, list):
                        arguments["todos"] = decoded_todos
    hypotheses = payload.get("hypotheses")
    if isinstance(hypotheses, list):
        hypotheses[:] = [
            hypothesis
            for hypothesis in hypotheses
            if not isinstance(hypothesis, dict)
            or any(
                hypothesis.get(field)
                for field in (
                    "summary",
                    "description",
                    "title",
                    "facts",
                    "inferences",
                    "unknowns",
                )
            )
        ]
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            if "summary" not in hypothesis:
                description = hypothesis.get("description")
                title = hypothesis.get("title")
                if isinstance(description, str):
                    hypothesis["summary"] = description
                elif isinstance(title, str):
                    hypothesis["summary"] = title
            hypothesis.pop("description", None)
            # Some OpenAI-compatible models add a display-only ``title``.
            # The internal proposal contract owns a single summary field.
            hypothesis.pop("title", None)
            # Hypothesis identifiers are assigned by the runtime after the
            # proposal is accepted; accepting a model-supplied id would break
            # that ownership boundary.
            hypothesis.pop("hypothesis_id", None)
            # Provider-facing labels such as ``status`` and ``confidence`` do
            # not affect an operation. Keep only the strict proposal contract
            # before Pydantic validates it.
            for key in set(hypothesis) - _HYPOTHESIS_PROPOSAL_FIELDS:
                hypothesis.pop(key)
    stop_signal = payload.get("stop_signal")
    if isinstance(stop_signal, dict) and "summary" not in stop_signal:
        reason = stop_signal.get("stop_reason", "unknown")
        stop_signal["summary"] = f"模型请求停止：{reason}"
