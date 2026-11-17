import { diffLines } from "diff";

export type ComparisonLine = {
  before?: string;
  after?: string;
  oldLine?: number;
  newLine?: number;
  changed: boolean;
};
const lines = (text: string) => {
  if (!text) return [];
  const result = text.split("\n");
  if (text.endsWith("\n")) result.pop();
  return result;
};

export function associationDiff(before: string, after: string) {
  const exact = diffLines(before, after, { timeout: 80, maxEditLength: 10000 });
  // Bound CPU work for unrelated long documents; keep every line available.
  const changes = exact ?? [
    { value: before, removed: true, added: false },
    { value: after, removed: false, added: true },
  ];
  const result: ComparisonLine[] = [];
  let oldLine = 1,
    newLine = 1,
    added = 0,
    removed = 0;
  for (let index = 0; index < changes.length; index++) {
    const change = changes[index];
    if (!change.added && !change.removed) {
      for (const text of lines(change.value)) {
        result.push({
          before: text,
          after: text,
          oldLine: oldLine++,
          newLine: newLine++,
          changed: false,
        });
      }
      continue;
    }
    const oldLines = change.removed ? lines(change.value) : [];
    const newLines = change.added ? lines(change.value) : [];
    if (change.removed && changes[index + 1]?.added) {
      newLines.push(...lines(changes[++index].value));
    }
    removed += oldLines.length;
    added += newLines.length;
    for (let row = 0; row < Math.max(oldLines.length, newLines.length); row++) {
      result.push({
        before: oldLines[row],
        after: newLines[row],
        oldLine: row < oldLines.length ? oldLine++ : undefined,
        newLine: row < newLines.length ? newLine++ : undefined,
        changed: true,
      });
    }
  }
  return { lines: result, added, removed, coarse: !exact };
}

export function comparisonGroups(rows: ComparisonLine[]) {
  const groups: { start: number; end: number; collapsed: boolean }[] = [];
  let index = 0;
  while (index < rows.length) {
    if (rows[index].changed) {
      groups.push({ start: index, end: ++index, collapsed: false });
      continue;
    }
    const start = index;
    while (index < rows.length && !rows[index].changed) index++;
    const end = index;
    const from = start ? Math.min(start + 3, end) : start;
    const to = end < rows.length ? Math.max(from, end - 3) : end;
    for (let i = start; i < from; i++)
      groups.push({ start: i, end: i + 1, collapsed: false });
    if (to > from) groups.push({ start: from, end: to, collapsed: true });
    for (let i = to; i < end; i++)
      groups.push({ start: i, end: i + 1, collapsed: false });
  }
  return groups;
}
