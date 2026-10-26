import { useEffect, useState } from "react";
import { Loader2, RefreshCw, Save, TestTube2 } from "lucide-react";
import { toast } from "sonner";
import { useTranslation } from "react-i18next";
import { api, type ChannelConfigEntry, type ChannelConfigResponse, type ChannelFieldHint } from "@/lib/api";

function labelFor(name: string, field: ChannelFieldHint): string {
  const key = field.help_key?.split(".").pop() || field.key;
  return `${name[0].toUpperCase()}${name.slice(1)} · ${key.replace(/_/g, " ")}`;
}

function inputValue(entry: ChannelConfigEntry, field: ChannelFieldHint): string | boolean {
  const value = entry.values[field.key];
  if (field.type === "bool") return Boolean(value);
  if (field.type === "list") return Array.isArray(value) ? value.join(", ") : String(value ?? "");
  return value == null ? "" : String(value);
}

function configPayload(values: Record<string, unknown>, fields: ChannelFieldHint[]): Record<string, unknown> {
  const payload = { ...values };
  for (const field of fields) {
    const value = payload[field.key];
    // Blank credential drafts preserve the saved secret. Only drafts, never
    // the masked server response, are sent back as replacement credentials.
    if (field.secret && (value == null || String(value).trim() === "")) {
      delete payload[field.key];
    } else if (field.type === "list" && typeof value === "string") {
      payload[field.key] = value.split(",").map((item) => item.trim()).filter(Boolean);
    } else if (field.type === "number" && typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
      payload[field.key] = Number(value);
    }
  }
  return payload;
}

export function ChannelConfigPanel() {
  const { t } = useTranslation();
  const [data, setData] = useState<ChannelConfigResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [loading, setLoading] = useState(false);

  const load = async () => {
    setLoading(true);
    try {
      const result = await api.getChannelConfig();
      setData(result);
      const name = selected && result.channels[selected] ? selected : Object.keys(result.channels)[0] || null;
      setSelected(name);
      if (name) setValues(result.channels[name].values);
    } catch (error) {
      toast.error(t("settings.channels.configLoadFailed", { defaultValue: "Failed to load channel configuration" }));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  const entry = selected && data ? data.channels[selected] : null;
  const setField = (field: ChannelFieldHint, raw: string | boolean) => {
    // Keep the draft intact while typing (including commas and decimals).
    setValues((current) => ({ ...current, [field.key]: raw }));
  };

  const save = async () => {
    if (!selected || !entry) return;
    setSaving(true);
    try {
      const result = await api.updateChannelConfig(selected, configPayload(values, entry.fields));
      setData((current) => current ? { ...current, channels: { ...current.channels, [selected]: result.channel } } : current);
      setValues(result.channel.values);
      toast.success(t("settings.channels.configSaved", { defaultValue: "Channel configuration saved" }));
    } catch (error) {
      toast.error(t("settings.channels.configSaveFailed", { defaultValue: "Failed to save channel configuration" }));
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    if (!selected || !entry) return;
    setTesting(true);
    try {
      const result = await api.testChannelConfig(selected, configPayload(values, entry.fields));
      if (result.ok) toast.success(t("settings.channels.connectionOk", { defaultValue: "Connection test passed" }));
      else toast.error(result.detail || t("settings.channels.connectionFailed", { defaultValue: "Connection test failed" }));
    } catch (error) {
      toast.error(t("settings.channels.connectionFailed", { defaultValue: "Connection test failed" }));
    } finally {
      setTesting(false);
    }
  };

  if (loading && !data) return <section className="rounded-lg border bg-card p-5"><Loader2 className="h-4 w-4 animate-spin" /></section>;
  if (!data || !entry || !selected) return null;

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">{t("settings.channels.configTitle", { defaultValue: "Channel configuration" })}</h2>
          <p className="text-sm text-muted-foreground">{t("settings.channels.configDescription", { defaultValue: "Edit channel settings. Credentials are masked and never returned in clear text." })}</p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading || saving || testing} className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm"><RefreshCw className="h-4 w-4" />{t("settings.channels.refresh", { defaultValue: "Refresh" })}</button>
      </div>
      <fieldset disabled={loading || saving || testing} className="grid min-w-0 gap-4 md:grid-cols-[180px_minmax(0,1fr)]">
        <div className="space-y-1">
          {Object.entries(data.channels).map(([name, item]) => <button key={name} type="button" onClick={() => { setSelected(name); setValues(item.values); }} className={`block w-full rounded-md px-3 py-2 text-start text-sm ${selected === name ? "bg-primary/10 text-primary" : "hover:bg-muted"}`}><span className="font-medium">{item.display_name || name}</span><span className="block text-xs text-muted-foreground">{name}</span></button>)}
        </div>
        <div className="space-y-4">
          {!entry.available && <p className="rounded-md border bg-muted/30 p-3 text-sm text-muted-foreground">{entry.install_hint || entry.error || t("settings.channels.unavailable", { defaultValue: "Channel unavailable" })}</p>}
          <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={Boolean(values.enabled ?? entry.enabled)} onChange={(event) => setField({ key: "enabled", type: "bool", secret: false, required: false }, event.target.checked)} />{t("settings.channels.enabled", { defaultValue: "Enabled" })}</label>
          {entry.fields.map((field) => {
            const secret = entry.secrets[field.key];
            const value = inputValue({ ...entry, values }, field);
            return <label key={field.key} className="grid gap-1 text-sm"><span>{labelFor(selected, field)}{field.required ? " *" : ""}{field.secret && secret?.set ? <span className="ms-2 text-xs text-muted-foreground">({secret.masked})</span> : null}</span>{field.type === "bool" ? <input type="checkbox" checked={Boolean(value)} onChange={(event) => setField(field, event.target.checked)} /> : <input className="w-full rounded-md border bg-background px-3 py-2" type={field.secret ? "password" : field.type === "number" ? "number" : "text"} step={field.type === "number" ? "any" : undefined} value={String(value)} placeholder={field.secret && secret?.set ? "•••••••• (unchanged)" : ""} onChange={(event) => setField(field, event.target.value)} />}</label>;
          })}
          <div className="flex flex-wrap gap-2"><button type="button" onClick={() => void save()} disabled={saving || !data.writable} className="inline-flex items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground"><Save className="h-4 w-4" />{saving ? t("settings.channels.saving", { defaultValue: "Saving…" }) : t("settings.channels.save", { defaultValue: "Save" })}</button><button type="button" onClick={() => void test()} disabled={testing || !entry.supports_test} className="inline-flex items-center gap-2 rounded-md border px-3 py-2 text-sm"><TestTube2 className="h-4 w-4" />{testing ? t("settings.channels.testing", { defaultValue: "Testing…" }) : t("settings.channels.test", { defaultValue: "Test connection" })}</button></div>
        </div>
      </fieldset>
    </section>
  );
}
