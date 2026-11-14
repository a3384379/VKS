import { memo, useEffect, useRef, useState } from "react";
import {
  ArrowUp,
  FileText,
  LoaderCircle,
  MessageCircle,
  Square,
  Trash2,
} from "lucide-react";
import { api, mutation } from "../../web/src/api";
import { RichMarkdown, type MarkdownReference } from "./RichMarkdown";
import type { Target } from "./runtime";

type Context = {
  target: Target;
  context: {
    scope?: "module" | "control";
    page_title: string;
    section_title: string;
    nearby_labels: string[];
  };
};
type Answer = {
  explanation: string;
  model_id: string;
  sections: MarkdownReference[];
  unknowns: string[];
};
type Turn = { id: string; question: string; answer: Answer };
export function useKnowledgeChat(
  key: string,
  prefix: string,
  baseline: { analysis_set_id?: string; release_id?: string },
  packageId: string,
  context: Context | null,
  document?: { revision_id?: string },
) {
  const [state, setState] = useState<{
    key: string;
    turns: Turn[];
    draft: string;
    pending: string;
    error: string;
  }>({ key, turns: [], draft: "", pending: "", error: "" });
  const current =
    state.key === key
      ? state
      : { key, turns: [], draft: "", pending: "", error: "" };
  const activeKey = useRef(key),
    serial = useRef(0),
    inFlight = useRef<{ key: string; sequence: number } | null>(null),
    controller = useRef<AbortController | null>(null);
  activeKey.current = key;
  useEffect(() => {
    serial.current++;
    inFlight.current = null;
    controller.current?.abort();
    // An abandoned request must not reappear as a live pending turn if the user
    // later returns to the same control without interacting with another one.
    setState({ key, turns: [], draft: "", pending: "", error: "" });
    return () => {
      serial.current++;
      controller.current?.abort();
    };
  }, [key]);
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
    const sequence = ++serial.current,
      abort = new AbortController();
    controller.current = abort;
    inFlight.current = { key, sequence };
    update({ pending: question, draft: "", error: "" });
    const history = current.turns.slice(-3).flatMap((turn) => [
      { role: "user", content: turn.question },
      { role: "assistant", content: turn.answer.explanation },
    ]);
    try {
      const answer = await api<Answer>(`${prefix}/questions`, {
        ...mutation("POST", {
          ...baseline,
          package_id: document ? null : packageId,
          ...(document?.revision_id
            ? { document_revision_id: document.revision_id }
            : {}),
          target: context.target,
          context: context.context,
          question,
          history,
        }),
        signal: abort.signal,
      });
      if (sequence === serial.current && activeKey.current === key)
        update({
          turns: [
            ...current.turns.slice(-19),
            { id: crypto.randomUUID(), question, answer },
          ],
          pending: "",
          draft: "",
        });
    } catch (error) {
      if (sequence === serial.current && activeKey.current === key)
        update({
          pending: "",
          draft: question,
          error: error instanceof Error ? error.message : String(error),
        });
    } finally {
      if (inFlight.current?.sequence === sequence) inFlight.current = null;
    }
  }
  function stop() {
    serial.current++;
    inFlight.current = null;
    controller.current?.abort();
    update({ pending: "", draft: current.pending || current.draft });
  }
  return {
    ...current,
    send,
    stop,
    setDraft: (draft: string) => update({ draft }),
    clear: () => {
      stop();
      update({ turns: [], pending: "", draft: "", error: "" });
    },
  };
}

const ConversationTurn = memo(function ConversationTurn({
  turn,
  onCitation,
}: {
  turn: Turn;
  onCitation: (reference: MarkdownReference) => void;
}) {
  return (
    <div className="chat-turn">
      <p className="chat-question">{turn.question}</p>
      <div className="chat-answer">
        <small>AI · {turn.answer.model_id || "文档检索"}</small>
        <RichMarkdown references={turn.answer.sections}>
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
        {!!turn.answer.sections.length && (
          <div className="chat-sources" aria-label="回答依据">
            {turn.answer.sections.map((ref, i) => (
              <button key={ref.revision_id} onClick={() => onCitation(ref)}>
                <FileText size={13} />
                <span>
                  {i + 1}. {ref.document_name}
                  <small>{ref.name}</small>
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
});

export function KnowledgeChat({
  chat,
  label,
  onCitation,
  onSettings,
}: {
  chat: ReturnType<typeof useKnowledgeChat>;
  label: string;
  onCitation: (reference: MarkdownReference) => void;
  onSettings: () => void;
}) {
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    if (!chat.pending) input.current?.focus({ preventScroll: true });
  }, [chat.pending]);
  useEffect(() => {
    if (!chat.turns.length && !chat.pending) {
      end.current?.parentElement?.scrollTo({ top: 0 });
      return;
    }
    end.current?.scrollIntoView({ block: "nearest" });
  }, [chat.turns.length, chat.pending]);
  return (
    <section className="knowledge-chat" aria-label="AI 知识问答">
      <header>
        <span>
          <MessageCircle size={14} />
          围绕当前功能提问
        </span>
        <button
          aria-label="清空问答"
          title="清空问答"
          disabled={!chat.turns.length && !chat.pending}
          onClick={chat.clear}
        >
          <Trash2 size={14} />
        </button>
      </header>
      <div
        className="chat-messages"
        aria-live="polite"
        aria-relevant="additions"
      >
        {!chat.turns.length && !chat.pending && (
          <div className="chat-welcome">
            <b>想了解「{label}」的哪一部分？</b>
            <p>从项目文档中查找依据，支持连续追问。</p>
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
          <ConversationTurn key={turn.id} turn={turn} onCitation={onCitation} />
        ))}
        {chat.pending && (
          <>
            <p className="chat-question">{chat.pending}</p>
            <p className="chat-status" role="status">
              <LoaderCircle size={15} className="spinning" />
              正在检索文档并回答…
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
        className="chat-composer"
        onSubmit={(event) => {
          event.preventDefault();
          void chat.send();
        }}
      >
        <textarea
          ref={input}
          aria-label="向知识助手提问"
          placeholder="输入问题，Enter 发送 · Shift+Enter 换行"
          maxLength={2000}
          rows={2}
          value={chat.draft}
          disabled={!!chat.pending}
          onChange={(e) => chat.setDraft(e.target.value)}
          onKeyDown={(event) => {
            if (
              event.key === "Enter" &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void chat.send();
            }
          }}
        />
        <div>
          <small>基于项目文档回答 · 引用可跳转原文</small>
          {chat.pending ? (
            <button
              key="stop"
              type="button"
              aria-label="停止等待"
              onClick={(event) => {
                event.preventDefault();
                chat.stop();
              }}
            >
              <Square size={14} />
            </button>
          ) : (
            <button
              className="primary"
              key="send"
              type="submit"
              aria-label="发送问题"
              disabled={!chat.draft.trim()}
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </form>
    </section>
  );
}
