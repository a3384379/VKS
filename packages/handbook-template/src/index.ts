/** Content-free handbook framework. Project data is supplied by the host. */
export const handbookTemplateManifest = Object.freeze({
  id: "handbook",
  version: "1.0.0",
  name: "知识手册",
  description: "分类目录、单篇阅读、全文搜索和上下篇导航",
  capabilities: Object.freeze([
    "navigation",
    "search",
    "markdown",
    "print",
    "ai",
    "update",
  ]),
  content: Object.freeze({
    documents: Object.freeze([]),
    categories: Object.freeze([]),
    vectors: Object.freeze([]),
  }),
});

export type HandbookConfig = {
  title: string;
  subtitle: string;
  introduction: string;
  accent: string;
  brand: string;
};
export const defaultHandbookConfig: Readonly<HandbookConfig> = Object.freeze({
  title: "项目知识手册",
  subtitle: "PROJECT KNOWLEDGE BASE",
  introduction:
    "选择上方分类，在左侧找到文章。一次专注一篇，随时通过搜索、引用和目录继续阅读。",
  accent: "#087e76",
  brand: "知",
});

export function handbookConfig(
  projectName: string,
  input: Partial<HandbookConfig> = {},
): HandbookConfig {
  const text = (value: unknown, fallback: string, max: number) =>
    typeof value === "string" && value.trim()
      ? value.trim().slice(0, max)
      : fallback;
  const title = text(
    input.title,
    projectName.trim() || defaultHandbookConfig.title,
    160,
  );
  return {
    title,
    subtitle: text(input.subtitle, defaultHandbookConfig.subtitle, 120),
    introduction: text(
      input.introduction,
      defaultHandbookConfig.introduction,
      1000,
    ),
    accent: /^#[a-f\d]{6}$/i.test(input.accent || "")
      ? input.accent!
      : defaultHandbookConfig.accent,
    brand: text(input.brand, [...title][0] || defaultHandbookConfig.brand, 2),
  };
}

export type HandbookDocument = {
  id: string;
  revision_id: string;
  name: string;
  path: string;
  namespace?: string;
  package_id?: string | null;
};
export type HandbookNode =
  | {
      kind: "document";
      id: string;
      name: string;
      path: string;
      entity_id?: string;
    }
  | {
      kind: "folder";
      id: string;
      name: string;
      path: string;
      children: HandbookNode[];
    };
export type HandbookCategory = {
  id: string;
  name: string;
  path: string;
  children: HandbookNode[];
};
export type HandbookPackage = {
  id: string;
  name: string;
  categories: HandbookCategory[];
};
export type HandbookNavigation = { packages: HandbookPackage[] };
export type HandbookEntry = {
  document: Extract<HandbookNode, { kind: "document" }>;
  package: HandbookPackage;
  category: HandbookCategory;
  folders: Extract<HandbookNode, { kind: "folder" }>[];
};
export type HandbookSection = {
  id: string;
  revision_id: string;
  name: string;
  content: string;
  heading_level?: number;
};
export type HandbookArticle = {
  revision_id: string;
  name: string;
  sections: HandbookSection[];
  content?: string;
};
export type HandbookHeading = {
  section: HandbookSection;
  children: HandbookHeading[];
};

/** Prune against the host's authorized, selected revision list before labels. */
export function handbookNavigation(
  navigation: HandbookNavigation | undefined,
  documents: HandbookDocument[],
): HandbookNavigation {
  const known = new Map(documents.map((doc) => [doc.revision_id, doc]));
  const retained = new Set<string>();
  const nodes = (items: HandbookNode[]): HandbookNode[] =>
    items.flatMap<HandbookNode>((node) => {
      if (node.kind === "document") {
        const doc = known.get(node.id);
        if (!doc) return [];
        retained.add(node.id);
        return [{ ...node, name: doc.name, path: doc.path || node.path }];
      }
      const children = nodes(node.children);
      return children.length ? [{ ...node, children }] : [];
    });
  const packages = (navigation?.packages || [])
    .map((pack) => ({
      ...pack,
      categories: pack.categories
        .map((category) => ({
          ...category,
          children: nodes(category.children),
        }))
        .filter((category) => category.children.length),
    }))
    .filter((pack) => pack.categories.length);
  const extra = new Map<string, HandbookDocument[]>();
  for (const doc of documents)
    if (!retained.has(doc.revision_id)) {
      const key = doc.package_id || doc.namespace || "project-documents";
      extra.set(key, [...(extra.get(key) || []), doc]);
    }
  for (const [id, docs] of extra) {
    const existing = packages.find((pack) => pack.id === id);
    const category = {
      id: id + ":all",
      name: "全部文档",
      path: "",
      children: docs.map((doc) => ({
        kind: "document" as const,
        id: doc.revision_id,
        entity_id: doc.id,
        name: doc.name,
        path: doc.path,
      })),
    };
    if (existing) existing.categories.push(category);
    else packages.push({ id, name: "项目文档", categories: [category] });
  }
  return { packages };
}

export function handbookEntries(tree: HandbookNavigation): HandbookEntry[] {
  const entries: HandbookEntry[] = [];
  for (const pack of tree.packages)
    for (const category of pack.categories) {
      const visit = (
        nodes: HandbookNode[],
        folders: Extract<HandbookNode, { kind: "folder" }>[],
      ) => {
        for (const node of nodes)
          if (node.kind === "document")
            entries.push({ document: node, package: pack, category, folders });
          else visit(node.children, [...folders, node]);
      };
      visit(category.children, []);
    }
  return entries;
}

/** Previous/next never crosses a logical source package. */
export function handbookNeighbors(
  entries: HandbookEntry[],
  documentId: string,
) {
  const current = entries.find((entry) => entry.document.id === documentId);
  const order = current
    ? [
        ...new Map(
          entries
            .filter((entry) => entry.package.id === current.package.id)
            .map((entry) => [entry.document.id, entry]),
        ).values(),
      ]
    : [];
  const index = order.findIndex((entry) => entry.document.id === documentId);
  return {
    previous: index > 0 ? order[index - 1] : undefined,
    next: index >= 0 ? order[index + 1] : undefined,
  };
}

export function handbookOutline(article: HandbookArticle): HandbookHeading[] {
  const roots: HandbookHeading[] = [];
  const stack: { node: HandbookHeading; level: number }[] = [];
  let first = true;
  for (const section of article.sections) {
    if (section.heading_level === 0) continue;
    const level =
      section.heading_level ||
      /^ {0,3}(#{1,6})\s/.exec(section.content)?.[1].length ||
      1;
    if (first && level === 1 && section.name === article.name) {
      first = false;
      continue;
    }
    first = false;
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    const node = { section, children: [] };
    (stack.length ? stack[stack.length - 1].node.children : roots).push(node);
    stack.push({ node, level });
  }
  return roots;
}

export function handbookSearchSnippet(
  text: string,
  query: string,
  length = 180,
) {
  const plain = text.replace(/\s+/g, " ").trim();
  const index = plain
    .toLocaleLowerCase()
    .indexOf(query.trim().toLocaleLowerCase());
  if (index < 0) return "";
  const start = Math.max(0, index - 45),
    end = Math.min(plain.length, start + length);
  return (
    (start ? "…" : "") +
    plain.slice(start, end) +
    (end < plain.length ? "…" : "")
  );
}
