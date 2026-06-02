import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, useEffect } from "react";
import { api } from "../lib/api";
import type { DnsChannel } from "../types/api";
import { Button } from "../components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { Card, CardContent } from "../components/ui/card";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from "../components/ui/dialog";
import { Input } from "../components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../components/ui/select";
import { Label } from "../components/ui/label";
import { Plus, Trash, Play, Pencil, X } from "lucide-react";
import { toast } from "sonner";
import { useForm, useFieldArray, useWatch } from "react-hook-form";
import { useI18n } from "../components/LocaleProvider";

type ProviderField = {
  name: string;
  optional?: boolean;
  requiredOnCreate?: boolean;
  hintKey?: 'dns.cfTokenHint' | 'dns.cfLegacyHint';
};

const PROVIDERS: Array<{ id: string; name: string; fields: ProviderField[] }> = [
  {
    id: 'dns_cf',
    name: 'Cloudflare',
    fields: [
      { name: 'CF_Token', requiredOnCreate: false, hintKey: 'dns.cfTokenHint' },
      { name: 'CF_Key', optional: true, hintKey: 'dns.cfLegacyHint' },
      { name: 'CF_Email', optional: true, hintKey: 'dns.cfLegacyHint' },
    ],
  },
  { id: 'dns_ali', name: 'Aliyun', fields: [{ name: 'Ali_Key' }, { name: 'Ali_Secret' }] },
  { id: 'dns_tencent', name: 'Tencent Cloud', fields: [{ name: 'Tencent_SecretId' }, { name: 'Tencent_SecretKey' }] },
  { id: 'dns_dp', name: 'DNSPod', fields: [{ name: 'DP_Id' }, { name: 'DP_Key' }] },
  { id: 'dns_huaweicloud', name: 'Huawei Cloud', fields: [{ name: 'HUAWEICLOUD_USERNAME' }, { name: 'HUAWEICLOUD_PASSWORD' }, { name: 'HUAWEICLOUD_DOMAIN_NAME' }] },
  { id: 'dns_gd', name: 'GoDaddy', fields: [{ name: 'GD_Key' }, { name: 'GD_Secret' }] },
  { id: 'custom', name: 'Custom', fields: [] }, // Custom uses dynamic field array
];

type FormValues = {
  id?: string;
  name: string;
  provider: string;
  credentials: Record<string, string>;
  customFields: { key: string; value: string }[];
};

export function DnsChannels() {
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const [isAddOpen, setIsAddOpen] = useState(false);
  const [deleteId, setDeleteId] = useState<string | null>(null);

  const { data: channels = [], isLoading } = useQuery<DnsChannel[]>({
    queryKey: ['dns-channels'],
    queryFn: () => api.get<DnsChannel[]>('/admin/dns-channels'),
  });

  const form = useForm<FormValues>({
    defaultValues: { name: "", provider: "dns_cf", credentials: {}, customFields: [] },
  });

  const { fields: customFields, append, remove } = useFieldArray({
    control: form.control,
    name: "customFields"
  });

  const provider = useWatch({ control: form.control, name: 'provider' });

  useEffect(() => {
    if (provider === 'custom' && customFields.length === 0) {
      append({ key: '', value: '' });
    }
  }, [provider, customFields.length, append]);

  const saveMutation = useMutation({
    mutationFn: (data: FormValues) => {
      const payload: { name: string; provider: string; credentials: Record<string, string> } = { name: data.name, provider: data.provider, credentials: {} };

      if (data.provider === 'custom') {
        data.customFields.forEach(f => {
          const key = f.key.trim();
          const value = f.value.trim();
          if (key && value) payload.credentials[key] = value;
        });
      } else {
        payload.credentials = Object.fromEntries(
          Object.entries(data.credentials).map(([key, value]) => [key, value.trim()]).filter(([, value]) => value !== '')
        );
      }

      if (data.id) return api.patch(`/admin/dns-channels/${data.id}`, payload);
      return api.post('/admin/dns-channels', payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dns-channels'] });
      setIsAddOpen(false);
      form.reset({ name: "", provider: "dns_cf", credentials: {}, customFields: [] });
      toast.success(t("dns.saved"));
    },
    onError: (err: unknown) => toast.error((err as Error).message || t("dns.saveFailed"))
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.delete(`/admin/dns-channels/${id}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['dns-channels'] });
      setDeleteId(null);
      toast.success(t("dns.deleted"));
    },
    onError: (err: unknown) => toast.error((err as Error).message || t("dns.deleteFailed"))
  });

  const testMutation = useMutation({
    mutationFn: (id: string) => api.post(`/admin/dns-channels/${id}/test`),
    onSuccess: () => toast.success(t("dns.testPassed")),
    onError: (err: unknown) => toast.error(t("dns.testFailed", { message: (err as Error).message }))
  });

  const onSubmit = (values: FormValues) => {
    const isEdit = Boolean(values.id);
    const trimmedCredentials = Object.fromEntries(
      Object.entries(values.credentials || {}).map(([key, value]) => [key, value.trim()])
    );

    if (!isEdit && values.provider === 'dns_cf') {
      const hasToken = Boolean(trimmedCredentials.CF_Token);
      const hasLegacyPair = Boolean(trimmedCredentials.CF_Key && trimmedCredentials.CF_Email);
      if (!hasToken && !hasLegacyPair) {
        toast.error(t("dns.cfValidation"));
        return;
      }
    }

    if (!isEdit && values.provider !== 'dns_cf' && values.provider !== 'custom') {
      const missingField = selectedProviderInfo.fields.find((field) => (field.requiredOnCreate ?? !field.optional) && !trimmedCredentials[field.name]);
      if (missingField) {
        toast.error(t("dns.requiredField", { field: missingField.name }));
        return;
      }
    }

    if (!isEdit && values.provider === 'custom') {
      const invalidCustomField = values.customFields.find((field) => field.key.trim() === '' || field.value.trim() === '');
      if (invalidCustomField) {
        toast.error(t("dns.customValidation"));
        return;
      }
    }

    saveMutation.mutate(values);
  };

  const openEdit = (c: DnsChannel) => {
    const isCustom = c.provider === 'custom';
    const cFields = isCustom ? Object.keys(c.credentials).map((k) => ({ key: k, value: '' })) : [];

    // For non-custom, we don't load the masked values into the form inputs to avoid sending '***' back
    const creds: Record<string, string> = {};
    if (!isCustom) {
      const providerInfo = PROVIDERS.find(p => p.id === c.provider);
      providerInfo?.fields.forEach((field) => { creds[field.name] = ''; });
    }

    form.reset({
      id: c.id,
      name: c.name,
      provider: c.provider,
      credentials: creds,
      customFields: cFields
    });
    setIsAddOpen(true);
  };

  const selectedProviderInfo = PROVIDERS.find(p => p.id === provider) || PROVIDERS[0];
  const providerLabel = selectedProviderInfo.name;

  return (
    <div className="p-4 sm:p-6 w-full max-w-full overflow-x-hidden space-y-4 sm:space-y-6">
      <div className="flex flex-col sm:flex-row gap-3 sm:items-center sm:justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t("dns.title")}</h1>
        <Button className="w-full sm:w-auto" onClick={() => { form.reset({ id: undefined, name: "", provider: "dns_cf", credentials: {}, customFields: [] }); setIsAddOpen(true); }}>
          <Plus className="mr-2 h-4 w-4" /> {t("dns.add")}
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="w-full overflow-x-auto">
            <Table className="min-w-[720px]">
            <TableHeader>
              <TableRow>
                <TableHead>{t("table.name")}</TableHead>
                <TableHead>{t("table.provider")}</TableHead>
                <TableHead>{t("table.configuration")}</TableHead>
                <TableHead className="text-right">{t("table.actions")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading ? (
                <TableRow><TableCell colSpan={4} className="text-center py-8">{t("common.loading")}</TableCell></TableRow>
              ) : channels.length === 0 ? (
                <TableRow><TableCell colSpan={4} className="text-center py-8 text-muted-foreground">{t("dns.empty")}</TableCell></TableRow>
              ) : (
                channels.map((c) => (
                  <TableRow key={c.id}>
                    <TableCell className="font-medium">{c.name}</TableCell>
                    <TableCell>{PROVIDERS.find(p => p.id === c.provider)?.name || c.provider}</TableCell>
                    <TableCell className="text-muted-foreground text-sm">
                      {t("dns.configured", { count: Object.keys(c.credentials || {}).length })}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button variant="ghost" size="sm" onClick={() => testMutation.mutate(c.id)}>
                        <Play className="mr-2 h-4 w-4" /> {t("dns.test")}
                      </Button>
                      <Button variant="ghost" size="icon" onClick={() => openEdit(c)}>
                        <Pencil className="h-4 w-4" />
                      </Button>
                      <Button variant="ghost" size="icon" className="text-destructive hover:text-destructive" onClick={() => setDeleteId(c.id)}>
                        <Trash className="h-4 w-4" />
                      </Button>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Dialog open={isAddOpen} onOpenChange={setIsAddOpen}>
        <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-md max-h-[90vh] overflow-y-auto">
          <form onSubmit={form.handleSubmit(onSubmit)}>
            <DialogHeader>
              <DialogTitle>{form.getValues('id') ? t("dns.editTitle") : t("dns.addTitle")}</DialogTitle>
              <DialogDescription>{t("dns.description")}</DialogDescription>
            </DialogHeader>
            <div className="grid gap-4 py-4 max-h-[60vh] overflow-y-auto px-1">
              <div className="grid gap-2">
                <Label htmlFor="name">{t("dns.channelName")}</Label>
                <Input id="name" placeholder={t("dns.channelPlaceholder")} required {...form.register('name')} />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="provider">{t("table.provider")}</Label>
                <Select value={provider} onValueChange={(val) => val && form.setValue('provider', val)}>
                  <SelectTrigger>
                    <SelectValue>{providerLabel}</SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    {PROVIDERS.map(p => (
                      <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="p-4 bg-muted/50 rounded-md space-y-4 mt-2 border">
                <p className="text-sm font-medium">{t("dns.credentialsFor", { provider: selectedProviderInfo.name })}</p>
                {form.getValues('id') && <p className="text-xs text-muted-foreground">{t("dns.keepExisting")}</p>}

                {provider !== 'custom' ? (
                  selectedProviderInfo.fields.map((field: ProviderField) => (
                    <div key={field.name} className="grid gap-2">
                      <Label htmlFor={field.name}>
                        {field.name}
                        {field.optional ? ` (${t("common.optional")})` : ''}
                      </Label>
                      <Input
                        id={field.name}
                        type="password"
                        required={!form.getValues('id') && (field.requiredOnCreate ?? !field.optional)}
                        placeholder={form.getValues('id') ? "********" : ""}
                        {...form.register(`credentials.${field.name}`)}
                      />
                      {field.hintKey ? <p className="text-xs text-muted-foreground">{t(field.hintKey)}</p> : null}
                    </div>
                  ))
                ) : (
                  <div className="space-y-3">
                    {customFields.map((field, index) => (
                      <div key={field.id} className="flex flex-col sm:flex-row items-stretch sm:items-center gap-2">
                        <Input placeholder={t("dns.customKey")} required {...form.register(`customFields.${index}.key`)} className="w-full sm:flex-1" />
                        <Input type="password" placeholder={form.getValues('id') ? "********" : t("dns.customValue")} required={!form.getValues('id')} {...form.register(`customFields.${index}.value`)} className="w-full sm:flex-1" />
                        <Button type="button" variant="ghost" size="icon" onClick={() => remove(index)} className="self-end sm:self-auto">
                          <X className="h-4 w-4" />
                        </Button>
                      </div>
                    ))}
                    <Button type="button" variant="outline" size="sm" onClick={() => append({ key: '', value: '' })}>
                      <Plus className="mr-2 h-4 w-4" /> {t("dns.addField")}
                    </Button>
                  </div>
                )}
              </div>
            </div>
            <DialogFooter>
              <Button type="submit" disabled={saveMutation.isPending}>
                {saveMutation.isPending ? t("common.saving") : t("dns.saveChannel")}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteId} onOpenChange={(open) => !open && setDeleteId(null)}>
        <DialogContent className="w-[calc(100vw-2rem)] sm:max-w-md max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>{t("dns.deleteTitle")}</DialogTitle>
            <DialogDescription>
              {t("dns.deleteDescription")}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteId(null)}>{t("common.cancel")}</Button>
            <Button variant="destructive" onClick={() => deleteId && deleteMutation.mutate(deleteId)} disabled={deleteMutation.isPending}>
              {deleteMutation.isPending ? t("common.deleting") : t("common.delete")}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
