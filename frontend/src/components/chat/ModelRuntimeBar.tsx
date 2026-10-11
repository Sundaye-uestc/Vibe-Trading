import { useEffect, useRef, useState } from "react";
import { Check, ChevronDown, Cpu } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { LLMSettings } from "@/lib/api";

/** Ordered effort options; "" is the provider default (no explicit value). */
const EFFORT_OPTIONS = ["", "none", "low", "medium", "high", "max"] as const;

interface Props {
  settings: LLMSettings | null;
  runtimeProvider?: string;
  runtimeModel?: string;
  runtimeReasoningEffort?: string;
  /** Pending user selection for the next turn; wins over the runtime value. */
  effortOverride?: string | null;
  /** When provided the effort pill becomes an interactive picker. */
  onEffortChange?: (value: string) => void;
  disabled?: boolean;
}

export function ModelRuntimeBar({
  settings,
  runtimeProvider,
  runtimeModel,
  runtimeReasoningEffort,
  effortOverride,
  onEffortChange,
  disabled = false,
}: Props) {
  const { t } = useTranslation();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  if (!settings) return null;

  const providerId = runtimeProvider || settings.provider;
  const provider = settings.providers.find((item) => item.name === providerId);
  const providerLabel = provider?.label || providerId || t("agent.unknownProvider");
  const model = runtimeModel || settings.model_name || t("agent.unknownModel");
  const effortLabels: Record<string, string> = {
    none: t("settings.reasoningEffortNone"),
    low: t("settings.reasoningEffortLow"),
    medium: t("settings.reasoningEffortMedium"),
    high: t("settings.reasoningEffortHigh"),
    max: t("settings.reasoningEffortMax"),
  };
  const reasoningEffort = effortOverride != null
    ? effortOverride
    : runtimeReasoningEffort !== undefined
      ? runtimeReasoningEffort
      : settings.reasoning_effort;
  const effortLabel = effortLabels[reasoningEffort] || t("settings.providerDefault");

  return (
    <div className="shrink-0 border-b border-border/70 bg-background/95 px-6 py-2 backdrop-blur-sm">
      <div className="mx-auto flex max-w-3xl items-center gap-2 overflow-hidden text-xs">
        <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
          <span className="absolute inline-flex h-full w-full rounded-full bg-success/30" />
          <span className="relative inline-flex h-2 w-2 rounded-full bg-success" />
        </span>
        <span className="shrink-0 font-medium text-foreground">{providerLabel}</span>
        <span className="text-muted-foreground/60">·</span>
        <span className="truncate font-mono text-[11px] text-muted-foreground" title={model}>{model}</span>
        {onEffortChange ? (
          <div className="relative ml-auto" ref={menuRef}>
            <button
              type="button"
              onClick={() => setMenuOpen((open) => !open)}
              disabled={disabled}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label={t("agent.reasoningStrength")}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border/70 bg-muted/35 px-2 py-0.5 text-[10px] text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Cpu className="h-3 w-3" aria-hidden="true" />
              <span>{t("agent.reasoningStrength")}: {effortLabel}</span>
              <ChevronDown className="h-3 w-3" aria-hidden="true" />
            </button>
            {menuOpen && (
              <div
                role="menu"
                className="absolute right-0 top-full z-50 mt-1 w-44 rounded-xl border bg-background/95 py-1 shadow-lg backdrop-blur-sm"
              >
                {EFFORT_OPTIONS.map((option) => {
                  const label = option === ""
                    ? t("settings.providerDefault")
                    : effortLabels[option];
                  const selected = option === reasoningEffort;
                  return (
                    <button
                      key={option || "default"}
                      type="button"
                      role="menuitemradio"
                      aria-checked={selected}
                      onClick={() => {
                        setMenuOpen(false);
                        onEffortChange(option);
                      }}
                      className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs transition-colors hover:bg-muted"
                    >
                      <Check
                        className={`h-3.5 w-3.5 shrink-0 ${selected ? "text-primary" : "opacity-0"}`}
                        aria-hidden="true"
                      />
                      <span>{label}</span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
        ) : (
          <span className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border/70 bg-muted/35 px-2 py-0.5 text-[10px] text-muted-foreground">
            <Cpu className="h-3 w-3" aria-hidden="true" />
            <span>{t("agent.reasoningStrength")}: {effortLabel}</span>
          </span>
        )}
      </div>
    </div>
  );
}
