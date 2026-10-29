import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { paperApi, type PaperPreflight } from "@/lib/api";

const field = "mt-1 w-full rounded-lg border border-border bg-background px-3 py-2 text-sm";

export function PaperDataCheck({ market, disabled }: { market: string; disabled: boolean }) {
  const { t } = useTranslation();
  const [symbols, setSymbols] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<PaperPreflight | null>(null);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => () => { generation.current += 1; }, []);

  function change(setter: (value: string) => void, value: string) {
    setter(value);
    setResult(null);
    setError("");
  }

  async function check(event: FormEvent) {
    event.preventDefault();
    if (busy || disabled) return;
    const current = ++generation.current;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await paperApi.preflight({ market, from_date: from, to_date: to,
        symbols: [...new Set(symbols.split(/[\s,，]+/).map((symbol) => symbol.trim().toUpperCase()).filter(Boolean))] });
      if (current === generation.current) setResult(response);
    } catch (e) {
      if (current === generation.current) setError((e as Error).message);
    } finally {
      if (current === generation.current) setBusy(false);
    }
  }

  return (
    <details className="rounded-2xl border border-border bg-card p-5">
      <summary className="cursor-pointer font-medium">{t("paper.preflightTitle")}</summary>
      <form onSubmit={check} className="mt-4 space-y-4">
        <p className="text-sm text-muted-foreground">{t("paper.preflightHint")}</p>
        <fieldset disabled={disabled || busy} className="grid min-w-0 gap-3 sm:grid-cols-3">
          <label className="text-sm">{t("paper.preflightSymbols")}
            <input className={field} value={symbols} maxLength={200}
              placeholder={market === "cn_a" ? "600522.SH" : market === "hk" ? "00700.HK" : "AAPL.US"}
              onChange={(event) => change(setSymbols, event.target.value)} />
          </label>
          <label className="text-sm">{t("paper.preflightFrom")}
            <input className={field} type="date" required value={from} max={to || undefined}
              onChange={(event) => change(setFrom, event.target.value)} />
          </label>
          <label className="text-sm">{t("paper.preflightTo")}
            <input className={field} type="date" required value={to} min={from || undefined}
              onChange={(event) => change(setTo, event.target.value)} />
          </label>
          <button className="rounded-lg border border-border px-3 py-2 text-sm hover:bg-muted disabled:opacity-50 sm:col-span-3" type="submit">
            {t(busy ? "paper.preflightWorking" : "paper.preflightRun")}
          </button>
        </fieldset>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        {result && <section aria-label={t("paper.preflightResult")} className="space-y-3 text-sm">
          <p role="status" className="font-medium">{t(`paper.preflightStatus_${result.status}`)}</p>
          <ul className="space-y-2">
            {result.checks.map((item) => <li key={item.key} className="rounded-lg border border-border p-3">
              <p className="font-medium">{t(`paper.preflightCheck_${item.key}`, { defaultValue: item.key })} · {t(`paper.preflightStatus_${item.status}`)}</p>
              <p className="mt-1 whitespace-pre-wrap break-words text-muted-foreground">{item.message}</p>
              {!!item.missing_dates?.length && <p className="mt-1 break-words">{t("paper.preflightMissing")}: {item.missing_dates.join(", ")}</p>}
            </li>)}
          </ul>
          {result.bars.map((bar) => <p key={bar.symbol} className="break-words">
            {bar.symbol} · {t("paper.preflightAsOf")}: {bar.as_of ?? "—"}
            {bar.missing_dates.length > 0 && ` · ${t("paper.preflightMissing")}: ${bar.missing_dates.join(", ")}`}
            {bar.invalid_dates.length > 0 && ` · ${t("paper.preflightInvalid")}: ${bar.invalid_dates.join(", ")}`}
          </p>)}
          <p className="text-muted-foreground">{t("paper.source")}: {result.provenance.source || "—"}</p>
          {result.warnings.map((warning, index) => <p className="text-muted-foreground" key={index}>{warning}</p>)}
        </section>}
      </form>
    </details>
  );
}
