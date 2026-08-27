import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowUp, Check, Plus, Star, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";

import { api, type Assistant } from "../api";

export function AssistantsPage() {
  const queryClient = useQueryClient();
  const { data: assistants } = useQuery({
    queryKey: ["assistants"],
    queryFn: api.assistants,
  });
  const { data: tools } = useQuery({ queryKey: ["tools"], queryFn: api.tools });
  const { data: providers } = useQuery({
    queryKey: ["providers"],
    queryFn: api.providers,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const current =
    assistants?.find((a) => a.name === selected) ?? assistants?.[0] ?? null;

  const modelOptions =
    providers?.flatMap((p) => p.models.map((m) => `${p.name}/${m.name}`)) ?? [];

  return (
    <div className="flex h-full">
      <div className="flex w-56 shrink-0 flex-col border-r border-neutral-200 dark:border-neutral-800">
        <div className="flex-1 space-y-1 overflow-y-auto p-3">
          {assistants?.map((a) => (
            <button
              key={a.name}
              onClick={() => setSelected(a.name)}
              className={`flex w-full items-center justify-between rounded-lg px-3 py-2.5 text-left text-sm ${
                current?.name === a.name
                  ? "bg-accent/10 font-medium text-accent-strong"
                  : "hover:bg-neutral-100 dark:hover:bg-neutral-800"
              }`}
            >
              <span>
                {a.display_name || a.name}
                <span className="ml-1.5 text-xs text-neutral-400">{a.name}</span>
              </span>
              {a.is_default && (
                <Star className="size-3.5 fill-amber-400 text-amber-400" />
              )}
            </button>
          ))}
        </div>
        <CreateAssistantForm
          modelOptions={modelOptions}
          onCreated={(name) => {
            queryClient.invalidateQueries({ queryKey: ["assistants"] });
            setSelected(name);
          }}
        />
      </div>
      {current && (
        <AssistantEditor
          key={current.name}
          assistant={current}
          toolNames={tools?.map((t) => t.name) ?? []}
          modelOptions={modelOptions}
          onSaved={() => queryClient.invalidateQueries({ queryKey: ["assistants"] })}
          onDeleted={() => {
            setSelected(null);
            queryClient.invalidateQueries({ queryKey: ["assistants"] });
          }}
        />
      )}
    </div>
  );
}

function CreateAssistantForm({
  modelOptions,
  onCreated,
}: {
  modelOptions: string[];
  onCreated: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [model, setModel] = useState("");
  const mutation = useMutation({
    mutationFn: () =>
      api.createAssistant({
        name: name.trim(),
        display_name: displayName.trim(),
        model: model.trim() || modelOptions[0] || "",
      }),
    onSuccess: (a) => {
      setOpen(false);
      setName("");
      setDisplayName("");
      onCreated(a.name);
    },
  });

  if (!open) {
    return (
      <div className="border-t border-neutral-200 p-3 dark:border-neutral-800">
        <button
          onClick={() => setOpen(true)}
          className="flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed border-neutral-300 px-3 py-2 text-sm text-neutral-500 hover:border-accent hover:text-accent-strong dark:border-neutral-700"
        >
          <Plus className="size-4" /> 新建人格
        </button>
      </div>
    );
  }
  return (
    <div className="space-y-2 border-t border-neutral-200 p-3 dark:border-neutral-800">
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="标识名（小写字母/数字/-）"
        className="input"
      />
      <input
        value={displayName}
        onChange={(e) => setDisplayName(e.target.value)}
        placeholder="显示名称（可选）"
        className="input"
      />
      <input
        value={model}
        onChange={(e) => setModel(e.target.value)}
        list="model-options"
        placeholder={`模型（默认 ${modelOptions[0] ?? "?"}）`}
        className="input"
      />
      {mutation.isError && (
        <div className="text-xs text-red-500">{String(mutation.error)}</div>
      )}
      <div className="flex gap-2">
        <button
          onClick={() => mutation.mutate()}
          disabled={!name.trim() || mutation.isPending}
          className="flex-1 rounded-lg bg-accent px-3 py-1.5 text-sm text-white hover:bg-accent-strong disabled:opacity-40"
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

function AssistantEditor({
  assistant,
  toolNames,
  modelOptions,
  onSaved,
  onDeleted,
}: {
  assistant: Assistant;
  toolNames: string[];
  modelOptions: string[];
  onSaved: () => void;
  onDeleted: () => void;
}) {
  const [form, setForm] = useState<Assistant>(assistant);
  const [saved, setSaved] = useState(false);
  useEffect(() => setForm(assistant), [assistant]);

  const mutation = useMutation({
    mutationFn: () =>
      api.updateAssistant(assistant.name, {
        display_name: form.display_name,
        prompt: form.prompt,
        model: form.model,
        fallback_models: form.fallback_models,
        temperature: form.temperature,
        max_tokens: form.max_tokens,
        tools: form.tools,
        max_tool_rounds: form.max_tool_rounds,
        policy: form.policy,
      }),
    onSuccess: () => {
      onSaved();
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
    },
  });
  const deleteMutation = useMutation({
    mutationFn: () => api.deleteAssistant(assistant.name),
    onSuccess: onDeleted,
  });

  const set = <K extends keyof Assistant>(key: K, value: Assistant[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  return (
    <div className="flex-1 space-y-4 overflow-y-auto p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">
          {assistant.display_name || assistant.name}
        </h2>
        <div className="flex items-center gap-2">
          {!assistant.is_default && (
            <button
              onClick={() => {
                if (confirm(`删除人格 ${assistant.name}？定义文件将被移除。`)) {
                  deleteMutation.mutate();
                }
              }}
              className="flex items-center gap-1.5 rounded-lg border border-red-200 px-3 py-2 text-sm text-red-500 hover:bg-red-50 dark:border-red-900 dark:hover:bg-red-950"
            >
              <Trash2 className="size-4" /> 删除
            </button>
          )}
          <button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
            className="flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-sm font-medium text-white hover:bg-accent-strong disabled:opacity-50"
          >
            {saved && <Check className="size-4" />}
            {saved ? "已保存" : mutation.isPending ? "保存中…" : "保存"}
          </button>
        </div>
      </div>
      {mutation.isError && (
        <div className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-300">
          {String(mutation.error)}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4">
        <Field label="显示名称">
          <input
            value={form.display_name}
            onChange={(e) => set("display_name", e.target.value)}
            className="input"
          />
        </Field>
        <Field label="模型">
          <input
            value={form.model}
            onChange={(e) => set("model", e.target.value)}
            list="model-options"
            className="input"
          />
          <datalist id="model-options">
            {modelOptions.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </Field>
        <Field label="温度（留空用模型默认）">
          <input
            type="number"
            step="0.1"
            value={form.temperature ?? ""}
            onChange={(e) =>
              set("temperature", e.target.value === "" ? null : Number(e.target.value))
            }
            className="input"
          />
        </Field>
        <Field label="最大工具调用轮数">
          <input
            type="number"
            value={form.max_tool_rounds}
            onChange={(e) => set("max_tool_rounds", Number(e.target.value))}
            className="input"
          />
        </Field>
      </div>

      <Field label="降级模型链（主模型不可用时按顺序尝试；顺序即优先级）">
        <div className="space-y-1.5">
          {form.fallback_models.map((m, i) => (
            <div
              key={`${m}-${i}`}
              className="flex items-center gap-2 rounded-lg border border-neutral-200 px-3 py-1.5 text-sm dark:border-neutral-700"
            >
              <span className="w-5 text-xs text-neutral-400">{i + 1}.</span>
              <span className="flex-1 font-mono text-xs">{m}</span>
              {i > 0 && (
                <button
                  onClick={() => {
                    const next = [...form.fallback_models];
                    [next[i - 1], next[i]] = [next[i], next[i - 1]];
                    set("fallback_models", next);
                  }}
                  className="rounded p-0.5 text-neutral-400 hover:text-accent-strong"
                  title="上移"
                >
                  <ArrowUp className="size-3.5" />
                </button>
              )}
              <button
                onClick={() =>
                  set(
                    "fallback_models",
                    form.fallback_models.filter((_, idx) => idx !== i),
                  )
                }
                className="rounded p-0.5 text-neutral-400 hover:text-red-500"
                title="移除"
              >
                <X className="size-3.5" />
              </button>
            </div>
          ))}
          <input
            list="model-options"
            placeholder="输入或选择模型后回车添加"
            className="input"
            onKeyDown={(e) => {
              const value = (e.target as HTMLInputElement).value.trim();
              if (e.key === "Enter" && value) {
                e.preventDefault();
                if (!form.fallback_models.includes(value)) {
                  set("fallback_models", [...form.fallback_models, value]);
                }
                (e.target as HTMLInputElement).value = "";
              }
            }}
          />
        </div>
      </Field>

      <Field label={`工具（${form.tools.length} 已启用）`}>
        <div className="flex flex-wrap gap-2">
          {toolNames.map((name) => {
            const enabled = form.tools.includes(name);
            return (
              <button
                key={name}
                onClick={() =>
                  set(
                    "tools",
                    enabled
                      ? form.tools.filter((t) => t !== name)
                      : [...form.tools, name],
                  )
                }
                className={`rounded-full border px-3 py-1 text-xs font-mono ${
                  enabled
                    ? "border-accent bg-accent/10 text-accent-strong"
                    : "border-neutral-300 text-neutral-500 dark:border-neutral-700"
                }`}
              >
                {name}
              </button>
            );
          })}
          {toolNames.length === 0 && (
            <span className="text-xs text-neutral-400">暂无可用工具</span>
          )}
        </div>
      </Field>

      <div className="grid grid-cols-3 gap-4">
        <Field label="拟人分段发送">
          <select
            value={form.policy.split ? "1" : "0"}
            onChange={(e) =>
              set("policy", { ...form.policy, split: e.target.value === "1" })
            }
            className="input"
          >
            <option value="1">开启</option>
            <option value="0">关闭</option>
          </select>
        </Field>
        <Field label="最多分几段">
          <input
            type="number"
            value={form.policy.max_segments}
            onChange={(e) =>
              set("policy", { ...form.policy, max_segments: Number(e.target.value) })
            }
            className="input"
          />
        </Field>
        <Field label="每字打字延迟（秒）">
          <input
            type="number"
            step="0.05"
            value={form.policy.delay_per_char}
            onChange={(e) =>
              set("policy", {
                ...form.policy,
                delay_per_char: Number(e.target.value),
              })
            }
            className="input"
          />
        </Field>
      </div>

      <Field label="人格 Prompt">
        <textarea
          value={form.prompt}
          onChange={(e) => set("prompt", e.target.value)}
          rows={18}
          className="input font-mono text-xs leading-relaxed"
        />
      </Field>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-neutral-500">{label}</span>
      {children}
    </label>
  );
}
