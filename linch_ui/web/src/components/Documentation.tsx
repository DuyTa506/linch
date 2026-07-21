import agentToolsImage from "../assets/documentation/agent-tools.png";
import goalTriggerPoster from "../assets/documentation/agent-goal-trigger.png";
import goalTriggerClip from "../assets/documentation/agent-goal-trigger.webm";
import mcpMemoryPoster from "../assets/documentation/capability-mcp-memory.png";
import mcpMemoryClip from "../assets/documentation/capability-mcp-memory.webm";
import subagentSkillPoster from "../assets/documentation/capability-subagent-skill.png";
import subagentSkillClip from "../assets/documentation/capability-subagent-skill.webm";
import toolAgentPoster from "../assets/documentation/capability-tool-agent.png";
import toolAgentClip from "../assets/documentation/capability-tool-agent.webm";
import routineScheduleImage from "../assets/documentation/routine-schedule.png";
import workflowA2AImage from "../assets/documentation/workflow-a2a.png";
import multiAgentPoster from "../assets/documentation/workflow-multi-agent.png";
import multiAgentClip from "../assets/documentation/workflow-multi-agent.webm";
import schedulePoster from "../assets/documentation/workflow-schedule.png";
import scheduleClip from "../assets/documentation/workflow-schedule.webm";
import { useT } from "../i18n";

function StepList({ steps }: { steps: string[] }) {
  return (
    <ol className="docs__steps">
      {steps.map((step) => (
        <li key={step}>{step}</li>
      ))}
    </ol>
  );
}

function ActualFlowFigure({
  src,
  alt,
  caption,
}: {
  src: string;
  alt: string;
  caption: string;
}) {
  const t = useT();
  return (
    <figure className="docs__figure">
      <div className="docs__figure-head">
        <b>[{t.docs.actualFlow}]</b>
        <span>Playwright · Linch Studio</span>
      </div>
      <img src={src} alt={alt} loading="lazy" decoding="async" />
      <figcaption>{caption}</figcaption>
    </figure>
  );
}

/**
 * A clip of the real Studio driving one recipe.
 *
 * Never autoplays: the reader decides. The poster is the clip's own last frame,
 * so the figure still says something before it is played, and the fallback keeps
 * the content reachable where WebM is not.
 */
function ActualFlowClip({
  src,
  poster,
  caption,
  label,
}: {
  src: string;
  poster: string;
  caption: string;
  label: string;
}) {
  const t = useT();
  return (
    <figure className="docs__figure">
      <div className="docs__figure-head">
        <b>[{t.docs.recording}]</b>
        <span>Playwright · Linch Studio</span>
      </div>
      <video
        className="docs__video"
        controls
        muted
        playsInline
        preload="metadata"
        poster={poster}
        aria-label={label}
      >
        <source src={src} type="video/webm" />
        <p>
          {t.docs.videoFallback}{" "}
          <a href={src} download>
            {t.docs.videoDownload}
          </a>
        </p>
      </video>
      <figcaption>{caption}</figcaption>
    </figure>
  );
}

export function Documentation({ onBack }: { onBack: () => void }) {
  const t = useT();
  const docs = t.docs;

  return (
    <main className="docs" data-testid="documentation-page">
      <div className="docs__shell">
        <aside className="docs__nav" aria-label={docs.navLabel}>
          <div className="docs__nav-title">{docs.navLabel}</div>
          <a href="#start">01 · {docs.navStart}</a>
          <a href="#agent-tools">02 · {docs.navTools}</a>
          <a href="#multi-agent">03 · {docs.navA2A}</a>
          <a href="#routines">04 · {docs.navRoutines}</a>
          <a href="#rails">05 · {docs.navRails}</a>
          <a href="#skills">06 · {docs.navSkills}</a>
          <a href="#goal">07 · {docs.navGoal}</a>
          <a href="#connections">08 · {docs.navConnections}</a>
          <button className="link docs__back" onClick={onBack}>
            ← {docs.back}
          </button>
        </aside>

        <article className="docs__content">
          <header className="docs__hero" id="start">
            <div className="docs__eyebrow">GETTING STARTED · 10 MIN</div>
            <h1>{docs.title}</h1>
            <p>{docs.intro}</p>
            <div className="docs__boundary">
              <b>[i] {docs.boundaryTitle}</b>
              <span>{docs.boundaryBody}</span>
            </div>
          </header>

          <section className="docs__section" aria-labelledby="mental-model-title">
            <div className="docs__section-number">01</div>
            <div>
              <h2 id="mental-model-title">{docs.mentalTitle}</h2>
              <p>{docs.mentalBody}</p>
            </div>
            <div className="docs__axes">
              {docs.axes.map((axis) => (
                <div className="docs__axis" key={axis.title}>
                  <b>{axis.title}</b>
                  <p>{axis.body}</p>
                </div>
              ))}
            </div>
          </section>

          <section className="docs__section" id="agent-tools" aria-labelledby="agent-tools-title">
            <div className="docs__section-number">02</div>
            <div>
              <div className="docs__recipe">{docs.recipe}</div>
              <h2 id="agent-tools-title">{docs.toolsTitle}</h2>
              <p>{docs.toolsBody}</p>
            </div>
            <StepList steps={docs.toolsSteps} />
            <div className="docs__field-map">
              <span>{docs.writes}</span>
              <code>spec.runtime.agent.preset</code>
              <code>spec.runtime.agent.tools</code>
            </div>
            <ActualFlowFigure
              src={agentToolsImage}
              alt={docs.toolsAlt}
              caption={docs.toolsCaption}
            />
            <ActualFlowClip
              src={toolAgentClip}
              poster={toolAgentPoster}
              caption={docs.toolsClipCaption}
              label={docs.toolsAlt}
            />
            <div className="docs__note">
              <b>{docs.deepTitle}</b>
              <span>{docs.deepBody}</span>
            </div>
          </section>

          <section className="docs__section" id="multi-agent" aria-labelledby="multi-agent-title">
            <div className="docs__section-number">03</div>
            <div>
              <div className="docs__recipe">{docs.recipe}</div>
              <h2 id="multi-agent-title">{docs.a2aTitle}</h2>
              <p>{docs.a2aBody}</p>
            </div>
            <div className="docs__warning">
              <b>[!!] {docs.a2aWarningTitle}</b>
              <span>{docs.a2aWarningBody}</span>
            </div>
            <StepList steps={docs.a2aSteps} />
            <div className="docs__field-map">
              <span>{docs.writes}</span>
              <code>workflow.nodes[].subagent</code>
              <code>workflow.nodes[].dependsOn</code>
            </div>
            <ActualFlowFigure
              src={workflowA2AImage}
              alt={docs.a2aAlt}
              caption={docs.a2aCaption}
            />
            <ActualFlowClip
              src={multiAgentClip}
              poster={multiAgentPoster}
              caption={docs.a2aClipCaption}
              label={docs.a2aAlt}
            />
          </section>

          <section className="docs__section" id="routines" aria-labelledby="routines-title">
            <div className="docs__section-number">04</div>
            <div>
              <div className="docs__recipe">{docs.recipe}</div>
              <h2 id="routines-title">{docs.routinesTitle}</h2>
              <p>{docs.routinesBody}</p>
            </div>
            <div className="docs__direction" aria-label={docs.runtimeDirection}>
              <span>TRIGGER</span>
              <b>→</b>
              <span>ROUTINE</span>
              <b>→</b>
              <span>WORKFLOW</span>
            </div>
            <StepList steps={docs.routinesSteps} />
            <ActualFlowFigure
              src={routineScheduleImage}
              alt={docs.routinesAlt}
              caption={docs.routinesCaption}
            />
            <ActualFlowClip
              src={scheduleClip}
              poster={schedulePoster}
              caption={docs.routinesClipCaption}
              label={docs.routinesAlt}
            />
            <div className="docs__note">
              <b>{docs.hostTitle}</b>
              <span>{docs.hostBody}</span>
            </div>
          </section>

          <section className="docs__section" id="rails" aria-labelledby="rails-title">
            <div className="docs__section-number">05</div>
            <div>
              <div className="docs__recipe">{docs.recipe}</div>
              <h2 id="rails-title">{docs.railsTitle}</h2>
              <p>{docs.railsBody}</p>
            </div>
            <StepList steps={docs.railsSteps} />
            <div className="docs__field-map">
              <span>{docs.writes}</span>
              <code>spec.runtime.provider</code>
              <code>spec.capabilities.memory</code>
              <code>spec.capabilities.extensions.mcpServers</code>
            </div>
            <ActualFlowClip
              src={mcpMemoryClip}
              poster={mcpMemoryPoster}
              caption={docs.railsClipCaption}
              label={docs.railsAlt}
            />
            <div className="docs__warning">
              <b>[!!] {docs.railsMcpTitle}</b>
              <span>{docs.railsMcpBody}</span>
            </div>
            <div className="docs__note">
              <b>{docs.railsMemoryTitle}</b>
              <span>{docs.railsMemoryBody}</span>
            </div>
          </section>

          <section className="docs__section" id="skills" aria-labelledby="skills-title">
            <div className="docs__section-number">06</div>
            <div>
              <div className="docs__recipe">{docs.recipe}</div>
              <h2 id="skills-title">{docs.skillsTitle}</h2>
              <p>{docs.skillsBody}</p>
            </div>
            <StepList steps={docs.skillsSteps} />
            <div className="docs__field-map">
              <span>{docs.writes}</span>
              <code>subagent.tools</code>
              <code>skill.allowedTools</code>
            </div>
            <ActualFlowClip
              src={subagentSkillClip}
              poster={subagentSkillPoster}
              caption={docs.skillsClipCaption}
              label={docs.skillsAlt}
            />
          </section>

          <section className="docs__section" id="goal" aria-labelledby="goal-title">
            <div className="docs__section-number">07</div>
            <div>
              <div className="docs__recipe">{docs.recipe}</div>
              <h2 id="goal-title">{docs.goalTitle}</h2>
              <p>{docs.goalBody}</p>
            </div>
            <StepList steps={docs.goalSteps} />
            <div className="docs__field-map">
              <span>{docs.writes}</span>
              <code>spec.runtime.agent.completion</code>
              <code>routine.charter</code>
              <code>routine.triggers</code>
            </div>
            <ActualFlowClip
              src={goalTriggerClip}
              poster={goalTriggerPoster}
              caption={docs.goalClipCaption}
              label={docs.goalAlt}
            />
            <div className="docs__note">
              <b>{docs.goalMechanismsTitle}</b>
              <span>{docs.goalMechanismsBody}</span>
            </div>
          </section>

          <section className="docs__section" id="connections" aria-labelledby="connections-title">
            <div className="docs__section-number">08</div>
            <div>
              <h2 id="connections-title">{docs.connectionsTitle}</h2>
              <p>{docs.connectionsBody}</p>
            </div>
            <div className="docs__table-wrap">
              <table className="docs__table">
                <thead>
                  <tr>
                    <th>{docs.from}</th>
                    <th>{docs.to}</th>
                    <th>{docs.meaning}</th>
                    <th>{docs.gesture}</th>
                  </tr>
                </thead>
                <tbody>
                  {docs.connections.map((row) => (
                    <tr key={row.join(":")}>
                      {row.map((cell, index) => (
                        <td key={`${index}:${cell}`}>{cell}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="docs__troubleshoot">
              <h3>{docs.troubleTitle}</h3>
              <ul>
                {docs.troubleItems.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          </section>

          <section className="docs__finish">
            <div>
              <div className="docs__eyebrow">READY</div>
              <h2>{docs.finishTitle}</h2>
              <p>{docs.finishBody}</p>
            </div>
            <button className="btn-solid" onClick={onBack}>
              {docs.finishCta} →
            </button>
          </section>
        </article>
      </div>
    </main>
  );
}
