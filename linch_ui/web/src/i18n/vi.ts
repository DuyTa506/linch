import type { Dictionary } from "./en";

/** Vietnamese copy. Key parity with `en.ts` is enforced by i18n.test.ts. */
export const vi: Dictionary = {
  brand: "linch-studio",
  homeSub: " :: dự án thiết kế",
  local: "[local 127.0.0.1]",
  newTemplate: "mẫu mới",
  newBlueprint: "+ blueprint mới",
  newEmpty: "+ dự án trống mới",
  flowLine:
    "luồng: blueprint → kiểm tra → xem trước → xuất · xuất một chiều, không ghi đè, không triển khai",
  colName: "TÊN",
  colId: "ID",
  colModel: "MODEL",
  colState: "TRẠNG THÁI",
  colExport: "XUẤT",
  colMod: "SỬA",
  homeFootLeft: (count: number) =>
    `${count} dự án · toàn bộ là file local · secret = chỉ tên biến env`,
  loadingProjects: "đang tải dự án…",
  noProjects: "chưa có dự án",
  noProjectsBody: "tạo một blueprint để bắt đầu. mọi thứ nằm trong workspace local của bạn.",
  docs: {
    homeEyebrow: "MỚI DÙNG?",
    homeTitle: "Tạo hệ agent đầu tiên trong 10 phút",
    homeBody:
      "Làm theo luồng Studio thật để gắn tool, chọn deep agent, tạo workflow đa agent và lịch chạy.",
    homeCta: "mở hướng dẫn bắt đầu",
    navLabel: "TÀI LIỆU",
    navStart: "Mô hình tư duy",
    navTools: "Agent + tool",
    navA2A: "Đa agent A2A",
    navRoutines: "Lập lịch workflow",
    navRails: "Rail runtime",
    navSkills: "Skill & subagent",
    navGoal: "Goal + tick",
    navConnections: "Bảng kết nối",
    back: "quay lại Studio",
    actualFlow: "LUỒNG THẬT",
    recording: "BẢN GHI",
    recordedNote: "quay từ Studio thật, không phải mockup",
    videoFallback: "Trình duyệt của bạn không phát được WebM.",
    videoDownload: "Tải clip về",
    title: "Từ một agent tới workflow đa agent chạy theo lịch",
    intro:
      "Hướng dẫn này dùng ảnh chụp từ Studio local thật. Hãy làm lần lượt hoặc nhảy thẳng tới mẫu bạn cần.",
    boundaryTitle: "Studio dùng để thiết kế và xuất mã.",
    boundaryBody:
      "Studio không chạy hay triển khai dự án, cũng không sở hữu scheduler, vòng đời process hoặc secret.",
    mentalTitle: "Hiểu bốn phần độc lập",
    mentalBody:
      "Đừng biến mọi cấu hình thành một cạnh trên graph. Mỗi phần trả lời một câu hỏi khác nhau.",
    axes: [
      {
        title: "Agent loop",
        body: "Model gọi tool thế nào cho tới khi kết thúc. Chọn standard, deep hoặc coordinator.",
      },
      {
        title: "Directed workflow",
        body: "Agent step nào chạy trước step nào. Code sở hữu DAG có thể replay này.",
      },
      {
        title: "Completion",
        body: "Agent tự đánh giá kết quả hay verifier có quyền yêu cầu thử lại.",
      },
      {
        title: "Routine",
        body: "Một invocation có giới hạn nhận trigger manual, cron, CI hoặc webhook như thế nào.",
      },
    ],
    recipe: "CÔNG THỨC TỪNG BƯỚC",
    toolsTitle: "Tạo agent và gắn một tool",
    toolsBody:
      "Khai báo Tool mới chỉ tạo capability trong project. Cạnh gắn magnetic mới quyết định runtime agent có được gọi tool đó hay không.",
    toolsSteps: [
      "Tạo blueprint Agent và mở scope Agent loop.",
      "Chọn primary agent. Trong inspector, chọn standard_agent, deep_agent hoặc coordinator.",
      "Thêm Function tool từ palette. Nó là Skeleton/TODO hữu hình cho tới khi bạn triển khai phần thân.",
      "Kéo card Tool lại gần cổng runtime đang sáng. Thả khi ghost edge xuất hiện.",
      "Kiểm tra cạnh vẫn còn sau khi reload, rồi đặt provider trong rail cấu hình project trên Project map.",
    ],
    writes: "GHI VÀO BLUEPRINT",
    toolsAlt:
      "Agent loop thật trong Linch Studio với deep agent và card Tool được gắn bằng magnetic snap",
    toolsCaption:
      "Agent loop thật sau khi đổi preset sang deep_agent và gắn một Function tool.",
    deepTitle: "Deep agent là preset, không phải một node khác.",
    deepBody:
      "Các filesystem/task tool tích hợp vẫn tồn tại. Tool riêng của project được thêm qua allowlist tường minh của runtime.",
    a2aTitle: "Tạo workflow đa agent A2A",
    a2aBody:
      "Directed workflow sở hữu thứ tự invocation. Mỗi agent_call step có thể bind tối đa một Subagent đã khai báo.",
    a2aWarningTitle: "Không nối Subagent trực tiếp vào runtime agent.",
    a2aWarningBody:
      "Chúng đã cùng thuộc một runtime tree và không có trường Blueprint nào biểu diễn cạnh đó. Hãy bind Subagent vào workflow step.",
    a2aSteps: [
      "Tạo hoặc mở scope Directed workflow.",
      "Thêm Subagent khi workflow đang mở. Studio tạo cả Subagent lẫn agent_call step đã bind.",
      "Thêm Subagent tiếp theo theo cùng cách.",
      "Kéo từ control-flow handle của step đầu sang step kế. Thao tác này ghi dependsOn.",
      "Gắn thẳng Tool vào Subagent hoặc step để thu hẹp bộ lọc; Studio tự nới allowlist của primary agent, nên không cần gắn vào primary agent trước.",
    ],
    a2aAlt:
      "Directed workflow thật trong Linch Studio với hai Subagent bind vào agent-call step và nối theo thứ tự A2A",
    a2aCaption:
      "Hai agent_call step đã bind thật. Cạnh control-flow là thứ tự A2A tường minh, không phải quan hệ membership runtime.",
    routinesTitle: "Lập lịch workflow ở bên ngoài graph",
    routinesBody:
      "Tạo workflow trước, bọc một invocation có giới hạn trong Routine, rồi để host chuyển Trigger vào.",
    runtimeDirection: "Chiều chạy: Trigger tới Routine tới Workflow",
    routinesSteps: [
      "Mở Directed workflow bạn muốn gọi.",
      "Chọn Scheduled workflow từ palette. Studio tạo workflow_run Routine và cron trigger UTC.",
      "Studio mở scope Routine mới. Kiểm tra cấu trúc Trigger → Routine → Workflow.",
      "Sửa cron expression, timezone, trần turn, token hoặc cost budget và permission policy headless.",
      "Xuất project và cấu hình scheduler của host để gọi từng invocation của routine.",
    ],
    routinesAlt:
      "Scope Routine thật trong Linch Studio với cron Trigger nối vào workflow-run Routine và workflow đích",
    routinesCaption:
      "Shortcut Scheduled workflow tạo cron wrapper do host sở hữu, nằm ngoài workflow DAG.",
    hostTitle: "Host sở hữu việc lặp lại.",
    hostBody:
      "Routine chạy một lần cho mỗi delivery. doneWhen chỉ báo xong cho invocation đó; nó không huỷ cron hay giữ daemon sống.",
    toolsClipCaption:
      "Quay từ Studio thật: một Function tool được khai báo ở trạng thái chưa gắn, rồi gắn từ inspector.",
    a2aClipCaption:
      "Quay từ Studio thật: hai Subagent thành agent_call step đã bind, sắp thứ tự bằng một cạnh A2A.",
    routinesClipCaption:
      "Quay từ Studio thật: shortcut Scheduled workflow dựng cron → Routine → Workflow.",

    railsTitle: "Nối rail provider, memory và MCP",
    railsBody:
      "Provider, memory và MCP là cấu hình runtime, không phải capability nối được trên canvas. Chúng nằm trong rail cấu hình project, hiện ra trên Project map khi không chọn card nào.",
    railsSteps: [
      "Mở Project map và bấm vùng canvas trống để bỏ chọn. Rail cấu hình sẽ hiện ra.",
      "Chọn loại provider và model, rồi đặt tên biến env chứa key và base URL. Không secret nào được ghi vào Blueprint.",
      "Chọn memory backend và namespace, đặt tên biến env DSN, rồi bật recall injection.",
      "Thêm memory search/upsert tool nếu muốn agent tự gọi memory; chỉ recall injection thì đó là hook, không phải tool call.",
      "Khai báo MCP server bằng JSON. Server http cần url và tokenEnv; server stdio cần command/args local do bạn tự viết.",
    ],
    railsMcpTitle: "MCP là rail, không phải Tool.",
    railsMcpBody:
      "MCP server là tích hợp runtime cung cấp tool và resource. Nó không phải card Tool và không bao giờ tạo cạnh graph — nối nó vào agent là nói dối về Blueprint.",
    railsMemoryTitle: "Memory là wiring, không phải node.",
    railsMemoryBody:
      "Store, recall hook và search/upsert tool là các công tắc riêng. Extraction hook là skeleton: nó xuất ra một seam và một TODO chặn, không phải extraction chạy được.",
    railsAlt:
      "Rail cấu hình project thật trong Linch Studio với provider Anthropic, memory backend SQLite và một MCP server HTTP",
    railsClipCaption:
      "Quay từ Studio thật: rail lưu cấu hình runtime và báo rõ không có cạnh graph nào được tạo.",

    skillsTitle: "Thêm skill và subagent mà không coi chúng là tool",
    skillsBody:
      "Skill là instruction Linch tự tìm trên đĩa. Subagent là thành viên runtime. Cả hai đều không thực thi như Tool và không có cạnh nối tới agent.",
    skillsSteps: [
      "Mở scope Agent loop và khai báo một Subagent. Nó vào runtime tree ngay; không cạnh nào được vẽ vì Blueprint không có cạnh đó.",
      "Khai báo một Skill. Studio sinh SKILL.md để Linch tự tìm lúc chạy.",
      "Gắn Tool vào Subagent để cấp tool đó. Studio tự nới allowlist của primary agent cho bộ lọc hợp lệ.",
      "Gắn Tool vào Skill để thu hẹp allowedTools. Thao tác này sửa frontmatter, không làm Skill chạy được.",
      "Bind Subagent vào một agent_call step trong Directed workflow khi muốn nó thực sự chạy.",
    ],
    skillsAlt:
      "Agent loop thật trong Linch Studio với một Subagent và một Skill, mỗi bên có một Tool được gắn",
    skillsClipCaption:
      "Quay từ Studio thật: cùng một Tool cấp quyền cho Subagent và thu hẹp Skill — hai ý nghĩa khác nhau.",

    goalTitle: "Kiểm chứng goal và để trigger gọi một tick",
    goalBody:
      "Ba cơ chế rất dễ nhầm. Chúng trả lời các câu hỏi khác nhau và không cơ chế nào dừng được lịch chạy.",
    goalSteps: [
      "Tạo blueprint Goal-verified và chọn primary agent. Completion mode của nó là verifier_gated.",
      "Thêm verifier. text_contains và json_schema chạy được; custom_todo chặn export tới khi bạn triển khai.",
      "Thêm agent-tick Routine rồi đặt charter, prompt, trần turn và token budget.",
      "Thêm cron Trigger và kéo nó vào Routine để bind. Host vẫn sở hữu việc chuyển trigger.",
    ],
    goalMechanismsTitle: "Ba câu hỏi khác nhau.",
    goalMechanismsBody:
      "Completion verifier ở gốc quyết định câu trả lời cuối bị thử lại hay được nhận. Routine verify quyết định một invocation có được nhận hay không. doneWhen báo trạng thái cho invocation đó. Không cơ chế nào huỷ lịch cron.",
    goalAlt:
      "Scope routine thật trong Linch Studio với cron Trigger bind vào agent-tick Routine trỏ tới agent loop",
    goalClipCaption:
      "Quay từ Studio thật: agent-tick Routine nhận charter và một cron Trigger đã bind.",

    connectionsTitle: "Biết chính xác thứ gì nối được",
    connectionsBody:
      "Kéo toàn card theo magnetic để tạo capability attachment. Thứ tự workflow luôn kéo handle-với-handle để di chuyển card không làm đổi logic.",
    from: "TỪ",
    to: "TỚI",
    meaning: "Ý NGHĨA",
    gesture: "THAO TÁC",
    connections: [
      ["Tool", "Runtime agent", "runtime.agent.tools", "card magnetic"],
      ["Tool", "Subagent", "subagent.tools", "card magnetic"],
      ["Tool", "Agent step", "node.tools", "card magnetic"],
      ["Subagent", "Agent step", "node.subagent", "card magnetic"],
      ["Step", "Step", "dependsOn", "flow handle"],
      ["Trigger", "Routine", "routine.triggers", "card magnetic"],
      ["Routine", "Target", "đích agent/workflow", "card magnetic"],
    ],
    troubleTitle: "Nếu các card không hút vào nhau",
    troubleItems: [
      "Kiểm tra scope: node chỉ dành cho workflow không gắn được từ Project map hoặc Agent loop.",
      "Chờ cổng đích phát sáng và ghost edge xuất hiện rồi mới thả.",
      "Rung đỏ nghĩa là relation matrix phía backend từ chối quan hệ ngữ nghĩa đó.",
      "Subagent → runtime agent bị tắt có chủ đích; hãy dùng workflow step đã bind.",
      "Gắn Tool vào child hoặc step sẽ tự nới list runtime.agent.tools tường minh — bạn không cần gắn vào primary agent trước.",
    ],
    finishTitle: "Bây giờ hãy tạo đường chạy nhỏ nhất",
    finishBody:
      "Bắt đầu với một Agent, một Tool, hai workflow step và một manual trigger. Kiểm tra file sinh ra trước khi thêm capability.",
    finishCta: "quay lại danh sách dự án",
  },
  loadFailed: "không kết nối được máy chủ studio",

  createTitle: "blueprint mới",
  createIdLabel: "MÃ DỰ ÁN",
  createIdHint: "chữ thường, số và gạch dưới — sẽ thành tên thư mục",
  createTitleLabel: "TIÊU ĐỀ",
  createTemplateLabel: "MẪU",
  create: "tạo",
  cancel: "huỷ",

  tabDesign: "thiết kế",
  tabYaml: "yaml",
  tabDiagnostics: "chẩn đoán",
  tabFiles: "files",
  tabExport: "xuất",

  validate: "kiểm tra",
  validateWord: "kiểm tra",
  export: "xuất",
  aiAssist: "ai-assist",
  digestTip: "digest chuẩn của blueprint — token so-sánh-và-đổi khi lưu",
  settings: "cài đặt",
  save: "lưu",

  saved: "đã lưu",
  savedTip: "đã ghi mọi thay đổi",
  draftSaved: "lưu nháp",
  draftSavedTip: "lỗi ngữ nghĩa vẫn lưu nháp — chặn xuất",
  unsavedBuffer: "buffer chưa lưu",
  unsavedBufferTip: "lỗi cấu trúc — không lưu được cho tới khi phân tích được",
  saveConflict: "xung đột lưu",
  saveConflictTip: "digest gốc đã đổi trên đĩa — xử lý trước khi lưu",
  newUnsaved: "mới · chưa lưu",
  newUnsavedTip: "dự án trống mới",
  saving: "đang lưu…",
  savingTip: "đang ghi xuống đĩa",

  aiReady: "sẵn sàng",
  aiOff: "tắt",
  aiTipReady: "provider qua biến env",
  aiTipOff: "chưa cấu hình provider — vẫn sửa tay đầy đủ",

  canvas: "canvas",
  search: "/ tìm node…",
  collapse: "thu gọn",
  expand: "mở rộng",
  paletteLegend1: "[rt] chạy được ngay · [td] khung/todo",
  paletteLegend2: "[un] chưa hỗ trợ · kéo hoặc bấm để thêm",
  canvasHint:
    "kéo card gần cổng tương thích: gắn · kéo handle của step: luồng điều khiển · del: xoá",
  magneticReady: "thả để gắn — quan hệ này sẽ được lưu vào Blueprint",
  connectionPersisted: "đã lưu vào Blueprint · cạnh nét đứt vẫn còn sau khi reload",
  toolDeclaredHint:
    "Đã khai báo Tool nhưng chưa gắn. Kéo nó vào agent hoặc dùng Gắn vào primary agent trong inspector.",
  toolAttachedHint:
    "Đã khai báo Tool. Agent này cấp mọi tool đã khai báo nên nó đã được gắn sẵn — thu hẹp allowlist trong inspector nếu muốn đổi.",
  subagentDeclaredHint:
    "Đã khai báo Subagent là thành viên runtime. Bind nó vào agent-call step để tạo thực thi A2A.",
  subagentBoundHint:
    "Đã khai báo Subagent và bind vào một agent-call step mới. Nối handle giữa các step để sắp xếp luồng A2A.",
  skillDeclaredHint:
    "Đã khai báo Skill. Linch tự tìm SKILL.md; chỉ gắn card Tool để sửa allowedTools.",
  scheduledDeclaredHint:
    "Đã khai báo cron Trigger → Routine → Workflow. Studio không khởi động scheduler; host sở hữu việc gọi chạy.",
  railTitle: "cấu hình project",
  railBody:
    "Provider, memory và MCP là runtime wiring. Chúng đổi project được xuất ra; chúng không bao giờ là card hay cạnh trên canvas.",
  railSaved: "[ok] Đã lưu cấu hình runtime — không có cạnh graph nào được tạo.",
  subagentMemberNote: "[ok] thành viên runtime · bind vào một workflow step để thực thi",
  skillDiscoveredNote: "[ok] đã sinh SKILL.md · gắn Tool để thu hẹp allowedTools",
  summaryTools: "tool project đã gắn",
  summaryEveryTool: "(mọi tool đã khai báo)",
  summaryMcp: "MCP server",
  summaryMemory: "backend bộ nhớ",
  summarySkills: "skill",
  summarySubagents: "subagent",
  attachToPrimary: "Gắn vào primary agent",
  attachedToPrimary: "[ok] đã gắn vào primary agent",
  attachedByAllowlist: "[ok] đã gắn — agent này cấp mọi tool đã khai báo",
  incompatibleConnection: "Hai card này không có quan hệ Blueprint được hỗ trợ.",
  subagentRuntimeHint:
    "Subagent này đã thuộc runtime của project. Hãy bind nó vào một agent-call step trong Directed workflow; cạnh agent nối trực tiếp agent bị vô hiệu hóa.",
  emptyTitle: "blueprint trống",
  emptyBody:
    "kéo một node từ palette, hoặc nhờ ai-assist soạn. mọi thứ ở đây ánh xạ sang python xác định khi xuất.",
  emptyCta: "✻ nhờ ai soạn",
  fit: "vừa",
  map: "bản đồ",

  basic: "cơ bản",
  advanced: "nâng cao",
  advNote:
    "trường nâng cao ánh xạ sang python thật hoặc khung TODO tường minh — không có gì trang trí.",
  delete: "xoá",
  deleteTip: "xoá node đang chọn (Del)",
  revealYaml: "xem trong yaml →",
  revealDesign: "xem trong design →",
  noSelection: "chưa chọn",
  noSelectionBody: "chọn một node để xem các trường blueprint mà nó sinh ra.",
  projectConfig: "cấu hình project",
  projectConfigBody:
    "Provider, MCP và Memory là runtime rail. Chúng cấu hình agent nhưng không tạo cạnh control-flow.",
  agentCapabilities: "CAPABILITY CỦA AGENT",
  attachToAgent: "gắn vào primary agent",
  attachedToAgent: "[ok] đã gắn vào primary agent",
  runtimeMember: "[ok] thành viên runtime · bind vào workflow step để thực thi",
  discoveredSkill: "[ok] sinh SKILL.md · tool bên dưới giới hạn allowedTools",
  envTip: "chỉ tên biến env — không bao giờ lưu giá trị",

  badgeRt: "[rt] chạy được ngay",
  badgeTd: "[td] khung/todo",
  badgeUn: "[un] chưa hỗ trợ",
  badgeRtTip: "sinh python chạy được",
  badgeTdTip: "sinh khung TODO",
  badgeUnTip: "generator chưa hỗ trợ",

  yamlFile: "linch-studio.yaml",
  yamlSynced: "đồng bộ với design",
  yamlDraft: "nháp · lỗi ngữ nghĩa",
  yamlBuffer: "buffer ≠ đĩa",
  commentNote: "(i) chú thích không round-trip",
  commentTip: "chú thích YAML không được giữ khi canvas tuần tự hoá lại blueprint",
  yamlOk: "[ok] phân tích được & hợp lệ · đồng bộ với canvas",
  yamlDraftFoot: "[!!] đã lưu nháp — còn lỗi ngữ nghĩa, chặn xuất",
  yamlErrFoot: "[xx] không phân tích được — giữ buffer, khoá lưu tới khi sửa · chặn xuất",
  saveBlueprint: "lưu",
  revert: "hoàn tác",

  diagFoot:
    "[ok] sẵn sàng — khi không còn lỗi, bảng này liệt kê các mục thông tin (tên biến env, khung TODO, ghi chú hosting).",
  noDiagnostics: "không có chẩn đoán",
  noDiagnosticsBody: "blueprint này hợp lệ và sẵn sàng xuất.",
  fix: "sửa:",

  filesBlockedTitle: "chưa có file được sinh",
  filesBlockedBody:
    "việc sinh mã bị chặn khi còn lỗi kiểm tra. xử lý trong chẩn đoán, rồi file sẽ hiện ở đây.",
  goDiag: "tới chẩn đoán →",
  filesFoot:
    "xem trước chỉ đọc — không chạy hay cài gì · khung sinh lỗi TODO, không giả vờ thành công · studio không bao giờ nhập lại dự án đã xuất",
  provenance: "nguồn gốc:",
  generated: "[ok] đã sinh",
  skeleton: "[TODO] khung",
  loadingFiles: "đang tạo bản xem trước…",

  readiness: "danh sách sẵn sàng",
  noOverwrite: "KHÔNG GHI ĐÈ.",
  noOverwriteBody:
    "xuất yêu cầu đích mới hoặc rỗng. không có ép buộc, sinh-lại-tại-chỗ, triển khai hay chạy — theo thiết kế. dự án đã xuất trở thành nguồn chân lý.",
  cliLabel: "lệnh cli tương đương — cùng kiểu xuất một chiều",
  expDir: "→ thư mục mới/rỗng",
  expZip: "→ zip xác định",
  expDirBtn: "xuất ra thư mục",
  expZipBtn: "tải zip",
  byteStable: "ổn định theo byte",
  targetLabel: "THƯ MỤC ĐÍCH",
  targetHint: "phải chưa tồn tại, hoặc là thư mục rỗng",
  expBlocked: "xuất bị chặn khi còn lỗi kiểm tra — xử lý trong chẩn đoán",
  expDeployNote: "triển khai/hosting — TODO trong DEVELOPMENT.md",
  exportedDir: "đã xuất ra thư mục",
  exportedZip: "đã tải zip",
  exportDoneNote:
    "dự án python đã xuất giờ là nguồn chân lý. studio sẽ không bao giờ mở lại hay ghi đè nó.",
  backExport: "← quay lại xuất",
  exporting: "đang xuất…",

  aiHeadSession: "· phiên",
  aiNoProvider: "chưa cấu hình provider",
  aiNoProviderBody:
    "ai authoring là tuỳ chọn và dùng provider & model của bạn. trình sửa tay vẫn đầy đủ khi không có nó.",
  aiEnvNote:
    "đặt các biến này trong shell, rồi khởi động lại studio. studio chỉ đọc tên biến — không bao giờ đọc giá trị khoá.",
  aiIntro:
    "mô tả thứ bạn muốn xây. agent hỏi làm rõ trước, rồi sinh một blueprint ứng viên hoàn chỉnh — không phải patch — bạn nhận hoặc từ chối trọn gói.",
  aiManualOnly: "ai không thể thêm những thứ này — chỉ thủ công:",
  aiThinking: "agent đang suy nghĩ…",
  aiThinkingLabel: "suy nghĩ",
  aiThinkingLive: "đang suy nghĩ…",
  aiToolRunning: "đang chạy…",
  aiAnswerSend: "gửi câu trả lời",
  aiOtherOption: "khác…",
  aiOtherPlaceholder: "nhập câu trả lời của bạn",
  aiPlanAccept: "dựng theo plan này",
  aiPlanEditHint: "hoặc gõ bên dưới để chỉnh plan",
  aiAccepted: "đã nhận vào blueprint",
  aiProposalGone: "đề xuất đã bị loại bỏ",
  aiRules:
    "trọn gói: không gộp chọn lọc, không tự rebase. ai không bao giờ lưu, chạy hay triển khai.",
  aiPlaceholder: "vd: thêm researcher và verifier",
  generate: "tạo",
  accept: "nhận đề xuất",
  reject: "từ chối",
  proposal: "đề xuất",
  staleTitle: "[!!] đề xuất đã cũ.",
  staleBody:
    "digest blueprint đã đổi từ lúc sinh: bạn đã sửa tay. việc nhận bị khoá — không tự rebase.",
  staleCta: "huỷ & xin đề xuất mới",
  invalidNote: "ứng viên này có lỗi và không thể nhận. hãy từ chối hoặc chỉnh lại chỉ dẫn.",
  semanticDiffHead: "DIFF NGỮ NGHĨA — ỨNG VIÊN VS HIỆN TẠI",
  candidateDiag: "CHẨN ĐOÁN ỨNG VIÊN",
  stPending: "[--] chờ duyệt",
  stInvalid: "[xx] không hợp lệ — không thể nhận",
  stStale: "[!!] cũ — digest đã đổi",

  language: "ngôn ngữ",
  theme: "giao diện",
  light: "☀ sáng",
  dark: "☾ tối",
  savedLocal: "lưu cục bộ · giữ trên máy này",

  designOnly: "không chạy/triển khai — chỉ thiết kế",
  nodes: "node",
  edges: "cạnh",
  output: "đầu ra",
  errWord: "lỗi",
  warnWord: "cảnh báo",
  exportOk: "xuất ok",
  exportBlocked: "chặn xuất",
  loading: "đang tải…",
  retry: "thử lại",
};
