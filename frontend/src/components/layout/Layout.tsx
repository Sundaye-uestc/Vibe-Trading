import { useTranslation } from "react-i18next";
import { useEffect, useRef, useState, useCallback } from "react";
import { Link, Outlet, useLocation, useSearchParams } from "react-router";
import { Activity, BarChart3, Bot, CalendarClock, CandlestickChart, Check, ChevronDown, FileText, FlaskConical, Languages, Moon, Sun, Plus, Trash2, Pencil, MessageSquare, ChevronsLeft, ChevronsRight, Settings, Layers, Loader2, WalletCards, Package, Image, Menu, X, UserRound, ArrowUpRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { useDarkMode } from "@/hooks/useDarkMode";
import { api, type SessionItem } from "@/lib/api";
import { safeGet, safeSet } from "@/lib/storage";
import { useAgentStore } from "@/stores/agent";
import { BrandMark } from "@/components/common/BrandMark";
import { ConfirmDialog } from "@/components/common/ConfirmDialog";
import { DialogSurface } from "@/components/common/DialogSurface";
import { ConnectionBanner } from "@/components/layout/ConnectionBanner";
import { SUPPORTED_LANGUAGES } from "@/i18n";
import { AvatarSelector } from "@/components/chat/AvatarSelector";
import { BackgroundSelector } from "@/components/layout/BackgroundSelector";
import { getStoredBackground, saveBackground } from "@/hooks/useBackground";
import { toast } from "sonner";
import "@/styles/shell.css";

const MIN_SIDEBAR = 48;
const MAX_SIDEBAR = 500;
const COLLAPSE_THRESHOLD = 100;
const DEFAULT_SIDEBAR = 232;

export function Layout() {
  const { t, i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const { pathname } = useLocation();
  const [searchParams] = useSearchParams();
  const { dark, toggle } = useDarkMode();
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(true);
  const sseStatus = useAgentStore(s => s.sseStatus);
  const sseRetryAttempt = useAgentStore(s => s.sseRetryAttempt);
  const streamingSessionId = useAgentStore(s => s.streamingSessionId);
  const activeSessionId = searchParams.get("session");
  const [sidebarWidth, setSidebarWidth] = useState(() => {
    const value = parseInt(safeGet("qa-sidebar-width") ?? "", 10);
    return Number.isFinite(value) && value >= MIN_SIDEBAR && value <= MAX_SIDEBAR ? value : DEFAULT_SIDEBAR;
  });
  const [expandedWidth, setExpandedWidth] = useState(() => {
    const value = parseInt(safeGet("qa-sidebar-expanded-width") ?? "", 10);
    return Number.isFinite(value) && value >= COLLAPSE_THRESHOLD && value <= MAX_SIDEBAR ? value : DEFAULT_SIDEBAR;
  });
  const [mobile, setMobile] = useState(() => window.matchMedia("(max-width: 767px)").matches);
  const [mobileOpen, setMobileOpen] = useState(false);
  const sidebarRef = useRef<HTMLElement>(null);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLButtonElement>(null);
  const closeMenuRef = useRef<HTMLButtonElement>(null);
  const resizeCleanup = useRef<(() => void) | null>(null);
  const sessionGeneration = useRef(0);
  const collapsed = !mobile && sidebarWidth < COLLAPSE_THRESHOLD;
  const [deleteTarget, setDeleteTarget] = useState<SessionItem | null>(null);
  const [renameTarget, setRenameTarget] = useState<SessionItem | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [mutationBusy, setMutationBusy] = useState(false);
  const [avatarDialogOpen, setAvatarDialogOpen] = useState(false);
  const [bgDialogOpen, setBgDialogOpen] = useState(false);
  const [bg, setBg] = useState<string | null>(getStoredBackground);
  const overlayOpen = mobileOpen || avatarDialogOpen || bgDialogOpen || Boolean(deleteTarget) || Boolean(renameTarget);
  const groups = [
    { label: t("layout.workspaceGroup", { defaultValue: zh ? "工作空间" : "WORKSPACE" }), items: [
      { to: "/", icon: Bot, label: t("layout.agent") },
      { to: "/portfolio", icon: WalletCards, label: t("layout.portfolio") },
      { to: "/paper-trading", icon: FlaskConical, label: t("layout.paperTrading") },
      { to: "/reports", icon: FileText, label: t("layout.reports") },
      { to: "/scheduled", icon: CalendarClock, label: t("layout.scheduled") },
      { to: "/runtime", icon: Activity, label: t("layout.runtime") },
    ] },
    { label: t("layout.researchGroup", { defaultValue: zh ? "研究工具" : "RESEARCH TOOLS" }), items: [
      { to: "/alpha-zoo", icon: Layers, label: t("layout.alphaZoo") },
      { to: "/options", icon: CandlestickChart, label: t("layout.optionsLab") },
      { to: "/correlation", icon: BarChart3, label: t("layout.correlation") },
      { to: "/skills", icon: Package, label: t("layout.skills") },
    ] },
  ];
  const isActive = (to: string) => to === "/" ? pathname === "/" || pathname.startsWith("/agent") : pathname.startsWith(to);
  const pageTitle = groups.flatMap(group => group.items).find(item => isActive(item.to))?.label
    ?? (pathname.startsWith("/settings") ? t("layout.settings") : "Vibe-Trading");

  const persistWidth = useCallback((width: number) => {
    safeSet("qa-sidebar-width", String(width));
    if (width >= COLLAPSE_THRESHOLD) {
      safeSet("qa-sidebar-expanded-width", String(width));
      setExpandedWidth(width);
    }
  }, []);
  const toggleCollapse = () => {
    const width = collapsed ? expandedWidth : MIN_SIDEBAR;
    setSidebarWidth(width);
    persistWidth(width);
  };
  const onResizeMouseDown = useCallback((event: React.MouseEvent) => {
    event.preventDefault();
    resizeCleanup.current?.();
    const start = event.clientX;
    const initial = sidebarWidth;
    const direction = document.documentElement.dir === "rtl" ? -1 : 1;
    let current = initial;
    document.body.classList.add("sidebar-resizing");
    const move = (next: MouseEvent) => {
      current = Math.max(MIN_SIDEBAR, Math.min(MAX_SIDEBAR, initial + (next.clientX - start) * direction));
      setSidebarWidth(current);
    };
    const finish = () => {
      document.body.classList.remove("sidebar-resizing");
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", finish);
      resizeCleanup.current = null;
      persistWidth(current);
    };
    resizeCleanup.current = finish;
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", finish);
  }, [sidebarWidth, persistWidth]);
  useEffect(() => () => resizeCleanup.current?.(), []);
  useEffect(() => { safeSet("qa-sidebar-width", String(sidebarWidth)); }, [sidebarWidth]);
  useEffect(() => {
    const media = window.matchMedia("(max-width: 767px)");
    const changed = () => { setMobile(media.matches); if (!media.matches) setMobileOpen(false); };
    changed();
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, []);
  useEffect(() => {
    const changed = (event: StorageEvent) => {
      if (event.key === "qa-sidebar-width") {
        const next = parseInt(safeGet("qa-sidebar-width") ?? "", 10);
        if (Number.isFinite(next) && next >= MIN_SIDEBAR && next <= MAX_SIDEBAR) setSidebarWidth(next);
      } else if (event.key === "qa-background") setBg(getStoredBackground());
    };
    window.addEventListener("storage", changed);
    return () => window.removeEventListener("storage", changed);
  }, []);
  useEffect(() => { setMobileOpen(false); }, [pathname, activeSessionId]);
  useEffect(() => {
    if (!mobile || !mobileOpen) return;
    const workspace = workspaceRef.current;
    if (workspace) workspace.inert = true;
    closeMenuRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      // Escape belongs to a disclosure above the drawer before the drawer.
      if (event.key === "Escape" && document.querySelector("[data-lang-menu]")) return;
      if (event.key === "Escape") { event.preventDefault(); setMobileOpen(false); }
      if (event.key !== "Tab") return;
      const nodes = Array.from(sidebarRef.current?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled])') ?? []);
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && (document.activeElement === first || !sidebarRef.current?.contains(document.activeElement))) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !sidebarRef.current?.contains(document.activeElement))) {
        event.preventDefault(); first?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      if (workspace) workspace.inert = false;
      document.removeEventListener("keydown", onKey);
      menuRef.current?.focus();
    };
  }, [mobile, mobileOpen]);

  const loadSessions = useCallback(() => {
    const generation = ++sessionGeneration.current;
    api.listSessions().then((list) => {
      if (generation === sessionGeneration.current) setSessions(Array.isArray(list) ? list : []);
    }).catch(() => {}).finally(() => {
      if (generation === sessionGeneration.current) setSessionsLoading(false);
    });
  }, []);
  useEffect(() => {
    loadSessions();
    return () => { sessionGeneration.current += 1; };
  }, [loadSessions, pathname, activeSessionId]);
  useEffect(() => {
    window.addEventListener("vibe:sessions-refresh", loadSessions);
    return () => window.removeEventListener("vibe:sessions-refresh", loadSessions);
  }, [loadSessions]);
  const deleteSession = async () => {
    if (!deleteTarget || mutationBusy) return;
    setMutationBusy(true);
    try {
      await api.deleteSession(deleteTarget.session_id);
      setSessions(items => items.filter(item => item.session_id !== deleteTarget.session_id));
      setDeleteTarget(null);
    } catch { toast.error(t("layout.sessionActionFailed", { defaultValue: zh ? "操作失败，请重试。" : "Couldn't complete the action. Try again." })); }
    finally { setMutationBusy(false); }
  };
  const renameSession = async () => {
    if (!renameTarget || !renameValue.trim() || mutationBusy) return;
    setMutationBusy(true);
    try {
      await api.renameSession(renameTarget.session_id, renameValue.trim());
      setSessions(items => items.map(item => item.session_id === renameTarget.session_id ? { ...item, title: renameValue.trim() } : item));
      setRenameTarget(null);
    } catch { toast.error(t("layout.sessionActionFailed", { defaultValue: zh ? "操作失败，请重试。" : "Couldn't complete the action. Try again." })); }
    finally { setMutationBusy(false); }
  };
  const handleBgSave = (image: string | null): boolean => {
    if (!saveBackground(image)) { toast.error(t("layout.imageSaveFailed")); return false; }
    setBg(image);
    toast.success(t(image ? "layout.backgroundApplied" : "layout.backgroundRemoved"));
    return true;
  };
  const closeDrawer = () => setMobileOpen(false);

  return (
    <div className="studio-shell" data-overlay={overlayOpen || undefined} data-custom-background={Boolean(bg) || undefined}>
      <div className="studio-scene" data-custom={Boolean(bg) || undefined} aria-hidden="true" style={bg ? { backgroundImage: `url(${bg})` } : undefined} />
      <div className="studio-atmosphere" aria-hidden="true" />
      <a href="#main" className="studio-skip-link">{t("layout.skipToMain", { defaultValue: "Skip to main content" })}</a>
      {mobileOpen && <div className="studio-drawer-backdrop" aria-hidden="true" onClick={closeDrawer} />}
      <aside ref={sidebarRef} data-sidebar data-collapsed={collapsed || undefined} data-open={mobileOpen || undefined}
        aria-label={t("layout.sidebar", { defaultValue: "Vibe-Trading sidebar" })}
        role={mobile && mobileOpen ? "dialog" : undefined} aria-modal={mobile && mobileOpen ? true : undefined}
        inert={mobile && !mobileOpen} aria-hidden={mobile && !mobileOpen || undefined} className="studio-sidebar" style={mobile ? undefined : { width: sidebarWidth }}>
        <div className="studio-brand-row">
          <Link to="/" aria-label="Vibe-Trading" className="studio-brand" onClick={closeDrawer}>
            <span className="studio-brand-symbol"><BrandMark className="h-6 w-6" /></span>
            {!collapsed && <span className="studio-brand-wordmark"><strong>Vibe-Trading</strong><small>RESEARCH STUDIO</small></span>}
          </Link>
          <button ref={closeMenuRef} type="button" className="studio-icon-button studio-mobile-only" aria-label={t("layout.closeNavigation", { defaultValue: zh ? "关闭导航" : "Close navigation" })} onClick={closeDrawer}><X size={18} /></button>
          {!collapsed && <button type="button" className="studio-icon-button studio-desktop-only studio-collapse" onClick={toggleCollapse} title={t("layout.collapse")} aria-label={t("layout.collapse")}><ChevronsLeft size={16} /></button>}
        </div>
        <div className="studio-sidebar-scroll">
          <nav aria-label={t("layout.mainNavigation", { defaultValue: "Main navigation" })}>
            {groups.map(group => <div className="studio-nav-group" key={group.label}>
              {!collapsed && <p className="studio-section-label">{group.label}</p>}
              {group.items.map(({ to, icon: Icon, label }) => <Link key={to} to={to} onClick={closeDrawer}
                aria-label={label} aria-current={isActive(to) ? "page" : undefined} title={collapsed ? label : undefined}
                className={cn("studio-nav-link", isActive(to) && "is-active")}>
                <Icon size={17} strokeWidth={1.6} aria-hidden="true" />{!collapsed && <span>{label}</span>}
                {isActive(to) && !collapsed && <span className="studio-active-mark" aria-hidden="true" />}
              </Link>)}
            </div>)}
          </nav>
          {!collapsed && <section className="studio-sessions">
            <div className="studio-section-heading"><span className="studio-section-label">{t("layout.sessions")}</span>
              <Link to="/agent" className="studio-icon-button" aria-label={t("layout.newChat")} title={t("layout.newChat")} onClick={closeDrawer}><Plus size={16} /></Link>
            </div>
            {sessionsLoading && <div className="studio-session-skeleton" aria-hidden="true"><span /><span /><span /></div>}
            {!sessionsLoading && !sessions.length && <p className="studio-empty-sessions">{t("layout.noSessions")}</p>}
            <div className="studio-session-list">
              {sessions.map(session => <div className={cn("studio-session-row group", session.session_id === activeSessionId && "is-active")} key={session.session_id}>
                <Link to={`/agent?session=${session.session_id}`} onClick={closeDrawer} title={session.title || session.session_id} aria-current={session.session_id === activeSessionId ? "page" : undefined}>
                  {streamingSessionId === session.session_id ? <Loader2 size={13} className="animate-spin text-primary shrink-0" /> : <MessageSquare size={13} strokeWidth={1.4} className="shrink-0" />}
                  <span className="min-w-0 truncate">{session.title || session.session_id.slice(0, 16)}</span>
                </Link>
                <div className="studio-session-actions group-focus-within:opacity-100">
                  <button type="button" title={t("layout.rename")} aria-label={t("layout.rename")} onClick={() => { setRenameTarget(session); setRenameValue(session.title || ""); closeDrawer(); }}><Pencil size={13} /></button>
                  <button type="button" title={t("layout.delete")} aria-label={t("layout.delete")} onClick={() => { setDeleteTarget(session); closeDrawer(); }}><Trash2 size={13} /></button>
                </div>
              </div>)}
            </div>
          </section>}
        </div>
        <footer className="studio-sidebar-footer">
          <Link to="/settings" className={cn("studio-nav-link", isActive("/settings") && "is-active")} aria-label={t("layout.settings")} aria-current={isActive("/settings") ? "page" : undefined} title={collapsed ? t("layout.settings") : undefined} onClick={closeDrawer}><Settings size={17} strokeWidth={1.6} />{!collapsed && <span>{t("layout.settings")}</span>}</Link>
          <div className="studio-preference-tools">
            <button className="studio-icon-button" type="button" title={t("layout.changeAvatar")} aria-label={t("layout.changeAvatar")} onClick={() => { closeDrawer(); setAvatarDialogOpen(true); }}><UserRound size={16} /></button>
            <button className="studio-icon-button" type="button" title={t("layout.changeBackground")} aria-label={t("layout.changeBackground")} onClick={() => { closeDrawer(); setBgDialogOpen(true); }}><Image size={16} /></button>
            <button className="studio-icon-button" type="button" title={dark ? t("layout.light") : t("layout.dark")} aria-label={dark ? t("layout.light") : t("layout.dark")} onClick={toggle}>{dark ? <Sun size={16} /> : <Moon size={16} />}</button>
            {collapsed && <button className="studio-icon-button studio-desktop-only" type="button" title={t("layout.expand")} aria-label={t("layout.expand")} onClick={toggleCollapse}><ChevronsRight size={16} /></button>}
          </div>
          {!collapsed && <div className="studio-footer-meta"><LanguageSwitcher /><Link to="/about" onClick={closeDrawer} title={t("layout.about")}>{t("app.version")}<ArrowUpRight size={11} /></Link></div>}
        </footer>
        <div className="sidebar-resize-handle studio-resize-handle" onMouseDown={onResizeMouseDown} aria-hidden="true" />
      </aside>
      <div ref={workspaceRef} className="studio-workspace relative">
        {mobile && <header className="studio-mobile-header">
          <button ref={menuRef} type="button" className="studio-icon-button" aria-label={t("layout.openNavigation", { defaultValue: zh ? "打开导航" : "Open navigation" })} aria-expanded={mobileOpen} onClick={() => setMobileOpen(true)}><Menu size={20} /></button>
          <span>{pageTitle}</span>
          <Link to="/agent" className="studio-icon-button" aria-label={t("layout.newChat")} title={t("layout.newChat")}><Plus size={20} /></Link>
        </header>}
        <ConnectionBanner status={sseStatus} retryAttempt={sseRetryAttempt} />
        <main id="main" className="studio-main flex-1 min-h-0 overflow-auto"><Outlet /></main>
      </div>
      <AvatarSelector open={avatarDialogOpen} onClose={() => setAvatarDialogOpen(false)} />
      <BackgroundSelector open={bgDialogOpen} onClose={() => setBgDialogOpen(false)} onSave={handleBgSave} />
      <ConfirmDialog open={Boolean(deleteTarget)} title={t("layout.delete")} description={deleteTarget?.title || deleteTarget?.session_id}
        confirmLabel={t("layout.confirm")} cancelLabel={t("layout.cancel")} tone="destructive" onCancel={() => { if (!mutationBusy) setDeleteTarget(null); }} onConfirm={() => void deleteSession()} />
      <DialogSurface open={Boolean(renameTarget)} title={t("layout.rename")} closeLabel={t("layout.cancel")} onClose={() => { if (!mutationBusy) setRenameTarget(null); }}>
        <form onSubmit={event => { event.preventDefault(); void renameSession(); }} className="space-y-4">
          <input data-autofocus value={renameValue} maxLength={160} disabled={mutationBusy} onChange={event => setRenameValue(event.target.value)}
            aria-label={`${t("layout.rename")}: ${renameTarget?.title || renameTarget?.session_id || ""}`} className="studio-rename-input focus:ring-2 focus:ring-primary/40" />
          <div className="flex justify-end gap-2"><button type="button" className="studio-secondary-button" disabled={mutationBusy} onClick={() => setRenameTarget(null)}>{t("layout.cancel")}</button><button type="submit" className="studio-primary-button" disabled={mutationBusy || !renameValue.trim()}>{mutationBusy ? <Loader2 size={16} className="animate-spin" /> : t("layout.confirm")}</button></div>
        </form>
      </DialogSurface>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Language switcher — dropdown listing every language registered in
// src/i18n/index.ts. Persists the choice via i18next's localStorage detector
// and emits the `languageChanged` event handled in the i18n module to flip
// <html dir/lang> for RTL languages.
//
// Anchor to the footer trigger using logical CSS coordinates. The sidebar's
// backdrop-filter/transform establishes a containing block, so viewport-based
// fixed coordinates would double-count its RTL offset and overflow the screen.
// ---------------------------------------------------------------------------
function LanguageSwitcher() {
  const { i18n, t } = useTranslation();
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement | null>(null);

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
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      }
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
    <div style={{ position: "relative" }}>
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
      {open && (
        <ul
          data-lang-menu
          aria-label="Select language"
          style={{
            position: "absolute",
            insetInlineStart: 0,
            bottom: "calc(100% + 8px)",
            width: 160,
            maxWidth: "calc(100vw - 24px)",
            maxHeight: "60dvh",
            overflowY: "auto",
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
