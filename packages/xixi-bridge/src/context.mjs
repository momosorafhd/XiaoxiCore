import { snapshot } from "./coordination.mjs";
import { normalizePromptSections, sectionText } from "./presentation.mjs";
export function normalizeFragments(value = []) {
  if (!Array.isArray(value) || value.length > 100)
    throw new Error(
      "contextFragments must be an array of at most100 fragments",
    );
  const ids = new Set();
  let chars = 0;
  return value.map((f) => {
    if (
      !f ||
      typeof f.id !== "string" ||
      !f.id ||
      ids.has(f.id) ||
      typeof f.source !== "string" ||
      !f.source ||
      !["string", "number"].includes(typeof f.version) ||
      !["system", "run"].includes(f.placement) ||
      typeof f.text !== "string"
    )
      throw new Error("Invalid or duplicate context fragment");
    ids.add(f.id);
    chars += f.text.length;
    if (chars > 200000)
      throw new Error("Context fragments exceed200000 characters");
    return snapshot({
      id: f.id,
      source: f.source,
      version: f.version,
      placement: f.placement,
      text: f.text,
    });
  });
}
export class ContextComposer {
  constructor(bridge) {
    this.bridge = bridge;
  }
  install(scoped, agent, config) {
    scoped.systemPrompt.section({
      name: "xixi:fragments:system",
      order: 50,
      interpolate: false,
      text: () => this.render(agent.id, "system"),
    });
    scoped.systemPrompt.context({
      name: "xixi:fragments:run",
      order: 50,
      interpolate: false,
      text: () => this.render(agent.id, "run"),
    });
  }
  activate(sessionId, message) {
    if (message.source?.kind !== "xixi-input") return;
    const record = this.bridge.store.get("input", message.source.inputId);
    if (!record) return;
    this.bridge.store.set("active-context", sessionId, {
      inputId: record.inputId,
      input: record.input,
      promptSections: normalizePromptSections(
        Object.hasOwn(record.input, "promptSections") ? record.input.promptSections : (this.bridge.store.get("session",sessionId)?.promptSections ?? [])),
      fragments: normalizeFragments(
        Object.hasOwn(record.input, "contextFragments")
          ? record.input.contextFragments
          : (this.bridge.store.get("session", sessionId)?.contextFragments ??
              []),
      ),
    });
  }
  fragments(sessionId) {
    const config = this.bridge.store.get("session", sessionId) ?? {},
      active = this.bridge.store.get("active-context", sessionId);
    const merged = new Map(
      normalizeFragments(
        active ? active.fragments : (config.contextFragments ?? []),
      ).map((f) => [f.id, f]),
    );
    for (const f of this.bridge.skills?.fragments(sessionId) ?? [])
      merged.set(`${f.source}:${f.id}`, f);
    return [...merged.values()];
  }
  render(sessionId, placement) {
    const active = this.bridge.store.get("active-context",sessionId);
    const sections = active?.promptSections ?? this.bridge.store.get("session",sessionId)?.promptSections ?? [];
    const configured = placement === "run" && active?.input?.presentationText === undefined ? sectionText(sections,"run") : "";
    return [configured, this.fragments(sessionId)
      .filter((f) => f.placement === placement && f.text)
      .map((f) => f.text)
      .join("\n\n")].filter(Boolean).join("\n\n");
  }
  inventory(sessionId) {
    return {
      sessionId,
      libraryMode: this.bridge.store.get("session",sessionId)?.libraryMode ?? "legacy-unspecified",
      unavailableByMode: this.bridge.store.get("session",sessionId)?.libraryMode === "deferred" ? ["library","memory-recall","history-read","todo","idle-maintenance","dream"] : [],
      inputId:
        this.bridge.store.get("active-context", sessionId)?.inputId ?? null,
      fragments: this.fragments(sessionId),
      promptSections: this.bridge.store.get("active-context",sessionId)?.promptSections ?? this.bridge.store.get("session",sessionId)?.promptSections ?? [],
      policy: "explicit-provider-only; no automatic Library search",
    };
  }
}
