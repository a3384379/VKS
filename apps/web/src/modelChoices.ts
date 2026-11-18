/** Compact choices derived only from the provider's actual IDs. */
const nonAnswer =
  /audio|realtime|embedding|rerank|whisper|(?:^|[-_/])tts(?:$|[-_/])|dall-e|moderation|transcrib|(?:^|[-_/])image(?:$|[-_/])|(?:^|[-_/])sora(?:$|[-_/])|^gpt-live|^text-search|^code-search/i;
const snapshot =
  /-(?:20\d{2}-\d{2}-\d{2}|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))$/;
// Public lifecycle notices for legacy families, including their dated aliases.
// https://developers.openai.com/api/docs/deprecations
const deprecatedOpenAIAnswer = new Set([
  "chatgpt-4o-latest",
  "gpt-4o",
  "gpt-4o-mini",
  "gpt-4.1",
  "gpt-4.1-mini",
  "gpt-4.1-nano",
  "gpt-4",
  "gpt-4-completions",
  "gpt-4-32k",
  "gpt-4-turbo",
  "gpt-4-turbo-preview",
  "gpt-4-turbo-completions",
  "gpt-4-turbo-preview-completions",
  "gpt-4-0125-preview",
  "gpt-4-1106-preview",
  "gpt-4-vision-preview",
  "gpt-3.5-turbo",
  "gpt-3.5-turbo-completions",
  "gpt-3.5-turbo-16k",
  "gpt-5",
  "gpt-5-mini",
  "o1",
  "o1-pro",
  "o3-pro",
]);

export function answerModelRestriction(id: string, endpoint: string): string {
  // A proxy can expose the same ID with a different deployment or lifecycle.
  if (!/^https:\/\/api\.openai\.com(?::443)?(?:\/|$)/i.test(endpoint.trim()))
    return "";
  if (nonAnswer.test(id)) return "不属于文本回答模型";
  if (deprecatedOpenAIAnswer.has(id.toLowerCase().replace(snapshot, "")))
    return "官方已弃用或停用";
  return "";
}

export function answerModelCandidates<T extends { id: string }>(
  models: T[],
  endpoint: string,
): T[] {
  return models.filter(({ id }) => !answerModelRestriction(id, endpoint));
}
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
