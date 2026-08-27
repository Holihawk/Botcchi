import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowDown,
  ArrowUp,
  Check,
  KeyRound,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { useState } from "react";

import { api, type ModelDetail, type Provider } from "../api";

export function ProvidersPage() {
  const queryClient = useQueryClient();
  const { data: providers } = useQuery({
    queryKey: ["providers"],
    queryFn: api.providers,
  });
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["providers"] });
    queryClient.invalidateQueries({ queryKey: ["assistants"] });
  };
  const reorderMutation = useMutation({
    mutationFn: (names: string[]) => api.reorderProviders(names),
    onSuccess: refresh,
  });

  function move(index: number, delta: number) {
    if (!providers) return;
    const names = providers.map((p) => p.name);
    const target = index + delta;
    if (target < 0 || target >= names.length) return;
    [names[index], names[target]] = [names[target], names[index]];
    reorderMutation.mutate(names);
  }

  return (
    <div className="h-full space-y-4 overflow-y-auto p-5">
      <p className="text-xs text-neutral-400">
        修改保存后立即生效并持久化到 data/providers.yaml（此后 config.yaml 中的
        providers 仅作首次引导）。排列顺序即无前缀模型解析与展示的优先级。
      </p>
      {providers?.map((p, i) => (
        <ProviderCard
          key={p.name}
          provider={p}
          onChanged={refresh}
          onMoveUp={i > 0 ? () => move(i, -1) : undefined}
          onMoveDown={
            providers && i < providers.length - 1 ? () => move(i, 1) : undefined
          }
        />
      ))}
      <CreateProviderCard onCreated={refresh} />
    </div>
  );
}

function ProviderCard({
  provider,
  onChanged,
  onMoveUp,
  onMoveDown,
}: {
  provider: Provider;
  onChanged: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const [apiBase, setApiBase] = useState(provider.api_base);
  const [models, setModels] = useState<ModelDetail[]>(provider.models);
  const [newKeys, setNewKeys] = useState("");
  const [removeIdx, setRemoveIdx] = useState<Set<number>>(new Set());
  const [saved, setSaved] = useState(false);

  const saveMutation = useMutation({
    mutationFn: () =>
      api.updateProvider(provider.name, {
        api_base: apiBase,
        models,
        add_keys: newKeys
          .split("\n")
          .map((k) => k.trim())
          .filter(Boolean),
        remove_key_indexes: [...removeIdx],
      }),
    onSuccess: () => {
      setNewKeys("");
      setRemoveIdx(new Set());
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
      onChanged();
    },
  });
  const deleteMutation = useMutation({
    mutationFn: () => api.deleteProvider(provider.name),
    onSuccess: onChanged,
  });

  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-semibold">{provider.name}</span>
          <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-xs text-neutral-500 dark:bg-neutral-800">
            {provider.api_type}
          </span>
        </div>
        <div className="flex items-center gap-1">
          {onMoveUp && (
            <IconButton title="提高优先级" onClick={onMoveUp}>
              <ArrowUp className="size-4" />
            </IconButton>
          )}
          {onMoveDown && (
            <IconButton title="降低优先级" onClick={onMoveDown}>
              <ArrowDown className="size-4" />
            </IconButton>
          )}
          <IconButton
            title="删除 Provider"
            onClick={() => {
              if (confirm(`删除 Provider ${provider.name}？`)) {
                deleteMutation.mutate();
              }
            }}
            danger
          >
            <Trash2 className="size-4" />
          </IconButton>
        </div>
      </div>
      {(saveMutation.isError || deleteMutation.isError) && (
        <div className="mt-2 rounded-lg bg-red-50 px-3 py-2 text-xs text-red-600 dark:bg-red-950 dark:text-red-300">
          {String(saveMutation.error ?? deleteMutation.error)}
        </div>
      )}

      <label className="mt-3 block space-y-1">
        <span className="text-xs font-medium text-neutral-500">API 地址</span>
        <input
          value={apiBase}
          onChange={(e) => setApiBase(e.target.value)}
          className="input font-mono text-xs"
        />
      </label>

      <div className="mt-3 space-y-1">
        <span className="flex items-center gap-1 text-xs font-medium text-neutral-500">
          <KeyRound className="size-3.5" /> 密钥（{provider.key_count} 个，勾选 ×
          将在保存时删除）
        </span>
        <div className="flex flex-wrap gap-2">
          {provider.key_masks.map((mask, i) => (
            <button
              key={i}
              onClick={() => {
                const next = new Set(removeIdx);
                if (next.has(i)) {
                  next.delete(i);
                } else {
                  next.add(i);
                }
                setRemoveIdx(next);
              }}
              className={`flex items-center gap-1 rounded-full border px-3 py-1 font-mono text-xs ${
                removeIdx.has(i)
                  ? "border-red-300 bg-red-50 text-red-500 line-through dark:bg-red-950"
                  : "border-neutral-300 text-neutral-600 dark:border-neutral-700 dark:text-neutral-300"
              }`}
            >
              {mask} <X className="size-3" />
            </button>
          ))}
        </div>
        <textarea
          value={newKeys}
          onChange={(e) => setNewKeys(e.target.value)}
          rows={newKeys ? 3 : 1}
          placeholder="添加密钥：每行一个，保存时生效"
          className="input font-mono text-xs"
        />
      </div>

      <div className="mt-3 space-y-1">
        <span className="text-xs font-medium text-neutral-500">模型</span>
        <div className="flex flex-wrap gap-2">
          {models.map((m, i) => (
            <span
              key={`${m.name}-${i}`}
              className="flex items-center gap-1.5 rounded-full border border-neutral-200 px-3 py-1 font-mono text-xs dark:border-neutral-700"
            >
              {m.name}
              {m.temperature !== undefined && m.temperature !== null && (
                <span className="text-neutral-400">t={m.temperature}</span>
              )}
              <button
                onClick={() => setModels(models.filter((_, idx) => idx !== i))}
                className="text-neutral-400 hover:text-red-500"
              >
                <X className="size-3" />
              </button>
            </span>
          ))}
          <input
            placeholder="模型名，回车添加"
            className="w-48 rounded-full border border-dashed border-neutral-300 px-3 py-1 text-xs outline-none focus:border-accent dark:border-neutral-700 dark:bg-neutral-900"
            onKeyDown={(e) => {
              const value = (e.target as HTMLInputElement).value.trim();
              if (e.key === "Enter" && value) {
                e.preventDefault();
                if (!models.some((m) => m.name === value)) {
                  setModels([...models, { name: value }]);
                }
                (e.target as HTMLInputElement).value = "";
              }
            }}
          />
        </div>
      </div>

      <div className="mt-3 flex justify-end">
        <button
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending}
          className="flex items-center gap-1.5 rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-50"
        >
          {saved && <Check className="size-4" />}
          {saved ? "已保存" : "保存"}
        </button>
      </div>
    </div>
  );
}

function CreateProviderCard({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [apiBase, setApiBase] = useState("https://");
  const [keys, setKeys] = useState("");
  const [modelNames, setModelNames] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api.createProvider({
        name: name.trim(),
        api_base: apiBase.trim(),
        api_keys: keys
          .split("\n")
          .map((k) => k.trim())
          .filter(Boolean),
        models: modelNames
          .split("\n")
          .map((m) => m.trim())
          .filter(Boolean)
          .map((m) => ({ name: m })),
      }),
    onSuccess: () => {
      setOpen(false);
      setName("");
      setApiBase("https://");
      setKeys("");
      setModelNames("");
      onCreated();
    },
  });

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-neutral-300 px-4 py-3 text-sm text-neutral-500 hover:border-accent hover:text-accent-strong dark:border-neutral-700"
      >
        <Plus className="size-4" /> 添加模型服务
      </button>
    );
  }
  return (
    <div className="space-y-2.5 rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="grid grid-cols-2 gap-2.5">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="名称（小写字母/数字/-）"
          className="input"
        />
        <input
          value={apiBase}
          onChange={(e) => setApiBase(e.target.value)}
          placeholder="API 地址，如 https://api.deepseek.com/v1"
          className="input font-mono text-xs"
        />
      </div>
      <textarea
        value={keys}
        onChange={(e) => setKeys(e.target.value)}
        rows={2}
        placeholder="API 密钥：每行一个（至少一个）"
        className="input font-mono text-xs"
      />
      <textarea
        value={modelNames}
        onChange={(e) => setModelNames(e.target.value)}
        rows={2}
        placeholder="模型名：每行一个（可留空，之后再加）"
        className="input font-mono text-xs"
      />
      {mutation.isError && (
        <div className="text-xs text-red-500">{String(mutation.error)}</div>
      )}
      <div className="flex gap-2">
        <button
          onClick={() => mutation.mutate()}
          disabled={!name.trim() || mutation.isPending}
          className="rounded-lg bg-accent px-4 py-1.5 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-40"
        >
          创建
        </button>
        <button
          onClick={() => setOpen(false)}
          className="rounded-lg px-3 py-1.5 text-sm text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
        >
          取消
        </button>
      </div>
    </div>
  );
}

function IconButton({
  children,
  onClick,
  title,
  danger,
}: {
  children: React.ReactNode;
  onClick: () => void;
  title: string;
  danger?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`rounded-lg p-1.5 ${
        danger
          ? "text-neutral-400 hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950"
          : "text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600 dark:hover:bg-neutral-800"
      }`}
    >
      {children}
    </button>
  );
}
