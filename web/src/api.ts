// 后端 API 客户端与类型定义

export interface Status {
  version: string;
  default_assistant: string;
  storage: "database" | "memory";
  memory_enabled: boolean;
  assistants: string[];
  providers: string[];
}

export interface OutputPolicy {
  split: boolean;
  split_min_chars: number;
  max_segments: number;
  max_chars: number;
  delay_per_char: number;
  max_delay: number;
}

export interface GreetingPolicy {
  enabled: boolean;
  replies: string[];
  cooldown_seconds: number;
}

export interface Assistant {
  name: string;
  display_name: string;
  prompt: string;
  model: string;
  fallback_models: string[];
  temperature: number | null;
  max_tokens: number | null;
  tools: string[];
  max_tool_rounds: number;
  policy: OutputPolicy;
  greeting: GreetingPolicy;
  is_default: boolean;
}

export interface ToolInfo {
  name: string;
  description: string;
}

export interface ModelDetail {
  name: string;
  temperature?: number;
  max_tokens?: number;
}

export interface Provider {
  name: string;
  api_type: string;
  api_base: string;
  key_count: number;
  key_masks: string[];
  models: ModelDetail[];
}

export interface ProviderUpdate {
  api_base?: string;
  models?: ModelDetail[];
  add_keys?: string[];
  remove_key_indexes?: number[];
}

export interface ConversationSummary {
  session_key: string;
  message_count: number;
  last_at: string;
}

export interface MessageView {
  id: number;
  role: string;
  content: string;
  reasoning: string | null;
  created_at: string;
}

export interface MemoryScope {
  scope_key: string;
  count: number;
}

export interface MemoryEntry {
  id: number;
  content: string;
  created_at: string;
}

export type ChatEvent =
  | { type: "reasoning"; text: string }
  | { type: "text"; text: string }
  | { type: "tool_call"; name: string; arguments: string }
  | { type: "tool_result"; name: string; result: string }
  | { type: "done"; reply: string; assistant: string }
  | { type: "error"; message: string };

export interface FeatureInfo {
  key: string;
  description: string;
}

export interface BanView {
  id: number;
  user_id: string | null;
  group_id: string | null;
  until: string | null;
  reason: string;
  operator: string;
  by_superuser: boolean;
}

export interface LevelGrantView {
  id: number;
  user_id: string;
  group_id: string | null;
  level: number;
  granted_by: string;
}

export interface GroupPermView {
  group_id: string;
  active: boolean;
  blocked: string[];
  su_blocked: string[];
}

export interface ScheduledJobView {
  id: number;
  kind: string;
  session_key: string;
  next_run_at: string;
  recurrence: string | null;
  payload: Record<string, unknown>;
  created_by: string;
}

export interface UsageOverview {
  days: number;
  total: number;
  total_all: number;
  today: number;
  daily: { day: string; count: number }[];
  by_name: { kind: string; name: string; count: number }[];
  top_users: { user_id: string; user_name: string; count: number }[];
  top_groups: { group_id: string; count: number }[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`${resp.status}: ${body.slice(0, 300)}`);
  }
  return resp.json();
}

export const api = {
  status: () => request<Status>("/api/status"),
  assistants: () => request<Assistant[]>("/api/assistants"),
  tools: () => request<ToolInfo[]>("/api/tools"),
  updateAssistant: (name: string, patch: Partial<Assistant>) =>
    request<Assistant>(`/api/assistants/${name}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  createAssistant: (data: {
    name: string;
    display_name: string;
    model: string;
  }) =>
    request<Assistant>("/api/assistants", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  deleteAssistant: (name: string) =>
    request(`/api/assistants/${name}`, { method: "DELETE" }),
  providers: () => request<Provider[]>("/api/providers"),
  createProvider: (data: {
    name: string;
    api_base: string;
    api_keys: string[];
    models: ModelDetail[];
  }) =>
    request<Provider>("/api/providers", {
      method: "POST",
      body: JSON.stringify(data),
    }),
  updateProvider: (name: string, patch: ProviderUpdate) =>
    request<Provider>(`/api/providers/${name}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
  deleteProvider: (name: string) =>
    request(`/api/providers/${name}`, { method: "DELETE" }),
  reorderProviders: (names: string[]) =>
    request<Provider[]>("/api/providers/reorder", {
      method: "POST",
      body: JSON.stringify({ names }),
    }),
  conversations: () => request<ConversationSummary[]>("/api/conversations"),
  messages: (session: string) =>
    request<MessageView[]>(`/api/messages?session=${encodeURIComponent(session)}`),
  deleteConversation: (session: string) =>
    request(`/api/conversations?session=${encodeURIComponent(session)}`, {
      method: "DELETE",
    }),
  memoryScopes: () => request<MemoryScope[]>("/api/memories/scopes"),
  memories: (scope: string) =>
    request<MemoryEntry[]>(`/api/memories?scope=${encodeURIComponent(scope)}`),
  deleteMemory: (id: number) =>
    request(`/api/memories/${id}`, { method: "DELETE" }),
  resetChat: (conversationId: string) =>
    request("/api/chat/reset", {
      method: "POST",
      body: JSON.stringify({ conversation_id: conversationId }),
    }),
  stats: (days: number) => request<UsageOverview>(`/api/stats?days=${days}`),
  jobs: () => request<ScheduledJobView[]>("/api/jobs"),
  deleteJob: (id: number) => request(`/api/jobs/${id}`, { method: "DELETE" }),
  permFeatures: () => request<FeatureInfo[]>("/api/perms/features"),
  permBans: () => request<BanView[]>("/api/perms/bans"),
  deleteBan: (id: number) => request(`/api/perms/bans/${id}`, { method: "DELETE" }),
  permLevels: () => request<LevelGrantView[]>("/api/perms/levels"),
  putLevel: (data: { user_id: string; group_id?: string | null; level: number }) =>
    request("/api/perms/levels", { method: "PUT", body: JSON.stringify(data) }),
  deleteLevel: (id: number) =>
    request(`/api/perms/levels/${id}`, { method: "DELETE" }),
  permGroups: () => request<GroupPermView[]>("/api/perms/groups"),
  putGroup: (
    groupId: string,
    patch: { active?: boolean; su_blocked?: string[] },
  ) =>
    request(`/api/perms/groups/${groupId}`, {
      method: "PUT",
      body: JSON.stringify(patch),
    }),
};

export async function streamChat(
  conversationId: string,
  message: string,
  assistant: string | null,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      conversation_id: conversationId,
      message,
      assistant,
    }),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`请求失败: ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      try {
        onEvent(JSON.parse(line.slice(5).trim()) as ChatEvent);
      } catch {
        // 忽略不完整的块
      }
    }
  }
}
