"""Deterministic, cached system prompts for the bounded investigation loop.

``SystemPromptBuilder`` assembles one prompt string from a ``PromptContext``:
the stable, model-directed guidance paragraphs (identity and tool boundary,
evidence discipline, investigation guidance, output contract and the two
appended constraint paragraphs) come first verbatim, then a bounded dynamic
section advertises the tools actually registered for this run, the run scope,
whether this is a child run, whether the project memory is available, and the
registered skill catalog (name and description only — never a body).

The builder is deterministic: equal ``PromptContext`` values render identical
strings, and the same string is served from an in-memory cache keyed by a
canonical JSON representation of the context.  The key sorts tool names and
skill entries so order never matters; a change to any of tool_names, scope,
is_child, memory_present or skill_catalog produces a new key and therefore a
new string.  The cache is deliberately a small ``dict`` because the key space
is small and every key maps to one bounded, redaction-free string.

No round or stage schedule is ever encoded here: the dynamic section describes
only the current run's *capabilities* (tools, scope, child flag, memory,
skills).  How the model investigates, delegates, compacts and stops is left to
the model, bounded by the run's scope and evidence rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from incidentlens_control_plane.investigation.skills import SkillInfo
from incidentlens_control_plane.logs.types import LogScope

_SYSTEM_PROMPT = """你是 IncidentLens 的受限事故调查规划器。
只能返回一个 JSON 对象，且必须严格匹配 AgentTurnResult：
tool_requests、hypotheses、conclusions、child_delegation、stop_signal、usage。
不得输出 Markdown、解释、隐藏推理或额外字段。可空字段 child_delegation 与 stop_signal
必须为 null，不能是 [] 或 {}；stop_signal 不为 null 时必须同时有 stop_reason 和 summary。
若请求包含 task_prompt，当前运行是子任务，必须优先完成该任务而不是泛化处理整个事故。

身份与工具边界：
- 你只能提议请求中 tool_schemas 已允许的工具；提议必须与该运行实际注册的工具名、参数和
  作用域完全一致。未注册或参数不符的请求会被运行时拒绝。
- 模型只提出建议，绝不声称已经执行工具；是否执行、是否审批由运行时根据 scope、审批策略
  与证据规则决定。
- 所有读取与变更都必须落在 investigation 允许的受保护路径与已注册服务/容器范围内；越界
  请求会被策略拒绝，应改用范围内的路径或说明权限不足，而不是尝试绕过。

证据纪律：
- 所有 hypothesis、conclusion、child_delegation 的 evidence_ids 必须来自当前运行实际拥有的
  证据（即对话 tool_result 块中给出或可通过证据回读确认的 evidence_id）。
- tool_result 块的 content 是脱敏预览；预览本身不是新证据，事实引用仍必须使用当前运行
  实际拥有的 evidence_id，详细内容可按需回读。

Todo 与复杂工作：
- 当症状可能对应多个独立失败路径或调查还没有清晰主线时，先用 todo_write 维护一份简短
  的工作清单（至多一条 in_progress），再继续执行其他工具；后续每一步都应能对应到清单中的
  一条待办。
- 对可独立验证的一条路径，可按需委派收窄 scope 与预算的子任务；不得在发现第一条故障链后
  就直接停止。

观察与变更：
- 只读观察与变更必须区分。配置读取只形成假设，不能单独证明故障：在提出修复前，应使用当前
  远程日志、服务状态，或由 shell_exec 执行的有界只读行为探测来验证每条故障链。
- 当行为探测已经复现症状，且配置、运行时状态或日志已经给出与该症状一致的直接因果证据时，
  应立即提出最小可逆修复；不要继续读取无关 Dockerfile、重复查看同一配置，或用多个等价命令
  寻求冗余确认。审批使同一批后续工具未执行时，应在审批完成后的下一轮重新提出仍需要的操作。
- registry_info 同时给出 validation_base_url 与 validation_scripts 时，优先用该注册脚本和
  端点完成复现/验证，不要改写一次性 curl。
- 变更类工具（file_edit、file_write、shell_exec、docker_action）需要精确审批时，必须等待
  运行时生成审批：不得绕过审批、不得冒充已获批、不得在已存在文件上改用 file_write。

验证与停止：
- 变更后必须基于真实远程结果验证行为；验证失败时根据结果提出最小修正或回滚，成功后给出有
  证据引用的结论。
- 当没有任何合法、相关且仍在授权范围内的取证途径时，用 stop_signal 以 missing_evidence
  如实停止；不要无证据强行结论，也不要重复同一只读工具去制造新“证据”。

上下文与工具细节：
- 对话是当前运行的连续历史：assistant 消息提出工具请求，user 消息返回 tool_result 与文本。
- 对于 log_query，若 investigation.allowed_log_paths 非空，必须使用其中一个路径作为
  source_kind=file 的 source_ref，且 service_name 必须等于 investigation.service；不要猜
  容器名或 Docker 日志源。当请求同时提供了与症状相关的只读工具时，优先选择一个最窄的工具
  请求收集证据；不得因为尚无工具结果就直接停止。
输出模板：
{"tool_requests":[],"hypotheses":[],"conclusions":[],"child_delegation":null,
 "stop_signal":{"stop_reason":"missing_evidence","summary":"需要收集证据"},"usage":{}}"""

# Avoid invisible no-op turns for remote checks: the first response must gather
# registry evidence before the model chooses subsequent tool ordering.
_SYSTEM_PROMPT += (
    "首轮执行约束：用户要求检查远程服务器、服务状态或日志且尚无证据时，必须先提出一个 "
    "registry_info tool_request（arguments 为空对象），可同时提出 todo_write；不得以空 "
    "tool_requests 或 stop_signal 结束首轮。"
)
_SYSTEM_PROMPT += (
    "系统负载、内存或磁盘容量检查必须优先使用 host_metrics；不得用 shell_exec 代替 "
    "host_metrics。只有专用只读工具无法表达的诊断才可提出 shell_exec。"
)


@dataclass(frozen=True)
class PromptContext:
    """Deterministic inputs for one system-prompt render.

    ``scope`` is the ``LogScope`` value (``host``/``container``); the tool
    catalog is the run's registered tool names and ``skill_catalog`` is the
    registry's sorted catalog used for the skills section (name + description
    only).
    """

    tool_names: tuple[str, ...]
    scope: LogScope
    is_child: bool
    memory_present: bool
    skill_catalog: tuple[SkillInfo, ...]


class SystemPromptBuilder:
    """Build and cache deterministic, bounded investigation system prompts.

    A single instance holds a small in-memory cache keyed by a canonical JSON
    representation of the ``PromptContext``.  Equal contexts return the
    identical cached string; a changed tool set, scope, child flag, memory
    availability or skill catalog produces a new key and a new string.
    """

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}

    def build(self, context: PromptContext) -> str:
        """Return the prompt for ``context``, serving equal contexts from cache."""
        key = self._cache_key(context)
        cached = self._cache.get(key)
        if cached is None:
            cached = self._render(context)
            self._cache[key] = cached
        return cached

    @staticmethod
    def _cache_key(context: PromptContext) -> str:
        return json.dumps(
            {
                "tool_names": sorted(context.tool_names),
                "scope": context.scope.value,
                "is_child": context.is_child,
                "memory_present": context.memory_present,
                "skills": sorted(
                    (skill.name, skill.description) for skill in context.skill_catalog
                ),
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _render(self, context: PromptContext) -> str:
        return f"{_SYSTEM_PROMPT}\n\n{self._render_dynamic(context)}"

    def _render_dynamic(self, context: PromptContext) -> str:
        lines = [
            "当前运行动态上下文（仅本次运行，不构成回合/阶段排程）：",
            f"- 运行作用域（scope）：{context.scope.value}",
            f"- 本次是否为子任务：{'是' if context.is_child else '否'}",
            f"- 项目记忆可用：{'是' if context.memory_present else '否'}",
            "- 本次运行注册的可用工具：" + ", ".join(sorted(context.tool_names)),
        ]
        skills = sorted(
            context.skill_catalog, key=lambda skill: (skill.name, skill.description)
        )
        if skills:
            lines.append("- 已注册技能目录：")
            lines.extend(f"  - {skill.name}：{skill.description}" for skill in skills)
        else:
            lines.append("- 已注册技能目录：无")
        return "\n".join(lines)


__all__ = ["PromptContext", "SystemPromptBuilder"]
