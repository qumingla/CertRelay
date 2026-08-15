import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { createEventStream } from "../lib/sse";
import type { OverviewResponse, SystemEvent } from "../types/api";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Badge } from "../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Server, Globe, AlertTriangle, AlertCircle, Activity, Clock } from "lucide-react";
import { useI18n } from "../components/LocaleProvider";

export function Dashboard() {
  const { t, formatRelative, statusLabel, eventTypeLabel } = useI18n();
  const { data: overview, isLoading, error } = useQuery<OverviewResponse>({
    queryKey: ['overview'],
    queryFn: () => api.get('/admin/overview'),
    refetchInterval: 30000,
  });

  const [events, setEvents] = useState<SystemEvent[]>([]);

  useEffect(() => {
    const es = createEventStream();
    es.onMessage((event) => {
      setEvents((prev) => [event, ...prev].slice(0, 50));
    });

    return () => {
      es.close();
    };
  }, []);

  if (isLoading) return <div className="p-8 text-muted-foreground">{t("dashboard.loading")}</div>;
  if (error) return <div className="p-8 text-destructive">{t("dashboard.loadFailed")}</div>;
  if (!overview) return null;

  const { stats, certificates, nodes } = overview;

  return (
    <div className="p-4 sm:p-6 w-full max-w-full overflow-x-hidden space-y-4 sm:space-y-6">
      <div className="grid gap-4 grid-cols-1 sm:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">{t("dashboard.onlineNodes")}</CardTitle>
            <Server className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{stats.onlineNodes} / {stats.totalNodes}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">{t("dashboard.totalDomains")}</CardTitle>
            <Globe className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{stats.totalDomains}</div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">{t("dashboard.expiringSoon")}</CardTitle>
            <AlertTriangle className="h-4 w-4 text-warning text-yellow-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{stats.expiringSoon}</div>
            <p className="text-xs text-muted-foreground">{t("dashboard.within7Days")}</p>
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
            <CardTitle className="text-sm font-medium">{t("dashboard.failedJobs")}</CardTitle>
            <AlertCircle className="h-4 w-4 text-destructive" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{stats.failedJobs}</div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 grid-cols-1 lg:grid-cols-7">
        <div className="lg:col-span-5 space-y-4 sm:space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.certHealth")}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="w-full overflow-x-auto">
                <Table className="min-w-[720px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("table.domain")}</TableHead>
                    <TableHead>{t("table.status")}</TableHead>
                    <TableHead>{t("table.expiresIn")}</TableHead>
                    <TableHead className="text-right">{t("table.dnsChannel")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {certificates.length === 0 ? (
                    <TableRow><TableCell colSpan={4} className="text-center text-muted-foreground py-4">{t("dashboard.noDomains")}</TableCell></TableRow>
                  ) : certificates.slice(0, 5).map((cert) => (
                    <TableRow key={cert.id}>
                      <TableCell className="font-medium">{cert.domain}</TableCell>
                      <TableCell>
                        <Badge variant={cert.status === 'active' ? 'default' : cert.status === 'expiring' ? 'secondary' : 'destructive'}>
                          {statusLabel(cert.status)}
                        </Badge>
                      </TableCell>
                      <TableCell>{cert.daysRemaining !== null ? t("common.days", { days: cert.daysRemaining }) : '-'}</TableCell>
                      <TableCell className="text-right text-muted-foreground">{cert.dnsChannelId}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("dashboard.nodeHealth")}</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="w-full overflow-x-auto">
                <Table className="min-w-[720px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("table.node")}</TableHead>
                    <TableHead>{t("table.ip")}</TableHead>
                    <TableHead>{t("table.status")}</TableHead>
                    <TableHead className="text-right">{t("table.presenceTime")}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {nodes.length === 0 ? (
                    <TableRow><TableCell colSpan={4} className="text-center text-muted-foreground py-4">{t("dashboard.noNodes")}</TableCell></TableRow>
                  ) : nodes.slice(0, 5).map((node) => (
                    <TableRow key={node.id}>
                      <TableCell className="font-medium">{node.name}</TableCell>
                      <TableCell className="font-mono text-sm">{node.ip}</TableCell>
                      <TableCell>
                        <Badge variant={node.isOnline ? 'default' : 'destructive'}>
                          {node.isOnline ? t("status.online") : t("status.offline")}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right text-muted-foreground">
                        {node.isOnline
                          ? (node.lastHeartbeatAt ? formatRelative(node.lastHeartbeatAt) : t("common.never"))
                          : (node.offlineAt ? t("nodes.offlineSince", { time: formatRelative(node.offlineAt) }) : t("common.never"))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="lg:col-span-2 order-last lg:order-none">
          <Card className="h-full flex flex-col">
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2">
                <Activity className="h-4 w-4" />
                {t("dashboard.liveEvents")}
              </CardTitle>
            </CardHeader>
            <CardContent className="flex-1 overflow-y-auto max-h-[600px] pr-2">
              <div className="space-y-4">
                {events.length === 0 ? (
                  <div className="text-sm text-muted-foreground text-center py-8">{t("dashboard.waitingEvents")}</div>
                ) : (
                  events.map((evt) => (
                    <div key={evt.id} className="flex flex-col gap-1 border-b border-border/50 pb-3 last:border-0">
                      <div className="flex items-center justify-between">
                        <Badge variant={evt.level === 'error' ? 'destructive' : evt.level === 'warning' ? 'secondary' : 'outline'} className="text-[10px] px-1.5 py-0">
                          {eventTypeLabel(evt.type)}
                        </Badge>
                        <span className="text-[10px] text-muted-foreground flex items-center gap-1">
                          <Clock className="w-3 h-3" />
                          {formatRelative(evt.createdAt)}
                        </span>
                      </div>
                      <p className="text-sm">{evt.message}</p>
                    </div>
                  ))
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
