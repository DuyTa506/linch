import { Handle, Position, type NodeProps, type Node } from "@xyflow/react";

import type { StudioNodeData } from "../model/graph";
import { BadgeCode } from "./Badge";

export type FlowNode = Node<StudioNodeData, "studio">;

/**
 * A canvas card, styled per the design: header (glyph + title + OUT/‖) over a
 * sub row (description + capability badge). Selection inverts the header and
 * adds the hard offset shadow.
 */
export function StudioNodeView({ data, selected }: NodeProps<FlowNode>) {
  const enabled = data.status !== "unsupported";
  const acceptsWorkflowInput = data.kind === "workflow_step" && enabled;
  const emitsWorkflowOutput = acceptsWorkflowInput && !data.isOutput;
  const acceptsAttachment = enabled && data.canAttachIn === true;
  const emitsAttachment = enabled && data.canAttachOut === true;
  // A handle already anchoring an edge must stay visible/measurable even when
  // no *new* attachment is eligible (e.g. a step whose one allowed subagent is
  // already bound) — otherwise the existing edge's endpoint is unmeasurable
  // and the line renders through an unrelated fallback point.
  const showAttachmentIn = acceptsAttachment || (enabled && data.hasAttachmentIn === true);
  const showAttachmentOut = emitsAttachment || (enabled && data.hasAttachmentOut === true);
  const connectable =
    acceptsWorkflowInput || emitsWorkflowOutput || acceptsAttachment || emitsAttachment;
  return (
    <div
      className={`node${selected ? " node--selected" : ""}${
        connectable ? " node--connectable" : ""
      }${data.kind === "subagent" ? " node--subagent" : ""}`}
    >
      <Handle
        id="workflow-in"
        type="target"
        position={Position.Left}
        isConnectable={acceptsWorkflowInput}
        className={`node__handle node__handle--in${
          acceptsWorkflowInput ? "" : " node__handle--disabled"
        }`}
        aria-label={`connect to ${data.title}`}
      />
      <Handle
        id="attachment-in"
        type="target"
        position={Position.Top}
        isConnectable={acceptsAttachment}
        className={`node__handle node__handle--attach-in${
          showAttachmentIn ? "" : " node__handle--disabled"
        }`}
        aria-label={`attach to ${data.title}`}
      />
      <div className="node__head">
        <b style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {data.glyph} {data.title}
        </b>
        <span style={{ display: "flex", gap: 6, alignItems: "center", flex: "none" }}>
          {data.isOutput && <b className="node__out">OUT</b>}
          {data.isParallel && <span style={{ color: "var(--g9)" }}>‖</span>}
        </span>
      </div>
      <div className="node__sub">
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {data.sub}
        </span>
        <BadgeCode status={data.status} />
      </div>
      <Handle
        id="workflow-out"
        type="source"
        position={Position.Right}
        isConnectable={emitsWorkflowOutput}
        className={`node__handle node__handle--out${
          emitsWorkflowOutput ? "" : " node__handle--disabled"
        }`}
        aria-label={`connect from ${data.title}`}
      />
      <Handle
        id="attachment-out"
        type="source"
        position={Position.Bottom}
        isConnectable={emitsAttachment}
        className={`node__handle node__handle--attach-out${
          showAttachmentOut ? "" : " node__handle--disabled"
        }`}
        aria-label={`attach from ${data.title}`}
      />
    </div>
  );
}
