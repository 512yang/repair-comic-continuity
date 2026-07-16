# Codex Comic Review Workbench Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a distributable Windows Codex plugin that adds a simple input/output comic annotation workbench to the existing `repair-comic-continuity` Skill while preserving its evidence, text-engine, learning, and release gates.

**Architecture:** Convert the existing public Skill repository into a plugin root containing the Skill, a Node MCP server, and a React single-file MCP App widget. The widget owns user interaction only; the MCP server owns safe local persistence and manifest export; Codex and the embedded Skill own interpretation, repair, review, learning, and promotion.

**Tech Stack:** Codex plugin manifest, Node.js 22+, TypeScript, `@modelcontextprotocol/ext-apps`, React 19, Vite single-file build, native Canvas 2D, Vitest, Testing Library, Playwright, Python 3.11+ `unittest`, existing Pillow/LaMa/text-engine code, GPT Image 2 through the Codex image-generation tool.

---

## Working directory and source-control contract

All commands run from:

```text
D:\自动修改\7878\repair-comic-continuity-workbench
```

The source branch is `agent/continuity-v4-implementation`; implementation uses `agent/workbench-plugin-v1`. The existing repository remains the source of truth. Do not develop from the installed Skill directory or a `.publish` release copy.

## Planned file structure

```text
repair-comic-continuity-workbench/
├─ .codex-plugin/plugin.json             # plugin identity and component discovery
├─ .mcp.json                             # local stdio MCP server declaration
├─ package.json                          # pinned JS build/test commands
├─ package-lock.json                     # reproducible dependency graph
├─ tsconfig.json
├─ vite.config.ts                        # single-file widget output
├─ mcp/server.ts                         # MCP entrypoint and App resource registration
├─ src/shared/contracts.ts               # cross-layer domain types and schemas
├─ src/shared/stateMachine.ts            # mode/phase/submit behavior
├─ src/server/atomicJson.ts               # atomic JSON persistence
├─ src/server/projectInventory.ts        # safe paths, natural order, SHA-256 inventory
├─ src/server/projectSession.ts          # session creation, resume, mode lock
├─ src/server/draftStore.ts               # per-page autosave drafts
├─ src/server/annotationExport.ts         # raw-to-normalized manifest handoff
├─ src/server/progressReader.ts           # read-only Skill gate status projection
├─ src/server/tools.ts                    # MCP tool implementations
├─ src/widget/main.tsx
├─ src/widget/App.tsx
├─ src/widget/api.ts                      # widget-to-MCP calls
├─ src/widget/styles.css
├─ src/widget/components/ProjectStart.tsx
├─ src/widget/components/PageRail.tsx
├─ src/widget/components/AnnotationCanvas.tsx
├─ src/widget/components/AnnotationPanel.tsx
├─ src/widget/components/ProgressPanel.tsx
├─ src/widget/components/OutputCompare.tsx
├─ src/widget/components/SubmitBar.tsx
├─ tests/                                 # TypeScript unit/integration tests
├─ e2e/                                   # Playwright browser tests
├─ skills/repair-comic-continuity/        # existing Skill, moved without semantic loss
└─ scripts/package_release.ps1            # validated Windows ZIP build
```

---

### Task 1: Establish a real repository and scaffold the plugin root

**Files:**
- Create: `.codex-plugin/plugin.json`
- Create: `.mcp.json`
- Create: `package.json`
- Create: `tsconfig.json`
- Create: `vite.config.ts`
- Move: existing Skill files into `skills/repair-comic-continuity/`
- Test: `tests/plugin-manifest.test.ts`

- [ ] **Step 1: Install or expose Git and clone the existing source branch**

Run:

```powershell
git --version
```

Expected: a version line. If the command is missing, run:

```powershell
winget install --id Git.Git --exact --silent
```

Open a new PowerShell, then run:

```powershell
Set-Location 'D:\自动修改\7878'
git clone --branch agent/continuity-v4-implementation https://github.com/512yang/repair-comic-continuity.git repair-comic-continuity-workbench
Set-Location 'D:\自动修改\7878\repair-comic-continuity-workbench'
git switch -c agent/workbench-plugin-v1
git status --short
```

Expected: clean worktree on `agent/workbench-plugin-v1`.

- [ ] **Step 2: Write a failing plugin-manifest test**

Create `tests/plugin-manifest.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("plugin manifest", () => {
  it("binds the embedded skill and local MCP server", () => {
    const manifest = JSON.parse(readFileSync(".codex-plugin/plugin.json", "utf8"));
    expect(manifest.name).toBe("repair-comic-continuity-workbench");
    expect(manifest.skills).toBe("./skills/");
    expect(manifest.mcpServers).toBe("./.mcp.json");
  });
});
```

- [ ] **Step 3: Move the Skill and create the plugin/build manifests**

Use `git mv` for `SKILL.md`, `agents`, `assets`, `docs`, `references`, `scripts`, and the existing `tests` directory into `skills/repair-comic-continuity/`. Rename the existing test directory during the move so plugin tests can use root `tests/`.

After the move, copy the approved design and this plan into the new plugin-level documentation tree:

```powershell
New-Item -ItemType Directory -Force '.\docs\superpowers\specs', '.\docs\superpowers\plans' | Out-Null
Copy-Item '..\docs\superpowers\specs\2026-07-16-comic-review-workbench-plugin-design.md' '.\docs\superpowers\specs\'
Copy-Item '..\docs\superpowers\plans\2026-07-16-comic-review-workbench-plugin-implementation.md' '.\docs\superpowers\plans\'
```

Create `.codex-plugin/plugin.json` with this exact shape:

```json
{
  "name": "repair-comic-continuity-workbench",
  "version": "1.0.0",
  "description": "Review, annotate, repair, and re-review Chinese comic pages through Codex.",
  "author": {"name": "512yang", "url": "https://github.com/512yang"},
  "homepage": "https://github.com/512yang/repair-comic-continuity",
  "repository": "https://github.com/512yang/repair-comic-continuity",
  "license": "MIT",
  "keywords": ["comic", "continuity", "annotation", "text-repair"],
  "skills": "./skills/",
  "mcpServers": "./.mcp.json",
  "interface": {
    "displayName": "漫画审校工作台",
    "shortDescription": "圈画、批注并由 Codex 完成漫画修复闭环",
    "longDescription": "在人工审查或全自动模式下审核输入、监管处理并逐页复核输出。",
    "developerName": "512yang",
    "category": "Productivity",
    "capabilities": ["Interactive", "Write"],
    "defaultPrompt": ["启动漫画审校工作台", "继续当前漫画审校项目"]
  }
}
```

Create `.mcp.json`:

```json
{
  "mcpServers": {
    "comicReviewWorkbench": {
      "title": "漫画审校工作台",
      "description": "Open and persist the local comic review workbench.",
      "cwd": ".",
      "command": "node",
      "args": ["./dist/mcp/server.cjs", "--stdio"]
    }
  }
}
```

Create `package.json` scripts `build`, `typecheck`, `test`, `test:e2e`, and `validate:plugin`; install React, Vite, Vitest, Testing Library, Playwright, TypeScript, `@modelcontextprotocol/ext-apps`, and `vite-plugin-singlefile`, then commit `package-lock.json`.

- [ ] **Step 4: Verify the scaffold**

Run:

```powershell
npm test -- tests/plugin-manifest.test.ts
python 'C:\Users\骆阳\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py' .
python 'C:\Users\骆阳\.codex\skills\.system\skill-creator\scripts\quick_validate.py' '.\skills\repair-comic-continuity'
```

Expected: all three commands pass.

- [ ] **Step 5: Commit**

```powershell
git add .
git commit -m "chore: scaffold comic review workbench plugin"
```

---

### Task 2: Define shared modes, phases, annotations, and submit behavior

**Files:**
- Create: `src/shared/contracts.ts`
- Create: `src/shared/stateMachine.ts`
- Test: `tests/stateMachine.test.ts`

- [ ] **Step 1: Write failing state-machine tests**

```ts
import { describe, expect, it } from "vitest";
import { deriveSubmitAction } from "../src/shared/stateMachine";

describe("deriveSubmitAction", () => {
  it("uses one button for first submission and revisions", () => {
    expect(deriveSubmitAction({phase: "input_review", dirty: true, running: false, blockers: []})).toBe("initial_submit");
    expect(deriveSubmitAction({phase: "output_review", dirty: true, running: false, blockers: []})).toBe("revision_submit");
  });
  it("blocks duplicate, running, and incomplete submissions", () => {
    expect(deriveSubmitAction({phase: "input_review", dirty: false, running: false, blockers: []})).toBe("disabled_no_changes");
    expect(deriveSubmitAction({phase: "processing", dirty: true, running: true, blockers: []})).toBe("disabled_running");
    expect(deriveSubmitAction({phase: "output_review", dirty: true, running: false, blockers: ["2 pages unreviewed"]})).toBe("disabled_blocked");
  });
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npm test -- tests/stateMachine.test.ts`

Expected: FAIL because the shared modules do not exist.

- [ ] **Step 3: Implement the contracts and pure state machine**

Define exact unions in `contracts.ts`:

```ts
export type ReviewMode = "human_visual_auto_text" | "automatic";
export type RunPhase = "project_setup" | "input_review" | "processing" | "output_review" | "blocked" | "complete";
export type PageReviewState = "unreviewed" | "correct" | "annotated" | "processing" | "passed" | "needs_revision" | "locked";
export type SubmitAction = "initial_submit" | "revision_submit" | "disabled_no_changes" | "disabled_running" | "disabled_blocked";
export interface SubmitContext { phase: RunPhase; dirty: boolean; running: boolean; blockers: string[]; }
```

Implement `deriveSubmitAction` as a total function that gives blockers precedence, then running, then dirty state, then phase. Throw for `project_setup`, `blocked`, or `complete` only when none of the disabled outcomes applies.

- [ ] **Step 4: Run tests and typecheck**

Run:

```powershell
npm test -- tests/stateMachine.test.ts
npm run typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/shared tests/stateMachine.test.ts
git commit -m "feat: define workbench state contracts"
```

---

### Task 3: Seal project inventory and implement atomic per-page drafts

**Files:**
- Create: `src/server/atomicJson.ts`
- Create: `src/server/projectInventory.ts`
- Create: `src/server/projectSession.ts`
- Create: `src/server/draftStore.ts`
- Test: `tests/projectSession.test.ts`

- [ ] **Step 1: Write failing inventory and recovery tests**

Create a temporary project with `输入/2.jpg`, `输入/10.jpg`, `输出/`, a novel, and references. Assert natural ordering, SHA-256 binding, mode immutability after first submission, atomic draft recovery, and rejection of `../` paths.

```ts
expect(session.inventory.pages.map((p) => p.path)).toEqual(["2.jpg", "10.jpg"]);
expect(() => session.lockMode("automatic")).toThrow(/mode is already locked/);
expect(() => saveDraft(root, "../2.jpg", draft)).toThrow(/safe relative path/);
```

- [ ] **Step 2: Verify failure**

Run: `npm test -- tests/projectSession.test.ts`

Expected: FAIL because the server modules do not exist.

- [ ] **Step 3: Implement safe inventory and draft persistence**

Use `createHash("sha256")`, `Intl.Collator("zh-CN", {numeric: true})`, and resolved-path containment checks. Store workbench state only below:

```text
<project>/.comic-review-workbench/
├─ session.json
├─ inventory.json
└─ drafts/<phase>/<url-encoded-relative-name>.json
```

`atomicJson.ts` must write UTF-8 JSON to a sibling temporary file, flush and close it, then rename it over the target. `projectSession.ts` must persist `mode_locked_at` on first submission and reject later mode changes. Never write inside `输入` or `输出`.

- [ ] **Step 4: Run tests**

Run: `npm test -- tests/projectSession.test.ts`

Expected: PASS, including interrupted temporary-file recovery.

- [ ] **Step 5: Commit**

```powershell
git add src/server tests/projectSession.test.ts
git commit -m "feat: seal projects and autosave page drafts"
```

---

### Task 4: Capture rectangle, freehand, polygon, and full-page annotations

**Files:**
- Modify: `src/shared/contracts.ts`
- Create: `src/shared/annotationGeometry.ts`
- Create: `src/server/annotationExport.ts`
- Modify: `skills/repair-comic-continuity/scripts/validate_human_issue_annotations.py`
- Test: `tests/annotationGeometry.test.ts`
- Test: `skills/repair-comic-continuity/tests/test_human_issue_annotations.py`

- [ ] **Step 1: Write failing geometry tests**

```ts
const polygon = normalizeShape({kind: "polygon", points: [[100, 50], [300, 80], [250, 200]]}, {width: 1000, height: 500});
expect(polygon.bbox_norm).toEqual([0.1, 0.1, 0.3, 0.4]);
expect(polygon.polygon_norm).toEqual([[0.1, 0.1], [0.3, 0.16], [0.25, 0.4]]);
expect(() => normalizeShape({kind: "full_page", points: []}, {width: 0, height: 500})).toThrow(/positive dimensions/);
```

Add Python tests proving optional `shape_kind`, `polygon_norm`, and `freehand_norm` are accepted only when their points are in `[0,1]` and their derived bounding box matches `bbox_norm`.

- [ ] **Step 2: Verify both suites fail**

```powershell
npm test -- tests/annotationGeometry.test.ts
python '.\skills\repair-comic-continuity\tests\test_human_issue_annotations.py'
```

Expected: both fail on missing shape support.

- [ ] **Step 3: Implement normalized geometry and backward-compatible export**

Every UI shape retains its exact normalized path and exports a V5.5-compatible `bbox_norm`. `annotationExport.ts` emits raw drafts for Codex interpretation; it must not invent `targets`, `defect_codes`, `observed_state`, or `required_state`. Add those fields only through the later normalized commit tool.

- [ ] **Step 4: Run both suites**

Expected: PASS with rectangle, polygon, freehand, full-page, out-of-bounds, degenerate, and bbox-mismatch cases.

- [ ] **Step 5: Commit**

```powershell
git add src/shared src/server/annotationExport.ts tests/annotationGeometry.test.ts skills/repair-comic-continuity
git commit -m "feat: support precise workbench annotation geometry"
```

---

### Task 5: Add revision feedback without overwriting earlier evidence

**Files:**
- Create: `skills/repair-comic-continuity/scripts/validate_human_revision_feedback.py`
- Create: `skills/repair-comic-continuity/tests/test_human_revision_feedback.py`
- Modify: `skills/repair-comic-continuity/scripts/closed_loop_controller.py`
- Modify: `skills/repair-comic-continuity/scripts/human_issue_learning.py`
- Modify: `skills/repair-comic-continuity/references/failure-learning.md`

- [ ] **Step 1: Write failing revision-contract tests**

Use this exact record shape:

```python
{
    "feedback_id": "0003-attempt-2-skin",
    "origin": "missed_detection",
    "attempt": 2,
    "page": {"path": "0003.jpg", "sha256": source_sha},
    "candidate": {"path": "candidates/attempt-1/0003.jpg", "sha256": candidate_sha},
    "parent_annotation_ids": [],
    "regions": [{"region_id": "face", "bbox_norm": [0.2, 0.1, 0.5, 0.5], "description": "skin tone"}],
    "user_note": "同一男人肤色仍然不一致",
}
```

Test all five origins: `missed_detection`, `unresolved`, `introduced_error`, `damaged_correct_content`, and `text_result_error`. Reject stale candidate hashes, duplicate IDs, attempt zero, unknown pages, and overwriting a prior attempt.

- [ ] **Step 2: Verify failure**

Run: `python '.\skills\repair-comic-continuity\tests\test_human_revision_feedback.py'`

Expected: FAIL because the validator does not exist.

- [ ] **Step 3: Implement validation, controller receipt, and learning intake**

Store each round at `evidence/human_revision_feedback/attempt-<N>.json`. Extend the controller with a `revision_feedback_ingested` receipt before a revision task can be released. Convert each accepted feedback-target pair into observed failure evidence; never promote it directly to an effective rule.

- [ ] **Step 4: Run focused and existing learning tests**

```powershell
python '.\skills\repair-comic-continuity\tests\test_human_revision_feedback.py'
python '.\skills\repair-comic-continuity\tests\test_human_issue_learning.py'
python '.\skills\repair-comic-continuity\tests\test_closed_loop_controller.py'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add skills/repair-comic-continuity
git commit -m "feat: bind human revision feedback to learning"
```

---

### Task 6: Expose the workbench through a local MCP App server

**Files:**
- Create: `mcp/server.ts`
- Create: `src/server/tools.ts`
- Create: `src/widget/api.ts`
- Test: `tests/mcpTools.test.ts`

- [ ] **Step 1: Write failing tool-protocol tests**

Test these tools and exact responsibilities:

```ts
const toolNames = [
  "open_comic_review_workbench",
  "save_review_draft",
  "submit_review_drafts",
  "commit_normalized_annotations",
  "get_comic_review_status",
  "record_output_page_decision",
];
expect(server.listToolNames()).toEqual(toolNames);
```

`submit_review_drafts` returns raw user notes and shapes to Codex. Only `commit_normalized_annotations` can write the Skill manifests, after it receives all required structured fields and the embedded Python validators pass.

- [ ] **Step 2: Verify failure**

Run: `npm test -- tests/mcpTools.test.ts`

Expected: FAIL because the MCP server and tools do not exist.

- [ ] **Step 3: Implement the stdio server and widget resource**

Register one MCP App HTML resource at `ui://comic-review-workbench/index.html`. Register the six tools above. Resolve all project paths through `projectInventory.ts`; invoke Python validators with argument arrays rather than shell-built strings; return structured blocker codes instead of prose-only success claims.

- [ ] **Step 4: Build and test**

```powershell
npm run build
npm test -- tests/mcpTools.test.ts
Test-Path '.\dist\mcp\server.cjs'
Test-Path '.\dist\widget\index.html'
```

Expected: build and tests pass; both files exist.

- [ ] **Step 5: Commit**

```powershell
git add mcp src/server/tools.ts src/widget/api.ts tests/mcpTools.test.ts dist
git commit -m "feat: expose comic review MCP app tools"
```

---

### Task 7: Build the simple mode selection and page-review shell

**Files:**
- Create: `src/widget/main.tsx`
- Create: `src/widget/App.tsx`
- Create: `src/widget/styles.css`
- Create: `src/widget/components/ProjectStart.tsx`
- Create: `src/widget/components/PageRail.tsx`
- Create: `src/widget/components/SubmitBar.tsx`
- Test: `tests/App.test.tsx`

- [ ] **Step 1: Write failing UI tests**

```tsx
render(<App initialState={humanProject} api={fakeApi} />);
expect(screen.getByRole("button", {name: "人工审查"})).toBeVisible();
expect(screen.getByRole("button", {name: "全自动"})).toBeVisible();
await user.click(screen.getByRole("button", {name: "人工审查"}));
expect(screen.getByRole("button", {name: "正确并下一页"})).toBeVisible();
expect(screen.getByRole("button", {name: "提交给 Codex"})).toBeDisabled();
```

Also test that automatic mode skips input review and enters processing only after the single submit button is pressed.

- [ ] **Step 2: Verify failure**

Run: `npm test -- tests/App.test.tsx`

Expected: FAIL because the widget components do not exist.

- [ ] **Step 3: Implement the shell**

Use a three-column layout with thumbnail rail, full-resolution canvas host, and annotation panel. Keep only mode choice, `正确/通过并下一页`, annotation controls, comparison controls, and `提交给 Codex` visible. Put hashes and evidence details under a collapsed `高级信息` disclosure.

- [ ] **Step 4: Run UI tests and typecheck**

```powershell
npm test -- tests/App.test.tsx
npm run typecheck
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/widget tests/App.test.tsx
git commit -m "feat: add simple comic review app shell"
```

---

### Task 8: Implement high-resolution annotation, zoom, pan, and autosave

**Files:**
- Create: `src/widget/components/AnnotationCanvas.tsx`
- Create: `src/widget/components/AnnotationPanel.tsx`
- Test: `tests/AnnotationCanvas.test.tsx`
- Test: `e2e/annotation.spec.ts`

- [ ] **Step 1: Write failing canvas tests**

Test rectangle, freehand, polygon, full-page, undo, delete, zoom, pan, one page with multiple annotations, and autosave after every committed shape. Assert that rendered overlay coordinates change with zoom while stored normalized coordinates do not.

- [ ] **Step 2: Verify failure**

Run: `npm test -- tests/AnnotationCanvas.test.tsx`

Expected: FAIL because the canvas component does not exist.

- [ ] **Step 3: Implement native Canvas 2D drawing**

Maintain an immutable normalized shape model; keep viewport scale and translation separate. Debounce only pointer-move previews. Persist immediately on pointer-up, polygon close, note edit blur, undo, and delete. Never write overlay pixels into source images.

- [ ] **Step 4: Run unit and browser tests**

```powershell
npm test -- tests/AnnotationCanvas.test.tsx
npm run test:e2e -- e2e/annotation.spec.ts
```

Expected: PASS at 100%, 200%, and fit-to-window zoom.

- [ ] **Step 5: Commit**

```powershell
git add src/widget/components tests/AnnotationCanvas.test.tsx e2e/annotation.spec.ts
git commit -m "feat: add precise autosaved comic annotations"
```

---

### Task 9: Add output comparison, mandatory review, one-button revision, and locks

**Files:**
- Create: `src/widget/components/OutputCompare.tsx`
- Create: `src/widget/components/ProgressPanel.tsx`
- Modify: `src/widget/App.tsx`
- Modify: `src/server/projectSession.ts`
- Test: `tests/outputReview.test.tsx`
- Test: `e2e/output-review.spec.ts`

- [ ] **Step 1: Write failing output-review tests**

Assert side-by-side, slider, and alpha-overlay modes. Assert that first output review blocks submission until every page is passed or annotated; revision review covers only changed pages; passed pages are locked; the same `提交给 Codex` button emits `revision_submit`.

- [ ] **Step 2: Verify failure**

Run: `npm test -- tests/outputReview.test.tsx`

Expected: FAIL because output review is not implemented.

- [ ] **Step 3: Implement comparison and review gates**

Store page decisions separately per attempt. Never mutate a prior attempt. Lock passed page path plus candidate SHA-256. If a later run presents a different hash for a locked page, surface `locked_page_hash_drift` and block release.

- [ ] **Step 4: Run unit and browser tests**

```powershell
npm test -- tests/outputReview.test.tsx
npm run test:e2e -- e2e/output-review.spec.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/widget src/server/projectSession.ts tests/outputReview.test.tsx e2e/output-review.spec.ts
git commit -m "feat: require human output review and targeted revisions"
```

---

### Task 10: Add hash-valid project-index reuse without creating another mode

**Files:**
- Create: `skills/repair-comic-continuity/scripts/project_index_cache.py`
- Create: `skills/repair-comic-continuity/tests/test_project_index_cache.py`
- Modify: `skills/repair-comic-continuity/scripts/prepare_run_workspace.py`
- Modify: `skills/repair-comic-continuity/scripts/closed_loop_controller.py`
- Modify: `skills/repair-comic-continuity/SKILL.md`
- Modify: `skills/repair-comic-continuity/references/scene-cluster-pipeline.md`

- [ ] **Step 1: Write failing cache tests**

Test a cache key that includes novel hash, ordered input hashes, reference hashes, pipeline version, and relevant rule-registry revision. Assert full reuse when all match, affected-cluster invalidation after one page changes, full alignment invalidation after novel changes, and no mode name other than `human_visual_auto_text` or `automatic`.

- [ ] **Step 2: Verify failure**

Run: `python '.\skills\repair-comic-continuity\tests\test_project_index_cache.py'`

Expected: FAIL because the cache module does not exist.

- [ ] **Step 3: Implement content-addressed cache receipts**

Cache immutable index artifacts outside production candidates. Every reuse receipt must list input artifact hashes and reused artifact hashes. `prepare_run_workspace.py` still creates a clean run root and copies only validated immutable evidence; it never audits directly inside a previous run.

- [ ] **Step 4: Run focused regression**

```powershell
python '.\skills\repair-comic-continuity\tests\test_project_index_cache.py'
python '.\skills\repair-comic-continuity\tests\test_prepare_run_workspace.py'
python '.\skills\repair-comic-continuity\tests\test_closed_loop_controller.py'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add skills/repair-comic-continuity
git commit -m "feat: reuse hash-valid project evidence incrementally"
```

---

### Task 11: Enforce textless redraw and add GPT Image 2 cleanup fallback

**Files:**
- Create: `skills/repair-comic-continuity/scripts/image2_text_cleanup.py`
- Create: `skills/repair-comic-continuity/tests/test_image2_text_cleanup.py`
- Modify: `skills/repair-comic-continuity/scripts/text_engine_pipeline.py`
- Modify: `skills/repair-comic-continuity/scripts/candidate_preflight.py`
- Modify: `skills/repair-comic-continuity/scripts/prompt_compiler.py`
- Modify: `skills/repair-comic-continuity/references/text-engine.md`
- Modify: `skills/repair-comic-continuity/references/qa-checklist.md`

- [ ] **Step 1: Write failing routing and preflight tests**

Test exact routes: `deterministic_fill`, `lama`, `gpt-image-2`, and `evidence_blocked`. The GPT Image 2 request must bind source page, source hash, tight mask, mask hash, text-block IDs, expected dimensions, immutable outside region, and the instruction to remove ordinary text only. Reject candidate size drift, residual glyph evidence, outside-mask changes beyond the reviewed feather band, panel-border damage, and attempts to render Chinese.

- [ ] **Step 2: Verify failure**

```powershell
python '.\skills\repair-comic-continuity\tests\test_image2_text_cleanup.py'
```

Expected: FAIL because the fallback contract does not exist.

- [ ] **Step 3: Implement request generation and candidate import, not a direct API client**

`image2_text_cleanup.py` must emit a hash-bound request for the coordinator. Codex invokes the `gpt-image-2` image-edit capability and supplies the resulting local candidate path back to the importer. The script must never read an API key or call the OpenAI API itself. `candidate_preflight.py` validates the imported candidate before deterministic typesetting.

For visual redraw pages, `prompt_compiler.py` must continue to demand a complete textless page before any text rendering. If visual revision occurs after typesetting, discard that typeset candidate and restart from the sealed source text/style evidence.

- [ ] **Step 4: Run text and candidate regressions**

```powershell
python '.\skills\repair-comic-continuity\tests\test_image2_text_cleanup.py'
python '.\skills\repair-comic-continuity\tests\test_candidate_preflight.py'
python '.\skills\repair-comic-continuity\tests\test_embedded_text_engine.py'
python '.\skills\repair-comic-continuity\tests\test_prompt_compiler.py'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add skills/repair-comic-continuity
git commit -m "feat: route unsafe text cleanup through gpt image 2"
```

---

### Task 12: Project real Skill progress and enforce exact output/locked-page release

**Files:**
- Create: `src/server/progressReader.ts`
- Test: `tests/progressReader.test.ts`
- Modify: `skills/repair-comic-continuity/scripts/release_gate.py`
- Modify: `skills/repair-comic-continuity/scripts/validate_output.py`
- Modify: `skills/repair-comic-continuity/tests/test_release_gate.py`

- [ ] **Step 1: Write failing progress and release tests**

`progressReader` must derive counts only from controller receipts, task registries, text-engine manifests, review decisions, and blockers. Release tests must reject unreviewed outputs, changed locked pages, filename case drift, nested-path flattening, missing revision feedback, and a typeset page whose textless visual candidate never passed.

- [ ] **Step 2: Verify failure**

```powershell
npm test -- tests/progressReader.test.ts
python '.\skills\repair-comic-continuity\tests\test_release_gate.py'
```

Expected: both fail on the new requirements.

- [ ] **Step 3: Implement read-only projection and release bindings**

Return only these user-facing counters: visual work, text-engine work, pending human review, blockers, and estimated remaining generation calls. Keep detailed artifact paths behind `高级信息`. Extend release binding with the current human-review manifest and locked-page hash registry.

- [ ] **Step 4: Run tests**

```powershell
npm test -- tests/progressReader.test.ts
python '.\skills\repair-comic-continuity\tests\test_release_gate.py'
python '.\skills\repair-comic-continuity\tests\test_validate_audit.py'
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/server/progressReader.ts tests/progressReader.test.ts skills/repair-comic-continuity
git commit -m "feat: expose real progress and lock reviewed outputs"
```

---

### Task 13: End-to-end acceptance, package validation, and signed release gate

**Files:**
- Create: `e2e/full-workflow.spec.ts`
- Create: `tests/fixtures/demo-project/`
- Create: `scripts/package_release.ps1`
- Create: `scripts/install_plugin.ps1`
- Create: `ACCEPTANCE_REPORT.md`
- Modify: `skills/repair-comic-continuity/scripts/pipeline_version.py`
- Modify: `skills/repair-comic-continuity/assets/release_certificate.json`

- [ ] **Step 1: Build a deterministic demo fixture and failing end-to-end test**

The fixture must cover human and automatic modes, two input pages, one correct page, one annotated visual page, two ordinary text blocks, one LaMa route, one mocked GPT Image 2 fallback, one missed-detection output annotation, one targeted revision, locked correct output, exact filenames, and one learned rule that remains project-scoped.

- [ ] **Step 2: Run the end-to-end test before final wiring**

Run: `npm run test:e2e -- e2e/full-workflow.spec.ts`

Expected: FAIL until every plugin/Skill handoff is connected.

- [ ] **Step 3: Complete only the missing integration wiring and package script**

`package_release.ps1` must:

```powershell
$ErrorActionPreference = 'Stop'
npm ci
npm run typecheck
npm test
npm run build
python -m unittest discover -s '.\skills\repair-comic-continuity\tests' -p 'test_*.py'
python 'C:\Users\骆阳\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py' .
python 'C:\Users\骆阳\.codex\skills\.system\skill-creator\scripts\quick_validate.py' '.\skills\repair-comic-continuity'
```

Then stage only runtime files, compute a SHA-256 inventory, create `repair-comic-continuity-workbench-1.0.0.zip`, and reopen the ZIP to verify every staged hash.

Stage the ZIP as a non-default local marketplace:

```text
comic-review-workbench-marketplace/
├─ marketplace.json
├─ install_plugin.ps1
└─ plugins/repair-comic-continuity-workbench/<validated plugin files>
```

Use this exact `marketplace.json` entry:

```json
{
  "name": "comic-review-workbench",
  "interface": {"displayName": "漫画审校工作台"},
  "plugins": [{
    "name": "repair-comic-continuity-workbench",
    "source": {"source": "local", "path": "./plugins/repair-comic-continuity-workbench"},
    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
    "category": "Productivity"
  }]
}
```

`install_plugin.ps1` must call commands rather than edit Codex config or marketplace JSON:

```powershell
$ErrorActionPreference = 'Stop'
$marketplaceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
codex plugin marketplace add $marketplaceRoot
codex plugin add 'repair-comic-continuity-workbench@comic-review-workbench'
Write-Host '安装完成。请新建一个 Codex 任务并输入：启动漫画审校工作台'
```

- [ ] **Step 4: Run complete verification and write acceptance evidence**

```powershell
npm run typecheck
npm test
npm run test:e2e
python -m unittest discover -s '.\skills\repair-comic-continuity\tests' -p 'test_*.py'
python '.\skills\repair-comic-continuity\scripts\validate_release_certificate.py' --root '.\skills\repair-comic-continuity' --json
powershell -ExecutionPolicy Bypass -File '.\scripts\package_release.ps1'
```

Expected: all tests pass, package hashes match, and the certificate is valid for the exact new control-plane hashes. If the signing key is unavailable, stop before changing `RELEASE_CERTIFICATE_ID`, leave the plugin non-production, and record `SIGNING_KEY_UNAVAILABLE` in `ACCEPTANCE_REPORT.md`; do not copy the V5.5 certificate or claim formal release.

- [ ] **Step 5: Commit and push the implementation branch**

```powershell
git add .
git commit -m "release: validate comic review workbench plugin v1"
git push -u origin agent/workbench-plugin-v1
```

Do not merge to the default branch until the user reviews the acceptance report and manually tests one complete project cluster.

---

## Final self-review checklist

- Every visible operation remains mode selection, pass/correct, circle-and-note, compare, or submit.
- `human_visual_auto_text` remains the only human visual mode.
- Text detection and rendering remain owned by the embedded text engine, not by Codex prose.
- Every visual redraw produces and passes a textless candidate before typesetting.
- LaMa failure routes to a validated GPT Image 2 request, never to a weak patch.
- First output review covers all pages in both modes; later review covers only revised pages.
- One submit button maps to initial or revision submission from state.
- Human misses and revision failures enter evidence-gated learning without immediate global promotion.
- Final outputs preserve the exact input-relative path set and page count.
- No task edits production images during planning, testing, or final read-only validation.

## Reference links

- GPT Image 2 model and image-edit endpoint: https://developers.openai.com/api/docs/models/gpt-image-2
- OpenAI Apps SDK examples and MCP App resources: https://developers.openai.com/resources
