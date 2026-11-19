import { isDeepStrictEqual } from "node:util";
export const snapshot = (value) => JSON.parse(JSON.stringify(value));
export function claim(store, kind, key, payload) {
  const saved = snapshot(payload),
    old = store.get(kind, key);
  if (old !== undefined && !isDeepStrictEqual(old, saved))
    throw Object.assign(new Error(`${kind.toUpperCase()}_ID_CONFLICT`), {
      code: "REQUEST_ID_CONFLICT",
    });
  if (old === undefined) store.set(kind, key, saved);
  return saved;
}
// Coalesce identical in-flight requests; reject conflicting same-ID payloads.
export class SingleFlight {
  constructor() {
    this.flights = new Map();
  }
  run(key, payload, operation) {
    const existing = this.flights.get(key);
    if (existing) {
      if (!isDeepStrictEqual(existing.payload, payload))
        return Promise.reject(
          Object.assign(new Error("REQUEST_ID_CONFLICT"), {
            code: "REQUEST_ID_CONFLICT",
          }),
        );
      return existing.promise;
    }
    const entry = { payload: snapshot(payload) };
    entry.promise = Promise.resolve()
      .then(operation)
      .finally(() => {
        if (this.flights.get(key) === entry) this.flights.delete(key);
      });
    this.flights.set(key, entry);
    return entry.promise;
  }
  async drain() {
    await Promise.allSettled([...this.flights.values()].map((e) => e.promise));
  }
}
// Per-key mutation order, while separate sessions/operations stay concurrent.
export class KeyedSerial {
  constructor() {
    this.tails = new Map();
  }
  run(key, operation) {
    const prior = this.tails.get(key) ?? Promise.resolve();
    const result = prior.catch(() => {}).then(operation);
    const tail = result
      .catch(() => {})
      .finally(() => {
        if (this.tails.get(key) === tail) this.tails.delete(key);
      });
    this.tails.set(key, tail);
    return result;
  }
  async drain() {
    await Promise.allSettled([...this.tails.values()]);
  }
}
export function canonicalJSON(value) {
  const sort = (v) =>
    Array.isArray(v)
      ? v.map(sort)
      : v && typeof v === "object"
        ? Object.fromEntries(
            Object.keys(v)
              .sort()
              .map((k) => [k, sort(v[k])]),
          )
        : v;
  return JSON.stringify(sort(snapshot(value)));
}
