import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Route, Routes } from "react-router";

import { Layout } from "./Layout";
import { AssistantsPage } from "./pages/AssistantsPage";
import { ChatPage } from "./pages/ChatPage";
import { ConversationsPage } from "./pages/ConversationsPage";
import { JobsPage } from "./pages/JobsPage";
import { MemoriesPage } from "./pages/MemoriesPage";
import { PermsPage } from "./pages/PermsPage";
import { ProvidersPage } from "./pages/ProvidersPage";
import { StatsPage } from "./pages/StatsPage";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout />}>
            <Route index element={<ChatPage />} />
            <Route path="assistants" element={<AssistantsPage />} />
            <Route path="conversations" element={<ConversationsPage />} />
            <Route path="memories" element={<MemoriesPage />} />
            <Route path="jobs" element={<JobsPage />} />
            <Route path="perms" element={<PermsPage />} />
            <Route path="stats" element={<StatsPage />} />
            <Route path="providers" element={<ProvidersPage />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
