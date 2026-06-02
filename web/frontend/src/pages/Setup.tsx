import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Server } from "lucide-react";
import { useAuth } from "../components/AuthProvider";
import { useI18n } from "../components/LocaleProvider";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { ApiClientError, api } from "../lib/api";
import type { AuthStatus } from "../types/api";

export function Setup() {
  const { login } = useAuth();
  const { t } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password !== confirmPassword) {
      setError(t("setup.passwordMismatch"));
      return;
    }

    setLoading(true);
    setError("");

    try {
      const data = await api.post<{ token: string }>("/auth/bootstrap", { username, password });
      if (data.token) {
        queryClient.setQueryData<AuthStatus>(["auth-status"], {
          initialized: true,
          setupRequired: false,
        });
        login(data.token);
        return;
      }
      throw new Error(t("setup.invalidToken"));
    } catch (err: unknown) {
      if (err instanceof ApiClientError && err.status === 409 && err.data?.setupRequired === false) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof ApiClientError ? err.message : (err as Error).message || t("setup.failed"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-muted/30 p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1 text-center">
          <div className="mb-4 flex justify-center">
            <div className="rounded-full bg-primary/10 p-3">
              <Server className="h-8 w-8 text-primary" />
            </div>
          </div>
          <CardTitle className="text-2xl tracking-tight">{t("setup.title")}</CardTitle>
          <CardDescription>{t("setup.description")}</CardDescription>
        </CardHeader>
        <form onSubmit={handleSubmit}>
          <CardContent className="space-y-4">
            {error && (
              <div className="rounded-md bg-destructive/90 p-3 text-sm text-destructive-foreground">
                {error}
              </div>
            )}
            <Input
              id="setup-username"
              placeholder={t("setup.username")}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
            <Input
              id="setup-password"
              type="password"
              placeholder={t("setup.password")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
            />
            <Input
              id="setup-confirm-password"
              type="password"
              placeholder={t("setup.confirmPassword")}
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={8}
            />
          </CardContent>
          <CardFooter>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? t("setup.initializing") : t("setup.submit")}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
}
