import { createHash, randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { join, resolve } from "node:path";

import type { ReviewMode, RunPhase } from "../shared/contracts";
import { readAtomicJson, writeAtomicJson } from "./atomicJson";
import { inventoryProject, type ProjectInventory } from "./projectInventory";

interface StoredSession {
  schema_version: "comic-review-workbench-session-v1";
  session_id: string;
  project_root: string;
  mode: ReviewMode;
  mode_locked_at: string | null;
  phase: RunPhase;
  inventory_sha256: string;
  created_at: string;
  updated_at: string;
}

export class ProjectSession {
  readonly inventory: ProjectInventory;
  private readonly sessionPath: string;
  private state: StoredSession;

  constructor(sessionPath: string, state: StoredSession, inventory: ProjectInventory) {
    this.sessionPath = sessionPath;
    this.state = state;
    this.inventory = inventory;
  }

  get mode(): ReviewMode {
    return this.state.mode;
  }

  get sessionId(): string {
    return this.state.session_id;
  }

  get projectRoot(): string {
    return this.state.project_root;
  }

  get phase(): RunPhase {
    return this.state.phase;
  }

  get modeLockedAt(): string | null {
    return this.state.mode_locked_at;
  }

  chooseMode(mode: ReviewMode): void {
    if (this.state.mode_locked_at) {
      if (this.state.mode !== mode) {
        throw new Error(`Project mode is already locked to ${this.state.mode}.`);
      }
      return;
    }
    if (this.state.mode === mode) return;
    const now = new Date().toISOString();
    this.state = { ...this.state, mode, updated_at: now };
    writeAtomicJson(this.sessionPath, this.state);
  }

  lockMode(mode: ReviewMode): void {
    if (this.state.mode_locked_at) {
      if (this.state.mode !== mode) {
        throw new Error(
          `Project mode is already locked to ${this.state.mode} at ${this.state.mode_locked_at}.`,
        );
      }
      return;
    }
    const now = new Date().toISOString();
    this.state = {
      ...this.state,
      mode,
      mode_locked_at: now,
      phase: mode === "automatic" ? "processing" : "input_review",
      updated_at: now,
    };
    writeAtomicJson(this.sessionPath, this.state);
  }
}

export function createProjectSession(
  projectRoot: string,
  mode: ReviewMode,
): ProjectSession {
  const root = resolve(projectRoot);
  const stateRoot = join(root, ".comic-review-workbench");
  const sessionPath = join(stateRoot, "session.json");
  const inventoryPath = join(stateRoot, "inventory.json");
  const inventory = inventoryProject(root);
  const inventorySha = hashInventory(inventory);
  writeAtomicJson(inventoryPath, inventory);

  if (existsSync(sessionPath)) {
    const existing = openProjectSession(root);
    const stored = readAtomicJson<StoredSession>(sessionPath);
    if (stored.mode_locked_at && stored.mode !== mode) {
      throw new Error(`Project mode is already locked to ${stored.mode}.`);
    }
    existing.chooseMode(mode);
    return existing;
  }

  const now = new Date().toISOString();
  const state: StoredSession = {
    schema_version: "comic-review-workbench-session-v1",
    session_id: randomUUID(),
    project_root: root,
    mode,
    mode_locked_at: null,
    phase: "project_setup",
    inventory_sha256: inventorySha,
    created_at: now,
    updated_at: now,
  };
  writeAtomicJson(sessionPath, state);
  return new ProjectSession(sessionPath, state, inventory);
}

export function openProjectSession(projectRoot: string): ProjectSession {
  const root = resolve(projectRoot);
  const stateRoot = join(root, ".comic-review-workbench");
  const sessionPath = join(stateRoot, "session.json");
  if (!existsSync(sessionPath)) {
    throw new Error("Workbench session does not exist for this project.");
  }
  const stored = readAtomicJson<StoredSession>(sessionPath);
  const inventory = inventoryProject(root);
  if (stored.project_root !== root) {
    throw new Error("Stored session belongs to a different project root.");
  }
  if (stored.inventory_sha256 !== hashInventory(inventory)) {
    throw new Error("Project inventory hash drift requires a new or reconciled session.");
  }
  return new ProjectSession(sessionPath, stored, inventory);
}

function hashInventory(inventory: ProjectInventory): string {
  const stable = {
    pages: inventory.pages,
    novels: inventory.novels,
    references: inventory.references,
  };
  return createHash("sha256").update(JSON.stringify(stable)).digest("hex");
}
