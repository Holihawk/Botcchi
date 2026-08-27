import { useQuery } from "@tanstack/react-query";
import {
  BarChart3,
  Bot,
  Brain,
  Cable,
  CalendarClock,
  MessageCircle,
  MessagesSquare,
  Moon,
  ShieldCheck,
  Sun,
} from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router";

import { api } from "./api";

const NAV = [
  { to: "/", label: "对话", icon: MessageCircle },
  { to: "/assistants", label: "人格", icon: Bot },
  { to: "/conversations", label: "会话", icon: MessagesSquare },
  { to: "/memories", label: "记忆", icon: Brain },
  { to: "/jobs", label: "定时任务", icon: CalendarClock },
  { to: "/perms", label: "权限", icon: ShieldCheck },
  { to: "/stats", label: "统计", icon: BarChart3 },
  { to: "/providers", label: "模型服务", icon: Cable },
];

function useDarkMode() {
  const [dark, setDark] = useState(
    () =>
      localStorage.getItem("theme") === "dark" ||
      (localStorage.getItem("theme") === null &&
        window.matchMedia("(prefers-color-scheme: dark)").matches),
  );
  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("theme", dark ? "dark" : "light");
  }, [dark]);
  return { dark, toggle: () => setDark((d) => !d) };
}

export function Layout() {
  const { dark, toggle } = useDarkMode();
  const { data: status } = useQuery({ queryKey: ["status"], queryFn: api.status });

  return (
    <div className="flex h-screen bg-neutral-50 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
      <aside className="flex w-52 shrink-0 flex-col border-r border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900">
        <div className="flex items-center gap-2 px-4 py-4">
          <span className="flex size-8 items-center justify-center rounded-lg bg-accent font-bold text-white">
            B
          </span>
          <div>
            <div className="font-semibold leading-tight">Botcchi</div>
            <div className="text-xs text-neutral-400">
              {status ? `v${status.version}` : "…"}
            </div>
          </div>
        </div>
        <nav className="flex-1 space-y-1 px-2">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${
                  isActive
                    ? "bg-accent/10 font-medium text-accent-strong"
                    : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800"
                }`
              }
            >
              <Icon className="size-4" />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="flex items-center justify-between border-t border-neutral-200 px-4 py-3 dark:border-neutral-800">
          <span className="text-xs text-neutral-400">
            {status?.storage === "database" ? "PostgreSQL" : "内存模式"}
          </span>
          <button
            onClick={toggle}
            className="rounded-lg p-1.5 text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
            title="切换主题"
          >
            {dark ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  );
}
