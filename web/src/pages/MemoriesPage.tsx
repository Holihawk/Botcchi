import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2, User, Users } from "lucide-react";
import { useState } from "react";

import { api } from "../api";

export function MemoriesPage() {
  const queryClient = useQueryClient();
  const { data: scopes, error } = useQuery({
    queryKey: ["memory-scopes"],
    queryFn: api.memoryScopes,
  });
  const [scope, setScope] = useState<string | null>(null);
  const { data: entries } = useQuery({
    queryKey: ["memories", scope],
    queryFn: () => api.memories(scope!),
    enabled: scope !== null,
  });
  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteMemory(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["memories"] });
      queryClient.invalidateQueries({ queryKey: ["memory-scopes"] });
    },
  });

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-neutral-400">
        {String(error).includes("409")
          ? "未配置 database.url，长期记忆不可用"
          : String(error)}
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <div className="w-64 shrink-0 space-y-1 overflow-y-auto border-r border-neutral-200 p-3 dark:border-neutral-800">
        {scopes?.length === 0 && (
          <div className="px-3 py-2 text-sm text-neutral-400">
            还没有任何长期记忆
          </div>
        )}
        {scopes?.map((s) => {
          const isGroup = s.scope_key.startsWith("group:");
          const Icon = isGroup ? Users : User;
          return (
            <button
              key={s.scope_key}
              onClick={() => setScope(s.scope_key)}
              className={`flex w-full items-center justify-between rounded-lg px-3 py-2.5 text-sm ${
                scope === s.scope_key
                  ? "bg-accent/10 text-accent-strong"
                  : "hover:bg-neutral-100 dark:hover:bg-neutral-800"
              }`}
            >
              <span className="flex items-center gap-2">
                <Icon className="size-4 text-neutral-400" />
                {s.scope_key.split(":")[1]}
              </span>
              <span className="text-xs text-neutral-400">{s.count} 条</span>
            </button>
          );
        })}
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto p-5">
        {!scope && (
          <div className="flex h-full items-center justify-center text-sm text-neutral-400">
            选择左侧用户/群查看记忆
          </div>
        )}
        {entries?.map((m) => (
          <div
            key={m.id}
            className="group flex items-start justify-between gap-3 rounded-xl border border-neutral-200 bg-white px-4 py-3 text-sm dark:border-neutral-800 dark:bg-neutral-900"
          >
            <div>
              <div>{m.content}</div>
              <div className="mt-1 text-xs text-neutral-400">
                {m.created_at.slice(0, 19)}
              </div>
            </div>
            <button
              onClick={() => deleteMutation.mutate(m.id)}
              className="hidden rounded p-1 text-neutral-400 hover:text-red-500 group-hover:block"
              title="删除这条记忆"
            >
              <Trash2 className="size-4" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
