import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  ChevronRight,
  Eraser,
  Plus,
  SendHorizonal,
  Square,
  Trash2,
  Wrench,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api, streamChat } from "../api";

interface ToolUse {
  name: string;
  arguments: string;
  result?: string;
}

interface ChatMsg {
  role: "user" | "assistant";
  content: string;
  reasoning?: string;
  tools?: ToolUse[];
  streaming?: boolean;
}

interface ConvMeta {
  id: string;
  title: string;
}

function loadConvs(): ConvMeta[] {
  try {
    return JSON.parse(localStorage.getItem("web-convs") ?? "[]");
  } catch {
    return [];
  }
}

function saveConvs(convs: ConvMeta[]) {
  localStorage.setItem("web-convs", JSON.stringify(convs.slice(0, 50)));
}

function newId(): string {
  return crypto.randomUUID().replaceAll("-", "").slice(0, 16);
}

export function ChatPage() {
  const [convs, setConvs] = useState<ConvMeta[]>(loadConvs);
  const [convId, setConvId] = useState<string>(() => loadConvs()[0]?.id ?? newId());
  const [assistant, setAssistant] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const { data: assistants } = useQuery({
    queryKey: ["assistants"],
    queryFn: api.assistants,
  });

  // 切换会话时加载历史
  const history = useQuery({
    queryKey: ["web-history", convId],
    queryFn: () => api.messages(`web:web:${convId}`),
  });
  useEffect(() => {
    if (history.data) {
      setMessages(
        history.data.map((m) => ({
          role: m.role === "user" ? "user" : "assistant",
          content: m.content,
          reasoning: m.reasoning ?? undefined,
        })),
      );
    }
  }, [history.data]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const resetMutation = useMutation({
    mutationFn: () => api.resetChat(convId),
    onSuccess: () => setMessages([]),
  });

  function selectConv(id: string) {
    abortRef.current?.abort();
    setBusy(false);
    setMessages([]);
    setConvId(id);
  }

  function createConv() {
    const id = newId();
    selectConv(id);
  }

  function removeConv(id: string) {
    api.deleteConversation(`web:web:${id}`).catch(() => {});
    const rest = convs.filter((c) => c.id !== id);
    setConvs(rest);
    saveConvs(rest);
    if (id === convId) selectConv(rest[0]?.id ?? newId());
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setBusy(true);

    // 首条消息时登记会话
    if (!convs.some((c) => c.id === convId)) {
      const next = [{ id: convId, title: text.slice(0, 20) }, ...convs];
      setConvs(next);
      saveConvs(next);
    }

    setMessages((ms) => [
      ...ms,
      { role: "user", content: text },
      { role: "assistant", content: "", streaming: true },
    ]);

    const patch = (fn: (m: ChatMsg) => ChatMsg) =>
      setMessages((ms) => [...ms.slice(0, -1), fn(ms[ms.length - 1])]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await streamChat(
        convId,
        text,
        assistant,
        (e) => {
          if (e.type === "reasoning") {
            patch((m) => ({ ...m, reasoning: (m.reasoning ?? "") + e.text }));
          } else if (e.type === "text") {
            patch((m) => ({ ...m, content: m.content + e.text }));
          } else if (e.type === "tool_call") {
            patch((m) => ({
              ...m,
              tools: [...(m.tools ?? []), { name: e.name, arguments: e.arguments }],
            }));
          } else if (e.type === "tool_result") {
            patch((m) => ({
              ...m,
              tools: (m.tools ?? []).map((t) =>
                t.name === e.name && t.result === undefined
                  ? { ...t, result: e.result }
                  : t,
              ),
            }));
          } else if (e.type === "error") {
            patch((m) => ({
              ...m,
              content: m.content + `\n\n> ⚠️ ${e.message}`,
            }));
          } else if (e.type === "done") {
            patch((m) => ({ ...m, content: e.reply || m.content }));
          }
        },
        controller.signal,
      );
    } catch (err) {
      if (!(err instanceof DOMException && err.name === "AbortError")) {
        patch((m) => ({ ...m, content: m.content + `\n\n> ⚠️ ${String(err)}` }));
      }
    } finally {
      patch((m) => ({ ...m, streaming: false }));
      setBusy(false);
      abortRef.current = null;
    }
  }

  return (
    <div className="flex h-full">
      {/* 会话列表 */}
      <div className="flex w-56 shrink-0 flex-col border-r border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
        <div className="p-3">
          <button
            onClick={createConv}
            className="flex w-full items-center justify-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-white hover:bg-accent-strong"
          >
            <Plus className="size-4" /> 新对话
          </button>
        </div>
        <div className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-2">
          {convs.map((c) => (
            <div
              key={c.id}
              onClick={() => selectConv(c.id)}
              className={`group flex cursor-pointer items-center justify-between rounded-lg px-3 py-2 text-sm ${
                c.id === convId
                  ? "bg-neutral-100 font-medium dark:bg-neutral-800"
                  : "text-neutral-600 hover:bg-neutral-50 dark:text-neutral-300 dark:hover:bg-neutral-800/50"
              }`}
            >
              <span className="truncate">{c.title || "（空对话）"}</span>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  removeConv(c.id);
                }}
                className="hidden rounded p-0.5 text-neutral-400 hover:text-red-500 group-hover:block"
              >
                <Trash2 className="size-3.5" />
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* 对话区 */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
          <select
            value={assistant ?? ""}
            onChange={(e) => setAssistant(e.target.value || null)}
            className="rounded-lg border border-neutral-200 bg-white px-2.5 py-1.5 text-sm dark:border-neutral-700 dark:bg-neutral-900"
          >
            <option value="">默认人格</option>
            {assistants?.map((a) => (
              <option key={a.name} value={a.name}>
                {a.display_name || a.name}
              </option>
            ))}
          </select>
          <button
            onClick={() => resetMutation.mutate()}
            className="flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-sm text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
            title="清空上下文"
          >
            <Eraser className="size-4" /> 重置
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
          {messages.length === 0 && (
            <div className="flex h-full items-center justify-center text-sm text-neutral-400">
              和波奇打个招呼吧～
            </div>
          )}
          {messages.map((m, i) => (
            <MessageBubble key={i} msg={m} />
          ))}
          <div ref={bottomRef} />
        </div>

        <footer className="border-t border-neutral-200 p-3 dark:border-neutral-800">
          <div className="flex items-end gap-2">
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={Math.min(input.split("\n").length, 6)}
              placeholder="输入消息，Enter 发送，Shift+Enter 换行"
              className="flex-1 resize-none rounded-xl border border-neutral-200 bg-white px-3.5 py-2.5 text-sm outline-none focus:border-accent dark:border-neutral-700 dark:bg-neutral-900"
            />
            {busy ? (
              <button
                onClick={() => abortRef.current?.abort()}
                className="rounded-xl bg-neutral-200 p-2.5 text-neutral-600 hover:bg-neutral-300 dark:bg-neutral-700 dark:text-neutral-200"
                title="停止"
              >
                <Square className="size-5" />
              </button>
            ) : (
              <button
                onClick={send}
                disabled={!input.trim()}
                className="rounded-xl bg-accent p-2.5 text-white hover:bg-accent-strong disabled:opacity-40"
              >
                <SendHorizonal className="size-5" />
              </button>
            )}
          </div>
        </footer>
      </div>
    </div>
  );
}

function MessageBubble({ msg }: { msg: ChatMsg }) {
  if (msg.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[75%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm text-white">
          {msg.content}
        </div>
      </div>
    );
  }
  return (
    <div className="flex justify-start">
      <div className="max-w-[85%] space-y-2">
        {msg.reasoning && <ReasoningBlock text={msg.reasoning} done={!msg.streaming} />}
        {msg.tools?.map((t, i) => (
          <div
            key={i}
            className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-700 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-300"
          >
            <Wrench className="size-3.5 shrink-0" />
            <span className="font-mono">{t.name}</span>
            {t.result !== undefined ? (
              <span className="truncate text-amber-600 dark:text-amber-400">
                → {t.result.slice(0, 80)}
              </span>
            ) : (
              <span className="animate-pulse">调用中…</span>
            )}
          </div>
        ))}
        {(msg.content || !msg.streaming) && (
          <div className="prose-chat rounded-2xl rounded-bl-md bg-white px-4 py-2.5 text-sm shadow-sm dark:bg-neutral-900">
            <Markdown remarkPlugins={[remarkGfm]}>{msg.content || "…"}</Markdown>
          </div>
        )}
        {msg.streaming && !msg.content && !msg.reasoning && (
          <div className="rounded-2xl rounded-bl-md bg-white px-4 py-2.5 text-sm text-neutral-400 shadow-sm dark:bg-neutral-900">
            <span className="animate-pulse">思考中…</span>
          </div>
        )}
      </div>
    </div>
  );
}

function ReasoningBlock({ text, done }: { text: string; done: boolean }) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (done) setOpen(false);
  }, [done]);
  return (
    <div className="rounded-lg border border-neutral-200 bg-neutral-50 text-xs dark:border-neutral-800 dark:bg-neutral-900/50">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1 px-3 py-1.5 text-neutral-500"
      >
        {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
        思考过程{done ? "" : "（进行中…）"}
      </button>
      {(open || !done) && (
        <div className="max-h-40 overflow-y-auto whitespace-pre-wrap px-3 pb-2 text-neutral-500">
          {text}
        </div>
      )}
    </div>
  );
}
