"""讯飞星辰 MaaS 的 OpenAI 兼容 Provider 适配器。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from incidentlens_control_plane.investigation.provider import (
    AgentTurnRequest,
    AgentTurnResult,
    ModelProvider,
    ProviderError,
)
from incidentlens_control_plane.investigation.types import ProviderUsage


@dataclass(frozen=True, slots=True)
class XfyunMaaSConfig:
    """MaaS OpenAI 兼容接口的非秘密连接配置。"""

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 90.0


class XfyunMaaSProvider(ModelProvider):
    """只把受限回合上下文交给 MaaS，绝不执行模型提出的操作。"""

    def __init__(self, config: XfyunMaaSConfig) -> None:
        self._config = config

    async def generate_turn(self, request: AgentTurnRequest) -> AgentTurnResult:
        payload = {
            "model": self._config.model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        request.model_dump(mode="json"), ensure_ascii=False
                    ),
                },
            ],
        }
        response = await asyncio.to_thread(self._post, payload)
        try:
            content = response["choices"][0]["message"]["content"]
            result_payload = json.loads(_strip_fence(content))
            _normalise_optional_fields(result_payload)
            usage = response.get("usage", {})
            result_payload["usage"] = ProviderUsage(
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                output_bytes=len(content.encode("utf-8")),
            ).model_dump()
            return AgentTurnResult.model_validate(result_payload)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            # Keep the diagnostic bounded and free of request/response bodies:
            # callers need the schema reason to distinguish a provider-format
            # issue from a transport error, but raw model text may be sensitive.
            raise ProviderError(
                f"MaaS 返回的结构化调查回合无效：{str(exc)[:500]}", retryable=False
            ) from exc

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self._config.timeout_seconds) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            retryable = exc.code in {408, 429, 500, 502, 503, 504}
            raise ProviderError(
                f"讯飞 MaaS 请求失败（HTTP {exc.code}）", retryable=retryable
            ) from exc
        except (TimeoutError, URLError) as exc:
            raise ProviderError("讯飞 MaaS 连接失败", retryable=True) from exc


def _strip_fence(content: object) -> str:
    if not isinstance(content, str):
        raise ValueError("message.content must be a string")
    text = content.strip()
    if text.startswith("```") and text.endswith("```"):
        return text.split("\n", 1)[1].rsplit("\n", 1)[0]
    return text


def _normalise_optional_fields(payload: object) -> None:
    """只规范 MaaS 常见的空值表示，不补造任何操作或外部事实。"""
    if not isinstance(payload, dict):
        raise ValueError("provider result must be an object")
    if payload.get("child_delegation") == []:
        payload["child_delegation"] = None
    tool_requests = payload.get("tool_requests")
    if isinstance(tool_requests, list):
        for tool_request in tool_requests:
            if (
                isinstance(tool_request, dict)
                and "arguments" not in tool_request
                and isinstance(tool_request.get("parameters"), dict)
            ):
                # OpenAI-style tools are commonly documented with
                # ``parameters``; internally our proposal contract calls this
                # payload ``arguments``.  This is a name-only conversion: the
                # downstream schema/scope validator still validates every key.
                tool_request["arguments"] = tool_request.pop("parameters")
    hypotheses = payload.get("hypotheses")
    if isinstance(hypotheses, list):
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, dict):
                continue
            if "summary" not in hypothesis and isinstance(hypothesis.get("description"), str):
                hypothesis["summary"] = hypothesis.pop("description")
            # Hypothesis identifiers are assigned by the runtime after the
            # proposal is accepted; accepting a model-supplied id would break
            # that ownership boundary.
            hypothesis.pop("hypothesis_id", None)
    stop_signal = payload.get("stop_signal")
    if isinstance(stop_signal, dict) and "summary" not in stop_signal:
        reason = stop_signal.get("stop_reason", "unknown")
        stop_signal["summary"] = f"模型请求停止：{reason}"


_SYSTEM_PROMPT = """你是 IncidentLens 的受限事故调查规划器。
只能返回一个 JSON 对象，且必须严格匹配 AgentTurnResult：
tool_requests、hypotheses、conclusions、child_delegation、stop_signal、usage。
不得输出 Markdown、解释、隐藏推理或额外字段。你只能提议请求中 tool_schemas 已允许的工具；
所有 hypothesis/conclusion/child_delegation 的 evidence_ids 必须来自请求 evidence。
可空字段 child_delegation 与 stop_signal 必须为 null，不能是 [] 或 {}。
stop_signal 不为 null 时必须同时有 stop_reason 和 summary。
模型只提出建议，绝不声称已经执行工具。首次回合且请求提供了与症状相关的
只读工具时，优先从 tool_schemas 中选择一个最窄的工具请求来收集证据：例如
已给出授权日志文件时可提出一次 log_query；不得因为 evidence 为空就直接停止。
对于 log_query，若 investigation.allowed_log_paths 非空，必须使用其中一个路径作为
source_kind=file 的 source_ref，且 service_name 必须等于 investigation.service；不要猜测
容器名或 Docker 日志源。
当 evidence 已包含能直接解释 symptom 的可观察日志事实时，不要重复调用同一工具：提出
一条仅引用这些 evidence_ids 的 conclusion，并设置 stop_signal 为 completed。结论的
summary、facts、evidence_ids 必须使用 AgentTurnResult 的字段名。
只有没有任何合法、相关的只读取证工具时，才使用 stop_signal，stop_reason 为
missing_evidence。每次最多提出一个工具请求，并为 tool_call_id 使用简短唯一标识。
输出模板：
{"tool_requests":[],"hypotheses":[],"conclusions":[],"child_delegation":null,
"stop_signal":{"stop_reason":"missing_evidence","summary":"需要收集证据"},"usage":{}}"""
