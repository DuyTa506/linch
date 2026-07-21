import type { components } from "./schema";

type Schemas = components["schemas"];

export type ServiceInfo = Schemas["ServiceInfo"];
export type ProjectSummary = Schemas["ProjectSummary"];
export type ProjectListResponse = Schemas["ProjectListResponse"];
export type Diagnostic = Schemas["Diagnostic"];
export type ValidationResponse = Schemas["ValidationResponse"];
export type LayoutDocument = Schemas["LayoutDocument"];
export type LayoutNode = Schemas["LayoutNode"];
export type LayoutViewport = Schemas["LayoutViewport"];
export type LayoutResponse = Schemas["LayoutResponse"];
export type ExportPreviewResponse = Schemas["ExportPreviewResponse"];
export type GeneratedFilePreview = Schemas["GeneratedFilePreview"];
export type DirectoryExportResponse = Schemas["DirectoryExportResponse"];
export type SemanticDiffEntry = Schemas["SemanticDiffEntry"];
export type CapabilityRecord = Schemas["CapabilityRecordResponse"];
export type CapabilityStatusRecord = Schemas["CapabilityStatusResponse"];
export type CapabilityArea = Schemas["CapabilityAreaResponse"];
export type ErrorBody = Schemas["ErrorBody"];

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export interface BudgetSpec {
  maxTokens?: number | null;
  maxCostUsd?: number | null;
  warnRatio?: number;
}

export interface ProviderSpec {
  kind?:
    | "openai_responses"
    | "openai_chat"
    | "anthropic"
    | "gemini"
    | "llama_cpp"
    | "vllm"
    | "sglang"
    | "custom"
    | null;
  model?: string | null;
  apiKeyEnv?: string | null;
  baseUrlEnv?: string | null;
  projectEnv?: string | null;
  location?: string | null;
  contextWindow?: number | null;
  streaming?: boolean;
  fallbackModels?: string[];
  thinking?: { enabled?: boolean; effort?: string | null; budgetTokens?: number | null };
}

export interface TextContainsVerifier {
  kind: "text_contains";
  id: string;
  text: string;
  feedback?: string | null;
}

export interface JsonSchemaVerifier {
  kind: "json_schema";
  id: string;
  schema: Record<string, JsonValue>;
  feedback?: string | null;
}

export interface CustomTodoVerifier {
  kind: "custom_todo";
  id: string;
  description: string;
}

export type VerifierSpec = TextContainsVerifier | JsonSchemaVerifier | CustomTodoVerifier;

export interface CompletionSpec {
  mode: "agent_judged" | "verifier_gated";
  maxRetries?: number;
  verifiers?: VerifierSpec[];
}

export interface RuntimeAgentSpec {
  preset: "standard_agent" | "deep_agent" | "coordinator";
  instructions?: string;
  /** Omitted/null inherits every declared project tool; a list is an exact root allowlist. */
  tools?: string[] | null;
  maxTurns?: number | null;
  budget?: BudgetSpec;
  completion?: CompletionSpec;
}

export interface RuntimeSpec {
  provider?: ProviderSpec;
  agent?: RuntimeAgentSpec;
}

export interface ToolSpec {
  kind: "function" | "class" | "database";
  id: string;
  displayName: string;
  description: string;
  scope?: "read" | "write" | "exec";
  parallel?: boolean;
  retryable?: boolean;
  timeoutMs?: number | null;
  inputSchema?: Record<string, JsonValue>;
  resources?: { resource: string; mode?: "read" | "write" }[];
  operation?: "read" | "write";
  idempotencyArgument?: string | null;
}

export interface SubagentSpec {
  id: string;
  displayName: string;
  description?: string;
  instructions: string;
  tools?: string[];
}

export interface SkillSpec {
  id: string;
  displayName: string;
  description?: string;
  instructions: string;
  allowedTools?: string[];
}

export interface AgentCallNodeSpec {
  type: "agent_call";
  id: string;
  label: string;
  prompt: string;
  dependsOn?: string[];
  subagent?: string | null;
  phase?: string | null;
  tools?: string[];
}

/** Parsed only so migrated invalid documents can render an honest unsupported card. */
export interface DirectToolNodeSpec {
  type: "direct_tool";
  id: string;
  label: string;
  tool: string;
  dependsOn?: string[];
}

export type WorkflowNodeSpec = AgentCallNodeSpec | DirectToolNodeSpec;

export interface DirectedWorkflowSpec {
  kind: "directed";
  id: string;
  displayName: string;
  description?: string;
  nodes?: WorkflowNodeSpec[];
  output?: string | null;
  maxConcurrency?: number;
}

interface RoutineBase {
  id: string;
  displayName: string;
  triggers?: string[];
  maxTurns?: number | null;
  budget?: BudgetSpec;
  verify?: VerifierSpec | null;
  doneWhen?: VerifierSpec | null;
}

export interface AgentTickRoutineSpec extends RoutineBase {
  kind: "agent_tick";
  charter: string;
  prompt: string;
  target: "runtime_agent";
}

export interface WorkflowRunRoutineSpec extends RoutineBase {
  kind: "workflow_run";
  target: string;
}

export type RoutineSpec = AgentTickRoutineSpec | WorkflowRunRoutineSpec;

export type TriggerSpec =
  | { kind: "manual"; id: string; displayName: string }
  | { kind: "cron"; id: string; displayName: string; cron: string; timezone?: string }
  | {
      kind: "ci";
      id: string;
      displayName: string;
      provider?: "generic" | "github_actions";
    }
  | { kind: "webhook"; id: string; displayName: string; signingSecretEnv?: string | null };

export interface ProjectSpec {
  package: string;
  target?: { python?: string; linch?: string };
  runtime?: RuntimeSpec;
  tools?: ToolSpec[];
  subagents?: SubagentSpec[];
  skills?: SkillSpec[];
  workflows?: DirectedWorkflowSpec[];
  routines?: RoutineSpec[];
  triggers?: TriggerSpec[];
  capabilities?: Record<string, unknown> & {
    memory?: { backend?: string; namespace?: string | null; dsnEnv?: string | null };
  };
}

export interface Blueprint {
  apiVersion: "studio.linch.dev/v1alpha2";
  kind: "LinchProject";
  metadata: { name: string; title: string; description?: string };
  spec: ProjectSpec;
}

type GeneratedProjectDocument = Schemas["ProjectDocument"];
export type ProjectDocument = Omit<GeneratedProjectDocument, "blueprint"> & {
  blueprint: Blueprint;
};

type GeneratedProposalResponse = Schemas["ProposalResponse"];
export type ProposalResponse = Omit<GeneratedProposalResponse, "candidate"> & {
  candidate: Blueprint;
};
export type ProposalListResponse = Omit<Schemas["ProposalListResponse"], "proposals"> & {
  proposals: ProposalResponse[];
};

export type TurnMessage = Schemas["TurnMessage"];
export type TurnQuestion = Schemas["TurnQuestion"];
export type TurnToolCall = Schemas["TurnToolCall"];
export type TurnResponse = Omit<Schemas["TurnResponse"], "proposal"> & {
  proposal?: ProposalResponse | null;
};

/** Ergonomic Support contracts layered on the generated OpenAPI document. */
export type SupportMode = "auto" | "documentation" | "implementation" | "pipeline";
export type ResolvedSupportMode = Exclude<SupportMode, "auto">;
export type SupportCoverage = "documented" | "partial" | "not_found";
export type SupportKind =
  | "answer"
  | "recipe"
  | "mode_confirmation"
  | "questions"
  | "plan"
  | "proposal";

export interface SupportEvidence {
  anchor: string;
  claim: string;
  excerpt?: string | null;
}

export interface SupportRecipeFile {
  path: string;
  language: "python" | "toml" | "yaml" | "text" | "shell";
  content: string;
  provenance: "copied" | "composed" | "skeleton";
  evidence: string[];
  explanation?: string | null;
}

export interface SupportRecipeCommand {
  command: string;
  purpose: string;
}

export interface SupportRecipeTodo {
  description: string;
  blocking: boolean;
  owner: "developer" | "host" | "security";
}

export interface SupportHandoff {
  startHere: string[];
  environment: string[];
  commands: SupportRecipeCommand[];
  todos: SupportRecipeTodo[];
}

export interface SupportImplementationIntent {
  summary: string;
  capabilities: string[];
  schedule?: string | null;
  workflowShape?: string | null;
  constraints: string[];
}

export interface SupportRecipe {
  title: string;
  overview: string;
  intent: SupportImplementationIntent;
  files: SupportRecipeFile[];
  tests: SupportRecipeFile[];
  handoff: SupportHandoff;
}

export interface SupportPipelineIntent {
  summary: string;
  proposedComponents: string[];
  requiresProject: boolean;
}

export interface SupportTurnResponse {
  kind: SupportKind;
  mode: ResolvedSupportMode;
  answer?: string | null;
  recipe?: SupportRecipe | null;
  pipelineIntent?: SupportPipelineIntent | null;
  evidence: SupportEvidence[];
  coverage: SupportCoverage;
  followUp?: string | null;
  questions: TurnQuestion[];
  plan?: string | null;
  planDigest?: string | null;
  proposal?: ProposalResponse | null;
  toolCalls: TurnToolCall[];
}

export interface SupportTurnRequest {
  messages: TurnMessage[];
  requestedMode?: SupportMode;
  pipelineConfirmed?: boolean;
  projectId?: string | null;
  stage?: "chat" | "build";
  approvedPlanDigest?: string | null;
}

export interface RelationMatrixDocument {
  version: { apiVersion: string; revision: number; blueprintApiVersion: string };
  relations: Array<{
    id: string;
    sourceKind: string;
    targetKind: string;
    relation: string;
    decision: "allow" | "deny";
    field: string | null;
    constraints: string[];
    reason: string;
    order: number;
  }>;
}

export type CapabilityCatalog = Schemas["CapabilityCatalogResponse"] & {
  relationMatrix?: RelationMatrixDocument;
};

/** Catalog status ids — the authority for capability badges. */
export type StatusId = CapabilityStatusRecord["id"];

export type Severity = Diagnostic["severity"];

/**
 * Error codes from `linch_studio/server/errors.py`. The UI switches on these
 * rather than on HTTP status, because several codes share a status.
 */
export type StudioErrorCode =
  | "server.error"
  | "project.invalid_id"
  | "project.not_found"
  | "project.already_exists"
  | "project.unsafe_path"
  | "project.corrupt"
  | "blueprint.structural_invalid"
  | "blueprint.stale_digest"
  | "export.validation_blocked"
  | "export.target_unavailable"
  | "layout.invalid"
  | "api.request_invalid"
  | "proposal.not_found"
  | "authoring.unavailable"
  | "authoring.failed"
  | "support.unavailable"
  | "support.failed";
