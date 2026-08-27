import { useQuery } from "@tanstack/react-query";
import { RefreshCw } from "lucide-react";
import { useState } from "react";

import { api } from "../api";

const KIND_LABELS: Record<string, string> = {
  command: "指令",
  tool: "工具",
  chat: "对话",
};

const KIND_STYLES: Record<string, string> = {
  command: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  tool: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
  chat: "bg-pink-500/10 text-pink-600 dark:text-pink-400",
};

const WINDOWS = [7, 14, 30, 90];

function StatCard({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-xl border border-neutral-200 bg-white px-4 py-3 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="text-xs text-neutral-400">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">
        {value.toLocaleString()}
      </div>
    </div>
  );
}

function DailyChart({ daily }: { daily: { day: string; count: number }[] }) {
  const max = Math.max(1, ...daily.map((d) => d.count));
  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="mb-3 text-sm font-medium">每日调用量</div>
      <div className="flex h-32 items-end gap-1">
        {daily.map((d) => (
          <div
            key={d.day}
            className="group relative flex-1 rounded-t bg-accent/70 transition-colors hover:bg-accent"
            style={{ height: `${Math.max(2, (d.count / max) * 100)}%` }}
          >
            <div className="pointer-events-none absolute -top-8 left-1/2 z-10 hidden -translate-x-1/2 whitespace-nowrap rounded bg-neutral-800 px-2 py-0.5 text-xs text-white group-hover:block dark:bg-neutral-700">
              {d.day.slice(5)} · {d.count}
            </div>
          </div>
        ))}
      </div>
      <div className="mt-1 flex justify-between text-xs text-neutral-400">
        <span>{daily[0]?.day}</span>
        <span>{daily[daily.length - 1]?.day}</span>
      </div>
    </div>
  );
}

function RankList({
  title,
  rows,
}: {
  title: string;
  rows: { key: string; label: string; badge?: string; count: number }[];
}) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900">
      <div className="mb-3 text-sm font-medium">{title}</div>
      {rows.length === 0 && (
        <div className="py-6 text-center text-sm text-neutral-400">暂无数据</div>
      )}
      <div className="space-y-2">
        {rows.map((r) => (
          <div key={r.key} className="flex items-center gap-2 text-sm">
            {r.badge && (
              <span
                className={`w-10 shrink-0 rounded px-1 py-0.5 text-center text-xs ${
                  KIND_STYLES[r.badge] ?? "bg-neutral-500/10 text-neutral-500"
                }`}
              >
                {KIND_LABELS[r.badge] ?? r.badge}
              </span>
            )}
            <span className="w-32 shrink-0 truncate" title={r.label}>
              {r.label}
            </span>
            <div className="h-2 min-w-0 flex-1 rounded-full bg-neutral-100 dark:bg-neutral-800">
              <div
                className="h-2 rounded-full bg-accent/80"
                style={{ width: `${(r.count / max) * 100}%` }}
              />
            </div>
            <span className="w-12 shrink-0 text-right text-xs tabular-nums text-neutral-500">
              {r.count.toLocaleString()}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function StatsPage() {
  const [days, setDays] = useState(14);
  const { data, error, refetch, isFetching } = useQuery({
    queryKey: ["stats", days],
    queryFn: () => api.stats(days),
  });

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-neutral-400">
        {String(error).includes("409")
          ? "未配置 database.url，调用统计不可用"
          : String(error)}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-4 overflow-y-auto p-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">调用统计</h1>
          <p className="text-sm text-neutral-400">
            指令 / LLM 工具 / AI 对话的使用情况（按 Asia/Shanghai 归日）
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-lg border border-neutral-200 p-0.5 dark:border-neutral-700">
            {WINDOWS.map((w) => (
              <button
                key={w}
                onClick={() => setDays(w)}
                className={`rounded-md px-2.5 py-1 text-xs ${
                  days === w
                    ? "bg-accent/10 font-medium text-accent-strong"
                    : "text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
                }`}
              >
                {w} 天
              </button>
            ))}
          </div>
          <button
            onClick={() => refetch()}
            className="flex items-center gap-1.5 rounded-lg border border-neutral-200 px-3 py-1.5 text-sm hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
          >
            <RefreshCw className={`size-4 ${isFetching ? "animate-spin" : ""}`} />
            刷新
          </button>
        </div>
      </div>

      {data && (
        <>
          <div className="grid grid-cols-3 gap-3">
            <StatCard label="今日调用" value={data.today} />
            <StatCard label={`近 ${data.days} 天`} value={data.total} />
            <StatCard label="累计" value={data.total_all} />
          </div>
          <DailyChart daily={data.daily} />
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            <RankList
              title="功能与工具排行"
              rows={data.by_name.slice(0, 15).map((r) => ({
                key: `${r.kind}:${r.name}`,
                label: r.name,
                badge: r.kind,
                count: r.count,
              }))}
            />
            <div className="space-y-4">
              <RankList
                title="活跃用户 Top 10"
                rows={data.top_users.map((r) => ({
                  key: r.user_id,
                  label: r.user_name
                    ? `${r.user_name}（${r.user_id}）`
                    : r.user_id,
                  count: r.count,
                }))}
              />
              <RankList
                title="活跃群 Top 10"
                rows={data.top_groups.map((r) => ({
                  key: r.group_id,
                  label: `群 ${r.group_id}`,
                  count: r.count,
                }))}
              />
            </div>
          </div>
        </>
      )}
    </div>
  );
}
