import { useTranslation } from "react-i18next";
import { useEffect, useRef, useState, useCallback } from "react";
import { Link, Outlet, useLocation, useSearchParams } from "react-router";
import { Activity, BarChart3, Bot, CalendarClock, CandlestickChart, Check, ChevronDown, FileText, Languages, Moon, Sun, Plus, Trash2, Pencil, MessageSquare, ChevronsLeft, ChevronsRight, Settings, Layers, Loader2, WalletCards, Package, Image } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { api, type SessionItem } from "@/lib/api";
import { safeGet, safeSet } from "@/lib/storage";
import { useAgentStore } from "@/stores/agent";
import { BrandMark } from "@/components/common/BrandMark";
import { ConnectionBanner } from "@/components/layout/ConnectionBanner";
import { SUPPORTED_LANGUAGES } from "@/i18n";
import { AvatarSelector } from "@/components/chat/AvatarSelector";
import { BackgroundSelector } from "@/components/layout/BackgroundSelector";
import { getStoredBackground, saveBackground } from "@/hooks/useBackground";
import { toast } from "sonner";

// APP_VERSION is sourced from i18n locale files (app.version key) to keep a
// single source of truth across the footer and every localised README.

const MIN_SIDEBAR = 48;
const MAX_SIDEBAR = 500;
const COLLAPSE_THRESHOLD = 100;

export function Layout() {
  const { t } = useTranslation();

  // "/" is the product (chat); marketing moved to /about. The Agent entry
  // matches both "/" and legacy "/agent" deep links.
  const NAV = [
    { to: "/", icon: Bot, label: t('layout.agent') },
    { to: "/runtime", icon: Activity, label: t('layout.runtime') },
    { to: "/scheduled", icon: CalendarClock, label: t('layout.scheduled') },
    { to: "/reports", icon: FileText, label: t('layout.reports') },
    { to: "/portfolio", icon: WalletCards, label: t('layout.portfolio') },
    { to: "/skills", icon: Package, label: t('layout.skills') },
    { to: "/alpha-zoo", icon: Layers, label: t('layout.alphaZoo') },
    { to: "/options", icon: CandlestickChart, label: t('layout.optionsLab') },
    { to: "/settings", icon: Settings, label: t('layout.settings') },
    { to: "/correlation", icon: BarChart3, label: t('layout.correlation') },
  ];
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();
  const { dark, toggle } = useDarkMode();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const sseStatus = useAgentStore(s => s.sseStatus);
  const sseRetryAttempt = useAgentStore(s => s.sseRetryAttempt);

  /* ---- Sidebar resize ---- */
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const saved = safeGet("qa-sidebar-width");
    if (saved) {
      const n = parseInt(saved, 10);
      if (!isNaN(n) && n >= MIN_SIDEBAR && n <= MAX_SIDEBAR) return n;
    }
    return 256;
  });
  const [expandedWidth, setExpandedWidth] = useState(() => {
    const saved = safeGet("qa-sidebar-expanded-width");
    if (saved) {
      const n = parseInt(saved, 10);
      if (!isNaN(n) && n >= COLLAPSE_THRESHOLD && n <= MAX_SIDEBAR) return n;
    }
    return 256;
  });

  const collapsed = sidebarWidth < COLLAPSE_THRESHOLD;
  const isResizingRef = useRef(false);
  const startXRef = useRef(0);
  const startWidthRef = useRef(0);

  const persistWidth = useCallback((w: number) => {
    safeSet("qa-sidebar-width", String(w));
    if (w >= COLLAPSE_THRESHOLD) {
      safeSet("qa-sidebar-expanded-width", String(w));
      setExpandedWidth(w);
    }
  }, []);

  const toggleCollapse = useCallback(() => {
    if (collapsed) {
      setSidebarWidth(expandedWidth);
      persistWidth(expandedWidth);
    } else {
      setSidebarWidth(48);
      safeSet("qa-sidebar-width", "48");
    }
  }, [collapsed, expandedWidth, persistWidth]);

  /* Mouse resize handlers */
  const onResizeMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    isResizingRef.current = true;
    startXRef.current = e.clientX;
    startWidthRef.current = sidebarWidth;
    document.body.classList.add("sidebar-resizing");

    const onMouseMove = (ev: MouseEvent) => {
      if (!isResizingRef.current) return;
      const delta = ev.clientX - startXRef.current;
      const next = Math.max(MIN_SIDEBAR, Math.min(MAX_SIDEBAR, startWidthRef.current + delta));
      setSidebarWidth(next);
    };

    const onMouseUp = () => {
      isResizingRef.current = false;
      document.body.classList.remove("sidebar-resizing");
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      const el = document.querySelector<HTMLElement>("[data-sidebar]");
      if (el) {
        const finalW = el.offsetWidth;
        persistWidth(finalW);
      }
    };

    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  }, [sidebarWidth, persistWidth]);

  /* Persist width on change */
  useEffect(() => {
    safeSet("qa-sidebar-width", String(sidebarWidth));
  }, [sidebarWidth]);

  /* Adopt a width changed in another tab, mirroring the storage event. */
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== "qa-sidebar-width") return;
      const next = parseInt(safeGet("qa-sidebar-width") ?? "", 10);
      if (!isNaN(next) && next >= MIN_SIDEBAR && next <= MAX_SIDEBAR) {
        setSidebarWidth(next);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  /* ---- Sessions ---- */
  const activeSessionId = searchParams.get("session");
  const streamingSessionId = useAgentStore(s => s.streamingSessionId);

  const loadSessions = () => {
    api.listSessions()
      .then((list) => setSessions(Array.isArray(list) ? list : []))
      .catch(() => {})
      .finally(() => setSessionsLoading(false));
  };

  const isAgentPage = pathname.startsWith("/agent");
  useEffect(() => { loadSessions(); }, [isAgentPage, activeSessionId]);

  // Re-list after out-of-band title changes (e.g. LLM auto-titling on the
  // first completed exchange).
  useEffect(() => {
    const refresh = () => loadSessions();
    window.addEventListener("vibe:sessions-refresh", refresh);
    return () => window.removeEventListener("vibe:sessions-refresh", refresh);
  }, []);

  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [renameTarget, setRenameTarget] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState("");

  const deleteSession = async (sid: string) => {
    try {
      await api.deleteSession(sid);
      setSessions((prev) => prev.filter((s) => s.session_id !== sid));
    } catch { /* ignore */ }
    setDeleteTarget(null);
  };

  const renameSession = async (sid: string) => {
    if (!renameValue.trim()) { setRenameTarget(null); return; }
    try {
      await api.renameSession(sid, renameValue.trim());
      setSessions((prev) => prev.map((s) => s.session_id === sid ? { ...s, title: renameValue.trim() } : s));
    } catch { /* ignore */ }
    setRenameTarget(null);
  };

  /* ---- Background ---- */
  const [bg, setBg] = useState<string | null>(getStoredBackground);

  const handleBgSave = (dataUrl: string | null) => {
    // Update state first so the UI reacts immediately.
    setBg(dataUrl);
    // Persist in background; localStorage may fail if the data URL is huge.
    try {
      saveBackground(dataUrl);
    } catch (e) {
      console.error("Failed to persist background to localStorage", e);
    }
    if (dataUrl) {
      toast.success(t("layout.backgroundApplied"));
    } else {
      toast.success(t("layout.backgroundRemoved"));
    }
  };

  /* ---- Dialogs ---- */
  const [avatarDialogOpen, setAvatarDialogOpen] = useState(false);
  const [bgDialogOpen, setBgDialogOpen] = useState(false);

  return (
    <div
      className="flex h-screen relative bg-background rtl:flex-row-reverse"
      style={
        bg
          ? {
              backgroundImage: `url(${bg})`,
              backgroundSize: "cover",
              backgroundPosition: "center",
              backgroundAttachment: "fixed",
            }
          : undefined
      }
    >
      {/* Tint overlay — keeps content readable over custom background */}
      {bg && <div className="absolute inset-0 bg-background/90 pointer-events-none" />}
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:fixed focus:start-4 focus:top-4 focus:z-[70] focus:rounded-md focus:bg-background focus:px-4 focus:py-2 focus:text-sm focus:font-medium focus:text-foreground focus:shadow-lg focus:outline-none focus:ring-2 focus:ring-primary/40"
      >
        {t('layout.skipToMain', { defaultValue: 'Skip to main content' })}
      </a>
      {/* Sidebar */}
      <aside
        data-sidebar
        aria-label={t('layout.sidebar', { defaultValue: 'Vibe-Trading sidebar' })}
        className={cn(
          "max-md:w-12 border-e border-border/60 bg-card flex flex-col shrink-0 transition-[width] duration-200 overflow-visible relative",
          collapsed ? "w-12" : ""
        )}
        style={{ width: sidebarWidth }}
      >
        {/* Brand */}
        <div className={cn("border-b border-border/60", collapsed ? "p-2 flex justify-center" : "p-4 max-md:p-2 max-md:flex max-md:justify-center")}>
          <Link
            to="/"
            aria-label="Vibe-Trading"
            className={cn("flex items-center", collapsed ? "justify-center" : "gap-2 max-md:justify-center")}
          >
            <BrandMark className="h-6 w-6 shrink-0" />
            {!collapsed && (
              <span className="text-[15px] font-semibold tracking-tight max-md:hidden">Vibe-Trading</span>
            )}
          </Link>
        </div>

        {/* Nav */}
        <nav
          aria-label={t('layout.mainNavigation', { defaultValue: 'Main navigation' })}
          className={cn("space-y-0.5", collapsed ? "p-1" : "p-2 max-md:p-1")}
        >
          {NAV.map(({ to, icon: Icon, label }) => {
            const text = label;
            return (
              <Link
                key={to}
                to={to}
                aria-label={text}
                className={cn(
                  "flex items-center rounded-md text-[13px] transition-colors",
                  collapsed ? "justify-center px-2 py-1.5" : "gap-3 px-3 py-1.5 max-md:justify-center max-md:px-2",
                  (to === "/" ? pathname === "/" || pathname.startsWith("/agent") : pathname.startsWith(to))
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                )}
                title={collapsed ? text : undefined}
              >
                <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                {!collapsed && <span className="max-md:hidden">{text}</span>}
              </Link>
            );
          })}
        </nav>

        {/* Sessions — hidden when collapsed */}
        {!collapsed && (
          <div className="flex-1 overflow-auto border-t border-border/60 mt-2 flex flex-col max-md:hidden">
            <div className="flex items-center justify-between px-4 py-2">
              <span className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                <MessageSquare className="h-3.5 w-3.5" />
                {t('layout.sessions')}
              </span>
              <Link
                to="/agent"
                aria-label={t('layout.newChat')}
                className="flex items-center gap-1 p-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                title={t('layout.newChat')}
              >
                <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              </Link>
            </div>

            <div className="px-2 pb-2 space-y-0.5 overflow-auto flex-1">
              {sessionsLoading ? (
                <div className="space-y-1.5 px-2 py-1">
                  {[1, 2, 3].map((i) => (
                    <div key={i} className="h-7 rounded-md bg-muted/50 animate-pulse" />
                  ))}
                </div>
              ) : sessions.length === 0 ? (
                <p className="px-3 py-2 text-xs text-muted-foreground/60">{t('layout.noSessions')}</p>
              ) : null}
              {sessions.map((s) => {
                const isActive = s.session_id === activeSessionId;
                const isDeleting = deleteTarget === s.session_id;
                const isRenaming = renameTarget === s.session_id;
                return (
                  <div key={s.session_id} className="group relative flex items-center">
                    {isRenaming ? (
                      <input
                        autoFocus
                        value={renameValue}
                        onChange={(e) => setRenameValue(e.target.value)}
                        onKeyDown={(e) => { if (e.key === "Enter") renameSession(s.session_id); if (e.key === "Escape") setRenameTarget(null); }}
                        onBlur={() => renameSession(s.session_id)}
                        aria-label={`${t('layout.rename')}: ${s.title || s.session_id}`}
                        className="flex-1 min-w-0 ps-3 pe-2 py-1.5 rounded-md text-xs border border-primary bg-background outline-none focus:ring-2 focus:ring-primary/40"
                      />
                    ) : (
                      <Link
                        to={`/agent?session=${s.session_id}`}
                        className={cn(
                          "flex-1 min-w-0 ps-3 pe-14 py-1.5 rounded-md text-xs transition-colors truncate block border-s-2",
                          isActive
                            ? "border-s-primary bg-primary/10 text-primary font-medium"
                            : "border-s-transparent text-muted-foreground hover:bg-muted hover:text-foreground"
                        )}
                        title={s.title || s.session_id}
                      >
                        <span className="flex min-w-0 items-center gap-1.5">
                          {streamingSessionId === s.session_id ? (
                            <Loader2 className="h-3 w-3 shrink-0 animate-spin text-primary" />
                          ) : (
                            // Transparent placeholder keeps titles aligned with
                            // spinner rows without a meaningless gray dot.
                            <span className={cn(
                              "h-1.5 w-1.5 rounded-full shrink-0",
                              isActive ? "bg-primary/70" : "bg-transparent"
                            )} />
                          )}
                          <span className="min-w-0 truncate">{s.title || s.session_id.slice(0, 16)}</span>
                        </span>
                      </Link>
                    )}
                    {!isRenaming && isDeleting ? (
                      <div className="absolute right-0.5 flex items-center gap-0.5">
                        <button onClick={() => deleteSession(s.session_id)} className="p-1.5 text-danger hover:bg-danger/10 rounded text-[10px] font-medium">{t('layout.confirm')}</button>
                        <button onClick={() => setDeleteTarget(null)} className="p-1.5 text-muted-foreground hover:bg-muted rounded text-[10px]">{t('layout.cancel')}</button>
                      </div>
                    ) : !isRenaming ? (
                      <div className="absolute right-1 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 flex items-center gap-0.5 transition-opacity">
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setRenameTarget(s.session_id); setRenameValue(s.title || ""); }}
                          className="p-1.5 text-muted-foreground hover:text-foreground rounded"
                          title={t('layout.rename')}
                        >
                          <Pencil className="h-3 w-3" />
                        </button>
                        <button
                          onClick={(e) => { e.preventDefault(); e.stopPropagation(); setDeleteTarget(s.session_id); }}
                          className="p-1.5 text-muted-foreground hover:text-danger rounded"
                          title={t('layout.delete')}
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Spacer when collapsed */}
        {collapsed && <div className="flex-1" />}

        {/* Footer */}
        <div className={cn("mt-auto border-t border-border/60", collapsed ? "p-1 flex flex-col items-center gap-1" : "p-3 space-y-2 max-md:p-1 max-md:flex max-md:flex-col max-md:items-center max-md:gap-1 max-md:space-y-0")}>
          {/* Avatar + Background row */}
          {collapsed ? (
            <>
              <button
                onClick={() => setAvatarDialogOpen(true)}
                className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
                title={t('layout.changeAvatar')}
              >
                <span className="text-[10px] font-bold">P</span>
              </button>
              <button
                onClick={() => setBgDialogOpen(true)}
                className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
                title={t('layout.changeBackground')}
              >
                <Image className="h-3.5 w-3.5 shrink-0" />
              </button>
              {bg && (
                <button
                  onClick={() => handleBgSave(null)}
                  className="p-1.5 text-destructive/70 hover:text-destructive rounded transition-colors"
                  title={t('layout.removeBackground')}
                >
                  <Trash2 className="h-3.5 w-3.5 shrink-0" />
                </button>
              )}
            </>
          ) : (
            <div className="flex items-center w-full">
              <button
                onClick={() => setAvatarDialogOpen(true)}
                className="flex-1 flex items-center justify-center gap-1.5 text-xs text-muted-foreground hover:text-foreground rounded py-1 transition-colors"
              >
                {t('layout.changeAvatar')}
              </button>
              <span className="w-px h-5 bg-border shrink-0" />
              <button
                onClick={() => setBgDialogOpen(true)}
                className="flex-1 flex items-center justify-center text-xs text-muted-foreground hover:text-foreground rounded py-1 transition-colors"
              >
                {t('layout.changeBackground')}
              </button>
              {bg && (
                <>
                  <span className="w-px h-5 bg-border shrink-0" />
                  <button
                    onClick={() => handleBgSave(null)}
                    className="flex items-center justify-center gap-1 text-xs text-destructive/80 hover:text-destructive rounded py-1 px-1.5 transition-colors"
                    title={t('layout.removeBackground')}
                  >
                    <Trash2 className="h-3 w-3 shrink-0" />
                  </button>
                </>
              )}
            </div>
          )}

          {collapsed ? (
            <>
              <button onClick={toggle} className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors" title={dark ? t('layout.light') : t('layout.dark')}>
                {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
              </button>
              <button onClick={toggleCollapse} className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors" title={t('layout.expand')}>
                <ChevronsRight className="h-3.5 w-3.5" />
              </button>
            </>
          ) : (
            <>
              <div className="flex items-center justify-between max-md:flex-col">
                <button
                  onClick={toggle}
                  className="flex items-center gap-1.5 p-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
                >
                  {dark ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
                  <span className="max-md:hidden">{dark ? t('layout.light') : t('layout.dark')}</span>
                </button>
                <div className="flex items-center gap-1 max-md:hidden">
                  <button
                    onClick={toggleCollapse}
                    className="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
                    title={t('layout.collapse')}
                  >
                    <ChevronsLeft className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <div className="flex flex-col gap-1 max-md:items-center">
                <LanguageSwitcher />
                {/* About is marketing, not a work surface — it lives here by
                    the version stamp instead of in the primary nav. */}
                <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground/60 max-md:hidden">
                  <span>{t('app.version')}</span>
                  <span aria-hidden="true">·</span>
                  <Link to="/about" className="transition-colors hover:text-foreground">
                    {t('layout.about')}
                  </Link>
                </div>
              </div>
            </>
          )}
        </div>

        {/* Resize handle */}
        <div
          className="sidebar-resize-handle absolute top-0 right-0 w-1 h-full z-10 transition-colors"
          style={{ right: -2 }}
          onMouseDown={onResizeMouseDown}
        />
      </aside>

      {/* Main */}
      <div className="relative flex-1 flex flex-col overflow-hidden">
        <ConnectionBanner status={sseStatus} retryAttempt={sseRetryAttempt} />
        <main id="main" className="flex-1 min-h-0 overflow-auto">
          <Outlet />
        </main>
      </div>

      {/* Dialogs */}
      <AvatarSelector open={avatarDialogOpen} onClose={() => setAvatarDialogOpen(false)} />
      <BackgroundSelector open={bgDialogOpen} onClose={() => setBgDialogOpen(false)} onSave={handleBgSave} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Language switcher — dropdown listing every language registered in
// src/i18n/index.ts. Persists the choice via i18next's localStorage detector
// and emits the `languageChanged` event handled in the i18n module to flip
// <html dir/lang> for RTL languages.
//
// Positioning: the menu uses `position: fixed` and is placed at
// `(triggerLeft, triggerTop - gap)`. This bypasses every ancestor's
// `overflow: hidden/auto/scroll`, stacking contexts, and CSS direction
// rules, so the dropdown is *always* fully visible regardless of where
// the trigger sits in the layout or which language is active. We measure
// the trigger with getBoundingClientRect() and update on resize/scroll.
// ---------------------------------------------------------------------------
function LanguageSwitcher() {
  const { i18n, t } = useTranslation();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const [menuStyle, setMenuStyle] = useState<{ left: number; bottom: number; minWidth: number } | null>(null);

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent | TouchEvent) => {
      if (
        triggerRef.current &&
        !triggerRef.current.contains(e.target as Node) &&
        !(e.target as HTMLElement).closest?.("[data-lang-menu]")
      ) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("touchstart", onClick, { passive: true });
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("touchstart", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Recompute the menu's fixed coordinates whenever it opens, or whenever
  // the viewport changes (resize / scroll / language switch). The menu is
  // anchored to the trigger's *left edge* and sits *above* the trigger.
  useEffect(() => {
    if (!open || !triggerRef.current) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (!r) return;
      // Anchor: align the menu's right edge with the trigger's right edge,
      // then clamp to the viewport so the menu never overflows the screen.
      const menuWidth = 160; // px — approx longest label "العربية" + padding
      const gap = 4; // mb-1
      const desiredLeft = r.right - menuWidth;
      const maxLeft = window.innerWidth - menuWidth - 8;
      const minLeft = 8;
      const left = Math.max(minLeft, Math.min(maxLeft, desiredLeft));
      setMenuStyle({
        left,
        // distance from viewport bottom: viewport height − trigger top + gap
        bottom: window.innerHeight - r.top + gap,
        minWidth: menuWidth,
      });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  // i18n.language (singular) is the primary active language. We try an exact
  // match first against SUPPORTED_LANGUAGES. If that fails (e.g. a regional
  // variant like "ja-JP"), we fall back to i18n.languages (plural) which
  // includes both the detected and resolved codes. NOTE: i18n.languages
  // always contains the fallback language ("en"), so it must NOT be the
  // primary match — otherwise "en" being first in SUPPORTED_LANGUAGES
  // would always win and the switcher would never show any other language.
  const current =
    SUPPORTED_LANGUAGES.find((l) => l.code === i18n.language) ??
    SUPPORTED_LANGUAGES.find((l) => i18n.languages?.includes(l.code)) ??
    SUPPORTED_LANGUAGES[0];

  return (
    <div>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={t("layout.language")}
        className="flex items-center gap-1 p-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors max-md:justify-center"
      >
        <Languages className="h-3.5 w-3.5 shrink-0" />
        <span className="whitespace-nowrap max-md:hidden">{current.label}</span>
        <ChevronDown className={cn("h-3 w-3 shrink-0 transition-transform max-md:hidden", open && "rotate-180")} />
      </button>
      {open && menuStyle && (
        <ul
          data-lang-menu
          aria-label="Select language"
          style={{
            position: "fixed",
            left: menuStyle.left,
            bottom: menuStyle.bottom,
            minWidth: menuStyle.minWidth,
            zIndex: 60,
          }}
          className="rounded-md border border-border/60 bg-popover shadow-lg ring-1 ring-black/5"
        >
          {SUPPORTED_LANGUAGES.map((lang) => {
            const active = lang.code === current.code;
            return (
              <li key={lang.code}>
                <button
                  type="button"
                  onClick={() => {
                    i18n.changeLanguage(lang.code).catch(console.error);
                    setOpen(false);
                  }}
                  aria-current={active || undefined}
                  className={cn(
                    "w-full flex items-center gap-2 px-2.5 py-1.5 text-xs hover:bg-muted hover:text-foreground transition-colors",
                    active && "text-foreground",
                  )}
                >
                  <span className="flex-1 text-start whitespace-nowrap">{lang.label}</span>
                  {active && <Check className="h-3 w-3 shrink-0" />}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
