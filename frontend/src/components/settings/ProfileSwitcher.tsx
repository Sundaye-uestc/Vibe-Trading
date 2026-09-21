import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { KeyRound, Loader2, Pencil, Plus, Trash2, X, Zap } from "lucide-react";
import { toast } from "sonner";
import {
  api,
  type LLMProfileInfo,
  type LLMProfilesResponse,
  type LLMProfileUpsertRequest,
  type LLMProviderOption,
} from "@/lib/api";

const fieldClass =
  "w-full rounded-md border bg-background px-3 py-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-60";
const labelClass = "text-sm font-medium";
const hintClass = "text-xs text-muted-foreground";

const PROVIDER_GRADIENTS: Record<string, string> = {
  openai: "from-emerald-500 to-teal-600",
  "openai-codex": "from-emerald-600 to-green-700",
  deepseek: "from-blue-500 to-indigo-600",
  openrouter: "from-violet-500 to-purple-600",
  gemini: "from-sky-500 to-blue-600",
  groq: "from-orange-500 to-red-600",
  dashscope: "from-cyan-500 to-blue-500",
  qwen: "from-cyan-500 to-blue-500",
  zhipu: "from-teal-500 to-emerald-600",
  glm: "from-teal-500 to-emerald-600",
  moonshot: "from-slate-600 to-slate-800",
  minimax: "from-rose-500 to-pink-600",
  mimo: "from-amber-500 to-orange-600",
  zai: "from-indigo-500 to-violet-600",
  volcengine: "from-blue-600 to-cyan-500",
  ollama: "from-gray-600 to-gray-800",
};

function providerGradient(provider: string): string {
  return PROVIDER_GRADIENTS[provider] ?? "from-primary/70 to-primary";
}

function providerInitial(profile: LLMProfileInfo): string {
  const source = profile.provider_label || profile.name || "?";
  return source.trim().charAt(0).toUpperCase() || "?";
}

interface EditorState {
  id: string | null;
  name: string;
  provider: string;
  model_name: string;
  base_url: string;
  api_key: string;
  clear_api_key: boolean;
  temperature: number;
  timeout_seconds: number;
  max_retries: number;
  reasoning_effort: string;
}

function blankEditor(providers: LLMProviderOption[]): EditorState {
  const provider = providers[0];
  return {
    id: null,
    name: "",
    provider: provider?.name ?? "",
    model_name: provider?.default_model ?? "",
    base_url: provider?.default_base_url ?? "",
    api_key: "",
    clear_api_key: false,
    temperature: 0,
    timeout_seconds: 120,
    max_retries: 2,
    reasoning_effort: "",
  };
}

function editorFromProfile(profile: LLMProfileInfo): EditorState {
  return {
    id: profile.id,
    name: profile.name,
    provider: profile.provider,
    model_name: profile.model_name,
    base_url: profile.base_url,
    api_key: "",
    clear_api_key: false,
    temperature: profile.temperature,
    timeout_seconds: profile.timeout_seconds,
    max_retries: profile.max_retries,
    reasoning_effort: profile.reasoning_effort || "",
  };
}

interface Props {
  providers: LLMProviderOption[];
  onActivated: () => void;
}

export function ProfileSwitcher({ providers, onActivated }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<LLMProfilesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [saving, setSaving] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await api.listLLMProfiles();
      setData(response);
      setLoadError(null);
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : String(error));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const providerOptions = data?.providers?.length ? data.providers : providers;

  const activate = async (profile: LLMProfileInfo) => {
    if (profile.active || busyId) return;
    setBusyId(profile.id);
    try {
      const response = await api.activateLLMProfile(profile.id);
      setData(response);
      toast.success(t("settings.activated", { name: profile.name }));
      onActivated();
    } catch (error) {
      toast.error(
        t("settings.activateFailed") +
          ": " +
          (error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (profile: LLMProfileInfo) => {
    if (!window.confirm(t("settings.deleteProfileConfirm", { name: profile.name }))) return;
    setBusyId(profile.id);
    try {
      const response = await api.deleteLLMProfile(profile.id);
      setData(response);
      toast.success(t("settings.profileDeleted"));
      if (profile.active) onActivated();
    } catch (error) {
      toast.error(
        t("settings.saveFailed") + ": " + (error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setBusyId(null);
    }
  };

  const submitEditor = async () => {
    if (!editor) return;
    setSaving(true);
    const payload: LLMProfileUpsertRequest = {
      name: editor.name,
      provider: editor.provider,
      model_name: editor.model_name,
      base_url: editor.base_url,
      api_key: editor.api_key.trim() || undefined,
      clear_api_key: editor.clear_api_key,
      temperature: editor.temperature,
      timeout_seconds: editor.timeout_seconds,
      max_retries: editor.max_retries,
      reasoning_effort: editor.reasoning_effort,
    };
    try {
      const response = editor.id
        ? await api.updateLLMProfile(editor.id, payload)
        : await api.createLLMProfile(payload);
      setData(response);
      setEditor(null);
      toast.success(t("settings.profileSaved"));
      const touchedActive = response.profiles.some((p) => p.active);
      if (touchedActive) onActivated();
    } catch (error) {
      toast.error(
        t("settings.saveFailed") + ": " + (error instanceof Error ? error.message : String(error)),
      );
    } finally {
      setSaving(false);
    }
  };

  const onEditorProviderChange = (name: string) => {
    if (!editor) return;
    const provider = providerOptions.find((item) => item.name === name);
    setEditor({
      ...editor,
      provider: name,
      model_name: provider?.default_model ?? editor.model_name,
      base_url: provider?.default_base_url ?? editor.base_url,
      api_key: "",
      clear_api_key: false,
    });
  };

  const activeProvider = editor
    ? providerOptions.find((item) => item.name === editor.provider)
    : undefined;

  return (
    <section className="rounded-lg border bg-card p-5 shadow-sm">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h2 className="text-base font-semibold">{t("settings.profilesTitle")}</h2>
          <p className="max-w-3xl text-sm text-muted-foreground">{t("settings.profilesDesc")}</p>
        </div>
        <button
          type="button"
          onClick={() => setEditor(blankEditor(providerOptions))}
          className="inline-flex shrink-0 items-center gap-2 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90"
        >
          <Plus className="h-4 w-4" />
          {t("settings.addProfile")}
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          {t("settings.loading")}
        </div>
      ) : loadError ? (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive">
          {t("settings.loadProfilesFailed")}: {loadError}
        </div>
      ) : !data || data.profiles.length === 0 ? (
        <p className="rounded-md border bg-muted/20 px-3 py-4 text-sm text-muted-foreground">
          {t("settings.noProfiles")}
        </p>
      ) : (
        <div className="grid gap-3">
          {data.profiles.map((profile) => {
            const busy = busyId === profile.id;
            return (
              <div
                key={profile.id}
                className={`flex items-center gap-3 rounded-lg border px-4 py-3 transition ${
                  profile.active
                    ? "border-primary/60 bg-primary/5"
                    : "bg-muted/10 hover:border-primary/30 hover:bg-muted/20"
                }`}
              >
                <div
                  className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-gradient-to-br text-sm font-semibold text-white ${providerGradient(
                    profile.provider,
                  )}`}
                >
                  {providerInitial(profile)}
                </div>

                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="truncate text-sm font-semibold">{profile.name}</span>
                    {profile.active && (
                      <span className="rounded-full bg-primary/10 px-2 py-0.5 text-[10px] font-medium text-primary">
                        {t("settings.active")}
                      </span>
                    )}
                    {profile.api_key_required && (
                      <span
                        className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${
                          profile.api_key_configured
                            ? "bg-success/10 text-success"
                            : "bg-warning/10 text-warning"
                        }`}
                      >
                        <KeyRound className="h-2.5 w-2.5" />
                        {profile.api_key_configured
                          ? t("settings.apiKeySet")
                          : t("settings.apiKeyMissing")}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 truncate text-xs text-muted-foreground">
                    {profile.provider_label} · {profile.model_name}
                  </div>
                </div>

                <button
                  type="button"
                  onClick={() => void activate(profile)}
                  disabled={profile.active || busy}
                  className={`inline-flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition ${
                    profile.active
                      ? "cursor-default bg-muted text-muted-foreground"
                      : "bg-primary text-primary-foreground hover:opacity-90 disabled:opacity-60"
                  }`}
                >
                  {busy ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <Zap className="h-3.5 w-3.5" />
                  )}
                  {busy ? t("settings.activating") : t("settings.activate")}
                </button>

                <button
                  type="button"
                  onClick={() => setEditor(editorFromProfile(profile))}
                  title={t("settings.editProfile")}
                  className="shrink-0 rounded-md p-1.5 text-muted-foreground transition hover:bg-muted hover:text-foreground"
                >
                  <Pencil className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => void remove(profile)}
                  title={t("settings.deleteProfile")}
                  className="shrink-0 rounded-md p-1.5 text-muted-foreground transition hover:bg-destructive/10 hover:text-destructive"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {data && (
        <div className="mt-4 rounded-md border bg-muted/30 px-3 py-2 text-xs text-muted-foreground">
          <span className="font-medium text-foreground">{t("settings.profileStore")}: </span>
          <span className="break-all font-mono">{data.store_path}</span>
        </div>
      )}

      {editor && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div
            className="absolute inset-0 bg-black/50 backdrop-blur-sm"
            onClick={() => setEditor(null)}
          />
          <div className="relative z-10 max-h-[90vh] w-[520px] max-w-[95vw] overflow-y-auto rounded-2xl border bg-card p-6 shadow-xl">
            <div className="mb-5 flex items-center justify-between">
              <h3 className="text-sm font-semibold">
                {editor.id ? t("settings.editProfile") : t("settings.newProfile")}
              </h3>
              <button
                type="button"
                onClick={() => setEditor(null)}
                className="rounded-md p-1 text-muted-foreground transition hover:bg-muted hover:text-foreground"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            <div className="grid gap-4">
              <label className="grid gap-2">
                <span className={labelClass}>{t("settings.profileName")}</span>
                <input
                  value={editor.name}
                  onChange={(event) => setEditor({ ...editor, name: event.target.value })}
                  className={fieldClass}
                  placeholder={t("settings.profileNamePlaceholder")}
                />
                <span className={hintClass}>{t("settings.profileNameHint")}</span>
              </label>

              <label className="grid gap-2">
                <span className={labelClass}>{t("settings.provider")}</span>
                <select
                  value={editor.provider}
                  onChange={(event) => onEditorProviderChange(event.target.value)}
                  className={fieldClass}
                >
                  {providerOptions.map((provider) => (
                    <option key={provider.name} value={provider.name}>
                      {provider.label}
                    </option>
                  ))}
                </select>
              </label>

              <label className="grid gap-2">
                <span className={labelClass}>{t("settings.model")}</span>
                <input
                  value={editor.model_name}
                  onChange={(event) => setEditor({ ...editor, model_name: event.target.value })}
                  className={fieldClass}
                  required
                />
              </label>

              <label className="grid gap-2">
                <span className={labelClass}>{t("settings.baseUrl")}</span>
                <input
                  value={editor.base_url}
                  onChange={(event) => setEditor({ ...editor, base_url: event.target.value })}
                  className={fieldClass}
                  placeholder={activeProvider?.default_base_url}
                  disabled={activeProvider?.auth_type === "oauth"}
                />
              </label>

              {activeProvider?.api_key_required ? (
                <label className="grid gap-2">
                  <span className={labelClass}>{t("settings.apiKey")}</span>
                  <div className="relative">
                    <KeyRound className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                    <input
                      type="password"
                      value={editor.api_key}
                      onChange={(event) => setEditor({ ...editor, api_key: event.target.value })}
                      className={`${fieldClass} pl-9`}
                      placeholder={t("settings.leaveBlankToKeep")}
                      autoComplete="current-password"
                      disabled={editor.clear_api_key}
                    />
                  </div>
                  <div className="flex items-center justify-between gap-3">
                    <span className={hintClass}>{t("settings.leaveBlankToKeep")}</span>
                    <label className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
                      <input
                        type="checkbox"
                        checked={editor.clear_api_key}
                        onChange={(event) =>
                          setEditor({
                            ...editor,
                            clear_api_key: event.target.checked,
                            api_key: event.target.checked ? "" : editor.api_key,
                          })
                        }
                        className="h-3.5 w-3.5 accent-primary"
                      />
                      {t("settings.clearSavedApiKey")}
                    </label>
                  </div>
                </label>
              ) : null}

              <div className="grid gap-4 sm:grid-cols-3">
                <label className="grid gap-2">
                  <span className={labelClass}>{t("settings.temperature")}</span>
                  <input
                    type="number"
                    min={0}
                    max={2}
                    step={0.1}
                    value={editor.temperature}
                    onChange={(event) =>
                      setEditor({ ...editor, temperature: Number(event.target.value) })
                    }
                    className={fieldClass}
                  />
                </label>
                <label className="grid gap-2">
                  <span className={labelClass}>{t("settings.timeoutSeconds")}</span>
                  <input
                    type="number"
                    min={1}
                    max={3600}
                    step={1}
                    value={editor.timeout_seconds}
                    onChange={(event) =>
                      setEditor({ ...editor, timeout_seconds: Number(event.target.value) })
                    }
                    className={fieldClass}
                  />
                </label>
                <label className="grid gap-2">
                  <span className={labelClass}>{t("settings.maxRetries")}</span>
                  <input
                    type="number"
                    min={0}
                    max={20}
                    step={1}
                    value={editor.max_retries}
                    onChange={(event) =>
                      setEditor({ ...editor, max_retries: Number(event.target.value) })
                    }
                    className={fieldClass}
                  />
                </label>
              </div>

              <label className="grid gap-2">
                <span className={labelClass}>{t("settings.reasoningEffort")}</span>
                <select
                  value={editor.reasoning_effort}
                  onChange={(event) =>
                    setEditor({ ...editor, reasoning_effort: event.target.value })
                  }
                  className={fieldClass}
                >
                  <option value="">{t("settings.off")}</option>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="max">max</option>
                </select>
              </label>
            </div>

            <div className="mt-6 flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setEditor(null)}
                className="rounded-lg border px-4 py-2 text-sm text-muted-foreground transition hover:bg-muted hover:text-foreground"
              >
                {t("settings.cancel")}
              </button>
              <button
                type="button"
                onClick={() => void submitEditor()}
                disabled={saving || !editor.model_name.trim()}
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {saving && <Loader2 className="h-4 w-4 animate-spin" />}
                {t("settings.saveProfile")}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}

