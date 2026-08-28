"""Local runtime service container and lifecycle."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from incidentlens_control_plane.agent_sessions.service import AgentSessionService
from incidentlens_control_plane.agent_sessions.store import AgentSessionStore
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.auth.service import AuthService, profiles_from_json
from incidentlens_control_plane.changes.backup import EncryptedBackupVault
from incidentlens_control_plane.changes.manager import ChangeManager
from incidentlens_control_plane.changes.store import ChangeSetStore
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.evidence.service import EvidenceService
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.idempotency.service import IdempotencyService
from incidentlens_control_plane.idempotency.store import IdempotencyStore
from incidentlens_control_plane.investigation.context import (
    AgentContextManager,
    ContextBudgetPolicy,
)
from incidentlens_control_plane.investigation.delegation import DelegationValidator
from incidentlens_control_plane.investigation.events import InvestigationEventPublisher
from incidentlens_control_plane.investigation.fake_provider import (
    FakeProvider,
    FakeProviderRegistry,
)
from incidentlens_control_plane.investigation.hooks import (
    HookEventType,
    HookRunner,
    RuntimeHookRecorder,
)
from incidentlens_control_plane.investigation.model_transport import (
    OpenAICompatibleConfig,
    OpenAICompatibleTransport,
)
from incidentlens_control_plane.investigation.openai_compactor import (
    OpenAICompatibleCompactor,
)
from incidentlens_control_plane.investigation.openai_provider import (
    OpenAICompatibleProvider,
)
from incidentlens_control_plane.investigation.orchestrator import AgentOrchestrator
from incidentlens_control_plane.investigation.prompt import (
    PromptContext,
    SystemPromptBuilder,
)
from incidentlens_control_plane.investigation.recovery import RecoveryService
from incidentlens_control_plane.investigation.registry_proposals import (
    RegistryProposalService,
)
from incidentlens_control_plane.investigation.service import InvestigationService
from incidentlens_control_plane.investigation.skills import SkillRegistry
from incidentlens_control_plane.investigation.source_discovery import (
    SourceDiscoveryService,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.tool_executor import ToolExecutor
from incidentlens_control_plane.logs.service import LogService
from incidentlens_control_plane.logs.store import LogStore
from incidentlens_control_plane.logs.subscriptions import LogSubscriptionManager
from incidentlens_control_plane.operations.dispatcher import OperationDispatcher
from incidentlens_control_plane.operations.events import OperationEventPublisher
from incidentlens_control_plane.operations.handlers import (
    build_agent_message_handler,
    build_rollback_handler,
    build_target_test_handler,
)
from incidentlens_control_plane.operations.recovery import OperationRecovery
from incidentlens_control_plane.operations.service import OperationService
from incidentlens_control_plane.operations.store import OperationStore
from incidentlens_control_plane.operations.types import OperationKind
from incidentlens_control_plane.project_memory.openai_adapter import (
    OpenAIProjectMemoryAdapter,
    ProjectMemoryCoordinator,
)
from incidentlens_control_plane.project_memory.service import ProjectMemoryService
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.projections.evidence import EvidenceProjectionService
from incidentlens_control_plane.projections.investigations import (
    InvestigationSummaryProjectionService,
)
from incidentlens_control_plane.projections.issues import IssueProjectionService
from incidentlens_control_plane.projections.overview import OverviewProjectionService
from incidentlens_control_plane.projections.services import ServiceProjectionService
from incidentlens_control_plane.remote_ops.asyncssh_adapter import (
    AsyncSshTransportFactory,
)
from incidentlens_control_plane.remote_ops.gateway import RemoteToolGateway
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.transport import RemoteTransportFactory
from incidentlens_control_plane.targets.service import TargetService
from incidentlens_control_plane.targets.store import TargetStore


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Container for all runtime services."""

    projects: ProjectRegistryStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker
    sessions: SessionManager
    approvals: ApprovalService
    change_store: ChangeSetStore
    backups: EncryptedBackupVault
    changes: ChangeManager
    remote_tools: RemoteToolGateway
    log_store: LogStore
    evidence: EvidenceStore
    logs: LogService
    subscriptions: LogSubscriptionManager
    evidence_service: EvidenceService
    investigation_store: InvestigationStore
    investigations: InvestigationService
    registry_proposals: RegistryProposalService
    source_discovery: SourceDiscoveryService
    fake_provider: FakeProviderRegistry
    context_manager: AgentContextManager
    recovery: RecoveryService
    reports: object  # ReportService — 前向引用避免循环导入
    project_memory_store: ProjectMemoryStore
    project_memory: ProjectMemoryCoordinator
    auth: AuthService
    idempotency: IdempotencyService
    target_store: TargetStore
    target_service: TargetService
    overview_projection: OverviewProjectionService
    service_projection: ServiceProjectionService
    issue_projection: IssueProjectionService
    investigation_summary_projection: InvestigationSummaryProjectionService
    evidence_projection: EvidenceProjectionService
    operation_store: OperationStore
    operation_service: OperationService
    operation_recovery: OperationRecovery
    dispatcher: OperationDispatcher
    agent_session_store: AgentSessionStore
    agent_sessions: AgentSessionService
    settings: RuntimeSettings


def build_runtime(
    settings: RuntimeSettings,
    *,
    transport_factory: RemoteTransportFactory | None = None,
    fake_provider_registry: FakeProviderRegistry | None = None,
    model_transport: OpenAICompatibleTransport | None = None,
) -> RuntimeServices:
    """Build and initialize the local runtime services.

    Creates the data directory, initializes SQLite databases, and runs migrations
    for all stores.  Services are constructed in dependency order: stores and the
    event broker first, then the approval service, session manager, change
    manager, and the remote-tool gateway, then the Phase 4 evidence/provider/tool
    stack, the orchestrator, the investigation service, and finally the recovery
    service (which owns startup recovery and orderly shutdown).
    """
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.data_dir.chmod(0o700)
    database_path = settings.data_dir / "runtime.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    projects = ProjectRegistryStore(connect)
    events = RuntimeEventStore(connect)
    operation_store = OperationStore(connect)
    approval_store = ApprovalStore(connect)
    change_store = ChangeSetStore(connect)
    log_store = LogStore(connect)
    evidence = EvidenceStore(connect)
    investigation_store = InvestigationStore(connect)
    project_memory_store = ProjectMemoryStore(connect)
    idempotency_store = IdempotencyStore(connect)
    target_store = TargetStore(connect)
    agent_session_store = AgentSessionStore(connect)
    projects.migrate()
    events.migrate()
    operation_store.migrate()
    approval_store.migrate()
    change_store.migrate()
    log_store.migrate()
    evidence.migrate()
    investigation_store.migrate()
    project_memory_store.migrate()
    idempotency_store.migrate()
    target_store.migrate()
    agent_session_store.migrate()

    broker = RuntimeEventBroker(queue_size=settings.stream_subscriber_queue_size)
    approvals = ApprovalService(
        approvals=approval_store,
        events=events,
        broker=broker,
    )

    sessions = SessionManager(transport_factory or AsyncSshTransportFactory())
    backups = EncryptedBackupVault(
        settings.data_dir / "vault",
        settings.data_dir / "vault.key",
    )

    # No targets are pre-registered at build time; both services resolve the
    # target from the project record's ``targets`` per request.
    changes = ChangeManager(
        store=change_store,
        vault=backups,
        approvals=approvals,
        events=events,
        broker=broker,
        projects=projects,
        sessions=sessions,
    )
    remote_tools = RemoteToolGateway(
        projects=projects,
        sessions=sessions,
        changes=changes,
        approvals=approvals,
        events=events,
        broker=broker,
    )
    logs = LogService(
        projects=projects,
        store=log_store,
        sessions=sessions,
        evidence=evidence,
    )
    subscriptions = LogSubscriptionManager(
        store=log_store,
        service=logs,
        events=events,
        broker=broker,
        settings=settings,
    )

    # Phase 4 investigation stack: a scripted provider drives the bounded
    # orchestrator until a production model provider is wired in Task 16, and
    # every service shares the same evidence, approval and event services so
    # there is exactly one execution channel and one event stream.
    evidence_service = EvidenceService(evidence, investigations=investigation_store)
    # One fixed, observational hook runner and one validator are shared by the
    # executor and orchestrator; hooks never participate in authorization.
    hooks = HookRunner()
    hook_recorder = RuntimeHookRecorder(
        InvestigationEventPublisher(events, broker)
    )
    for event_type in HookEventType:
        hooks.register(event_type, hook_recorder)
    delegation = DelegationValidator(projects)
    skills = SkillRegistry(root=settings.data_dir / "skills")
    executor = ToolExecutor(
        projects=projects,
        sessions=sessions,
        gateway=remote_tools,
        logs=logs,
        log_store=log_store,
        evidence=evidence_service,
        evidence_store=evidence,
        investigations=investigation_store,
        approvals=approvals,
        hooks=hooks,
        delegation=delegation,
        skills=skills,
    )
    fake_provider = fake_provider_registry or FakeProviderRegistry()
    provider = FakeProvider(fake_provider)
    compactor = None
    transport = model_transport
    if settings.agent_mode == "llm_agent":
        if (
            not settings.llm_api_key
            or not settings.llm_active_model
            or not settings.llm_base_url
        ):
            raise ValueError(
                "llm_agent 模式需要 INCIDENTLENS_LLM_API_KEY、"
                "INCIDENTLENS_LLM_BASE_URL 和 INCIDENTLENS_LLM_ACTIVE_MODEL"
            )
        provider_config = OpenAICompatibleConfig(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_active_model,
        )
        transport = OpenAICompatibleTransport(provider_config)
        provider = OpenAICompatibleProvider(
            provider_config, transport=transport, skill_registry=skills
        )
        compactor = OpenAICompatibleCompactor(provider_config, transport=transport)

    # Project Memory: durable store + deterministic admission service + the
    # shared-transport adapter, coordinated so extraction never blocks the
    # orchestrator and rendering always degrades to the deterministic path.
    project_memory_service = ProjectMemoryService(project_memory_store)
    memory_adapter = None
    if transport is not None:
        memory_config = OpenAICompatibleConfig(
            api_key=getattr(settings, "llm_api_key", None) or "local",
            base_url=getattr(settings, "llm_base_url", None) or "http://local.invalid",
            model=getattr(settings, "llm_active_model", None) or "project-memory",
        )
        memory_adapter = OpenAIProjectMemoryAdapter(
            memory_config, transport=transport, service=project_memory_service
        )
    project_memory = ProjectMemoryCoordinator(
        store=project_memory_store,
        service=project_memory_service,
        adapter=memory_adapter,
        events=events,
        broker=broker,
    )
    prompt_builder = SystemPromptBuilder()

    def render_system_prompt(run, investigation, tool_schemas, memory) -> str:
        """Render the prompt the orchestrator's provider will send this turn.

        This mirrors ``openai_provider._system_prompt`` exactly for the
        orchestrator path: ``is_child`` is True iff the run has a
        ``parent_run_id`` (the delegated task is present iff the run is a
        child), and ``memory_present`` is set from the active-context session
        (compaction) memory the provider will be told about, not the project
        memory store.  The registry catalog is the
        same shared ``SkillRegistry`` injected into the provider, so the budget
        counts the real prompt string without double-counting the checkpoint /
        snapshot attachment.
        """
        return prompt_builder.build(
            PromptContext(
                tool_names=tuple(schema.tool_name for schema in tool_schemas),
                scope=run.scope.scope,
                is_child=run.parent_run_id is not None,
                memory_present=memory is not None,
                skill_catalog=skills.catalog(),
            )
        )

    context_manager = AgentContextManager(
        investigation_store,
        policy=ContextBudgetPolicy(
            context_window=settings.agent_context_window_tokens,
            max_output_tokens=settings.agent_context_max_output_tokens,
            reserve_tokens=settings.agent_context_reserve_tokens,
            tool_result_budget_chars=settings.agent_tool_result_budget_chars,
            micro_compact_after_seconds=settings.agent_micro_compact_after_seconds,
            compact_max_failures=settings.agent_compact_max_failures,
            reactive_keep_recent_groups=settings.agent_reactive_keep_recent_groups,
            semantic_compact_at_fraction=settings.agent_context_semantic_compact_at_fraction,
        ),
        compactor=compactor,
        memory_renderer=project_memory.render_relevant,
        system_prompt_renderer=render_system_prompt,
    )
    orchestrator = AgentOrchestrator(
        store=investigation_store,
        provider=provider,
        executor=executor,
        evidence=evidence_service,
        projects=projects,
        sessions=sessions,
        global_child_limit=settings.max_active_children,
        default_budget=settings.default_run_budget(),
        max_provider_retries=settings.max_provider_retries,
        events=events,
        broker=broker,
        context_manager=context_manager,
        hooks=hooks,
        delegation=delegation,
        memory_collector=project_memory.enqueue,
    )
    source_discovery = SourceDiscoveryService(
        projects=projects,
        gateway=remote_tools,
        sessions=sessions,
        evidence=evidence_service,
        investigations=investigation_store,
    )
    registry_proposals = RegistryProposalService(
        projects=projects,
        investigations=investigation_store,
        approvals=approvals,
        evidence=evidence_service,
        events=events,
        broker=broker,
        gateway=remote_tools,
        sessions=sessions,
    )
    investigation_service = InvestigationService(
        store=investigation_store,
        orchestrator=orchestrator,
        approvals=approvals,
        executor=executor,
        registry_proposals=registry_proposals,
        events=events,
        broker=broker,
        default_investigation_budget=settings.default_investigation_budget(),
        max_active_investigations=settings.max_active_investigations,
    )
    recovery = RecoveryService(
        store=investigation_store,
        investigations=investigation_service,
        orchestrator=orchestrator,
        evidence=evidence_service,
        approvals=approvals,
        shutdown_grace_seconds=settings.shutdown_grace_seconds,
        events=events,
        broker=broker,
    )

    from incidentlens_control_plane.reports.service import ReportService

    report_dir = settings.report_output_dir or (settings.data_dir / "reports")
    reports = ReportService(
        investigations=investigation_store,
        evidence=evidence,
        output_dir=report_dir,
    )

    auth = AuthService(
        profiles=profiles_from_json(settings.auth_profiles_json),
        signing_key=settings.session_signing_key.get_secret_value(),
        session_ttl_seconds=settings.session_ttl_seconds,
        secure_cookies=settings.secure_cookies,
    )

    idempotency = IdempotencyService(idempotency_store)

    target_service = TargetService(
        projects=projects,
        target_store=target_store,
        investigations=investigation_store,
    )
    service_projection = ServiceProjectionService(
        target_service=target_service,
        target_store=target_store,
        projects=projects,
        approvals=approval_store,
        investigations=investigation_store,
        operations=operation_store,
        logs=log_store,
    )
    overview_projection = OverviewProjectionService(
        target_service=target_service,
        target_store=target_store,
        projects=projects,
        approvals=approval_store,
        investigations=investigation_store,
        operations=operation_store,
        logs=log_store,
        evidence=evidence,
    )
    issue_projection = IssueProjectionService(
        target_service=target_service,
        target_store=target_store,
        investigations=investigation_store,
        approvals=approval_store,
        changes=change_store,
        evidence=evidence,
        logs=log_store,
    )
    investigation_summary_projection = InvestigationSummaryProjectionService(
        target_service=target_service,
        target_store=target_store,
        investigations=investigation_store,
        approvals=approval_store,
        changes=change_store,
        evidence=evidence,
        logs=log_store,
        events=events,
    )
    evidence_projection = EvidenceProjectionService(
        target_service=target_service,
        target_store=target_store,
        evidence=evidence,
        logs=log_store,
    )

    operation_service = OperationService(
        store=operation_store,
        publisher=OperationEventPublisher(events, broker),
    )

    agent_sessions = AgentSessionService(
        sessions=agent_session_store,
        operations=operation_service,
        investigations=investigation_service,
        events=events,
        broker=broker,
    )

    # Task 7: classify leftovers after a restart and dispatch queued durable
    # work.  Recovery runs before the dispatcher's first claim (start()), and
    # only queued (never started) dangerous work is ever executed by a worker.
    operation_recovery = OperationRecovery(
        store=operation_store,
        operations=operation_service,
        investigations=investigation_store,
    )
    dispatcher = OperationDispatcher(
        store=operation_store,
        operations=operation_service,
        recovery=operation_recovery,
    )
    dispatcher.register(
        OperationKind.AGENT_MESSAGE,
        build_agent_message_handler(
            sessions=agent_session_store,
            session_service=agent_sessions,
            investigations=investigation_service,
            projects=projects,
            target_store=target_store,
        ),
    )
    dispatcher.register(
        OperationKind.ROLLBACK,
        build_rollback_handler(changes),
    )
    dispatcher.register(
        OperationKind.TARGET_TEST,
        build_target_test_handler(
            target_store=target_store,
            projects=projects,
            sessions=sessions,
        ),
    )

    return RuntimeServices(
        projects=projects,
        events=events,
        broker=broker,
        sessions=sessions,
        approvals=approvals,
        change_store=change_store,
        backups=backups,
        changes=changes,
        remote_tools=remote_tools,
        log_store=log_store,
        evidence=evidence,
        logs=logs,
        subscriptions=subscriptions,
        evidence_service=evidence_service,
        investigation_store=investigation_store,
        investigations=investigation_service,
        registry_proposals=registry_proposals,
        source_discovery=source_discovery,
        fake_provider=fake_provider,
        context_manager=context_manager,
        recovery=recovery,
        reports=reports,
        project_memory_store=project_memory_store,
        project_memory=project_memory,
        auth=auth,
        idempotency=idempotency,
        target_store=target_store,
        target_service=target_service,
        overview_projection=overview_projection,
        service_projection=service_projection,
        issue_projection=issue_projection,
        investigation_summary_projection=investigation_summary_projection,
        evidence_projection=evidence_projection,
        operation_store=operation_store,
        operation_service=operation_service,
        operation_recovery=operation_recovery,
        dispatcher=dispatcher,
        agent_session_store=agent_session_store,
        agent_sessions=agent_sessions,
        settings=settings,
    )
