"""SciTeam — standalone multi-agent scientific team runtime.

Self-contained package for research experiments. Intended for independent
open-source release.
"""

from sciteam.adaptive_planner import AdaptiveCampaignPlanner, PlanReviser
from sciteam.aggregation import apply_aggregation
from sciteam.assessment_facts import build_assessment_facts
from sciteam.campaign import (
    BudgetClock,
    Campaign,
    CampaignBudget,
    CampaignPlanner,
    CampaignResult,
    PlannerDecision,
    ScriptedPlaybookPlanner,
)
from sciteam.capabilities import (
    PreflightResult,
    RuntimeCapabilities,
    parse_requires_capabilities,
    preflight_mission,
)
from sciteam.checkpoint import ResumeState, read_resume_state, record_mission_decision
from sciteam.compact import CompactionProfile, WorkingPack, build_working_pack
from sciteam.coordination import CoordinationSpec, SchedulingMode
from sciteam.coordinator import ContractCoordinator, ScriptedCoordinator, TeamCoordinator
from sciteam.event_log import (
    CORE_KINDS,
    GENESIS_HASH,
    LEDGER_FILENAME,
    ChainVerification,
    CostLine,
    EventLog,
    InstitutionEvent,
    LedgerError,
    LedgerState,
    cost_report,
    read_events,
    replay,
    verify_chain,
)
from sciteam.evidence import EvidenceGraph
from sciteam.kb import CampaignKB, KBEntry
from sciteam.ledger_view import export_html_view, render_html
from sciteam.llm import LlmClient, LlmConfig, LlmError, LlmResponse, LlmUsage
from sciteam.llm_worker import LlmWorkerRuntime, PromptAssets
from sciteam.mission import (
    ContractValidator,
    MissionBudget,
    MissionOutcome,
    MissionRunner,
    MissionSpec,
    MissionSpecError,
)
from sciteam.models import (
    AssessmentDecision,
    RoundAssessment,
    RoundPlan,
    RoundTask,
    TeamRun,
    TeamRunState,
)
from sciteam.mutation import (
    OPERATORS,
    MutationOperator,
    MutationReport,
    MutationResult,
    mutate,
    mutate_file,
)
from sciteam.orchestrator import CreateSpec, DriveResult, TeamOrchestrator
from sciteam.plugin_api import (
    PLUGIN_API_VERSION,
    HookResult,
    PluginAPI,
    PluginError,
    PluginManifest,
    PluginRegistry,
    load_all_plugins,
)
from sciteam.ports import (
    ClaimSpan,
    ComputeJobResult,
    ComputeJobSpec,
    ComputePort,
    DatasetManifest,
    DatasetPort,
    JobState,
    LiteraturePort,
    Reference,
    SearchQuery,
    SearchResult,
    VerdictRecord,
    VerifierPort,
)
from sciteam.provenance import compare, environment_fingerprint
from sciteam.resilience import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
    ResiliencePolicy,
    ResilientRuntimePort,
    RetryConfig,
    TimeoutConfig,
)
from sciteam.roster_policy import load_role_catalog, validate_roster
from sciteam.round_assessor import (
    AssessorPort,
    LlmAssessorPort,
    ScriptedAssessor,
    make_contract_coordinator,
)
from sciteam.runtime import AgentRunResult, Run, RunSpec, RunState, RuntimePort
from sciteam.scaffold import list_templates, scaffold_project
from sciteam.team_loader import build_registry, load_team_configs, resolve_start_params
from sciteam.tool_catalog import ToolCatalog, ToolSpec
from sciteam.trace import CampaignTrace

__all__ = [
    "AdaptiveCampaignPlanner",
    "AgentRunResult",
    "AssessmentDecision",
    "AssessorPort",
    "BudgetClock",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitState",
    "build_assessment_facts",
    "ClaimSpan",
    "PlanReviser",
    "ComputeJobResult",
    "ComputeJobSpec",
    "ComputePort",
    "CORE_KINDS",
    "ResumeState",
    "read_resume_state",
    "record_mission_decision",
    "OPERATORS",
    "MutationOperator",
    "MutationReport",
    "MutationResult",
    "mutate",
    "mutate_file",
    "compare",
    "environment_fingerprint",
    "GENESIS_HASH",
    "LEDGER_FILENAME",
    "ChainVerification",
    "CostLine",
    "DatasetManifest",
    "DatasetPort",
    "EventLog",
    "EvidenceGraph",
    "InstitutionEvent",
    "LedgerError",
    "LedgerState",
    "cost_report",
    "read_events",
    "replay",
    "verify_chain",
    "export_html_view",
    "render_html",
    "list_templates",
    "scaffold_project",
    "JobState",
    "LiteraturePort",
    "Reference",
    "SearchQuery",
    "SearchResult",
    "ToolCatalog",
    "ToolSpec",
    "VerdictRecord",
    "VerifierPort",
    "Campaign",
    "CampaignBudget",
    "CampaignKB",
    "CampaignPlanner",
    "CampaignResult",
    "CampaignTrace",
    "CompactionProfile",
    "ContractCoordinator",
    "ContractValidator",
    "PreflightResult",
    "RuntimeCapabilities",
    "parse_requires_capabilities",
    "preflight_mission",
    "load_role_catalog",
    "validate_roster",
    "LlmClient",
    "LlmConfig",
    "LlmError",
    "LlmResponse",
    "LlmUsage",
    "LlmAssessorPort",
    "LlmWorkerRuntime",
    "PromptAssets",
    "ScriptedAssessor",
    "make_contract_coordinator",
    "WorkingPack",
    "build_working_pack",
    "CoordinationSpec",
    "CreateSpec",
    "DriveResult",
    "KBEntry",
    "MissionBudget",
    "MissionOutcome",
    "MissionRunner",
    "MissionSpec",
    "MissionSpecError",
    "PlannerDecision",
    "RoundAssessment",
    "RoundPlan",
    "RoundTask",
    "Run",
    "RunSpec",
    "RunState",
    "RuntimePort",
    "ResiliencePolicy",
    "ResilientRuntimePort",
    "RetryConfig",
    "TimeoutConfig",
    "SchedulingMode",
    "ScriptedCoordinator",
    "ScriptedPlaybookPlanner",
    "TeamCoordinator",
    "TeamOrchestrator",
    "TeamRun",
    "TeamRunState",
    "apply_aggregation",
    "build_registry",
    "load_team_configs",
    "resolve_start_params",
    "PLUGIN_API_VERSION",
    "HookResult",
    "PluginAPI",
    "PluginError",
    "PluginManifest",
    "PluginRegistry",
    "load_all_plugins",
]

__version__ = "0.3.0.dev0"
