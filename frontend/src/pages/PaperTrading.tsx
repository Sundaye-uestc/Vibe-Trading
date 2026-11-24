import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ArrowUpRight, FlaskConical, Plus, Trash2 } from "lucide-react";
import "@/styles/paper-studio.css";
import { PaperDataUpdate } from "@/components/paper/PaperDataUpdate";
import { PaperDataCheck } from "@/components/paper/PaperDataCheck";
import {
  paperApi,
  type PaperAccount,
  type PaperOrder,
  type PaperPosition,
  type PaperResult,
} from "@/lib/api";

const field = "studio-paper-field";
const button = "studio-paper-button";
const panel = "studio-paper-panel";

export function PaperTrading() {
  const { t } = useTranslation();
  const [accounts, setAccounts] = useState<PaperAccount[]>([]);
  const [selected, setSelected] = useState("");
  const [label, setLabel] = useState("");
  const [market, setMarket] = useState("us");
  const [start, setStart] = useState("today");
  const [cash, setCash] = useState("100000");
  const [positions, setPositions] = useState<PaperPosition[]>([]);
  const [orders, setOrders] = useState<PaperOrder[]>([]);
  const [mode, setMode] = useState("orders");
  const [strategy, setStrategy] = useState("");
  const [date, setDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [dataBusy, setDataBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [result, setResult] = useState<PaperResult | null>(null);
  const [history, setHistory] = useState<PaperResult[]>([]);
  const historyRequest = useRef(0);
  // Keep uncertain attempts even if the user edits and later restores a draft.
  const createRequests = useRef(new Map<string, string>());
  const account = accounts.find((a) => a.id === selected);
  const currency =
    account?.currency ?? { cn_a: "CNY", hk: "HKD", us: "USD" }[market];

  useEffect(() => {
    let active = true;
    paperApi
      .list()
      .then((items) => {
        if (active) setAccounts(items);
      })
      .catch((e: Error) => {
        if (active) setError(e.message);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    let active = true;
    const generation = ++historyRequest.current;
    setHistory([]);
    setResult(null);
    setNotice("");
    setError("");
    setOrders([]);
    setMode("orders");
    setStrategy("");
    setDate("");
    if (selected)
      paperApi
        .history(selected)
        .then((items) => {
          if (active && generation === historyRequest.current) setHistory(items);
        })
        .catch((e: Error) => {
          if (active && generation === historyRequest.current) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [selected]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (busy || loading || dataBusy) return;
    setBusy(true);
    setError("");
    const payload = { label, market, start, account_state: { cash: Number(cash), positions } };
    const fingerprint = JSON.stringify(payload);
    const requestKey = createRequests.current.get(fingerprint) ?? crypto.randomUUID();
    createRequests.current.set(fingerprint, requestKey);
    try {
      const created = await paperApi.create({ ...payload, idempotency_key: requestKey });
      createRequests.current.delete(fingerprint);
      setAccounts((items) => [created, ...items.filter((item) => item.id !== created.id)]);
      setSelected(created.id);
      setLabel("");
      setPositions([]);
    } catch (e) {
      setError((e as Error).message);
      // The server may have committed before the response was lost.
      try { setAccounts(await paperApi.list()); } catch { /* Keep the original error. */ }
    } finally {
      setBusy(false);
    }
  }
  async function advance(event: FormEvent) {
    event.preventDefault();
    if (!account || busy || loading || dataBusy) return;
    historyRequest.current += 1;
    setBusy(true);
    setError("");
    setNotice("");
    setResult(null);
    try {
      const reply = await paperApi.advance(account.id, {
        expected_revision: account.revision,
        to_date: date,
        ...(mode === "strategy" ? { strategy_path: strategy } : { orders }),
      });
      setAccounts((items) =>
        items.map((a) => (a.id === reply.account.id ? reply.account : a)),
      );
      setResult(reply.result);
      setNotice(t(reply.committed ? "paper.saved" : "paper.notSaved"));
      if (reply.committed) {
        setHistory((items) => [...items, reply.result]);
        try {
          setHistory(await paperApi.history(account.id));
        } catch (e) {
          // The ledger has committed even if its history refresh fails.
          setError((e as Error).message);
        }
      }
    } catch (e) {
      setError((e as Error).message);
      // A timed-out request may have committed. Reload before any explicit retry.
      try {
        const [freshAccounts, freshHistory] = await Promise.allSettled([
          paperApi.list(), paperApi.history(account.id),
        ]);
        if (freshAccounts.status === "fulfilled") setAccounts(freshAccounts.value);
        if (freshHistory.status === "fulfilled") setHistory(freshHistory.value);
      } catch {
        /* The original error stays visible. */
      }
    } finally {
      setBusy(false);
    }
  }
  const shown = result ?? account?.last_result;
  const navPoints = account?.last_result?.nav_series;
  const latestNav = navPoints?.[navPoints.length - 1];
  const number = (value: number) =>
    value.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return (
    <div className="studio-paper">
      <header className="studio-paper-header">
        <div className="studio-paper-kicker"><span aria-hidden="true" />{t("paper.studioEyebrow", { defaultValue: "A SPACE TO REHEARSE" })}</div>
        <div className="studio-paper-heading-row">
          <h1>{t("paper.title")}</h1>
          <FlaskConical className="studio-paper-emblem" aria-hidden="true" />
        </div>
        <p className="studio-paper-description">
          {t("paper.description")}
        </p>
      </header>
      {error && (
        <div
          role="alert"
          className="studio-paper-alert"
        >
          {error}
        </div>
      )}
      {account && <section className="studio-paper-overview" aria-label={t("paper.studioOverview", { defaultValue: "Account overview" })}>
        <div className="studio-paper-overview-heading">
          <span>{account.label}</span>
          <span>{t("paper.studioLedgerDate", { defaultValue: "Ledger through" })} · {account.last_date ?? t("paper.pending")}</span>
        </div>
        <dl className="studio-paper-metrics">
          <div><dt>{t("paper.cash")}</dt><dd>{number(account.account_state.cash)} <small>{currency}</small></dd></div>
          <div><dt>{t("paper.nav")}</dt><dd>{latestNav ? number(latestNav.nav) : "—"} <small>{currency}</small></dd></div>
          <div><dt>{t("paper.studioPositions", { defaultValue: "Positions" })}</dt><dd>{account.account_state.positions.length.toString().padStart(2, "0")}</dd></div>
        </dl>
      </section>}
      <div className="studio-paper-columns">
        <aside className="studio-paper-sidebar">
          <section className={panel}>
            <label className="block text-sm font-medium">
              {t("paper.accounts")}
              <select
                className={`${field} mt-2`}
                value={selected}
                disabled={busy || loading || dataBusy}
                onChange={(e) => setSelected(e.target.value)}
              >
                <option value="">{t("paper.newAccount")}</option>
                {accounts.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.label} · {a.currency}
                  </option>
                ))}
              </select>
            </label>
            {account && (
              <>
                <p className="studio-paper-eligible text-sm text-muted-foreground">
                  {t("paper.eligible", { date: account.start_on })}
                </p>
                {account.account_state.positions.map((p) => (
                  <div
                    key={p.symbol}
                    className="studio-paper-position"
                  >
                    <span>{p.symbol}</span>
                    <span>
                      {number(p.qty)} × {number(p.avg_cost)}
                    </span>
                  </div>
                ))}
              </>
            )}
          </section>
          {(account?.market ?? market) === "cn_a" && (
            <p className="studio-paper-data-requirements">{t("paper.dataRequirements")}</p>
          )}
          {(account?.market ?? market) === "cn_a" && <PaperDataUpdate disabled={busy || loading} onBusyChange={setDataBusy} />}
          <PaperDataCheck key={`${selected}:${account?.market ?? market}`} market={account?.market ?? market} disabled={busy || loading || dataBusy} />
        </aside>
        <div className="studio-paper-editor">
          {!account ? (
            <form onSubmit={create} className={`${panel} studio-paper-form`}>
              <fieldset disabled={busy || loading || dataBusy} className="studio-paper-fieldset">
              <h2 className="text-lg font-semibold">{t("paper.newAccount")}</h2>
              <label className="block text-sm">
                {t("paper.label")}
                <input
                  className={`${field} mt-1`}
                  required
                  maxLength={80}
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                />
              </label>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="text-sm">
                  {t("paper.market")}
                  <select
                    className={`${field} mt-1`}
                    value={market}
                    onChange={(e) => setMarket(e.target.value)}
                  >
                    {(["us", "hk", "cn_a"] as const).map((m) => (
                      <option key={m} value={m}>
                        {t(`paper.${m}`)}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="text-sm">
                  {t("paper.start")}
                  <select
                    className={`${field} mt-1`}
                    value={start}
                    onChange={(e) => setStart(e.target.value)}
                  >
                    <option value="today">{t("paper.today")}</option>
                    <option value="next_session">
                      {t("paper.nextSession")}
                    </option>
                  </select>
                </label>
              </div>
              <label className="block text-sm">
                {t("paper.cash")} ({currency})
                <input
                  className={`${field} mt-1`}
                  type="number"
                  min="0"
                  step="any"
                  required
                  value={cash}
                  onChange={(e) => setCash(e.target.value)}
                />
              </label>
              <p className="text-sm font-medium">
                {t("paper.initialPositions")}
              </p>
              {positions.map((p, i) => (
                <div
                  className="studio-paper-order-row"
                  key={i}
                >
                  <input
                    aria-label={t("paper.symbol")}
                    placeholder={t("paper.symbol")}
                    className={field}
                    required
                    value={p.symbol}
                    onChange={(e) =>
                      setPositions((ps) =>
                        ps.map((v, j) =>
                          j === i ? { ...v, symbol: e.target.value } : v,
                        ),
                      )
                    }
                  />
                  <input
                    aria-label={t("paper.quantity")}
                    className={field}
                    type="number"
                    min="0.00000001"
                    step="any"
                    required
                    value={p.qty}
                    onChange={(e) =>
                      setPositions((ps) =>
                        ps.map((v, j) =>
                          j === i ? { ...v, qty: Number(e.target.value) } : v,
                        ),
                      )
                    }
                  />
                  <input
                    aria-label={t("paper.cost")}
                    className={field}
                    type="number"
                    min="0.00000001"
                    step="any"
                    required
                    value={p.avg_cost}
                    onChange={(e) =>
                      setPositions((ps) =>
                        ps.map((v, j) =>
                          j === i
                            ? { ...v, avg_cost: Number(e.target.value) }
                            : v,
                        ),
                      )
                    }
                  />
                  <button
                    type="button"
                    className={button}
                    aria-label={t("paper.remove")}
                    onClick={() =>
                      setPositions((ps) => ps.filter((_, j) => j !== i))
                    }
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              ))}
              <button
                type="button"
                className={`${button} flex items-center gap-2`}
                onClick={() =>
                  setPositions((ps) => [
                    ...ps,
                    { symbol: "", qty: 1, avg_cost: 1 },
                  ])
                }
              >
                <Plus size={16} />
                {t("paper.addPosition")}
              </button>
              <button
                disabled={busy}
                className={`${button} studio-paper-button--primary`}
              >
                {t(busy ? "paper.working" : "paper.create")}<ArrowUpRight size={16} aria-hidden="true" />
              </button>
              </fieldset>
            </form>
          ) : (
            <form onSubmit={advance} className={`${panel} studio-paper-form`}>
              <fieldset disabled={busy || loading || dataBusy} className="studio-paper-fieldset">
              <h2 className="text-lg font-semibold">{t("paper.advance")}</h2>
              <p className="text-sm text-muted-foreground">
                {t("paper.executionNote")}
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="text-sm">
                  {t("paper.date")}
                  <input
                    type="date"
                    required
                    className={`${field} mt-1`}
                    min={account.start_on}
                    value={date}
                    onChange={(e) => setDate(e.target.value)}
                  />
                </label>
                <label className="text-sm">
                  {t("paper.mode")}
                  <select
                    className={`${field} mt-1`}
                    value={mode}
                    onChange={(e) => setMode(e.target.value)}
                  >
                    <option value="orders">{t("paper.orders")}</option>
                    <option value="strategy">{t("paper.strategy")}</option>
                  </select>
                </label>
              </div>
              {mode === "strategy" ? (
                <label className="block text-sm">
                  {t("paper.strategyPath")}
                  <input
                    className={`${field} mt-1`}
                    required
                    value={strategy}
                    onChange={(e) => setStrategy(e.target.value)}
                  />
                </label>
              ) : (
                <>
                  {orders.map((o, i) => (
                    <div
                      key={i}
                      className="studio-paper-order-row"
                    >
                      <input
                        aria-label={t("paper.symbol")}
                        placeholder={t("paper.symbol")}
                        className={field}
                        required
                        value={o.symbol}
                        onChange={(e) =>
                          setOrders((os) =>
                            os.map((v, j) =>
                              j === i ? { ...v, symbol: e.target.value } : v,
                            ),
                          )
                        }
                      />
                      <select
                        aria-label={t("paper.side")}
                        className={field}
                        value={o.side}
                        onChange={(e) =>
                          setOrders((os) =>
                            os.map((v, j) =>
                              j === i
                                ? {
                                    ...v,
                                    side: e.target.value as "buy" | "sell",
                                  }
                                : v,
                            ),
                          )
                        }
                      >
                        <option value="buy">{t("paper.buy")}</option>
                        <option value="sell">{t("paper.sell")}</option>
                      </select>
                      <input
                        aria-label={t("paper.quantity")}
                        type="number"
                        min="0.00000001"
                        step="any"
                        required
                        className={field}
                        value={o.qty}
                        onChange={(e) =>
                          setOrders((os) =>
                            os.map((v, j) =>
                              j === i
                                ? { ...v, qty: Number(e.target.value) }
                                : v,
                            ),
                          )
                        }
                      />
                      <button
                        type="button"
                        className={button}
                        aria-label={t("paper.remove")}
                        onClick={() =>
                          setOrders((os) => os.filter((_, j) => j !== i))
                        }
                      >
                        <Trash2 size={16} />
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    className={button}
                    onClick={() =>
                      setOrders((os) => [
                        ...os,
                        { symbol: "", side: "buy", qty: 1 },
                      ])
                    }
                  >
                    {t("paper.addOrder")}
                  </button>
                  <p className="text-xs text-muted-foreground">
                    {t("paper.emptyOrders")}
                  </p>
                </>
              )}
              <button
                disabled={busy}
                className={`${button} studio-paper-button--primary`}
              >
                {t(busy ? "paper.working" : "paper.advance")}<ArrowUpRight size={16} aria-hidden="true" />
              </button>
              </fieldset>
            </form>
          )}
          {notice && (
            <p role="status" className="studio-paper-notice">
              {notice}
            </p>
          )}
          {shown && (
            <section className={panel}>
              <h2 className="font-semibold">{t("paper.result")}</h2>
              {shown.code && (
                <p role="alert">
                  {shown.code}: {shown.message}
                </p>
              )}
              {shown.data_provenance && (
                <p className="text-xs text-muted-foreground">
                  {shown.data_provenance.source} ·{" "}
                  {shown.data_provenance.bars_as_of}
                </p>
              )}
              {!!shown.fills?.length && (
                <div className="overflow-x-auto">
                  <table className="studio-paper-table studio-paper-table--fills">
                    <thead>
                      <tr>
                        {(
                          [
                            "date",
                            "symbol",
                            "side",
                            "quantity",
                            "price",
                            "fee",
                          ] as const
                        ).map((key) => (
                          <th key={key} className="p-2">
                            {t(`paper.${key}`)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {shown.fills.map((f, i) => (
                        <tr key={i} className="border-t border-border">
                          <td className="p-2">{f.trade_date}</td>
                          <td>{f.symbol}</td>
                          <td>
                            {t(f.side === "buy" ? "paper.buy" : "paper.sell")}
                          </td>
                          <td>{number(f.qty)}</td>
                          <td>{number(f.price)}</td>
                          <td>{number(f.fee)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {shown.rejected_orders?.map((r, i) => (
                <p key={i} className="text-sm text-danger">
                  {r.symbol}: {r.reason_code}
                </p>
              ))}
              {shown.warnings?.map((w, i) => (
                <p key={i} className="text-xs text-muted-foreground">
                  {w}
                </p>
              ))}
            </section>
          )}
          {!!history.length && (
            <section className={panel}>
              <h2 className="font-semibold">
                {t("paper.history")} ({currency})
              </h2>
              <div className="max-h-64 overflow-auto">
                <table className="studio-paper-table">
                  <thead>
                    <tr>
                      <th className="text-start">{t("paper.date")}</th>
                      <th className="text-end">{t("paper.nav")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history
                      .flatMap((h) => h.nav_series ?? [])
                      .map((n) => (
                        <tr key={n.date} className="border-t border-border">
                          <td className="py-2">{n.date}</td>
                          <td className="text-end">{number(n.nav)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
