import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { paperApi, type PaperDataStatus } from "@/lib/api";

const field = "mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm";
const button = "rounded-lg border border-border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50";
const JOB_LABELS = {
  updated: "paper.dataJob_updated",
  up_to_date: "paper.dataJob_up_to_date",
  update_failed: "paper.dataJob_update_failed",
  update_timeout: "paper.dataJob_update_timeout",
  interrupted: "paper.dataJob_interrupted",
  data_not_ready: "paper.dataJob_data_not_ready",
} as const;

function symbolsFrom(value: string): string[] {
  return [...new Set(value.split(/[\s,，]+/).map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))].sort();
}

function displayTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function PaperDataUpdate({ disabled, onBusyChange }: {
  disabled: boolean;
  onBusyChange: (busy: boolean) => void;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<PaperDataStatus | null>(null);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState<"save" | "update" | "refresh" | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const generation = useRef(0);
  const inFlight = useRef(false);
  const remoteRunning = status?.last_job?.status === "running";
  const mutating = action === "save" || action === "update" || remoteRunning;

  useEffect(() => {
    onBusyChange(Boolean(mutating));
  }, [mutating, onBusyChange]);

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    paperApi.dataStatus(controller.signal).then((response) => {
      if (current !== generation.current) return;
      setStatus(response);
      setDraft(response.watchlist.join(", "));
    }).catch((e: Error) => {
      if (current === generation.current && e.name !== "AbortError") setError(e.message);
    }).finally(() => {
      if (current === generation.current) setLoading(false);
    });
    return () => {
      generation.current += 1;
      controller.abort();
      onBusyChange(false);
    };
  }, [onBusyChange]);

  const symbols = symbolsFrom(draft);
  const maxSymbols = status?.limits.max_symbols ?? 10;
  const valid = symbols.length > 0 && symbols.length <= maxSymbols
    && symbols.every((symbol) => /^\d{6}\.(SH|SZ)$/.test(symbol));
  const dirty = JSON.stringify(symbols) !== JSON.stringify([...(status?.watchlist ?? [])].sort());
  const locked = disabled || loading || action !== null || remoteRunning;

  async function refresh() {
    if (inFlight.current || loading) return;
    inFlight.current = true;
    const current = ++generation.current;
    setAction("refresh");
    setError("");
    try {
      const response = await paperApi.dataStatus();
      if (current === generation.current) {
        setStatus(response);
        if (!status) setDraft(response.watchlist.join(", "));
      }
    } catch (e) {
      if (current === generation.current) setError((e as Error).message);
    } finally {
      inFlight.current = false;
      if (current === generation.current) setAction(null);
    }
  }

  async function mutate(kind: "save" | "update") {
    if (inFlight.current || locked || !status || (kind === "save" ? !valid || !dirty : dirty || !status.update_available || !status.watchlist.length)) return;
    inFlight.current = true;
    const current = ++generation.current;
    setAction(kind);
    setError("");
    setNotice("");
    try {
      const response = await (kind === "save" ? paperApi.saveWatchlist(symbols) : paperApi.updateData());
      if (current !== generation.current) return;
      setStatus(response);
      if (kind === "save") {
        setDraft(response.watchlist.join(", "));
        setNotice(t("paper.dataWatchlistSaved"));
      }
    } catch (e) {
      if (current !== generation.current) return;
      setError((e as Error).message);
      // A lost response does not mean the server abandoned the mutation.
      // Reconcile its durable state, but never automatically repeat the write.
      try {
        const response = await paperApi.dataStatus();
        if (current === generation.current) setStatus(response);
      } catch { /* Keep the original error and allow an explicit status refresh. */ }
    } finally {
      inFlight.current = false;
      if (current === generation.current) setAction(null);
    }
  }

  function save(event: FormEvent) {
    event.preventDefault();
    void mutate("save");
  }

  const job = status?.last_job;
  const jobLabel = job && Object.prototype.hasOwnProperty.call(JOB_LABELS, job.code)
    ? JOB_LABELS[job.code as keyof typeof JOB_LABELS] : JOB_LABELS.update_failed;
  return (
    <section aria-labelledby="paper-data-title" className="space-y-4 rounded-2xl border border-border bg-card p-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="paper-data-title" className="text-lg font-semibold">{t("paper.dataTitle")}</h2>
        <button type="button" className={button} disabled={disabled || loading || action !== null}
          onClick={() => void refresh()}>{t(action === "refresh" ? "paper.dataRefreshing" : "paper.dataRefresh")}</button>
      </div>
      <p className="text-sm text-muted-foreground">{t("paper.dataHint")}</p>
      {loading && <p className="text-sm" role="status">{t("paper.dataLoading")}</p>}
      {error && <p role="alert" className="break-words text-sm text-danger">{error}</p>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
      {status && <p className="text-sm text-muted-foreground">
        {t(`paper.dataState_${status.status_code}`)} · {t("paper.dataClosedThrough")}: {status.latest_closed_date}
      </p>}
      <form onSubmit={save} className="space-y-3">
        <fieldset disabled={locked || !status} className="min-w-0 space-y-3">
          <label className="block text-sm">{t("paper.dataWatchlist")}
            <input className={field} value={draft} maxLength={200} placeholder="600522.SH, 000001.SZ"
              onChange={(event) => { setDraft(event.target.value); setNotice(""); }}
              aria-describedby="paper-data-symbol-hint" />
          </label>
          <p id="paper-data-symbol-hint" className="text-xs text-muted-foreground">{t("paper.dataSymbolHint", { count: maxSymbols })}</p>
          <div className="flex flex-wrap items-center gap-3">
            <button className={button} type="submit" disabled={!valid || !dirty}>{t(action === "save" ? "paper.dataSaving" : "paper.dataSave")}</button>
            <button className={`${button} bg-primary text-primary-foreground`} type="button"
              disabled={dirty || !status?.update_available || !status.watchlist.length}
              onClick={() => void mutate("update")}>{t(action === "update" || remoteRunning ? "paper.dataUpdating" : "paper.dataUpdate")}</button>
          </div>
          {dirty && <p className="text-xs text-muted-foreground">{t("paper.dataSaveFirst")}</p>}
        </fieldset>
      </form>
      {job && <div className="rounded-lg border border-border p-3 text-sm" role={job.status === "failed" ? "alert" : "status"}>
        <p className="font-medium">{t("paper.dataLastUpdate")} · {t(job.outcome_unknown
          ? "paper.dataOutcomeUnknown" : job.status === "running" ? "paper.dataUpdating" : jobLabel)}</p>
        <p className="mt-1 break-words text-xs text-muted-foreground">{displayTime(job.finished_at ?? job.started_at)}</p>
        {job.updated_symbols.length > 0 && <p className="mt-1 break-words">{job.updated_symbols.join(", ")}</p>}
      </div>}
      {status && <div aria-label={t("paper.dataCoverage")} className="grid gap-3 lg:grid-cols-2">
        {status.coverage.length === 0 && <p className="text-sm text-muted-foreground">{t("paper.dataNoCoverage")}</p>}
        {status.coverage.map((item) => <article key={item.symbol} className="space-y-2 rounded-lg border border-border p-3 text-sm">
          <p className="font-medium">{item.symbol} · {t(`paper.dataCoverage_${item.status}`)}</p>
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-muted-foreground">{t("paper.dataRange")}</dt><dd className="break-words">{item.from_date ?? "—"} ～ {item.to_date ?? "—"}</dd>
            <dt className="text-muted-foreground">{t("paper.dataLatestBar")}</dt><dd>{item.latest_bar_date ?? "—"}</dd>
            <dt className="text-muted-foreground">{t("paper.dataMissingCount")}</dt><dd>{item.missing_count}</dd>
          </dl>
          {item.missing_dates.length > 0 && <p className="break-words text-xs text-muted-foreground">{item.missing_dates.join(", ")}{item.missing_count > item.missing_dates.length ? " …" : ""}</p>}
        </article>)}
      </div>}
      <p className="text-xs text-muted-foreground">{t("paper.dataSettlementHint")}</p>
    </section>
  );
}
