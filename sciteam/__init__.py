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
from sciteam.compact import CompactionProfile, WorkingPack, build_working_pack
from sciteam.coordination import CoordinationSpec, SchedulingMode
from sciteam.coordinator import ContractCoordinator, ScriptedCoordinator, TeamCoordinator
from sciteam.evidence import EvidenceGraph
from sciteam.kb import CampaignKB, KBEntry
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
from sciteam.roster_policy import load_role_catalog, validate_roster
from sciteam.round_assessor import (
    AssessorPort,
    LlmAssessorPort,
    ScriptedAssessor,
    make_contract_coordinator,
)
from sciteam.runtime import AgentRunResult, Run, RunSpec, RunState, RuntimePort
from sciteam.team_loader import build_registry, load_team_configs, resolve_start_params
from sciteam.tool_catalog import ToolCatalog, ToolSpec
from sciteam.trace import CampaignTrace

__all__ = [
    "AdaptiveCampaignPlanner",
    "AgentRunResult",
    "AssessmentDecision",
    "AssessorPort",
    "BudgetClock",
    "build_assessment_facts",
    "ClaimSpan",
    "PlanReviser",
    "ComputeJobResult",
    "ComputeJobSpec",
    "ComputePort",
    "DatasetManifest",
    "DatasetPort",
    "EvidenceGraph",
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

__version__ = "0.1.0"
