import "../../../packages/assistant-runtime/src/composer.css";
import { memo, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  FileText,
  LoaderCircle,
  MessageCircle,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { api, apiResponse, mutation } from "../../web/src/api";
import { conversationAnswer } from "./chatStream";
import {
  MarkdownReadingContext,
  RichMarkdown,
  type MarkdownReference,
} from "./RichMarkdown";
import type { Target } from "./runtime";
import { useDisplayedText } from "./chatPreferences";

type Context = {
  target: Target;
  context: {
    scope?: "module" | "control";
    page_title: string;
    section_title: string;
    nearby_labels: string[];
  };
};
type Operation = {
  kind: string;
  title: string;
  document_id?: string;
  revision_id?: string;
  job_id?: string;
};
type Answer = {
  operations?: Operation[];
  source_fingerprint?: string;
  explanation: string;
  model_id: string;
  sections: MarkdownReference[];
  unknowns: string[];
  suggested_questions?: string[];
};
type Turn = {
  id: string;
  question: string;
  answer: Answer;
  incomplete?: boolean;
};
const historyPrefix = "vks.studio.prototype-chat:v1:";
function savedChat(key: string): { turns: Turn[]; draft: string } {
  try {
    const raw = localStorage.getItem(historyPrefix + key + ":turns") || "[]";
    if (raw.length > 8_000_000) return { turns: [], draft: "" };
    const turns: unknown = JSON.parse(raw);
    if (!Array.isArray(turns)) return { turns: [], draft: "" };
    const valid = turns
      .slice(-20)
      .filter(
        (turn): turn is Turn =>
          typeof turn?.id === "string" &&
          typeof turn.question === "string" &&
          typeof turn.answer?.explanation === "string" &&
          typeof turn.answer.model_id === "string" &&
          Array.isArray(turn.answer.unknowns) &&
          turn.answer.unknowns.every((v: unknown) => typeof v === "string") &&
          Array.isArray(turn.answer.sections) &&
          turn.answer.sections.every(
            (ref: MarkdownReference) =>
              ref &&
              [
                ref.revision_id,
                ref.document_revision_id,
                ref.document_name,
                ref.name,
                ref.content,
              ].every((v) => typeof v === "string"),
          ) &&
          (!turn.answer.operations ||
            (Array.isArray(turn.answer.operations) &&
              turn.answer.operations.every(
                (operation: Operation) =>
                  operation &&
                  typeof operation.kind === "string" &&
                  typeof operation.title === "string",
              ))) &&
          (!turn.answer.suggested_questions ||
            (Array.isArray(turn.answer.suggested_questions) &&
              turn.answer.suggested_questions.every(
                (v: unknown) => typeof v === "string",
              ))),
      );
    return {
      turns: valid,
      draft: (localStorage.getItem(historyPrefix + key + ":draft") || "").slice(
        0,
        8000,
      ),
    };
  } catch {
    return { turns: [], draft: "" };
  }
}
function unfinished(question: string, explanation: string): Turn {
  return {
    id: crypto.randomUUID(),
    question,
    incomplete: true,
    answer: { explanation, model_id: "", sections: [], unknowns: [] },
  };
}
export function useKnowledgeChat(
  key: string,
  prefix: string,
  baseline: { analysis_set_id?: string; release_id?: string },
  packageId: string,
  context: Context | null,
  document?: { revision_id?: string },
  sourceFingerprint = "",
  onUpdated?: (operations: Operation[]) => Promise<void>,
) {
  const identity = key;
  key = JSON.stringify([identity, sourceFingerprint]);
  const saved = useMemo(() => savedChat(key), [key]);
  const [storageError, setStorageError] = useState("");
  const ownedTransition = useRef<{
    identity: string;
    fingerprint: string;
  } | null>(null);
  const acceptsTransition =
    ownedTransition.current?.identity === identity &&
    ownedTransition.current.fingerprint === sourceFingerprint;
  const [state, setState] = useState<{
    key: string;
    turns: Turn[];
    draft: string;
    pending: string;
    preview: string;
    error: string;
  }>({ key, ...saved, pending: "", preview: "", error: "" });
  const current =
    state.key === key
      ? state
      : acceptsTransition
        ? {
            ...state,
            key,
            turns: state.turns.slice(-1),
            pending: "",
            preview: "",
            draft: "",
          }
        : { key, ...saved, pending: "", preview: "", error: "" };
  const retry = useRef<{ key: string; question: string; id: string } | null>(
    null,
  );
  const activeKey = useRef(key),
    livePreview = useRef(""),
    serial = useRef(0),
    inFlight = useRef<{ key: string; sequence: number } | null>(null),
    controller = useRef<AbortController | null>(null);
  activeKey.current = key;
  useEffect(() => {
    serial.current++;
    inFlight.current = null;
    controller.current?.abort();
    livePreview.current = "";
    // An abandoned request must not reappear as a live pending turn if the user
    // later returns to the same control without interacting with another one.
    setState((previous) => ({
      key,
      turns: acceptsTransition ? previous.turns.slice(-1) : saved.turns,
      draft: acceptsTransition ? "" : saved.draft,
      pending: "",
      preview: "",
      error: "",
    }));
    ownedTransition.current = null;
    return () => {
      serial.current++;
      controller.current?.abort();
    };
  }, [key]);
  useEffect(() => {
    if (state.key !== key) return;
    try {
      localStorage.setItem(
        historyPrefix + key + ":turns",
        JSON.stringify(state.turns),
      );
      setStorageError("");
    } catch {
      setStorageError("浏览器未能保存问答记录，请导出记录或检查本地存储空间。");
    }
  }, [key, state.key, state.turns]);
  useEffect(() => {
    if (state.key !== key) return;
    const save = () => {
      try {
        localStorage.setItem(
          historyPrefix + key + ":draft",
          state.pending || state.draft,
        );
      } catch {
        setStorageError("浏览器未能保存问答草稿，请检查本地存储空间。");
      }
    };
    save();
    window.addEventListener("pagehide", save);
    return () => window.removeEventListener("pagehide", save);
  }, [key, state.key, state.draft, state.pending]);
  function update(patch: Partial<typeof current>) {
    setState({ ...current, ...patch });
  }
  async function send(question = current.draft) {
    question = question.trim();
    if (
      !question ||
      current.pending ||
      inFlight.current?.key === key ||
      !context ||
      (!packageId && !document)
    )
      return;
    if (retry.current?.key !== key || retry.current.question !== question)
      retry.current = { key, question, id: crypto.randomUUID() };
    const requestId = retry.current.id;
    const sequence = ++serial.current,
      abort = new AbortController();
    controller.current = abort;
    inFlight.current = { key, sequence };
    livePreview.current = "";
    update({ pending: question, preview: "", draft: "", error: "" });
    const history = current.turns
      .filter((turn) => !turn.incomplete && !turn.answer.operations?.length)
      .slice(-3)
      .flatMap((turn) => [
        { role: "user", content: turn.question },
        { role: "assistant", content: turn.answer.explanation.slice(0, 8000) },
      ]);
    let partial = "",
      paint: ReturnType<typeof setTimeout> | undefined;
    try {
      const request = mutation("POST", {
        ...baseline,
        package_id: document ? null : packageId,
        ...(document?.revision_id
          ? { document_revision_id: document.revision_id }
          : {}),
        target: context.target,
        context: context.context,
        question,
        history,
      });
      const response = await apiResponse(`${prefix}/conversation`, {
        ...request,
        headers: {
          ...request.headers,
          "Idempotency-Key": requestId,
          Accept: "text/event-stream",
        },
        signal: abort.signal,
      });
      const answer = await conversationAnswer<Answer>(
        response,
        (text) => {
          if (sequence !== serial.current || activeKey.current !== key) return;
          partial += text;
          if (partial.length > 16000) throw new Error("流式回答超过篇幅限制");
          livePreview.current = partial;
          if (paint === undefined)
            paint = setTimeout(() => {
              paint = undefined;
              if (sequence === serial.current && activeKey.current === key)
                setState((previous) =>
                  previous.key === key
                    ? { ...previous, preview: partial }
                    : previous,
                );
            }, 40);
        },
        abort.signal,
      );
      clearTimeout(paint);
      if (sequence === serial.current && activeKey.current === key) {
        if (answer.operations?.length && answer.source_fingerprint)
          ownedTransition.current = {
            identity,
            fingerprint: answer.source_fingerprint,
          };
        update({
          turns: [
            ...current.turns.slice(-19),
            { id: crypto.randomUUID(), question, answer },
          ],
          pending: "",
          preview: "",
          draft: "",
        });
        retry.current = null;
        if (answer.operations?.length && onUpdated) {
          try {
            await onUpdated(answer.operations);
          } catch (error) {
            if (activeKey.current === key)
              setState((previous) => ({
                ...previous,
                error: `操作已完成，工作区刷新失败：${error instanceof Error ? error.message : String(error)}`,
              }));
          }
        }
      }
    } catch (error) {
      if (sequence === serial.current && activeKey.current === key)
        update({
          turns: partial
            ? [...current.turns.slice(-19), unfinished(question, partial)]
            : current.turns,
          pending: "",
          preview: "",
          draft: question,
          error: error instanceof Error ? error.message : String(error),
        });
    } finally {
      clearTimeout(paint);
      if (inFlight.current?.sequence === sequence) inFlight.current = null;
    }
  }
  function stop() {
    serial.current++;
    inFlight.current = null;
    controller.current?.abort();
    const preview = livePreview.current;
    update({
      pending: "",
      preview: "",
      draft: current.pending || current.draft,
      turns: preview
        ? [...current.turns.slice(-19), unfinished(current.pending, preview)]
        : current.turns,
    });
  }
  return {
    ...current,
    storageError,
    pendingOperation:
      /修改|改写|重写|更新|改成|改为|调整|补充|重新关联|导出|export|rewrite|update|reassociate/i.test(
        current.pending,
      ),
    send,
    stop,
    setDraft: (draft: string) => update({ draft }),
    clear: () => {
      stop();
      livePreview.current = "";
      update({ turns: [], pending: "", preview: "", draft: "", error: "" });
    },
  };
}

const ConversationTurn = memo(function ConversationTurn({
  turn,
  onCitation,
  prefix,
  onRetry,
  onPreview,
}: {
  prefix: string;
  turn: Turn;
  onCitation: (reference: MarkdownReference) => void;
  onRetry: (question: string) => void;
  onPreview: (reference: MarkdownReference) => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState("");
  return (
    <div className="chat-turn">
      <p className="chat-question">{turn.question}</p>
      <div className="chat-answer">
        <small>
          AI · {turn.incomplete ? "未完成" : turn.answer.model_id || "文档检索"}
        </small>
        <RichMarkdown references={turn.answer.sections} onReference={onPreview}>
          {turn.answer.explanation}
        </RichMarkdown>
        {!!turn.answer.unknowns.length && (
          <details>
            <summary>尚需确认</summary>
            <ul>
              {turn.answer.unknowns.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          </details>
        )}
        {turn.answer.operations?.map((operation, i) => (
          <OperationResult
            key={i}
            operation={operation}
            prefix={prefix}
            onCitation={onCitation}
          />
        ))}
        {!!turn.answer.sections.length && (
          <details className="chat-source-details">
            <summary>查看 {turn.answer.sections.length} 处原文依据</summary>
            <div className="chat-sources" aria-label="回答依据">
              {turn.answer.sections.map((ref, i) => (
                <section key={ref.revision_id} className="chat-source-entry">
                  <button onClick={() => onPreview(ref)}>
                    <FileText size={13} />
                    <span>
                      {i + 1}. {ref.document_name}
                      <small>{ref.name}</small>
                    </span>
                  </button>
                </section>
              ))}
            </div>
          </details>
        )}
        <div className="chat-answer-actions">
          <button
            onClick={async (event) => {
              try {
                await event.currentTarget.ownerDocument.defaultView!.navigator.clipboard.writeText(
                  turn.answer.explanation,
                );
                setCopied(true);
              } catch {
                setCopyError("复制未获允许，可选中文字复制。");
              }
            }}
          >
            {copied ? "已复制" : "复制回答"}
          </button>
          {!turn.answer.operations?.length && (
            <button onClick={() => onRetry(turn.question)}>重新回答</button>
          )}
        </div>
        {copyError && <small role="status">{copyError}</small>}
        {!!turn.answer.suggested_questions?.length && (
          <div className="chat-followups" aria-label="继续提问">
            {turn.answer.suggested_questions.map((question) => (
              <button key={question} onClick={() => onRetry(question)}>
                {question}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

function OperationResult({
  operation,
  prefix,
  onCitation,
}: {
  operation: Operation;
  prefix: string;
  onCitation: (reference: MarkdownReference) => void;
}) {
  const [job, setJob] = useState<{
    state: string;
    progress?: number;
    result?: { download_url?: string; filename?: string };
    error?: { message: string };
  } | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!operation.job_id) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const project = prefix.split("/")[4];
    async function poll() {
      try {
        const current = await api<NonNullable<typeof job>>(
          `/jobs/${operation.job_id}?project_id=${project}`,
        );
        if (cancelled) return;
        setJob(current);
        setError("");
        if (
          ["queued", "running", "retry_wait", "cancelling"].includes(
            current.state,
          )
        )
          timer = setTimeout(poll, 1500);
      } catch (e) {
        if (!cancelled) {
          setJob(null);
          setError(e instanceof Error ? e.message : String(e));
        }
      }
    }
    void poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [operation.job_id, prefix, attempt]);
  if (operation.kind === "document" && operation.revision_id)
    return (
      <button
        onClick={() =>
          onCitation({
            revision_id: "",
            document_revision_id: operation.revision_id!,
            document_name: operation.title,
            name: "修改后的文档",
            content: "",
          })
        }
      >
        {operation.title}
      </button>
    );
  if (!operation.job_id)
    return <p className="chat-operation">{operation.title}</p>;
  const expected = `/api/v1/projects/${prefix.split("/")[4]}/exports/${operation.job_id}/download`;
  return (
    <div className="chat-operation" role="status">
      <b>{operation.title}</b>
      {error ? (
        <>
          <p>{error}</p>
          <button onClick={() => setAttempt(attempt + 1)}>重新查询导出</button>
        </>
      ) : job?.state === "succeeded" &&
        job.result?.download_url === expected ? (
        <a href={expected} download={job.result.filename}>
          下载导出文件
        </a>
      ) : job && ["failed", "cancelled"].includes(job.state) ? (
        <p>{job.error?.message || "导出未完成"}</p>
      ) : (
        <p>正在导出… {job?.progress || 0}%</p>
      )}
    </div>
  );
}

export function SourcePreview({
  reference,
  onClose,
  onOpen,
  returnLabel = "继续看回答",
}: {
  reference: MarkdownReference;
  onClose: () => void;
  onOpen: () => void;
  returnLabel?: string;
}) {
  const reading = useContext(MarkdownReadingContext);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialog.current?.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="chat-source-preview"
      aria-label="引用原文预览"
      onClose={onClose}
      onKeyDown={(event) => {
        if (event.key === "Escape") event.stopPropagation();
      }}
    >
      <header>
        <div>
          <small>{reference.document_name}</small>
          <h3>{reference.name}</h3>
        </div>
        <button
          aria-label="关闭原文预览"
          onClick={() => dialog.current?.close()}
        >
          <X size={18} />
        </button>
      </header>
      <div className="chat-source-preview-body">
        <RichMarkdown
          documentId={
            reading.documents.find(
              (document) =>
                document.revision_id === reference.document_revision_id,
            )?.id
          }
          revisionId={reference.document_revision_id}
        >
          {reference.content}
        </RichMarkdown>
      </div>
      <footer>
        <button onClick={() => dialog.current?.close()}>{returnLabel}</button>
        <button onClick={onOpen}>打开文档原文</button>
      </footer>
    </dialog>
  );
}

export function KnowledgeChat({
  chat,
  label,
  onCitation,
  onSettings,
  prefix,
}: {
  prefix: string;
  chat: ReturnType<typeof useKnowledgeChat>;
  label: string;
  onCitation: (reference: MarkdownReference) => void;
  onSettings: () => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const following = useRef(true);
  const [reference, setReference] = useState<MarkdownReference | null>(null);
  useEffect(() => setReference(null), [chat.key]);
  const preview = useDisplayedText(chat.preview);
  useEffect(() => {
    if (chat.pending) following.current = true;
  }, [chat.pending]);
  useEffect(() => {
    if (!chat.pending) input.current?.focus({ preventScroll: true });
  }, [chat.pending]);
  useEffect(() => {
    if (!chat.turns.length && !chat.pending) {
      end.current?.parentElement?.scrollTo({ top: 0 });
      return;
    }
    if (following.current) end.current?.scrollIntoView({ block: "nearest" });
  }, [chat.turns.length, chat.pending, preview]);
  return (
    <section className="knowledge-chat" aria-label="AI 知识问答">
      <header>
        <span>
          <MessageCircle size={14} />
          围绕当前功能提问
        </span>
      </header>
      <div
        className="chat-messages"
        aria-live="polite"
        aria-relevant="additions"
        onScroll={(event) => {
          const node = event.currentTarget;
          following.current =
            node.scrollHeight - node.scrollTop - node.clientHeight < 80;
        }}
      >
        {!chat.turns.length && !chat.pending && (
          <div className="chat-welcome">
            <b>想了解「{label}」的哪一部分？</b>
            <p>查找依据、连续追问；也可以要求修改当前关联、文档或导出项目。</p>
            {[
              "这个功能的办理流程是什么？",
              "使用前需要满足哪些条件？",
              "有哪些常见问题和注意事项？",
            ].map((q) => (
              <button key={q} onClick={() => void chat.send(q)}>
                {q}
              </button>
            ))}
          </div>
        )}
        {chat.turns.map((turn) => (
          <ConversationTurn
            key={turn.id}
            turn={turn}
            onCitation={onCitation}
            prefix={prefix}
            onRetry={(question) => void chat.send(question)}
            onPreview={setReference}
          />
        ))}
        {chat.pending && (
          <>
            <p className="chat-question">{chat.pending}</p>
            {chat.preview && (
              <div
                className="chat-answer chat-stream"
                aria-label="正在生成的回答"
              >
                <small>AI · 正在生成…</small>
                <RichMarkdown streaming>{preview}</RichMarkdown>
              </div>
            )}
            <p className="chat-status" role="status">
              <LoaderCircle size={15} className="spinning" />
              {chat.pendingOperation
                ? "正在执行请求并保存结果…"
                : chat.preview
                  ? "正在生成回答…"
                  : "正在检索文档并回答…"}
            </p>
          </>
        )}
        {chat.error && (
          <div className="assistant-error" role="alert">
            <p>{chat.error}</p>
            <button onClick={() => void chat.send()}>重试提问</button>
            <button onClick={onSettings}>AI 设置</button>
          </div>
        )}
        <div ref={end} />
      </div>
      <form
        className="chat-composer vks-ai-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void chat.send();
        }}
      >
        <textarea
          ref={input}
          aria-label="向知识助手提问"
          placeholder="输入问题…"
          title="Enter 发送 · Shift+Enter 换行"
          maxLength={2000}
          rows={2}
          value={chat.draft}
          disabled={!!chat.pending}
          onChange={(e) => chat.setDraft(e.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing &&
              event.keyCode !== 229
            ) {
              event.preventDefault();
              void chat.send();
            }
          }}
        />
        <div className="vks-ai-composer-actions">
          <button
            aria-label="清空问答"
            title="清空问答"
            disabled={
              chat.pendingOperation || (!chat.turns.length && !chat.pending)
            }
            onClick={chat.clear}
          >
            <Trash2 size={14} /> 清空问答
          </button>
          <button
            title="导出记录"
            disabled={!chat.turns.length}
            onClick={() => {
              const text = chat.turns
                .map(
                  (turn) =>
                    `## ${turn.question}\n\n${turn.incomplete ? "（未完成）\n\n" : ""}${turn.answer.explanation}\n\n${turn.answer.sections.map((ref, i) => `[${i + 1}] ${ref.document_name} / ${ref.name}`).join("\n")}`,
                )
                .join("\n\n---\n\n");
              const url = URL.createObjectURL(
                new Blob([text], { type: "text/markdown;charset=utf-8" }),
              );
              const link = document.createElement("a");
              link.href = url;
              link.download = "知识问答记录.md";
              link.click();
              setTimeout(() => URL.revokeObjectURL(url), 1000);
            }}
          >
            导出记录
          </button>
          <span className="vks-ai-composer-spacer" />
          {chat.pending ? (
            <button
              className="vks-ai-composer-send"
              key="stop"
              type="button"
              aria-label={chat.pendingOperation ? "操作执行中" : "停止等待"}
              disabled={chat.pendingOperation}
              onClick={(event) => {
                event.preventDefault();
                chat.stop();
              }}
            >
              <Square size={14} /> 停止
            </button>
          ) : (
            <button
              className="primary vks-ai-composer-send"
              key="send"
              type="submit"
              aria-label="发送问题"
              title="发送（Enter）"
              disabled={!chat.draft.trim()}
            >
              发送 <ArrowUp size={16} />
            </button>
          )}
        </div>
      </form>
      {chat.storageError && <small role="status">{chat.storageError}</small>}
      {reference && (
        <SourcePreview
          reference={reference}
          onClose={() => setReference(null)}
          onOpen={() => {
            setReference(null);
            onCitation(reference);
          }}
        />
      )}
    </section>
  );
}
