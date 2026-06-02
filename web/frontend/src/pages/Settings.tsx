import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { AuthAccount, BackupPayload, Settings as SettingsType } from "../types/api";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, CardFooter } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "../components/ui/dialog";
import { toast } from "sonner";
import { useForm, useWatch } from "react-hook-form";
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { Save, Bell, HardDrive, Terminal, Languages, Download, Upload, Shield } from "lucide-react";
import { type Language, useI18n } from "../components/LocaleProvider";

export function Settings() {
  const queryClient = useQueryClient();
  const { language, setLanguage, t } = useI18n();
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [pendingRestore, setPendingRestore] = useState<{ fileName: string; payload: BackupPayload } | null>(null);
  const [accountUsernameDraft, setAccountUsernameDraft] = useState<string | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmNewPassword, setConfirmNewPassword] = useState("");

  const { data: settings, isLoading } = useQuery<SettingsType>({
    queryKey: ['settings'],
    queryFn: () => api.get('/admin/settings'),
  });
  const { data: account } = useQuery<AuthAccount>({
    queryKey: ['auth-account'],
    queryFn: () => api.get('/auth/account'),
  });

  const form = useForm<SettingsType>({
    defaultValues: {
      webdav: { url: '', auth: '' },
      telegram: { botToken: '', chatId: '' },
      acme: { acmeHome: '', stagingBase: '/tmp/acme_staging', defaultRenewDays: 30, defaultCa: 'letsencrypt', accountEmail: '' },
      node: { publicBaseUrl: '' }
    }
  });
  const selectedAcmeCa = useWatch({ control: form.control, name: "acme.defaultCa" });
  const acmeCaLabel = selectedAcmeCa === "zerossl" ? t("settings.caZeroSsl") : t("settings.caLetsEncrypt");
  const languageLabel = language === "zh-CN" ? t("language.zh") : t("language.en");

  useEffect(() => {
    if (settings) {
      form.reset(settings);
    }
  }, [settings, form]);

  const updateMutation = useMutation({
    mutationFn: (data: SettingsType) => api.patch('/admin/settings', data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      toast.success(t("settings.saved"));
    },
    onError: (err: unknown) => toast.error((err as Error).message || t("settings.saveFailed"))
  });

  const testWebDavMutation = useMutation({
    mutationFn: (payload: SettingsType["webdav"]) => api.post('/admin/settings/webdav/test', payload),
    onSuccess: () => toast.success(t("settings.webdavOk")),
    onError: (err: unknown) => toast.error(t("settings.webdavFailed", { message: (err as Error).message }))
  });

  const testTgMutation = useMutation({
    mutationFn: (payload: SettingsType["telegram"]) => api.post('/admin/settings/telegram/test', payload),
    onSuccess: () => toast.success(t("settings.telegramOk")),
    onError: (err: unknown) => toast.error(t("settings.telegramFailed", { message: (err as Error).message }))
  });

  const restoreMutation = useMutation({
    mutationFn: (payload: BackupPayload) => api.post('/admin/backup/restore', payload),
    onSuccess: async () => {
      await queryClient.invalidateQueries();
      toast.success(t("settings.restoreSuccess"));
      setPendingRestore(null);
    },
    onError: (err: unknown) => toast.error(t("settings.restoreFailed", { message: (err as Error).message }))
  });
  const updateAccountMutation = useMutation({
    mutationFn: (payload: { username: string; currentPassword: string; newPassword: string }) => api.patch<AuthAccount>('/auth/account', payload),
    onSuccess: async (data) => {
      setAccountUsernameDraft(data.username);
      setCurrentPassword("");
      setNewPassword("");
      setConfirmNewPassword("");
      await queryClient.invalidateQueries({ queryKey: ['auth-account'] });
      toast.success(t("settings.accountUpdated"));
    },
    onError: (err: unknown) => toast.error(t("settings.accountUpdateFailed", { message: (err as Error).message })),
  });

  const onSubmit = (values: SettingsType) => {
    updateMutation.mutate(values);
  };

  const downloadBackup = async () => {
    try {
      const { blob, fileName } = await api.downloadJson('/admin/backup');
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = fileName || `ssl-sync-backup-${new Date().toISOString().replaceAll(':', '-')}.json`;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      URL.revokeObjectURL(url);
      toast.success(t("settings.backupDownloaded"));
    } catch (err) {
      toast.error(t("settings.backupDownloadFailed", { message: (err as Error).message }));
    }
  };

  const onPickRestoreFile = () => {
    fileInputRef.current?.click();
  };

  const onRestoreFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) {
      return;
    }

    try {
      const raw = await file.text();
      const parsed = JSON.parse(raw) as BackupPayload;
      if (!parsed || typeof parsed !== "object" || !parsed.settings || !Array.isArray(parsed.dnsChannels)) {
        throw new Error(t("settings.invalidBackup"));
      }
      setPendingRestore({ fileName: file.name, payload: parsed });
    } catch (err) {
      toast.error(t("settings.restoreParseFailed", { message: (err as Error).message }));
      event.target.value = "";
    }
  };

  const confirmRestore = () => {
    if (!pendingRestore) {
      return;
    }
    restoreMutation.mutate(pendingRestore.payload, {
      onSuccess: () => {
        if (fileInputRef.current) {
          fileInputRef.current.value = "";
        }
      },
    });
  };

  const closeRestoreDialog = () => {
    if (restoreMutation.isPending) {
      return;
    }
    setPendingRestore(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  if (isLoading) return <div className="p-8">{t("common.loading")}</div>;

  const accountUsername = accountUsernameDraft ?? account?.username ?? "";

  const submitAccountUpdate = () => {
    if (newPassword !== confirmNewPassword) {
      toast.error(t("settings.accountPasswordMismatch"));
      return;
    }
    if (account && accountUsername.trim() === account.username && !newPassword) {
      toast.error(t("settings.accountNoChanges"));
      return;
    }
    updateAccountMutation.mutate({
      username: accountUsername.trim(),
      currentPassword,
      newPassword,
    });
  };

  return (
    <div className="p-4 sm:p-6 w-full max-w-4xl overflow-x-hidden space-y-4 sm:space-y-6">
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t("settings.title")}</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Languages className="h-5 w-5" /> {t("settings.interface")}</CardTitle>
          <CardDescription>{t("settings.interfaceDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2">
          <Label htmlFor="language">{t("settings.language")}</Label>
          <Select value={language} onValueChange={(value) => setLanguage(value as Language)}>
            <SelectTrigger id="language" className="w-full sm:w-[220px]">
              <SelectValue>{languageLabel}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="zh-CN">{t("language.zh")}</SelectItem>
              <SelectItem value="en-US">{t("language.en")}</SelectItem>
            </SelectContent>
          </Select>
          <p className="text-sm text-muted-foreground">{t("settings.languageDescription")}</p>
          <div className="grid gap-2 pt-4">
            <Label htmlFor="node.publicBaseUrl">{t("settings.nodePublicBaseUrl")}</Label>
            <Input
              id="node.publicBaseUrl"
              placeholder="https://ssl.example.com"
              {...form.register('node.publicBaseUrl')}
            />
            <p className="text-sm text-muted-foreground">{t("settings.nodePublicBaseUrlDescription")}</p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Shield className="h-5 w-5" /> {t("settings.account")}</CardTitle>
          <CardDescription>{t("settings.accountDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-2">
              <Label htmlFor="account.username">{t("settings.currentUsername")}</Label>
              <Input
                id="account.username"
                value={accountUsername}
                onChange={(e) => setAccountUsernameDraft(e.target.value)}
                autoComplete="username"
              />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="account.currentPassword">{t("settings.currentPassword")}</Label>
            <Input
              id="account.currentPassword"
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              autoComplete="current-password"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="account.newPassword">{t("settings.newPassword")}</Label>
            <Input
              id="account.newPassword"
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              autoComplete="new-password"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="account.confirmNewPassword">{t("settings.confirmNewPassword")}</Label>
            <Input
              id="account.confirmNewPassword"
              type="password"
              value={confirmNewPassword}
              onChange={(e) => setConfirmNewPassword(e.target.value)}
              autoComplete="new-password"
            />
            <p className="text-sm text-muted-foreground">{t("settings.newPasswordHint")}</p>
          </div>
        </CardContent>
        <CardFooter className="flex justify-end">
          <Button className="w-full sm:w-auto" type="button" onClick={submitAccountUpdate} disabled={updateAccountMutation.isPending}>
            <Save className="mr-2 h-4 w-4" /> {updateAccountMutation.isPending ? t("common.saving") : t("settings.updateAccount")}
          </Button>
        </CardFooter>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><HardDrive className="h-5 w-5" /> {t("settings.backup")}</CardTitle>
          <CardDescription>{t("settings.backupDescription")}</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={onRestoreFileChange}
          />
          <p className="text-sm text-muted-foreground">{t("settings.backupHint")}</p>
        </CardContent>
        <CardFooter className="flex flex-col sm:flex-row justify-between gap-2">
          <Button className="w-full sm:w-auto" variant="outline" type="button" onClick={downloadBackup}>
            <Download className="mr-2 h-4 w-4" /> {t("settings.downloadBackup")}
          </Button>
          <Button className="w-full sm:w-auto" type="button" onClick={onPickRestoreFile}>
            <Upload className="mr-2 h-4 w-4" /> {t("settings.restoreBackup")}
          </Button>
        </CardFooter>
      </Card>

      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><HardDrive className="h-5 w-5" /> {t("settings.webdav")}</CardTitle>
            <CardDescription>{t("settings.webdavDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="webdav.url">{t("settings.webdavUrl")}</Label>
              <Input id="webdav.url" placeholder="https://dav.example.com/certs" {...form.register('webdav.url')} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="webdav.auth">{t("settings.webdavAuth")}</Label>
              <Input id="webdav.auth" type="password" placeholder="admin:password123" {...form.register('webdav.auth')} />
            </div>
          </CardContent>
          <CardFooter className="flex flex-col sm:flex-row justify-between gap-2">
            <Button className="w-full sm:w-auto" variant="outline" type="button" onClick={() => testWebDavMutation.mutate(form.getValues("webdav"))} disabled={testWebDavMutation.isPending}>
              {testWebDavMutation.isPending ? t("common.testing") : t("settings.testConnection")}
            </Button>
          </CardFooter>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Bell className="h-5 w-5" /> {t("settings.telegram")}</CardTitle>
            <CardDescription>{t("settings.telegramDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="telegram.botToken">{t("settings.botToken")}</Label>
              <Input id="telegram.botToken" type="password" placeholder="123456789:ABCDefgh..." {...form.register('telegram.botToken')} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="telegram.chatId">{t("settings.chatId")}</Label>
              <Input id="telegram.chatId" placeholder="-10012345678" {...form.register('telegram.chatId')} />
            </div>
          </CardContent>
          <CardFooter className="flex flex-col sm:flex-row justify-between gap-2">
            <Button className="w-full sm:w-auto" variant="outline" type="button" onClick={() => testTgMutation.mutate(form.getValues("telegram"))} disabled={testTgMutation.isPending}>
              {testTgMutation.isPending ? t("common.testing") : t("settings.testNotification")}
            </Button>
          </CardFooter>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2"><Terminal className="h-5 w-5" /> {t("settings.acme")}</CardTitle>
            <CardDescription>{t("settings.acmeDescription")}</CardDescription>
          </CardHeader>
          <CardContent className="grid gap-4">
            <div className="grid gap-2">
              <Label htmlFor="acme.acmeHome">{t("settings.acmeHome")}</Label>
              <Input id="acme.acmeHome" placeholder="/root/.acme.sh" {...form.register('acme.acmeHome')} />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="acme.defaultCa">{t("settings.defaultCa")}</Label>
              <Select value={selectedAcmeCa || "letsencrypt"} onValueChange={(value) => form.setValue("acme.defaultCa", value ?? "letsencrypt", { shouldDirty: true })}>
                <SelectTrigger id="acme.defaultCa" className="w-full sm:w-[240px]">
                  <SelectValue>{acmeCaLabel}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="letsencrypt">{t("settings.caLetsEncrypt")}</SelectItem>
                  <SelectItem value="zerossl">{t("settings.caZeroSsl")}</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="acme.accountEmail">{t("settings.accountEmail")}</Label>
              <Input id="acme.accountEmail" placeholder="name@example.com" {...form.register('acme.accountEmail')} />
              <p className="text-sm text-muted-foreground">{t("settings.accountEmailHint")}</p>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="acme.defaultRenewDays">{t("settings.renewDays")}</Label>
              <Input id="acme.defaultRenewDays" type="number" {...form.register('acme.defaultRenewDays', { valueAsNumber: true })} />
            </div>
          </CardContent>
        </Card>

        <div className="flex flex-col sm:flex-row justify-end gap-3 pb-10">
          <Button className="w-full sm:w-auto" type="button" variant="outline" onClick={() => form.reset(settings)}>{t("settings.discard")}</Button>
          <Button className="w-full sm:w-auto" type="submit" disabled={updateMutation.isPending}>
            <Save className="mr-2 h-4 w-4" /> {updateMutation.isPending ? t("common.saving") : t("settings.saveAll")}
          </Button>
        </div>
      </form>

      <Dialog open={!!pendingRestore} onOpenChange={(open) => !open && closeRestoreDialog()}>
        <DialogContent className="w-[calc(100vw-2rem)] max-w-lg max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("settings.restoreTitle")}</DialogTitle>
            <DialogDescription>
              {pendingRestore
                ? t("settings.restoreDescription", { fileName: pendingRestore.fileName })
                : t("settings.restoreDescriptionFallback")}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 text-sm text-muted-foreground">
            <p>{t("settings.restoreIncludes")}</p>
            <p>{t("settings.restoreWarning")}</p>
          </div>
          <DialogFooter>
            <Button variant="outline" type="button" onClick={closeRestoreDialog} disabled={restoreMutation.isPending}>
              {t("common.cancel")}
            </Button>
            <Button type="button" onClick={confirmRestore} disabled={restoreMutation.isPending}>
              {restoreMutation.isPending ? t("settings.restoring") : t("settings.confirmRestore")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
