import { randomUUID } from "node:crypto";

import type { NormalizedAnnotationShape } from "../shared/contracts";
import { assertSafeRelativePath } from "./projectInventory";

export interface RawAnnotationDraft {
  annotation_id?: string;
  page: string;
  source_sha256: string;
  note: string;
  shapes: NormalizedAnnotationShape[];
}

export interface RawAnnotationExport {
  schema_version: "comic-review-workbench-raw-annotations-v1";
  status: "raw_user_drafts";
  annotations: Array<{
    annotation_id: string;
    page: string;
    source_sha256: string;
    user_note: string;
    shapes: NormalizedAnnotationShape[];
  }>;
}

export function exportRawAnnotationDrafts(
  drafts: RawAnnotationDraft[],
): RawAnnotationExport {
  return {
    schema_version: "comic-review-workbench-raw-annotations-v1",
    status: "raw_user_drafts",
    annotations: drafts.map((draft) => {
      const page = assertSafeRelativePath(draft.page);
      const note = draft.note.trim();
      if (!note || draft.shapes.length === 0) {
        throw new Error(`Annotated page ${page} requires a note and at least one shape.`);
      }
      if (!/^[a-f0-9]{64}$/i.test(draft.source_sha256)) {
        throw new Error(`Annotated page ${page} has an invalid source SHA-256.`);
      }
      return {
        annotation_id: draft.annotation_id ?? randomUUID(),
        page,
        source_sha256: draft.source_sha256.toLocaleLowerCase(),
        user_note: note,
        shapes: draft.shapes,
      };
    }),
  };
}
