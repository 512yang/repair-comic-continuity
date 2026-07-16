import {
  closeSync,
  existsSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { randomUUID } from "node:crypto";
import { basename, dirname, join } from "node:path";

export function writeAtomicJson(path: string, value: unknown): void {
  const parent = dirname(path);
  mkdirSync(parent, { recursive: true });
  const temporary = join(
    parent,
    `.${basename(path)}.${process.pid}.${randomUUID()}.tmp`,
  );
  const handle = openSync(temporary, "wx");
  try {
    writeFileSync(handle, `${JSON.stringify(value, null, 2)}\n`, "utf8");
    fsyncSync(handle);
  } finally {
    closeSync(handle);
  }
  renameSync(temporary, path);
}

export function readAtomicJson<T>(path: string): T {
  if (!existsSync(path)) {
    recoverAtomicJson(path);
  }
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

function recoverAtomicJson(path: string): void {
  const parent = dirname(path);
  if (!existsSync(parent)) {
    throw new Error(`JSON file does not exist: ${path}`);
  }
  const prefix = `.${basename(path)}.`;
  const candidates = readdirSync(parent)
    .filter((name) => name.startsWith(prefix) && name.endsWith(".tmp"))
    .map((name) => join(parent, name))
    .sort((left, right) => statSync(right).mtimeMs - statSync(left).mtimeMs);
  const valid = candidates.filter((candidate) => {
    try {
      JSON.parse(readFileSync(candidate, "utf8"));
      return true;
    } catch {
      return false;
    }
  });
  if (valid.length !== 1) {
    throw new Error(
      `JSON file is missing and recovery is ambiguous: ${path} (${valid.length} valid temporary files)`,
    );
  }
  renameSync(valid[0]!, path);
}
