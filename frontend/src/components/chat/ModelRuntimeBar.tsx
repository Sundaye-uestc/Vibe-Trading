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
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onPointerDown = (event: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(event.target as Node)) {
        setMenuOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMenuOpen(false);
        triggerRef.current?.focus();
      }
    };
    const focusFrame = requestAnimationFrame(() => {
      const menu = menuRef.current;
      (menu?.querySelector<HTMLButtonElement>('[role="menuitemradio"][aria-checked="true"]')
        ?? menu?.querySelector<HTMLButtonElement>('[role="menuitemradio"]'))?.focus();
    });
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      cancelAnimationFrame(focusFrame);
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (disabled) setMenuOpen(false);
  }, [disabled]);

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
    <div className="studio-runtime-bar shrink-0">
      <div className="studio-runtime-inner">
        <div className="studio-model-identity">
          <Cpu className="h-3.5 w-3.5 shrink-0 text-primary" aria-hidden="true" />
          <span className="shrink-0 font-medium text-foreground">{providerLabel}</span>
          <span className="studio-model-divider" aria-hidden="true" />
          <span className="truncate text-[11px] text-muted-foreground" title={model}>{model}</span>
        </div>
        {onEffortChange ? (
          <div className="studio-effort-control relative" ref={menuRef}>
            <button
              ref={triggerRef}
              type="button"
              onClick={() => setMenuOpen((open) => !open)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown" && !disabled) {
                  event.preventDefault();
                  setMenuOpen(true);
                }
              }}
              disabled={disabled}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label={t("agent.reasoningStrength")}
              className="studio-effort-trigger inline-flex items-center gap-2 text-xs text-muted-foreground transition-colors hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Cpu className="h-3 w-3" aria-hidden="true" />
              <span>{t("agent.reasoningStrength")}: {effortLabel}</span>
              <ChevronDown className="h-3 w-3" aria-hidden="true" />
            </button>
            {menuOpen && (
              <div
                role="menu"
                aria-label={t("agent.reasoningStrength")}
                onKeyDown={(event) => {
                  const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'));
                  const index = items.indexOf(document.activeElement as HTMLButtonElement);
                  if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
                    event.preventDefault();
                    const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
                      : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
                    items[next]?.focus();
                  } else if (event.key === "Tab") setMenuOpen(false);
                }}
                className="studio-effort-menu absolute end-0 top-full z-50 mt-2 w-52 rounded-2xl border py-2"
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
                        triggerRef.current?.focus();
                      }}
                      className="flex min-h-11 w-full items-center gap-2 px-3 py-2 text-start text-xs transition-colors hover:bg-muted"
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
          <span className="studio-effort-trigger inline-flex items-center gap-2 text-xs text-muted-foreground">
            <Cpu className="h-3 w-3" aria-hidden="true" />
            <span>{t("agent.reasoningStrength")}: {effortLabel}</span>
          </span>
        )}
      </div>
    </div>
  );
}
