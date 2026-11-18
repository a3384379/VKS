export async function conversationAnswer<T>(
  response: Response,
  onDelta: (text: string) => void,
  signal: AbortSignal,
): Promise<T> {
  if (!response.headers.get("Content-Type")?.includes("text/event-stream")) {
    const value = await response.json();
    return (value.data ?? value) as T;
  }
  if (!response.body) throw new Error("服务未返回回答内容");
  const reader = response.body.getReader(),
    decoder = new TextDecoder();
  let buffer = "",
    size = 0;
  function event(raw: string): T | undefined {
    const lines = raw.replace(/\r\n/g, "\n").split("\n");
    const kind = lines
      .find((line) => line.startsWith("event:"))
      ?.slice(6)
      .trim();
    const data = lines
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    let value;
    try {
      value = JSON.parse(data);
    } catch {
      throw new Error("流式回答格式错误，回答未完成");
    }
    if (kind === "error")
      throw new Error(value.message || "本次回答未完成，请重试");
    if (kind === "delta" && typeof value.text === "string") onDelta(value.text);
    if (kind === "complete") {
      if (
        typeof value.explanation !== "string" ||
        !Array.isArray(value.sections) ||
        !Array.isArray(value.unknowns)
      )
        throw new Error("回答结果不完整，请重试");
      return value as T;
    }
  }
  try {
    while (true) {
      if (signal.aborted) throw new DOMException("Aborted", "AbortError");
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 2 * 1024 * 1024) throw new Error("流式回答超过大小限制");
      buffer += decoder.decode(value, { stream: true });
      let match;
      while ((match = /\r?\n\r?\n/.exec(buffer))) {
        const result = event(buffer.slice(0, match.index));
        buffer = buffer.slice(match.index + match[0].length);
        if (result !== undefined) return result;
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) {
      const result = event(buffer);
      if (result !== undefined) return result;
    }
    throw new Error("连接提前结束，回答未完成，可以重试");
  } finally {
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
