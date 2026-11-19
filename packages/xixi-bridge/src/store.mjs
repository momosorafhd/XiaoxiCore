import { DatabaseSync } from "node:sqlite";
import {
  mkdirSync,
  openSync,
  closeSync,
  fchmodSync,
  chmodSync,
  existsSync,
  constants,
} from "node:fs";
import { join } from "node:path";
export class BridgeStore {
  constructor(root) {
    mkdirSync(root, { recursive: true, mode: 0o700 });
    const path = join(root, "bridge.sqlite");
    const fd = openSync(
      path,
      constants.O_CREAT | constants.O_RDWR | constants.O_NOFOLLOW,
      0o600,
    );
    try {
      fchmodSync(fd, 0o600);
    } finally {
      closeSync(fd);
    }
    this.db = new DatabaseSync(path);
    this.db.exec(
      "PRAGMA journal_mode=WAL; CREATE TABLE IF NOT EXISTS records (kind TEXT,key TEXT,value TEXT,PRIMARY KEY(kind,key))",
    );
    for (const suffix of ["", "-wal", "-shm"])
      if (existsSync(path + suffix)) chmodSync(path + suffix, 0o600);
  }
  get(kind, key) {
    const r = this.db
      .prepare("SELECT value FROM records WHERE kind=? AND key=?")
      .get(kind, key);
    return r ? JSON.parse(r.value) : undefined;
  }
  set(kind, key, value) {
    this.db
      .prepare(
        "INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value",
      )
      .run(kind, key, JSON.stringify(value));
    return value;
  }
  all(kind) {
    return this.db
      .prepare("SELECT value FROM records WHERE kind=?")
      .all(kind)
      .map((r) => JSON.parse(r.value));
  }
  delete(kind, key) {
    this.db
      .prepare("DELETE FROM records WHERE kind=? AND key=?")
      .run(kind, key);
  }
  close() {
    this.db.close();
  }
}
