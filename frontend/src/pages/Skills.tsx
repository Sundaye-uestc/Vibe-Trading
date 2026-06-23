import i18n from "@/i18n";
import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ExternalLink, Loader2, Package, Search, Tag } from "lucide-react";
import { toast } from "sonner";
import { api, type SkillSummary, type SkillDetailResponse } from "@/lib/api";
import { cn } from "@/lib/utils";
import { useTranslation } from "react-i18next";

/* ---------- Constants ---------- */

const CATEGORY_ORDER = [
  "strategy", "analysis", "data-source", "asset-class",
  "crypto", "flow", "tool", "risk-analysis", "other",
];

const CATEGORY_STYLES: Record<string, string> = {
  "strategy":       "from-blue-500/10 to-blue-600/5 border-blue-500/20",
  "analysis":       "from-violet-500/10 to-violet-600/5 border-violet-500/20",
  "data-source":    "from-emerald-500/10 to-emerald-600/5 border-emerald-500/20",
  "asset-class":    "from-amber-500/10 to-amber-600/5 border-amber-500/20",
  "crypto":         "from-orange-500/10 to-orange-600/5 border-orange-500/20",
  "flow":           "from-cyan-500/10 to-cyan-600/5 border-cyan-500/20",
  "tool":           "from-slate-500/10 to-slate-600/5 border-slate-500/20",
  "risk-analysis":  "from-red-500/10 to-red-600/5 border-red-500/20",
  "other":          "from-zinc-500/10 to-zinc-600/5 border-zinc-500/20",
};

const CATEGORY_ICON_COLORS: Record<string, string> = {
  "strategy":       "text-blue-600 dark:text-blue-400",
  "analysis":       "text-violet-600 dark:text-violet-400",
  "data-source":    "text-emerald-600 dark:text-emerald-400",
  "asset-class":    "text-amber-600 dark:text-amber-400",
  "crypto":         "text-orange-600 dark:text-orange-400",
  "flow":           "text-cyan-600 dark:text-cyan-400",
  "tool":           "text-slate-600 dark:text-slate-400",
  "risk-analysis":  "text-red-600 dark:text-red-400",
  "other":          "text-zinc-600 dark:text-zinc-400",
};

/* ---------- Page entry ---------- */

export function Skills() {
  const params = useParams<{ skillName?: string }>();

  if (params.skillName) {
    return <DetailView skillName={params.skillName} />;
  }
  return <BrowseView />;
}

/* ---------- Browse view ---------- */

function BrowseView() {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<SkillSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [categoryFilter, setCategoryFilter] = useState<string>("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    let alive = true;
    setLoading(true);
    api
      .listSkills()
      .then((res) => {
        if (!alive) return;
        setSkills(res);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        const msg = err instanceof Error ? err.message : t("skills.loadError");
        toast.error(msg);
        setSkills([]);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => { alive = false; };
  }, []);

  // Category counts
  const catCounts = useMemo(() => {
    const map: Record<string, number> = {};
    for (const s of skills) {
      const c = s.category || "other";
      map[c] = (map[c] || 0) + 1;
    }
    return map;
  }, [skills]);

  // Unique categories present, in display order
  const categories = useMemo(() => {
    const present = new Set(skills.map((s) => s.category || "other"));
    return CATEGORY_ORDER.filter((c) => present.has(c));
  }, [skills]);

  // Filtered + searched
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return skills.filter((s) => {
      if (categoryFilter && (s.category || "other") !== categoryFilter) return false;
      if (q) {
        const catLabel = i18n.t(`skills.categories.${s.category || "other"}`, s.category || "other").toLowerCase();
        return (
          s.name.toLowerCase().includes(q) ||
          s.description.toLowerCase().includes(q) ||
          catLabel.includes(q)
        );
      }
      return true;
    });
  }, [skills, categoryFilter, search]);

  return (
    <div className="p-4 md:p-8 max-w-6xl mx-auto space-y-8">
      {/* Hero */}
      <div className="space-y-2">
        <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase tracking-wide">
          <Package className="h-3.5 w-3.5" aria-hidden="true" /> {t("skills.title")}
        </div>
        <h1 className="text-2xl md:text-3xl font-bold tracking-tight">
          {t("skills.countLabel", { count: skills.length })}
        </h1>
        <p className="text-sm text-muted-foreground max-w-2xl">
          {t("skills.subtitle")}
        </p>
      </div>

      {/* Category cards */}
      {categories.length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <button
            type="button"
            onClick={() => setCategoryFilter("")}
            className={cn(
              "text-left border rounded-xl p-3 space-y-1 transition bg-gradient-to-br from-zinc-500/5 to-zinc-600/5 hover:border-primary/50",
              !categoryFilter && "border-primary ring-1 ring-primary/30",
            )}
          >
            <div className="flex items-center justify-between">
              <Package className="h-4 w-4 text-primary" aria-hidden="true" />
              <span className="text-xs font-mono text-muted-foreground">{skills.length}</span>
            </div>
            <h3 className="font-semibold text-sm">{t("skills.title")}</h3>
          </button>

          {categories.map((cat) => {
            const active = categoryFilter === cat;
            const count = catCounts[cat] || 0;
            return (
              <button
                key={cat}
                type="button"
                onClick={() => setCategoryFilter(active ? "" : cat)}
                className={cn(
                  "text-left border rounded-xl p-3 space-y-1 transition bg-gradient-to-br",
                  CATEGORY_STYLES[cat] || CATEGORY_STYLES.other,
                  "hover:border-primary/50",
                  active && "border-primary ring-1 ring-primary/30",
                )}
              >
                <div className="flex items-center justify-between">
                  <Tag className={cn("h-4 w-4", CATEGORY_ICON_COLORS[cat] || CATEGORY_ICON_COLORS.other)} aria-hidden="true" />
                  <span className="text-xs font-mono text-muted-foreground">{count}</span>
                </div>
                <h3 className="font-semibold text-sm leading-tight">
                  {i18n.t(`skills.categories.${cat}`, cat)}
                </h3>
              </button>
            );
          })}
        </div>
      )}

      {/* Search bar */}
      <div className="flex items-end gap-3 border rounded-xl p-4 bg-card">
        <div className="flex-1 min-w-0">
          <label htmlFor="skill-search" className="text-xs text-muted-foreground block mb-1">
            {t("skills.searchPlaceholder")}
          </label>
          <div className="relative">
            <Search
              className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground"
              aria-hidden="true"
            />
            <input
              id="skill-search"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={t("skills.searchPlaceholder")}
              className="w-full pl-9 pr-3 py-2 rounded-lg border bg-background text-sm focus:outline-none focus:ring-2 focus:ring-primary/30"
            />
          </div>
        </div>
      </div>

      {/* Skill table */}
      <div className="border rounded-xl overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b bg-muted/40">
                <th className="text-left px-4 py-2.5 text-muted-foreground font-medium w-56">
                  {t("settings.name", "Skill")}
                </th>
                <th className="text-left px-4 py-2.5 text-muted-foreground font-medium">
                  {t("settings.description", t("skills.detail.source"))}
                </th>
                <th className="text-left px-4 py-2.5 text-muted-foreground font-medium w-28">
                  {t("skills.detail.category")}
                </th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr>
                  <td colSpan={3} className="px-4 py-12 text-center text-muted-foreground">
                    <Loader2 className="h-5 w-5 animate-spin inline mr-2" aria-hidden="true" />
                    Loading…
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={3} className="px-4 py-12 text-center text-muted-foreground">
                    {search || categoryFilter ? t("skills.noResults") : t("skills.noSkills")}
                  </td>
                </tr>
              ) : (
                filtered.map((s) => (
                  <tr
                    key={s.name}
                    className="border-b last:border-0 hover:bg-muted/20 transition-colors"
                  >
                    <td className="px-4 py-3 align-top">
                      <Link
                        to={`/skills/${encodeURIComponent(s.name)}`}
                        className="font-semibold text-sm text-primary hover:underline inline-flex items-center gap-1"
                      >
                        {s.name}
                        <ExternalLink className="h-3 w-3 opacity-50" aria-hidden="true" />
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-xs text-muted-foreground leading-relaxed">
                      {s.description}
                    </td>
                    <td className="px-4 py-3 align-top">
                      <span className={cn(
                        "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium",
                        CATEGORY_ICON_COLORS[s.category || "other"]?.replace("text-", "bg-").replace("600", "500/10").replace("400", "500/10") || "bg-zinc-500/10 text-zinc-500",
                      )}>
                        {i18n.t(`skills.categories.${s.category || "other"}`, s.category || "other")}
                      </span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

/* ---------- Detail view ---------- */

function DetailView({ skillName }: { skillName: string }) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<SkillDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    api
      .getSkill(skillName)
      .then((res) => {
        if (!alive) return;
        setDetail(res);
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : t("skills.loadError"));
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => { alive = false; };
  }, [skillName]);

  if (loading) {
    return (
      <div className="flex h-[60vh] items-center justify-center gap-2 text-muted-foreground">
        <Loader2 className="h-5 w-5 animate-spin" aria-hidden="true" />
        Loading…
      </div>
    );
  }

  if (error || !detail) {
    return (
      <div className="p-4 md:p-8 max-w-4xl mx-auto">
        <Link
          to="/skills"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground mb-6 transition-colors"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          {t("skills.detail.back")}
        </Link>
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-6 text-center">
          <p className="text-sm text-destructive">
            {error || t("skills.loadError")}
          </p>
        </div>
      </div>
    );
  }

  const cat = detail.category || "other";
  const catLabel = i18n.t(`skills.categories.${cat}`, cat);
  const metaEntries = Object.entries(detail.metadata || {}).filter(
    ([k]) => !["name", "description", "category", "license"].includes(k),
  );

  return (
    <div className="p-4 md:p-8 max-w-4xl mx-auto space-y-6">
      {/* Back link */}
      <Link
        to="/skills"
        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        {t("skills.detail.back")}
      </Link>

      {/* Hero */}
      <div className="space-y-3">
        <div className="flex items-center gap-2 text-xs text-muted-foreground uppercase tracking-wide">
          <Package className="h-3.5 w-3.5" aria-hidden="true" /> {t("skills.title")}
        </div>
        <h1 className="text-2xl md:text-3xl font-bold tracking-tight">{detail.name}</h1>
        <p className="text-sm text-muted-foreground">{detail.description}</p>
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium text-muted-foreground">
            <Tag className="h-3 w-3" aria-hidden="true" />
            {t("skills.detail.category")}: {catLabel}
          </span>
        </div>
      </div>

      {/* Metadata (if present beyond core fields) */}
      {metaEntries.length > 0 && (
        <div className="border rounded-xl p-4 space-y-2">
          <h2 className="text-sm font-semibold text-foreground">{t("skills.detail.metadata")}</h2>
          <dl className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-xs">
            {metaEntries.map(([key, value]) => (
              <div key={key}>
                <dt className="text-muted-foreground font-medium">{key}</dt>
                <dd className="text-foreground">
                  {typeof value === "string" ? value : JSON.stringify(value)}
                </dd>
              </div>
            ))}
          </dl>
        </div>
      )}

      {/* Skill body */}
      <div className="border rounded-xl overflow-hidden">
        <div className="px-4 py-2.5 border-b bg-muted/40">
          <h2 className="text-sm font-semibold text-foreground">{t("skills.detail.source")}</h2>
        </div>
        <div className="p-4">
          <pre className="text-xs leading-relaxed whitespace-pre-wrap font-mono text-muted-foreground max-h-[70vh] overflow-auto">
            {detail.body}
          </pre>
        </div>
      </div>
    </div>
  );
}
