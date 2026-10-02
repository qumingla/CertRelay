import { lazy, Suspense } from "react";
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
const Login = lazy(() => import("./pages/Login").then(module => ({ default: module.Login })));
const Setup = lazy(() => import("./pages/Setup").then(module => ({ default: module.Setup })));
const Dashboard = lazy(() => import("./pages/Dashboard").then(module => ({ default: module.Dashboard })));
const Domains = lazy(() => import("./pages/Domains").then(module => ({ default: module.Domains })));
const Nodes = lazy(() => import("./pages/Nodes").then(module => ({ default: module.Nodes })));
const NodeDetail = lazy(() => import("./pages/NodeDetail").then(module => ({ default: module.NodeDetail })));
const DnsChannels = lazy(() => import("./pages/DnsChannels").then(module => ({ default: module.DnsChannels })));
const Jobs = lazy(() => import("./pages/Jobs").then(module => ({ default: module.Jobs })));
const Settings = lazy(() => import("./pages/Settings").then(module => ({ default: module.Settings })));


const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15000,
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
    <Suspense fallback={<div className="p-6 text-muted-foreground" role="status">{t("common.loading")}</div>}>
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
    </Suspense>
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
