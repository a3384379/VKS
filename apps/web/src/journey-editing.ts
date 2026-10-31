import type { JourneyDSL } from "./journeys";
export type MergeConflict = {
  path: string;
  base: unknown;
  local: unknown;
  remote: unknown;
};
export function mergeJourney(
  base: JourneyDSL,
  local: JourneyDSL,
  remote: JourneyDSL,
  choices: Record<string, "local" | "remote"> = {},
) {
  if (base.key !== local.key || base.key !== remote.key)
    throw new Error("只能合并同一个逻辑流程");
  const conflicts: MergeConflict[] = [];
  const equal = (a: unknown, b: unknown) =>
    JSON.stringify(a) === JSON.stringify(b);
  function field(path: string, b: unknown, l: unknown, r: unknown): unknown {
    if (equal(l, r)) return l;
    if (equal(l, b)) return r;
    if (equal(r, b)) return l;
    conflicts.push({ path, base: b, local: l, remote: r });
    return choices[path] === "remote" ? r : l;
  }
  function collection<T extends { key: string }>(
    name: string,
    b: T[],
    l: T[],
    r: T[],
  ): T[] {
    const maps = [b, l, r].map(
      (rows) => new Map(rows.map((row) => [row.key, row])),
    );
    const order = [...new Set([...b, ...l, ...r].map((row) => row.key))];
    const result: T[] = [];
    for (const key of order) {
      const [before, ours, theirs] = maps.map((map) => map.get(key));
      if (!before || !ours || !theirs) {
        const row = field(`${name}.${key}`, before, ours, theirs) as
          | T
          | undefined;
        if (row) result.push(row);
        continue;
      }
      const value: Record<string, unknown> = { key };
      for (const property of new Set([
        ...Object.keys(before),
        ...Object.keys(ours),
        ...Object.keys(theirs),
      ])) {
        if (property === "key") continue;
        // Bindings and replay references stay atomic; never mix their revision/identity fields.
        const v = field(
          `${name}.${key}.${property}`,
          (before as Record<string, unknown>)[property],
          (ours as Record<string, unknown>)[property],
          (theirs as Record<string, unknown>)[property],
        );
        if (v !== undefined) value[property] = v;
      }
      result.push(value as T);
    }
    return result;
  }
  const merged: JourneyDSL = {
    schema_version: "2.0",
    key: base.key,
    title: field("title", base.title, local.title, remote.title) as string,
    entry: field("entry", base.entry, local.entry, remote.entry) as string,
    nodes: collection("nodes", base.nodes, local.nodes, remote.nodes),
    edges: collection("edges", base.edges, local.edges, remote.edges),
  };
  const semantics = field(
    "semantics",
    base.semantics,
    local.semantics,
    remote.semantics,
  ) as JourneyDSL["semantics"];
  if (semantics !== undefined) merged.semantics = semantics;
  return { merged, conflicts };
}

export function arrangeJourney(dsl: JourneyDSL): JourneyDSL {
  const nodes = new Map(dsl.nodes.map((node) => [node.key, node]));
  if (nodes.size !== dsl.nodes.length)
    throw new Error("节点 key 重复，无法自动排列");
  const incoming = new Map(dsl.nodes.map((node) => [node.key, 0]));
  const outgoing = new Map(dsl.nodes.map((node) => [node.key, [] as string[]]));
  for (const edge of dsl.edges) {
    if (!nodes.has(edge.source) || !nodes.has(edge.target))
      throw new Error("先修复悬空连接，再自动排列");
    incoming.set(edge.target, incoming.get(edge.target)! + 1);
    outgoing.get(edge.source)!.push(edge.target);
  }
  const queue = [...incoming]
      .filter(([, count]) => count === 0)
      .map(([key]) => key),
    ordered: string[] = [];
  while (queue.length) {
    const key = queue.shift()!;
    ordered.push(key);
    for (const next of outgoing.get(key)!) {
      incoming.set(next, incoming.get(next)! - 1);
      if (incoming.get(next) === 0) queue.push(next);
    }
  }
  if (ordered.length !== nodes.size)
    throw new Error("流程存在循环，无法自动排列");
  const positions = new Map(
    ordered.map((key, i) => [
      key,
      { x: 40 + (i % 12) * 260, y: 40 + Math.floor(i / 12) * 150 },
    ]),
  );
  return {
    ...dsl,
    nodes: dsl.nodes.map((node) => ({ ...node, ...positions.get(node.key)! })),
  };
}
