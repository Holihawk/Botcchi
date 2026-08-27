import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { MessageCircle, Trash2, User, Users } from "lucide-react";
import { useState } from "react";

import { api } from "../api";

function sessionLabel(key: string): { icon: typeof User; label: string } {
  const [channel, type, id] = key.split(":");
  if (type === "group") return { icon: Users, label: `群 ${id}` };
  if (channel === "web") return { icon: MessageCircle, label: `网页 ${id}` };
  return { icon: User, label: `私聊 ${id}` };
}

export function ConversationsPage() {
  const queryClient = useQueryClient();
  const { data: convs, error } = useQuery({
    queryKey: ["conversations"],
    queryFn: api.conversations,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const { data: messages } = useQuery({
    queryKey: ["messages", selected],
    queryFn: () => api.messages(selected!),
    enabled: selected !== null,
  });
  const deleteMutation = useMutation({
    mutationFn: (session: string) => api.deleteConversation(session),
    onSuccess: () => {
      setSelected(null);
      queryClient.invalidateQueries({ queryKey: ["conversations"] });
    },
  });

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-neutral-400">
        {String(error).includes("409")
          ? "未配置 database.url，会话数据不可浏览"
          : String(error)}
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <div className="w-72 shrink-0 space-y-1 overflow-y-auto border-r border-neutral-200 p-3 dark:border-neutral-800">
        {convs?.length === 0 && (
          <div className="px-3 py-2 text-sm text-neutral-400">暂无会话</div>
        )}
        {convs?.map((c) => {
          const { icon: Icon, label } = sessionLabel(c.session_key);
          return (
            <div
              key={c.session_key}
              onClick={() => setSelected(c.session_key)}
              className={`group flex cursor-pointer items-center justify-between rounded-lg px-3 py-2.5 text-sm ${
                selected === c.session_key
                  ? "bg-accent/10 text-accent-strong"
                  : "hover:bg-neutral-100 dark:hover:bg-neutral-800"
              }`}
            >
              <span className="flex min-w-0 items-center gap-2">
                <Icon className="size-4 shrink-0 text-neutral-400" />
                <span className="truncate">{label}</span>
              </span>
              <span className="flex items-center gap-2">
                <span className="text-xs text-neutral-400">{c.message_count}</span>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm(`删除会话 ${label} 的全部记录？`)) {
                      deleteMutation.mutate(c.session_key);
                    }
                  }}
                  className="hidden rounded p-0.5 text-neutral-400 hover:text-red-500 group-hover:block"
                >
                  <Trash2 className="size-3.5" />
                </button>
              </span>
            </div>
          );
        })}
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-5">
        {!selected && (
          <div className="flex h-full items-center justify-center text-sm text-neutral-400">
            选择左侧会话查看记录
          </div>
        )}
        {messages?.map((m) => (
          <div
            key={m.id}
            className={`max-w-[80%] rounded-xl px-3.5 py-2 text-sm ${
              m.role === "user"
                ? "bg-neutral-100 dark:bg-neutral-800"
                : "ml-auto bg-accent/10"
            }`}
          >
            <div className="mb-0.5 flex justify-between gap-4 text-xs text-neutral-400">
              <span>{m.role}</span>
              <span>{m.created_at.slice(0, 19)}</span>
            </div>
            <div className="whitespace-pre-wrap">{m.content}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
