import { createServer } from "node:http";
import { timingSafeEqual } from "node:crypto";

export function createBridgeServer(
  bridge,
  { token = process.env.XIXI_BRIDGE_TOKEN } = {},
) {
  if (!token || token.length < 24)
    throw new Error(
      "Set XIXI_BRIDGE_TOKEN to a private random value of at least 24 characters.",
    );
  function authorized(req) {
    const supplied = Buffer.from(req.headers.authorization ?? "");
    const expected = Buffer.from(`Bearer ${token}`);
    return (
      supplied.length === expected.length && timingSafeEqual(supplied, expected)
    );
  }
  async function body(req) {
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 4 * 1024 * 1024) throw new Error("BODY_TOO_LARGE");
      chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks).toString() || "{}");
  }
  return createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("Cache-Control", "no-store");
    const respond = (code, value) => {
      res.writeHead(code);
      res.end(JSON.stringify(value));
    };
    if (!authorized(req)) return respond(401, { error: "UNAUTHORIZED" });
    try {
      const u = new URL(req.url, "http://127.0.0.1");
      const parts = u.pathname
        .split("/")
        .filter(Boolean)
        .map(decodeURIComponent);
      let result;
      if (req.method === "GET" && u.pathname === "/health") {
        return respond(200, await bridge.health());
      } else if (req.method === "GET" && u.pathname === "/prompt-policy/capabilities") {
        if(typeof bridge.promptPolicyCapabilities!=="function")return respond(404,{error:"PROMPT_POLICY_CAPABILITIES_UNAVAILABLE"});
        result=await bridge.promptPolicyCapabilities();
      } else if (req.method === "GET" && u.pathname === "/models")
        result = await bridge.modelCatalog();
      else if (req.method === "GET" && u.pathname === "/plugins")
        result = await bridge.pluginInventory();
      else if (req.method === "POST" && parts[0] === "plugins" && parts[1])
        result = await bridge.updatePlugin(parts[1], await body(req));
      else if (req.method === "GET" && parts[0] === "maintenance" && parts[1])
        result = await bridge.maintenanceStatus(
          parts[1],
          Number(u.searchParams.get("attempt") ?? 0),
        );
      else if (
        req.method === "POST" &&
        parts[0] === "maintenance" &&
        parts[1] &&
        parts[2] === "stop"
      ) {
        const request = await body(req);
        result = await bridge.stopMaintenance(parts[1], request.attempt ?? 0);
      } else if (req.method === "POST" && u.pathname === "/sessions")
        result = await bridge.session(await body(req));
      else if (req.method === "POST" && u.pathname === "/maintenance")
        result = await bridge.maintenance(await body(req));
      else if (req.method === "POST" && u.pathname === "/tasks/run")
        result = await bridge.task(await body(req));
      else if (req.method === "POST" && parts[0] === "tasks" && parts[1] && parts[2] === "cancel")
        result = await bridge.cancelTask(parts[1]);
      else if (req.method === "GET" && parts[0] === "tasks" && parts[1])
        result = await bridge.taskStatus(parts[1]);
      else if (parts[0] === "sessions" && parts[1]) {
        const id = parts[1];
        if (req.method === "POST" && parts[2] === "deliver")
          result = await bridge.deliver(id, await body(req));
        else if (req.method === "POST" && parts[2] === "release")
          result = await bridge.releaseSession(id, await body(req));
        else if (req.method === "GET" && parts[2] === "context")
          result = await bridge.contextInventory(id);
        else if (
          req.method === "POST" &&
          parts[2] === "inputs" &&
          parts[3] &&
          parts[4] === "quarantine"
        )
          result = await bridge.quarantine(id, parts[3], await body(req));
        else if (req.method === "GET" && parts[2] === "state")
          result = await bridge.state(id);
        else if (req.method === "GET" && parts[2] === "usage")
          result = await bridge.usage(id);
        else if (req.method === "GET" && parts[2] === "inputs" && parts[3])
          result = await bridge.input(id, parts[3]);
        else if (req.method === "GET" && parts[2] === "history")
          result = bridge.history ? await bridge.history(id) : await bridge.ctx.sessionQuery.readSession(id);
        else if (req.method === "GET" && parts[2] === "surface")
          result = bridge.surface ? await bridge.surface(id) : await bridge.ctx.sessionQuery.readSurface(id);
        else if (req.method === "POST" && parts[2] === "cancel")
          result = await bridge.cancel(id, await body(req));
        else return respond(404, { error: "NOT_FOUND" });
      } else return respond(404, { error: "NOT_FOUND" });
      respond(200, result);
    } catch (error) {
      respond(400, {
        ok: false,
        error: {
          code: error.code ?? "BRIDGE_ERROR",
          message: String(error.message ?? error),
          retryable: false,
        },
      });
    }
  });
}
