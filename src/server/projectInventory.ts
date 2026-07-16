import { createHash } from "node:crypto";
import {
  existsSync,
  readFileSync,
  readdirSync,
  statSync,
} from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

const IMAGE_EXTENSIONS = new Set([
  ".bmp",
  ".gif",
  ".jpeg",
  ".jpg",
  ".png",
  ".tif",
  ".tiff",
  ".webp",
]);

const naturalCollator = new Intl.Collator("zh-CN", {
  numeric: true,
  sensitivity: "base",
});

export interface InventoryFile {
  path: string;
  sha256: string;
  bytes: number;
}

export interface ProjectInventory {
  schema_version: "comic-review-workbench-inventory-v1";
  project_root: string;
  input_root: string;
  pages: InventoryFile[];
  novels: InventoryFile[];
  references: InventoryFile[];
  sealed_at: string;
}

export function sha256File(path: string): string {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

export function assertSafeRelativePath(path: string): string {
  if (!path || isAbsolute(path) || /^[a-zA-Z]:/.test(path)) {
    throw new Error(`Expected a safe relative path, received: ${path}`);
  }
  const normalized = path.replaceAll("\\", "/");
  const segments = normalized.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    throw new Error(`Expected a safe relative path, received: ${path}`);
  }
  return normalized;
}

export function resolveInside(root: string, path: string): string {
  const safe = assertSafeRelativePath(path);
  const resolvedRoot = resolve(root);
  const target = resolve(resolvedRoot, ...safe.split("/"));
  const rootPrefix = `${resolvedRoot}${sep}`.toLocaleLowerCase();
  if (!target.toLocaleLowerCase().startsWith(rootPrefix)) {
    throw new Error(`Expected a safe relative path inside ${root}: ${path}`);
  }
  return target;
}

export function inventoryProject(projectRoot: string): ProjectInventory {
  const resolvedRoot = resolve(projectRoot);
  const inputRoot = resolve(resolvedRoot, "输入");
  if (!existsSync(inputRoot) || !statSync(inputRoot).isDirectory()) {
    throw new Error(`Project input directory is missing: ${inputRoot}`);
  }

  const pages = listFiles(inputRoot, (path) =>
    IMAGE_EXTENSIONS.has(extension(path)),
  );
  if (pages.length === 0) {
    throw new Error(`Project input directory contains no supported images: ${inputRoot}`);
  }
  const novels = listFiles(resolvedRoot, (path, absolutePath) => {
    const topLevel = !relative(resolvedRoot, absolutePath).includes(sep);
    return topLevel && extension(path) === ".txt";
  });
  const referenceRoot = resolve(resolvedRoot, "人物参考图");
  const references = existsSync(referenceRoot)
    ? listFiles(referenceRoot, (path) => IMAGE_EXTENSIONS.has(extension(path)))
    : [];

  return {
    schema_version: "comic-review-workbench-inventory-v1",
    project_root: resolvedRoot,
    input_root: inputRoot,
    pages,
    novels,
    references,
    sealed_at: new Date().toISOString(),
  };
}

function listFiles(
  root: string,
  include: (relativePath: string, absolutePath: string) => boolean,
): InventoryFile[] {
  const found: string[] = [];
  const visit = (directory: string): void => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const absolutePath = resolve(directory, entry.name);
      if (entry.isDirectory()) {
        visit(absolutePath);
      } else if (entry.isFile()) {
        const relativePath = relative(root, absolutePath).replaceAll("\\", "/");
        if (include(relativePath, absolutePath)) {
          found.push(relativePath);
        }
      }
    }
  };
  visit(root);
  return found.sort(naturalCollator.compare).map((path) => {
    const absolutePath = resolveInside(root, path);
    return {
      path,
      sha256: sha256File(absolutePath),
      bytes: statSync(absolutePath).size,
    };
  });
}

function extension(path: string): string {
  const index = path.lastIndexOf(".");
  return index === -1 ? "" : path.slice(index).toLocaleLowerCase();
}
