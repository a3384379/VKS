export type ModuleBinding = {
  id: string;
  package_id: string;
  scope?: string;
  target: {
    route: string;
    frame_path?: string[];
    selector: string;
    label: string;
  };
};
export type ModulePage = {
  package_id: string;
  route: string;
  label: string;
  frame_path?: string[];
};
export type BindingModuleNode = {
  id: string;
  label: string;
  path: string[];
  bindingIds: Set<string>;
  children: BindingModuleNode[];
};
export const bindingPageKey = (
  binding: Pick<ModuleBinding, "package_id" | "target">,
) => JSON.stringify([binding.package_id, binding.target.route]);
const cleanTitle = (name: string) => name.split(" · ")[0].trim();
const decode = (value: string) => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

/** Build only from readable bindings; metadata supplies names, never extra rows. */
export function bindingModuleTree(
  bindings: ModuleBinding[],
  packages: { id: string; name: string }[] = [],
  pages: ModulePage[] = [],
) {
  const pageNames = new Map<string, { name: string; depth: number }>();
  const remember = (key: string, label: string, depth: number) => {
    const name = cleanTitle(label),
      old = pageNames.get(key);
    if (name && (!old || depth < old.depth))
      pageNames.set(key, { name, depth });
  };
  for (const page of pages)
    remember(
      JSON.stringify([page.package_id, page.route]),
      page.label,
      (page.frame_path || []).length,
    );
  for (const binding of bindings)
    if (binding.scope === "module" || binding.target.selector === "body")
      remember(
        bindingPageKey(binding),
        binding.target.label,
        (binding.target.frame_path || []).length,
      );
  const packageNames = new Map(packages.map((p) => [p.id, p.name]));
  const hasDirectories = new Set(
    bindings
      .filter((b) => {
        const parts = b.target.route
          .split(/[?#]/)[0]
          .split("/")
          .filter(Boolean);
        return parts.length > (parts[0] === "actual" ? 2 : 1);
      })
      .map((b) => b.package_id),
  );
  const roots: BindingModuleNode[] = [],
    nodes = new Map<string, BindingModuleNode>(),
    byPage = new Map<string, BindingModuleNode>();
  for (const binding of bindings) {
    const route = binding.target.route.split(/[?#]/)[0];
    const parts = route.split("/").filter(Boolean).map(decode);
    // The offline adapter's /actual prefix is transport, not a business module.
    if (parts[0] === "actual") parts.shift();
    const directories = parts.slice(0, -1);
    const names = directories.length
      ? directories
      : [
          hasDirectories.has(binding.package_id)
            ? "其他页面"
            : packageNames.get(binding.package_id) || "其他页面",
        ];
    let children = roots;
    const ancestors: BindingModuleNode[] = [];
    for (let i = 0; i < names.length; i++) {
      const id = JSON.stringify([
        "module",
        binding.package_id,
        names.slice(0, i + 1),
      ]);
      let node = nodes.get(id);
      if (!node) {
        node = {
          id,
          label: names[i],
          path: names.slice(0, i + 1),
          bindingIds: new Set(),
          children: [],
        };
        nodes.set(id, node);
        children.push(node);
      }
      ancestors.push(node);
      children = node.children;
    }
    const pageId = bindingPageKey(binding);
    let page = byPage.get(pageId);
    if (!page) {
      const fallback = parts.at(-1)?.replace(/\.[a-z0-9]+$/i, "") || "首页";
      const label = pageNames.get(pageId)?.name || fallback;
      page = {
        id: JSON.stringify(["page", binding.package_id, binding.target.route]),
        label,
        path: [...names, label],
        bindingIds: new Set(),
        children: [],
      };
      byPage.set(pageId, page);
      nodes.set(page.id, page);
      children.push(page);
    }
    for (const node of [...ancestors, page]) node.bindingIds.add(binding.id);
  }
  // Keep equal labels in different packages distinct and identifiable.
  const rootCounts = new Map<string, number>();
  for (const node of roots)
    rootCounts.set(node.label, (rootCounts.get(node.label) || 0) + 1);
  for (const node of roots)
    if (rootCounts.get(node.label)! > 1) {
      const packageId = JSON.parse(node.id)[1];
      node.label += ` · ${packageNames.get(packageId) || packageId.slice(0, 8)}`;
      const qualify = (current: BindingModuleNode) => {
        current.path[0] = node.label;
        current.children.forEach(qualify);
      };
      qualify(node);
    }
  const sort = (rows: BindingModuleNode[]) => {
    rows.sort((a, b) => a.label.localeCompare(b.label, "zh-CN"));
    for (const node of rows) sort(node.children);
  };
  sort(roots);
  return { roots, nodes, byPage };
}
