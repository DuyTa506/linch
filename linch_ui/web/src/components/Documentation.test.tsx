import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { I18nContext } from "../i18n";
import { en } from "../i18n/en";
import { vi as viDictionary } from "../i18n/vi";
import { Documentation } from "./Documentation";

describe("Documentation", () => {
  it("teaches the real tool, A2A, and routine composition paths", () => {
    render(
      <I18nContext.Provider value={en}>
        <Documentation onBack={() => undefined} />
      </I18nContext.Provider>,
    );

    expect(screen.getByRole("heading", { name: en.docs.toolsTitle })).toBeVisible();
    expect(screen.getByRole("heading", { name: en.docs.a2aTitle })).toBeVisible();
    expect(screen.getByRole("heading", { name: en.docs.routinesTitle })).toBeVisible();
    expect(screen.getByText("spec.runtime.agent.tools")).toBeVisible();
    expect(screen.getByText("workflow.nodes[].dependsOn")).toBeVisible();
    expect(screen.getAllByText(new RegExp(en.docs.actualFlow))).toHaveLength(3);

    const images = screen.getAllByRole("img");
    expect(images).toHaveLength(3);
    for (const image of images) {
      expect(image).toHaveAttribute("src", expect.stringMatching(/\.png$/));
      expect(image).toHaveAttribute("alt");
      expect(image.getAttribute("alt")?.length).toBeGreaterThan(20);
    }
  });

  it("teaches that MCP, memory, skills, and subagents are not tools", () => {
    render(
      <I18nContext.Provider value={en}>
        <Documentation onBack={() => undefined} />
      </I18nContext.Provider>,
    );

    expect(screen.getByRole("heading", { name: en.docs.railsTitle })).toBeVisible();
    expect(screen.getByRole("heading", { name: en.docs.skillsTitle })).toBeVisible();
    expect(screen.getByRole("heading", { name: en.docs.goalTitle })).toBeVisible();
    expect(screen.getByText(en.docs.railsMcpBody)).toBeVisible();
    expect(screen.getByText(en.docs.railsMemoryBody)).toBeVisible();
    expect(screen.getByText(en.docs.goalMechanismsBody)).toBeVisible();
    expect(screen.getByText("spec.capabilities.extensions.mcpServers")).toBeVisible();
  });

  it("plays every recipe clip from a real recording, and never on its own", () => {
    const { container } = render(
      <I18nContext.Provider value={en}>
        <Documentation onBack={() => undefined} />
      </I18nContext.Provider>,
    );

    const videos = container.querySelectorAll("video");
    expect(videos).toHaveLength(6);
    for (const video of videos) {
      expect(video).toHaveAttribute("controls");
      expect(video).toHaveAttribute("poster", expect.stringMatching(/\.png$/));
      expect(video).not.toHaveAttribute("autoplay");
      expect(video.getAttribute("aria-label")?.length ?? 0).toBeGreaterThan(20);
      expect(video.querySelector("source")).toHaveAttribute("type", "video/webm");
      expect(video.querySelector("source")?.getAttribute("src")).toMatch(/\.webm$/);
      // A browser without WebM must still be told what it is missing.
      expect(video.textContent).toContain(en.docs.videoFallback);
      expect(video.querySelector("a[download]")).not.toBeNull();
    }
  });

  it("does not claim a tool must reach the primary agent before a child", () => {
    for (const dictionary of [en, viDictionary]) {
      const prose = JSON.stringify(dictionary.docs);
      expect(prose).not.toMatch(/must be in that list first/);
      expect(prose).not.toMatch(/phải có trong list đó trước/);
      expect(prose).not.toMatch(/phải nằm trong tool pool/);
    }
  });

  it("renders Vietnamese content and returns to the previous Studio surface", () => {
    const onBack = vi.fn();
    render(
      <I18nContext.Provider value={viDictionary}>
        <Documentation onBack={onBack} />
      </I18nContext.Provider>,
    );

    expect(screen.getByRole("heading", { name: viDictionary.docs.title })).toBeVisible();
    fireEvent.click(screen.getAllByRole("button", { name: /quay lại/i })[0]);
    expect(onBack).toHaveBeenCalledOnce();
  });
});
