import { BrowserRouter, Routes, Route, Navigate, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { ThemeProvider } from "./components/ThemeProvider";
import { AuthProvider, useAuth } from "./components/AuthProvider";
import { LocaleProvider, useI18n } from "./components/LocaleProvider";
import { Layout } from "./components/layout/Layout";
import { Toaster } from "./components/ui/sonner";
import { TooltipProvider } from "./components/ui/tooltip";
import { api } from "./lib/api";
import type { AuthStatus } from "./types/api";

// Pages
import { Login } from "./pages/Login";
import { Setup } from "./pages/Setup";
import { Dashboard } from "./pages/Dashboard";
import { Domains } from "./pages/Domains";
import { Nodes } from "./pages/Nodes";
import { NodeDetail } from "./pages/NodeDetail";
import { DnsChannels } from "./pages/DnsChannels";
import { Jobs } from "./pages/Jobs";
import { Settings } from "./pages/Settings";


const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      refetchOnWindowFocus: false,
    },
  },
});

function AuthBootstrapGate() {
  const location = useLocation();
  const { token } = useAuth();
  const { t } = useI18n();
  const { data, isLoading, isError } = useQuery({
    queryKey: ["auth-status"],
    queryFn: () => api.get<AuthStatus>("/auth/status"),
  });

  if (isLoading) {
    return <div className="flex min-h-screen items-center justify-center text-sm text-muted-foreground">{t("common.loading")}</div>;
  }

  if (isError || !data) {
    return (
      <div className="flex min-h-screen items-center justify-center p-4">
        <div className="max-w-md rounded-lg border bg-background p-6 text-sm text-muted-foreground shadow-sm">
          {t("setup.statusFailed")}
        </div>
      </div>
    );
  }

  if (data.setupRequired && location.pathname !== "/setup") {
    return <Navigate to="/setup" replace />;
  }

  if (!data.setupRequired && location.pathname === "/setup") {
    return <Navigate to={token ? "/" : "/login"} replace />;
  }

  if (token && location.pathname === "/login") {
    return <Navigate to="/" replace />;
  }

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/setup" element={<Setup />} />
      <Route element={<Layout />}>
        <Route path="/" element={<Dashboard />} />
        <Route path="/domains" element={<Domains />} />
        <Route path="/nodes" element={<Nodes />} />
        <Route path="/nodes/:id" element={<NodeDetail />} />
        <Route path="/dns-channels" element={<DnsChannels />} />
        <Route path="/jobs" element={<Jobs />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <ThemeProvider defaultTheme="system" storageKey="ssl-sync-theme">
      <QueryClientProvider client={queryClient}>
        <LocaleProvider>
          <TooltipProvider>
            <BrowserRouter>
              <AuthProvider>
                <AuthBootstrapGate />
              </AuthProvider>
            </BrowserRouter>
            <Toaster />
          </TooltipProvider>
        </LocaleProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
