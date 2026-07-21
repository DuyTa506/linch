/** English copy. `vi.ts` must define exactly these keys — guarded by i18n.test.ts. */
export const en = {
  brand: "linch-studio",
  homeSub: " :: design projects",
  local: "[local 127.0.0.1]",
  newTemplate: "new template",
  newBlueprint: "+ new blueprint",
  newEmpty: "+ new empty project",
  flowLine:
    "flow: blueprint → validate → preview → export · export is one-way, no overwrite, no deploy",
  colName: "NAME",
  colId: "ID",
  colModel: "MODEL",
  colState: "STATE",
  colExport: "EXPORT",
  colMod: "MOD",
  homeFootLeft: (count: number) => `${count} projects · all local files · secrets = env-var names only`,
  loadingProjects: "loading projects…",
  noProjects: "no projects yet",
  noProjectsBody: "create a blueprint to start designing. everything stays in your local workspace.",
  docs: {
    homeEyebrow: "NEW HERE?",
    homeTitle: "Build your first agent system in 10 minutes",
    homeBody:
      "Follow real Studio flows for tools, deep agents, multi-agent workflows, and schedules.",
    homeCta: "open getting started",
    navLabel: "DOCUMENTATION",
    navStart: "Mental model",
    navTools: "Agent + tools",
    navA2A: "Multi-agent A2A",
    navRoutines: "Schedule a workflow",
    navRails: "Runtime rails",
    navSkills: "Skills & subagents",
    navGoal: "Goal + tick",
    navConnections: "Connection reference",
    back: "back to Studio",
    actualFlow: "ACTUAL FLOW",
    recording: "RECORDING",
    recordedNote: "recorded from the real Studio, not a mockup",
    videoFallback: "Your browser cannot play WebM.",
    videoDownload: "Download the clip",
    title: "From an agent to a scheduled multi-agent workflow",
    intro:
      "This guide uses screenshots captured from the real local Studio. Complete the recipes in order, or jump directly to the pattern you need.",
    boundaryTitle: "Studio designs and exports.",
    boundaryBody:
      "It does not run or deploy the project and does not own schedulers, process lifetime, or secrets.",
    mentalTitle: "Understand the four independent parts",
    mentalBody:
      "Do not turn every setting into a graph edge. Each part answers a different question.",
    axes: [
      {
        title: "Agent loop",
        body: "How the model calls tools until it finishes. Choose standard, deep, or coordinator.",
      },
      {
        title: "Directed workflow",
        body: "Which agent step runs before another. Code owns this replayable DAG.",
      },
      {
        title: "Completion",
        body: "Whether the agent judges its own answer or a verifier can request a retry.",
      },
      {
        title: "Routine",
        body: "How one bounded agent or workflow invocation receives a manual, cron, CI, or webhook trigger.",
      },
    ],
    recipe: "STEP-BY-STEP RECIPE",
    toolsTitle: "Create an agent and attach a tool",
    toolsBody:
      "A declared Tool is only a project capability. The magnetic attachment decides whether the runtime agent can call it.",
    toolsSteps: [
      "Create an Agent blueprint and open the Agent loop scope.",
      "Select the primary agent. In the inspector, choose standard_agent, deep_agent, or coordinator.",
      "Add a Function tool from the palette. It starts as a visible Skeleton/TODO until you implement its body.",
      "Drag the Tool card close to the highlighted runtime port. Release when the ghost edge appears.",
      "Confirm the attachment edge remains after reload, then set the provider in the project configuration rail on the Project map.",
    ],
    writes: "WRITES TO BLUEPRINT",
    toolsAlt:
      "Real Linch Studio Agent loop showing a deep agent with a magnetically attached Tool card",
    toolsCaption:
      "The real Agent loop after changing the preset to deep_agent and attaching a Function tool.",
    deepTitle: "Deep agent is a preset, not another node.",
    deepBody:
      "Its built-in filesystem and task tools remain available. Custom project tools are added through the explicit runtime allowlist.",
    a2aTitle: "Build a multi-agent A2A workflow",
    a2aBody:
      "A Directed workflow owns invocation order. Each agent_call step may be bound to one declared Subagent.",
    a2aWarningTitle: "Do not connect Subagent directly to the runtime agent.",
    a2aWarningBody:
      "They already belong to the same runtime tree, and no Blueprint field represents that edge. Bind the Subagent to a workflow step instead.",
    a2aSteps: [
      "Create or open a Directed workflow scope.",
      "Add a Subagent while this workflow is open. Studio creates both the Subagent and its bound agent_call step.",
      "Add the next Subagent in the same way.",
      "Drag from the first step's control-flow handle to the next step. This writes dependsOn.",
      "Attach a Tool straight to a Subagent or step to narrow that filter; Studio widens the primary allowlist for you, so there is no need to attach it to the primary agent first.",
    ],
    a2aAlt:
      "Real Linch Studio Directed workflow with two Subagents bound to agent-call steps and connected in A2A order",
    a2aCaption:
      "Two real bound agent_call steps. The control-flow edge is explicit A2A order, not runtime membership.",
    routinesTitle: "Schedule the workflow outside its graph",
    routinesBody:
      "Build the workflow first, wrap one bounded invocation in a Routine, then let the host deliver a Trigger.",
    runtimeDirection: "Runtime direction: Trigger to Routine to Workflow",
    routinesSteps: [
      "Open the Directed workflow you want to invoke.",
      "Choose Scheduled workflow from the palette. Studio creates a workflow_run Routine and a UTC cron trigger.",
      "Studio opens the new Routine scope. Inspect the Trigger → Routine → Workflow composition.",
      "Edit the cron expression, timezone, turn ceiling, token or cost budget, and headless permission policy.",
      "Export the project and configure your host scheduler to call one routine invocation.",
    ],
    routinesAlt:
      "Real Linch Studio Routine scope showing a cron Trigger connected to a workflow-run Routine and its workflow target",
    routinesCaption:
      "The Scheduled workflow shortcut creates a host-owned cron wrapper outside the workflow DAG.",
    hostTitle: "The host owns repetition.",
    hostBody:
      "A Routine runs once per delivery. doneWhen reports done for that invocation; it does not cancel cron or keep a daemon alive.",
    toolsClipCaption:
      "Recorded from the real Studio: a Function tool is declared detached, then attached from the inspector.",
    a2aClipCaption:
      "Recorded from the real Studio: two Subagents become bound agent_call steps, ordered by one A2A edge.",
    routinesClipCaption:
      "Recorded from the real Studio: the Scheduled workflow shortcut composes cron → Routine → Workflow.",

    railsTitle: "Wire the provider, memory, and MCP rails",
    railsBody:
      "Provider, memory, and MCP are runtime configuration, not capabilities you can wire on the canvas. They live in the project configuration rail, which the Project map shows whenever no card is selected.",
    railsSteps: [
      "Open the Project map and click empty canvas so nothing is selected. The configuration rail appears.",
      "Choose the provider kind and model, then name the env vars that hold the key and any base URL. No secret is ever written to the Blueprint.",
      "Pick a memory backend and namespace, name its DSN env var, and turn on recall injection.",
      "Add memory search or upsert tools if the agent should call memory itself; recall injection alone is a hook, not a tool call.",
      "Declare MCP servers as JSON. An http server takes a url and tokenEnv; a stdio server takes a local command and args, which you write yourself.",
    ],
    railsMcpTitle: "MCP is a rail, not a Tool.",
    railsMcpBody:
      "An MCP server is a runtime integration that supplies tools and resources. It is not a Tool card and it never creates a graph edge — connecting it to an agent would be a lie about the Blueprint.",
    railsMemoryTitle: "Memory is wiring, not a node.",
    railsMemoryBody:
      "A store, a recall hook, and optional search/upsert tools are separate switches. The extraction hook is a skeleton: it exports a seam and a blocking TODO, not working extraction.",
    railsAlt:
      "Real Linch Studio project configuration rail with an Anthropic provider, a SQLite memory backend, and an HTTP MCP server",
    railsClipCaption:
      "Recorded from the real Studio: the rail saves runtime configuration and reports that no graph edge was created.",

    skillsTitle: "Add skills and subagents without pretending they are tools",
    skillsBody:
      "A Skill is instructions Linch discovers on disk. A Subagent is a runtime member. Neither executes like a Tool, and neither earns an edge to the agent.",
    skillsSteps: [
      "Open the Agent loop scope and declare a Subagent. It joins the runtime tree immediately; no edge is drawn because none exists in the Blueprint.",
      "Declare a Skill. Studio generates a SKILL.md that Linch discovers at runtime.",
      "Attach a Tool to the Subagent to grant it that tool. Studio widens the primary allowlist to keep the filter valid.",
      "Attach a Tool to the Skill to restrict its allowedTools. This edits frontmatter; it does not make the Skill executable.",
      "Bind the Subagent to an agent_call step in a Directed workflow when you want it to actually run.",
    ],
    skillsAlt:
      "Real Linch Studio Agent loop with a Subagent and a Skill, each holding a Tool attachment",
    skillsClipCaption:
      "Recorded from the real Studio: one Tool grants a Subagent and restricts a Skill — two different meanings.",

    goalTitle: "Verify the goal and let a trigger invoke a tick",
    goalBody:
      "Three mechanisms are easy to confuse. They answer different questions and none of them stops a schedule.",
    goalSteps: [
      "Create a Goal-verified blueprint and select the primary agent. Its completion mode is verifier_gated.",
      "Add verifiers. text_contains and json_schema run; a custom_todo blocks export until you implement it.",
      "Add an agent-tick Routine and give it a charter, a prompt, a turn ceiling, and a token budget.",
      "Add a cron Trigger and drag it onto the Routine to bind it. The host still owns delivery.",
    ],
    goalMechanismsTitle: "Three different questions.",
    goalMechanismsBody:
      "The root completion verifier decides whether a final answer is retried or accepted. A routine verify decides whether one invocation is accepted. doneWhen reports status for that invocation. None of them cancels a cron schedule.",
    goalAlt:
      "Real Linch Studio routine scope with a cron Trigger bound to an agent-tick Routine targeting the agent loop",
    goalClipCaption:
      "Recorded from the real Studio: an agent-tick Routine gains a charter and a bound cron Trigger.",

    connectionsTitle: "Know what can connect",
    connectionsBody:
      "Whole-card magnetic gestures create capability attachments. Workflow ordering stays handle-to-handle so moving a card cannot rewrite logic.",
    from: "FROM",
    to: "TO",
    meaning: "MEANING",
    gesture: "GESTURE",
    connections: [
      ["Tool", "Runtime agent", "runtime.agent.tools", "magnetic card"],
      ["Tool", "Subagent", "subagent.tools", "magnetic card"],
      ["Tool", "Agent step", "node.tools", "magnetic card"],
      ["Subagent", "Agent step", "node.subagent", "magnetic card"],
      ["Step", "Step", "dependsOn", "flow handles"],
      ["Trigger", "Routine", "routine.triggers", "magnetic card"],
      ["Routine", "Target", "agent/workflow target", "magnetic card"],
    ],
    troubleTitle: "If cards do not attach",
    troubleItems: [
      "Check the scope: workflow-only nodes do not attach from Project map or Agent loop.",
      "Wait for the target port to glow and the ghost edge to appear before releasing.",
      "A red shake means the backend relation matrix rejects that semantic relationship.",
      "Subagent → runtime agent is intentionally disabled; use a bound workflow step.",
      "Attaching a Tool to a child or step widens an explicit runtime.agent.tools list automatically — you do not have to attach it to the primary agent first.",
    ],
    finishTitle: "Now build the smallest working path",
    finishBody:
      "Start with one Agent, one Tool, two workflow steps, and one manual trigger. Validate generated files before adding more capabilities.",
    finishCta: "return to projects",
  },
  loadFailed: "could not reach the studio server",

  createTitle: "new blueprint",
  createIdLabel: "PROJECT ID",
  createIdHint: "lowercase letters, digits and underscores — becomes the folder name",
  createTitleLabel: "TITLE",
  createTemplateLabel: "TEMPLATE",
  create: "create",
  cancel: "cancel",

  tabDesign: "design",
  tabYaml: "yaml",
  tabDiagnostics: "diagnostics",
  tabFiles: "files",
  tabExport: "export",

  validate: "validate",
  validateWord: "validate",
  export: "export",
  aiAssist: "ai-assist",
  digestTip: "canonical blueprint digest — the compare-and-swap token for saves",
  settings: "settings",
  save: "save",

  saved: "saved",
  savedTip: "all changes written",
  draftSaved: "draft saved",
  draftSavedTip: "semantic-invalid saved as draft — export blocked",
  unsavedBuffer: "unsaved buffer",
  unsavedBufferTip: "structural error — cannot save until parseable",
  saveConflict: "save conflict",
  saveConflictTip: "base digest changed on disk — resolve before saving",
  newUnsaved: "new · unsaved",
  newUnsavedTip: "new empty project",
  saving: "saving…",
  savingTip: "writing to disk",

  aiReady: "ready",
  aiOff: "off",
  aiTipReady: "provider via env",
  aiTipOff: "no provider configured — manual editor fully functional",

  canvas: "canvas",
  search: "/ search nodes…",
  collapse: "collapse",
  expand: "expand",
  paletteLegend1: "[rt] runtime-ready · [td] skeleton/todo",
  paletteLegend2: "[un] unsupported · drag or click to add",
  canvasHint:
    "drag cards near compatible ports: attach · drag step handles: control flow · del: remove",
  magneticReady: "release to attach — this relation will be saved to the Blueprint",
  connectionPersisted: "saved to the Blueprint · the dashed edge remains after reload",
  toolDeclaredHint:
    "Tool declared, but not attached yet. Drag it to the agent or use Attach to primary agent in the inspector.",
  toolAttachedHint:
    "Tool declared. This agent grants every declared tool, so it is already attached — narrow the allowlist in the inspector to change that.",
  subagentDeclaredHint:
    "Subagent declared as a runtime member. Bind it to an agent-call step to create A2A execution.",
  subagentBoundHint:
    "Subagent declared and bound to a new agent-call step. Connect step handles to order the A2A flow.",
  skillDeclaredHint:
    "Skill declared. Linch discovers its SKILL.md; attach Tool cards only to edit allowedTools.",
  scheduledDeclaredHint:
    "Cron trigger → Routine → Workflow declared. Studio starts no scheduler; the host owns invocation.",
  railTitle: "project configuration",
  railBody:
    "Provider, memory, and MCP are runtime wiring. They change the exported project; they are never cards or edges on the canvas.",
  railSaved: "[ok] Saved runtime configuration — no graph edge was created.",
  subagentMemberNote: "[ok] runtime member · bind to a workflow step for execution",
  skillDiscoveredNote: "[ok] generated SKILL.md · Tool attachments restrict allowedTools",
  summaryTools: "attached project tools",
  summaryEveryTool: "(every declared tool)",
  summaryMcp: "MCP servers",
  summaryMemory: "memory backend",
  summarySkills: "skills",
  summarySubagents: "subagents",
  attachToPrimary: "Attach to primary agent",
  attachedToPrimary: "[ok] attached to primary agent",
  attachedByAllowlist: "[ok] attached — this agent grants every declared tool",
  incompatibleConnection: "These cards do not have a supported Blueprint relation.",
  subagentRuntimeHint:
    "This subagent already belongs to the project runtime. Bind it to an agent-call step in a Directed workflow; direct agent-to-agent edges are disabled.",
  emptyTitle: "empty blueprint",
  emptyBody:
    "drag a node from the palette, or ask ai-assist to draft one. everything here maps to deterministic python on export.",
  emptyCta: "✻ draft with ai",
  fit: "fit",
  map: "map",

  basic: "basic",
  advanced: "advanced",
  advNote:
    "advanced fields map to real generated python or an explicit TODO skeleton — nothing decorative.",
  delete: "delete",
  deleteTip: "delete selected node (Del)",
  revealYaml: "reveal in yaml →",
  revealDesign: "reveal in design →",
  noSelection: "no selection",
  noSelectionBody: "select a node to inspect the blueprint fields it generates.",
  projectConfig: "project configuration",
  projectConfigBody:
    "Provider, MCP, and Memory are runtime rails. They configure the agent but do not create control-flow edges.",
  agentCapabilities: "AGENT CAPABILITIES",
  attachToAgent: "attach to primary agent",
  attachedToAgent: "[ok] attached to primary agent",
  runtimeMember: "[ok] runtime member · bind to a workflow step for execution",
  discoveredSkill: "[ok] generated SKILL.md · tools below restrict allowedTools",
  envTip: "env-var name only — value never stored",

  badgeRt: "[rt] runtime-ready",
  badgeTd: "[td] skeleton/todo",
  badgeUn: "[un] unsupported",
  badgeRtTip: "generates working python",
  badgeTdTip: "generates a TODO skeleton",
  badgeUnTip: "not supported by the generator",

  yamlFile: "linch-studio.yaml",
  yamlSynced: "synced with design",
  yamlDraft: "draft · semantic errors",
  yamlBuffer: "buffer ≠ disk",
  commentNote: "(i) comments don't round-trip",
  commentTip: "YAML comments are not preserved when the canvas re-serializes the blueprint",
  yamlOk: "[ok] parsed & valid · synced with design canvas",
  yamlDraftFoot: "[!!] saved as draft — semantic errors present, export blocked",
  yamlErrFoot: "[xx] unparseable — buffer kept, save disabled until fixed · export blocked",
  saveBlueprint: "save",
  revert: "revert",

  diagFoot:
    "[ok] readiness — with 0 errors this panel lists informational readiness items (env-var names, TODO stubs, hosting notes).",
  noDiagnostics: "no diagnostics",
  noDiagnosticsBody: "this blueprint is valid and export-ready.",
  fix: "fix:",

  filesBlockedTitle: "no generated files yet",
  filesBlockedBody:
    "generation is blocked while validation errors exist. resolve them in diagnostics, then files appear here.",
  goDiag: "go to diagnostics →",
  filesFoot:
    "read-only preview — nothing is executed or installed · skeletons raise TODO errors, never fake success · studio never re-imports the exported project",
  provenance: "provenance:",
  generated: "[ok] generated",
  skeleton: "[TODO] skeleton",
  loadingFiles: "generating preview…",

  readiness: "readiness checklist",
  noOverwrite: "NO OVERWRITE.",
  noOverwriteBody:
    "export requires a new or empty target. there is no force, regenerate-in-place, deploy, or run — by design. the exported project becomes the source of truth.",
  cliLabel: "cli equivalent — same one-way export",
  expDir: "→ new/empty directory",
  expZip: "→ deterministic zip",
  expDirBtn: "export to directory",
  expZipBtn: "download zip",
  byteStable: "byte-stable",
  targetLabel: "TARGET DIRECTORY",
  targetHint: "must not exist, or be an empty directory",
  expBlocked: "export is blocked while validation errors exist — resolve them in diagnostics",
  expDeployNote: "deployment/hosting — TODO in DEVELOPMENT.md",
  exportedDir: "exported to directory",
  exportedZip: "zip downloaded",
  exportDoneNote:
    "the exported python project is now the source of truth. studio will never re-open or overwrite it.",
  backExport: "← back to export",
  exporting: "exporting…",

  aiHeadSession: "· session",
  aiNoProvider: "no provider configured",
  aiNoProviderBody:
    "ai authoring is optional and uses your own provider & model. the manual editor stays fully functional without it.",
  aiEnvNote:
    "set these in your shell, then restart studio. studio reads only the variable names — never the key value.",
  aiIntro:
    "describe what you want to build. the agent asks clarifying questions first, then generates a complete candidate blueprint — not a patch — that you accept or reject as a whole.",
  aiManualOnly: "ai cannot introduce these — manual only:",
  aiThinking: "agent is thinking…",
  aiThinkingLabel: "thinking",
  aiThinkingLive: "thinking…",
  aiToolRunning: "running…",
  aiAnswerSend: "send answers",
  aiOtherOption: "other…",
  aiOtherPlaceholder: "type your own answer",
  aiPlanAccept: "build this plan",
  aiPlanEditHint: "or type below to revise the plan",
  aiAccepted: "accepted into the blueprint",
  aiProposalGone: "proposal dismissed",
  aiRules:
    "all-or-nothing: no selective merge, no auto-rebase. ai never saves, runs, or deploys.",
  aiPlaceholder: "e.g. add a researcher and verifier",
  generate: "generate",
  accept: "accept proposal",
  reject: "reject",
  proposal: "proposal",
  staleTitle: "[!!] proposal is stale.",
  staleBody:
    "the blueprint digest changed since generation: you made a manual edit. acceptance is disabled — no auto-rebase.",
  staleCta: "discard & request new proposal",
  invalidNote:
    "this candidate has errors and cannot be accepted. reject it or refine your instruction.",
  semanticDiffHead: "SEMANTIC DIFF — CANDIDATE VS CURRENT",
  candidateDiag: "CANDIDATE DIAGNOSTICS",
  stPending: "[--] awaiting review",
  stInvalid: "[xx] invalid — cannot accept",
  stStale: "[!!] stale — digest changed",

  language: "language",
  theme: "theme",
  light: "☀ light",
  dark: "☾ dark",
  savedLocal: "saved locally · persists on this machine",

  designOnly: "no run/deploy — design only",
  nodes: "nodes",
  edges: "edges",
  output: "output",
  errWord: "err",
  warnWord: "warn",
  exportOk: "export ok",
  exportBlocked: "export blocked",
  loading: "loading…",
  retry: "retry",
};

export type Dictionary = typeof en;
