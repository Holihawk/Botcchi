import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, Trash2 } from "lucide-react";

import { api, type ScheduledJobView } from "../api";

const KIND_LABELS: Record<string, string> = {
  group_summary: "定时总结",
  daily_report: "定时日报",
  reminder: "提醒",
};

function kindLabel(kind: string) {
  return KIND_LABELS[kind] ?? kind;
}

function sessionLabel(key: string) {
  const [, chatType, chatId] = key.split(":");
  if (chatType === "group") return `群 ${chatId}`;
  if (chatType === "private") return `私聊 ${chatId}`;
  return key;
}

function scheduleLabel(job: ScheduledJobView) {
  const next = new Date(job.next_run_at).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  if (job.recurrence?.startsWith("daily@")) {
    return `每天 ${job.recurrence.slice(6)}（下次 ${next}）`;
  }
  return `一次性 · ${next}`;
}

function payloadLabel(job: ScheduledJobView) {
  if (job.kind === "reminder") return String(job.payload.message ?? "");
  if (job.kind === "group_summary")
    return `消息数门槛 ${job.payload.min_messages ?? 50}`;
  return "";
}

export function JobsPage() {
  const queryClient = useQueryClient();
  const { data: jobs, error, refetch, isFetching } = useQuery({
    queryKey: ["jobs"],
    queryFn: api.jobs,
  });
  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteJob(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-neutral-400">
        {String(error).includes("409")
          ? "未配置 database.url，定时任务不可用"
          : String(error)}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">定时任务</h1>
          <p className="text-sm text-neutral-400">
            定时总结、定时日报与用户提醒；删除后立即生效
          </p>
        </div>
        <button
          onClick={() => refetch()}
          className="flex items-center gap-1.5 rounded-lg border border-neutral-200 px-3 py-1.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          <RefreshCw className={`size-4 ${isFetching ? "animate-spin" : ""}`} />
          刷新
        </button>
      </div>

      {jobs?.length === 0 && (
        <div className="rounded-xl border border-dashed border-neutral-300 p-10 text-center text-sm text-neutral-400 dark:border-neutral-700">
          还没有任何定时任务。可在群里用「总结定时 22」「日报定时 8」设置，
          或让用户对波奇说「明早九点提醒我…」
        </div>
      )}

      <div className="space-y-2">
        {jobs?.map((job) => (
          <div
            key={job.id}
            className="flex items-center gap-4 rounded-xl border border-neutral-200 bg-white px-4 py-3 dark:border-neutral-800 dark:bg-neutral-900"
          >
            <span className="w-16 shrink-0 rounded-md bg-accent/10 px-2 py-1 text-center text-xs font-medium text-accent-strong">
              {kindLabel(job.kind)}
            </span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm">
                {payloadLabel(job) || sessionLabel(job.session_key)}
              </div>
              <div className="mt-0.5 flex gap-3 text-xs text-neutral-400">
                <span>{scheduleLabel(job)}</span>
                <span>{sessionLabel(job.session_key)}</span>
                {job.created_by && <span>由 {job.created_by} 设置</span>}
                <span>#{job.id}</span>
              </div>
            </div>
            <button
              onClick={() => {
                if (confirm(`删除任务 #${job.id}（${kindLabel(job.kind)}）？`))
                  deleteMutation.mutate(job.id);
              }}
              className="shrink-0 rounded-lg p-2 text-neutral-400 hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950"
              title="删除"
            >
              <Trash2 className="size-4" />
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}
