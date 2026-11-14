/** Compact choices derived only from the provider's actual IDs. */
const nonAnswer =
  /audio|realtime|embedding|rerank|whisper|(?:^|[-_/])tts(?:$|[-_/])|dall-e|moderation|transcrib|(?:^|[-_/])image(?:$|[-_/])|(?:^|[-_/])sora(?:$|[-_/])/i;
const snapshot =
  /-(?:20\d{2}-\d{2}-\d{2}|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))$/;
function family(id: string) {
  return (
    id
      .match(/gpt|claude|gemini|qwen|deepseek|^o\d/i)?.[0]
      .replace(/^o\d$/, "reasoning")
      .toLowerCase() || "other"
  );
}
function version(id: string) {
  return (
    id
      .replace(snapshot, "")
      .match(/\d+(?:\.\d+)?/)?.[0]
      .split(".")
      .map(Number) || []
  );
}
function newest(a: { id: string }, b: { id: string }) {
  const av = version(a.id),
    bv = version(b.id);
  return (
    (bv[0] || 0) - (av[0] || 0) ||
    (bv[1] || 0) - (av[1] || 0) ||
    a.id.localeCompare(b.id, undefined, { numeric: true })
  );
}
export function preferredAnswerModels<T extends { id: string }>(
  models: T[],
  limit = 6,
  provider?: string,
): T[] {
  const aliases = new Map<string, T>();
  for (const model of models) {
    if (nonAnswer.test(model.id)) continue;
    const base = model.id.replace(snapshot, "");
    const prior = aliases.get(base);
    if (
      !prior ||
      model.id === base ||
      (prior.id !== base && model.id > prior.id)
    )
      aliases.set(base, model);
  }
  const families = new Map<string, T[]>();
  for (const model of aliases.values()) {
    const key = family(model.id);
    families.set(key, [...(families.get(key) || []), model]);
  }
  const preferredFamily =
    provider === "openai"
      ? "gpt"
      : provider === "deepseek"
        ? "deepseek"
        : provider === "qwen"
          ? "qwen"
          : "";
  const groups = (
    preferredFamily && families.has(preferredFamily)
      ? [families.get(preferredFamily)!]
      : [...families.values()]
  ).map((rows) => {
    rows.sort(newest);
    const generations = [
      ...new Set(rows.map((r) => version(r.id).join(".")).filter(Boolean)),
    ].slice(0, 2);
    return rows.filter(
      (r) =>
        !version(r.id).length || generations.includes(version(r.id).join(".")),
    );
  });
  const output: T[] = [];
  while (output.length < limit && groups.some((g) => g.length)) {
    for (const group of groups) {
      if (group.length && output.length < limit) output.push(group.shift()!);
    }
  }
  return output;
}
