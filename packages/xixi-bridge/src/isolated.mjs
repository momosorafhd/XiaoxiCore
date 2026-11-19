import {
  SingleFlight,
  KeyedSerial,
  claim,
  snapshot,
} from "./coordination.mjs";
import { validateLibraryMode, requireAvailableMaintenance } from "./capabilities.mjs";
const terminal = (status) =>
  ["completed", "failed", "stopped", "quarantined"].includes(status);
function validId(id) {
  if (typeof id !== "string" || !/^[A-Za-z0-9_.:-]{1,120}$/.test(id))
    throw new Error("Invalid operation id");
}
export class IsolatedOperations {
  constructor(bridge) {
    this.bridge = bridge;
    this.isolated = new SingleFlight();
    this.requests = new SingleFlight();
    this.serial = new KeyedSerial();
  }
  async runIsolated(config, text) {
    const b = this.bridge;
    config = snapshot(config);
    if (typeof text !== "string")
      throw new Error("Isolated text must be a string");
    const payload = claim(b.store, "isolated-request", config.id, {
      config,
      text,
    });
    return this.isolated.run(config.id, payload, async () => {
      const inputId = `${config.id}:request`,
        existing = b.store.get("input", inputId);
      if (existing) {
        const result = await b.input(config.id, inputId);
        if (terminal(result.status) || result.status === "unknown")
          return result;
      } else {
        if (b.store.get("task-control", config.id)?.stopRequested) return {status:"stopped",text:""};
        await b.session(config);
        if (b.store.get("task-control", config.id)?.stopRequested) return {status:"stopped",text:""};
        const receivedAt =
          b.store.get("isolated-time", config.id) ??
          b.store.set("isolated-time", config.id, new Date().toISOString());
        await b.deliver(config.id, {
          inputId,
          source: "system",
          text,
          presentationText: text,
          promptSections: config.promptSections ?? [],
          receivedAt,
        });
      }
      return b.waitInput(config.id, inputId);
    });
  }
  async maintenance(request) {
    const b = this.bridge;
    request = snapshot(request);
    validateLibraryMode(request.libraryMode);
    requireAvailableMaintenance({...request,kind:request.kind === "dream" ? "dream" : "idle"});
    validId(request.id);
    const attempt = request.attempt ?? 0;
    if (!Number.isSafeInteger(attempt) || attempt < 0 || attempt > 1000000)
      throw new Error("Invalid maintenance attempt");
    if (
      request.previousError !== undefined &&
      (typeof request.previousError !== "string" ||
        request.previousError.length > 10000)
    )
      throw new Error("Invalid previousError");
    if(request.retryFeedbackText !== undefined && (typeof request.retryFeedbackText !== "string" || request.retryFeedbackText.length > 20000)) throw new Error("Invalid retryFeedbackText");
    const business = { ...request };
    delete business.attempt;
    delete business.previousError;
    delete business.retryFeedbackText;
    delete business.retryFeedbackVersion;
    claim(b.store, "maintenance-request", request.id, business);
    const exact = {
      ...business,
      attempt,
      ...(request.retryFeedbackText === undefined ? {} : {retryFeedbackText:request.retryFeedbackText}),
      ...(request.retryFeedbackVersion === undefined ? {} : {retryFeedbackVersion:request.retryFeedbackVersion}),
      ...(request.previousError === undefined
        ? {}
        : { previousError: request.previousError }),
    };
    const key = `maintenance:${request.id}:attempt:${attempt}`;
    claim(b.store, "maintenance-attempt-request", key, exact);
    return this.requests.run(key, exact, () =>
      this.serial.run(`maintenance:${request.id}`, () =>
        this.maintainAttempt(exact, key),
      ),
    );
  }
  async maintainAttempt(request, key) {
    const b = this.bridge,
      { attempt } = request;
    const stopped = b.store.get("maintenance-control", key);
    if (stopped?.safeToRelease) return { status: "stopped", ...stopped };
    const saved = b.store.get("operation", key);
    if (saved !== undefined) return saved;
    const previous = b.store.get("maintenance-progress", request.id);
    if (previous && attempt < previous.attempt)
      throw new Error("MAINTENANCE_ATTEMPT_RETIRED");
    if (previous && attempt > previous.attempt) {
      let prior = await b.input(previous.sessionId, previous.inputId);
      if (["accepted", "running", "preparing"].includes(prior.status))
        prior = await b.waitInput(previous.sessionId, previous.inputId);
      if (!terminal(prior.status) && previous.status !== "failed_before_input")
        return {
          status: "unknown",
          attempt: previous.attempt,
          error:
            "Previous maintenance model outcome is not proven terminal; no new attempt started",
        };
    }
    const sessionId =
      attempt === 0
        ? `maint-${request.id}`
        : `maint-${request.id}-attempt-${attempt}`;
    const progress = {
      attempt,
      sessionId,
      inputId: `${sessionId}:request`,
      status: "starting",
    };
    b.store.set("maintenance-progress", request.id, progress);
    let result;
    try {
      result = await this.runIsolated(
        {
          id: sessionId,
          kind: request.kind === "dream" ? "dream" : "idle",
          promptSections: request.promptSections,
          nativeCompaction: request.nativeCompaction,
          model: request.model,
          modelOptions: request.modelOptions,
          agentPreset: request.agentPreset,
          promptPolicy: request.promptPolicy,
          contextFragments: request.contextFragments,
          toolScopes: request.toolScopes,
          libraryMode: request.libraryMode,
        },
        [request.presentationText ?? request.sourceMessages?.map(m => `${m.sentAt ?? ""} ${m.source ?? ""}: ${m.text ?? ""}`).join("\n\n") ?? "",request.retryFeedbackText ?? ""].filter(Boolean).join("\n\n"),
      );
    } catch (error) {
      const input = b.store.get("input", progress.inputId);
      b.store.set("maintenance-progress", request.id, {
        ...progress,
        status: input ? "unknown" : "failed_before_input",
        error: String(error),
      });
      throw error;
    }
    const control = b.store.get("maintenance-control", key);
    if (control?.safeToRelease) return { status: "stopped", ...control };
    b.store.set("maintenance-progress", request.id, {
      ...progress,
      status: result.status,
    });
    if (!terminal(result.status))
      return {
        status: "unknown",
        attempt,
        error: result.error ?? "Maintenance model outcome is unknown",
      };
    if (result.status !== "completed")
      throw new Error(result.error ?? "MAINTENANCE_MODEL_FAILED");
    try {
      const text = result.text
        .trim()
        .replace(/^```(?:json)?\s*/, "")
        .replace(/\s*```$/, "");
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new Error("MAINTENANCE_INVALID_JSON");
      }
      if (
        !parsed ||
        typeof parsed.handoff !== "string" ||
        !Array.isArray(parsed.selectedOriginals) ||
        !Array.isArray(parsed.memories)
      )
        throw new Error("MAINTENANCE_INVALID_SCHEMA");
      return b.store.set("operation", key, parsed);
    } catch (error) {
      b.store.set("maintenance-progress", request.id, {
        ...progress,
        status: "invalid",
        error: String(error),
      });
      throw error;
    }
  }
  async task(request) {
    const b = this.bridge;
    request = snapshot(request);
    validateLibraryMode(request.libraryMode);
    validId(request.id);
    claim(b.store, "task-request", request.id, request);
    return this.requests.run(`task:${request.id}`, request, () =>
      this.serial.run(`task:${request.id}`, async () => {
        const key = `task:${request.id}`,
          old = b.store.get("operation", key);
        if (old !== undefined) return old;
        if (b.store.get("task-control", `work-${request.id}`)?.stopRequested)
          return this.cancelTask(request.id);
        const result = await this.runIsolated(
          {
            id: `work-${request.id}`,
            kind: "work",
            promptSections: request.promptSections,
          nativeCompaction: request.nativeCompaction,
            model: request.model,
            modelOptions: request.modelOptions,
            agentPreset: request.agentPreset,
            promptPolicy: request.promptPolicy,
            contextFragments: request.contextFragments,
            toolScopes: request.toolScopes,
            libraryMode: request.libraryMode,
          },
          request.presentationText ?? request.objective ?? "",
        );
        if (b.store.get("task-control", `work-${request.id}`)?.stopRequested) return this.cancelTask(request.id);
        const concurrent = b.store.get("operation", key);
        if (concurrent !== undefined) return concurrent;
        if (!terminal(result.status))
          return {
            status: "unknown",
            summary: result.error ?? "Execution outcome unknown",
            artifactRefs: this.artifacts(request.id),
          };
        return b.store.set("operation", key, {
          status: result.status === "completed" ? "completed" : result.status === "stopped" || b.store.get("task-control",`work-${request.id}`)?.stopRequested ? "stopped" : "failed",
          summary: result.text ?? result.error ?? "",
          artifactRefs: this.artifacts(request.id),
        });
      }),
    );
  }
  async taskStatus(id) {
    validId(id);
    const b = this.bridge,
      old = b.store.get("operation", `task:${id}`);
    if (old !== undefined) return old;
    if(b.store.get("task-control",`work-${id}`)?.stopRequested) return {status:"unknown",summary:"",artifactRefs:this.artifacts(id),progress:this.progress(id)};
    const result = await b.input(`work-${id}`, `work-${id}:request`);
    if (result.status === "not_found") return result;
    const status =
      result.status === "completed"
        ? "completed"
        : result.status === "failed"
          ? "failed"
          : result.status;
    const summary = status === "completed" ? (result.text ?? "") : (result.error ?? "");
    if (terminal(status)) return b.store.set("operation", `task:${id}`, {
      status: b.store.get("task-control", `work-${id}`)?.stopRequested && status !== "completed" ? "stopped" : status,
      summary, artifactRefs:this.artifacts(id),
    });
    return { status, summary, artifactRefs: this.artifacts(id), progress: this.progress(id) };
  }
  artifacts(id) {
    return this.bridge.store.all("work-artifact").filter(a => a.sessionId === `work-${id}`).map(a => a.ref);
  }
  progress(id) {
    const b = this.bridge, sid = `work-${id}`;
    const effects = b.store.all("work-tool").filter(a => a.sessionId === sid);
    return {sessionId:sid, inputId:`${sid}:request`, toolCalls:effects.length,
      completedTools:effects.filter(x => x.status === "completed").length,
      lastTool:effects.at(-1) ?? null, stopRequested:!!b.store.get("task-control",sid)?.stopRequested};
  }
  async cancelTask(id) {
    validId(id);
    return this.requests.run(`task-stop:${id}`,{id},()=>this.stopTask(id));
  }
  async stopTask(id) {
    const b = this.bridge, sid = `work-${id}`;
    const current = await this.taskStatus(id);
    if (terminal(current.status)) return current;
    b.store.set("task-control",sid,{stopRequested:true,at:new Date().toISOString()});
    // A permanent stop fence precedes any awaited load or network completion.
    const config = b.store.get("session",sid);
    if (!config) return b.store.set("operation",`task:${id}`,{status:"stopped",summary:"",artifactRefs:this.artifacts(id),safeToRelease:true,evidence:{fencedBeforeAdmission:true}});
    const proof = await b.quarantine(sid,`${sid}:request`,{stop:true});
    if (!proof.safeToRelease && !proof.safeToContinue && !terminal(proof.status))
      return {status:"unknown",safeToRelease:false,summary:"Work stop is not yet proven",artifactRefs:this.artifacts(id),evidence:proof};
    return b.store.set("operation",`task:${id}`,{status:"stopped",summary:current.summary ?? "",artifactRefs:this.artifacts(id),safeToRelease:true,evidence:proof});
  }
  async drain() {
    await this.requests.drain();
    await this.isolated.drain();
    await this.serial.drain();
  }
}
