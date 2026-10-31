export function layeredMap(
  keys: string[],
  edges: { from_revision_id: string; to_revision_id: string }[],
  seed: string,
) {
  const allowed = new Set(keys),
    layers = new Map<string, number>([[seed, 0]]),
    queue = [seed];
  const adjacent = new Map(keys.map((key) => [key, new Set<string>()]));
  for (const edge of edges) {
    if (
      allowed.has(edge.from_revision_id) &&
      allowed.has(edge.to_revision_id)
    ) {
      adjacent.get(edge.from_revision_id)!.add(edge.to_revision_id);
      adjacent.get(edge.to_revision_id)!.add(edge.from_revision_id);
    }
  }
  while (queue.length) {
    const key = queue.shift()!;
    for (const next of [...(adjacent.get(key) || [])].sort())
      if (!layers.has(next)) {
        layers.set(next, layers.get(key)! + 1);
        queue.push(next);
      }
  }
  const groups = new Map<number, string[]>();
  for (const key of keys) {
    const level = layers.get(key) ?? 0;
    groups.set(level, [...(groups.get(level) || []), key]);
  }
  const height = Math.max(
    660,
    ...[...groups.values()].map((rows) => rows.length * 116 + 140),
  );
  const width = Math.max(900, (Math.max(0, ...groups.keys()) + 1) * 280 + 120);
  const points = new Map<string, { x: number; y: number }>();
  for (const [level, rows] of groups)
    rows.forEach((key, i) =>
      points.set(key, {
        x: 150 + level * 280,
        y: (height - (rows.length - 1) * 116) / 2 + i * 116,
      }),
    );
  return { points, width, height };
}
