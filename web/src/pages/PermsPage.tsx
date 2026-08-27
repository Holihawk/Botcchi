import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Moon, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { api, type GroupPermView } from "../api";

function untilLabel(until: string | null) {
  if (!until) return "永久";
  return `至 ${new Date(until).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

function banTarget(userId: string | null, groupId: string | null) {
  if (userId && groupId) return `用户 ${userId} @ 群 ${groupId}`;
  if (userId) return `用户 ${userId}（全局）`;
  return `群 ${groupId}（整群）`;
}

const cardCls =
  "rounded-xl border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900";
const inputCls =
  "rounded-lg border border-neutral-200 bg-transparent px-2.5 py-1.5 text-sm outline-none focus:border-accent dark:border-neutral-700";

function BansSection() {
  const queryClient = useQueryClient();
  const { data: bans } = useQuery({ queryKey: ["perm-bans"], queryFn: api.permBans });
  const remove = useMutation({
    mutationFn: api.deleteBan,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["perm-bans"] }),
  });

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold">封禁</h2>
      {bans?.length === 0 && (
        <div className="text-sm text-neutral-400">
          暂无封禁。可在群里对波奇说「封禁 @某人 3天」
        </div>
      )}
      {bans?.map((ban) => (
        <div key={ban.id} className={`${cardCls} flex items-center gap-3 px-4 py-2.5`}>
          <div className="min-w-0 flex-1">
            <div className="truncate text-sm">{banTarget(ban.user_id, ban.group_id)}</div>
            <div className="mt-0.5 flex gap-3 text-xs text-neutral-400">
              <span>{untilLabel(ban.until)}</span>
              {ban.reason && <span>{ban.reason}</span>}
              <span>由 {ban.operator || "?"} 操作</span>
              {ban.by_superuser && <span className="text-amber-500">超管</span>}
            </div>
          </div>
          <button
            onClick={() => confirm("解除该封禁？") && remove.mutate(ban.id)}
            className="shrink-0 rounded-lg p-2 text-neutral-400 hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950"
            title="解封"
          >
            <Trash2 className="size-4" />
          </button>
        </div>
      ))}
    </section>
  );
}

function LevelsSection() {
  const queryClient = useQueryClient();
  const { data: grants } = useQuery({
    queryKey: ["perm-levels"],
    queryFn: api.permLevels,
  });
  const [userId, setUserId] = useState("");
  const [groupId, setGroupId] = useState("");
  const [level, setLevel] = useState("5");
  const put = useMutation({
    mutationFn: api.putLevel,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["perm-levels"] });
      setUserId("");
    },
  });
  const remove = useMutation({
    mutationFn: api.deleteLevel,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["perm-levels"] }),
  });

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold">权限等级授予</h2>
      <p className="text-xs text-neutral-400">
        群主/群管理员自动获得隐式等级，此处为额外授予；群号留空表示全局生效
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={userId}
          onChange={(e) => setUserId(e.target.value)}
          placeholder="用户 QQ 号"
          className={`${inputCls} w-36`}
        />
        <input
          value={groupId}
          onChange={(e) => setGroupId(e.target.value)}
          placeholder="群号（可空）"
          className={`${inputCls} w-36`}
        />
        <input
          value={level}
          onChange={(e) => setLevel(e.target.value)}
          placeholder="等级"
          className={`${inputCls} w-16`}
        />
        <button
          disabled={!userId.trim() || !/^\d+$/.test(level)}
          onClick={() =>
            put.mutate({
              user_id: userId.trim(),
              group_id: groupId.trim() || null,
              level: Number(level),
            })
          }
          className="flex items-center gap-1 rounded-lg bg-accent px-3 py-1.5 text-sm text-white disabled:opacity-40"
        >
          <Plus className="size-4" /> 授予
        </button>
      </div>
      {grants?.map((g) => (
        <div key={g.id} className={`${cardCls} flex items-center gap-3 px-4 py-2.5`}>
          <span className="w-12 shrink-0 rounded-md bg-accent/10 px-2 py-1 text-center text-xs font-medium text-accent-strong">
            Lv.{g.level}
          </span>
          <div className="min-w-0 flex-1 text-sm">
            {g.user_id}
            <span className="ml-2 text-xs text-neutral-400">
              {g.group_id ? `群 ${g.group_id}` : "全局"}
              {g.granted_by && ` · 由 ${g.granted_by} 授予`}
            </span>
          </div>
          <button
            onClick={() => confirm(`取消 ${g.user_id} 的授权？`) && remove.mutate(g.id)}
            className="shrink-0 rounded-lg p-2 text-neutral-400 hover:bg-red-50 hover:text-red-500 dark:hover:bg-red-950"
            title="取消授权"
          >
            <Trash2 className="size-4" />
          </button>
        </div>
      ))}
    </section>
  );
}

function GroupCard({ group }: { group: GroupPermView }) {
  const queryClient = useQueryClient();
  const { data: features } = useQuery({
    queryKey: ["perm-features"],
    queryFn: api.permFeatures,
  });
  const put = useMutation({
    mutationFn: (patch: { active?: boolean; su_blocked?: string[] }) =>
      api.putGroup(group.group_id, patch),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["perm-groups"] }),
  });

  const toggleFeature = (key: string) => {
    const next = group.su_blocked.includes(key)
      ? group.su_blocked.filter((k) => k !== key)
      : [...group.su_blocked, key];
    put.mutate({ su_blocked: next });
  };

  return (
    <div className={`${cardCls} space-y-2 px-4 py-3`}>
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium">群 {group.group_id}</span>
        {!group.active && (
          <span className="flex items-center gap-1 text-xs text-amber-500">
            <Moon className="size-3" /> 休眠中
          </span>
        )}
        <button
          onClick={() => put.mutate({ active: !group.active })}
          className="ml-auto rounded-lg border border-neutral-200 px-2.5 py-1 text-xs hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
        >
          {group.active ? "休眠" : "唤醒"}
        </button>
      </div>
      <div className="flex flex-wrap gap-1.5">
        {features?.map((f) => {
          const suBlocked = group.su_blocked.includes(f.key);
          const adminBlocked = group.blocked.includes(f.key);
          return (
            <button
              key={f.key}
              onClick={() => toggleFeature(f.key)}
              title={`${f.description}${adminBlocked ? "（群管已关闭）" : ""}`}
              className={`rounded-full px-2.5 py-1 text-xs transition-colors ${
                suBlocked
                  ? "bg-red-100 text-red-600 line-through dark:bg-red-950"
                  : adminBlocked
                    ? "bg-amber-100 text-amber-600 dark:bg-amber-950"
                    : "bg-neutral-100 text-neutral-600 dark:bg-neutral-800 dark:text-neutral-300"
              }`}
            >
              {f.key}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function GroupsSection() {
  const { data: groups } = useQuery({
    queryKey: ["perm-groups"],
    queryFn: api.permGroups,
  });

  return (
    <section className="space-y-2">
      <h2 className="text-sm font-semibold">群设置</h2>
      <p className="text-xs text-neutral-400">
        点击功能标签切换超管禁用（红色 = 已禁用，群管无法开启；黄色 = 群管自行关闭）。
        没调整过设置的群不在列表中
      </p>
      {groups?.length === 0 && (
        <div className="text-sm text-neutral-400">所有群都在默认设置</div>
      )}
      {groups?.map((g) => (
        <GroupCard key={g.group_id} group={g} />
      ))}
    </section>
  );
}

export function PermsPage() {
  const { error } = useQuery({ queryKey: ["perm-bans"], queryFn: api.permBans });

  if (error) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-neutral-400">
        {String(error).includes("409")
          ? "未配置 database.url，权限管理不可用"
          : String(error)}
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-4xl space-y-8 overflow-y-auto p-6">
      <div>
        <h1 className="text-lg font-semibold">权限</h1>
        <p className="text-sm text-neutral-400">
          封禁、数值等级授予与群功能开关；群里也可用「封禁 / 授权 / 关闭功能」等指令管理
        </p>
      </div>
      <BansSection />
      <LevelsSection />
      <GroupsSection />
    </div>
  );
}
