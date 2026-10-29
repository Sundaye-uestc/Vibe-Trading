import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PaperDataUpdate } from "../PaperDataUpdate";
import { paperApi, type PaperDataStatus } from "@/lib/api";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("@/lib/api", () => ({ paperApi: { dataStatus: vi.fn(), saveWatchlist: vi.fn(), updateData: vi.fn() } }));

const state: PaperDataStatus = {
  configured: true, update_available: true, status_code: "ready", watchlist: ["600522.SH"],
  latest_closed_date: "2026-10-29",
  coverage: [{ symbol: "600522.SH", from_date: "2026-04-01", to_date: "2026-10-28", latest_bar_date: "2026-10-28",
    missing_dates: ["2026-10-29"], missing_count: 1, status: "missing" }],
  last_job: null, limits: { max_symbols: 10, lookback_days: 200 },
};
const completed: PaperDataStatus = {
  ...state, update_available: false,
  coverage: [{ ...state.coverage[0], to_date: "2026-10-29", latest_bar_date: "2026-10-29", missing_dates: [], missing_count: 0, status: "ready" }],
  last_job: { id: "job-1", status: "success", code: "updated", started_at: "2026-10-29T08:00:00Z",
    finished_at: "2026-10-29T08:00:05Z", updated_symbols: ["600522.SH"], imported_snapshots: 1 },
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(paperApi.dataStatus).mockResolvedValue(state);
});
async function ready() {
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.dataUpdate" })).toBeEnabled());
}

test("initial load reads saved coverage without saving or downloading data", async () => {
  render(<PaperDataUpdate disabled={false} onBusyChange={vi.fn()} />);
  await ready();
  expect(screen.getByLabelText("paper.dataWatchlist")).toHaveValue("600522.SH");
  expect(screen.getByText("600522.SH · paper.dataCoverage_missing")).toBeInTheDocument();
  expect(screen.getByText("paper.dataClosedThrough", { exact: false })).toHaveTextContent("2026-10-29");
  expect(screen.getByText("2026-10-28")).toBeInTheDocument();
  expect(paperApi.saveWatchlist).not.toHaveBeenCalled();
  expect(paperApi.updateData).not.toHaveBeenCalled();
});

test("saves normalized symbols separately and requires a saved draft before updating", async () => {
  vi.mocked(paperApi.saveWatchlist).mockResolvedValue({ ...state, watchlist: ["000001.SZ", "600522.SH"] });
  render(<PaperDataUpdate disabled={false} onBusyChange={vi.fn()} />);
  await ready();
  fireEvent.change(screen.getByLabelText("paper.dataWatchlist"), { target: { value: "600522.sh， 000001.sz,600522.SH" } });
  expect(screen.getByRole("button", { name: "paper.dataUpdate" })).toBeDisabled();
  expect(screen.getByText("paper.dataSaveFirst")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "paper.dataSave" }));
  await screen.findByText("paper.dataWatchlistSaved");
  expect(paperApi.saveWatchlist).toHaveBeenCalledWith(["000001.SZ", "600522.SH"]);
  expect(screen.getByLabelText("paper.dataWatchlist")).toHaveValue("000001.SZ, 600522.SH");
  expect(paperApi.updateData).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "paper.dataSave" })).toBeDisabled();
});

test("rejects invalid markets and excessive symbols before making a request", async () => {
  render(<PaperDataUpdate disabled={false} onBusyChange={vi.fn()} />);
  await ready();
  for (const draft of ["AAPL.US", "600522.SH,430139.BJ", Array.from({ length: 11 }, (_, i) => `${String(i).padStart(6, "0")}.SZ`).join(",")]) {
    fireEvent.change(screen.getByLabelText("paper.dataWatchlist"), { target: { value: draft } });
    expect(screen.getByRole("button", { name: "paper.dataSave" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "paper.dataUpdate" })).toBeDisabled();
  }
  expect(paperApi.saveWatchlist).not.toHaveBeenCalled();
});

test("locks the form and parent during a single explicit update, then shows authoritative dates", async () => {
  const pending = deferred<PaperDataStatus>();
  const onBusy = vi.fn();
  vi.mocked(paperApi.updateData).mockReturnValue(pending.promise);
  render(<PaperDataUpdate disabled={false} onBusyChange={onBusy} />);
  await ready();
  const update = screen.getByRole("button", { name: "paper.dataUpdate" });
  fireEvent.click(update);
  fireEvent.click(update);
  expect(paperApi.updateData).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("paper.dataWatchlist")).toBeDisabled();
  expect(screen.getByRole("button", { name: "paper.dataRefresh" })).toBeDisabled();
  expect(onBusy).toHaveBeenLastCalledWith(true);
  await act(async () => { pending.resolve(completed); });
  expect(screen.getByText(/paper.dataJob_updated/)).toBeInTheDocument();
  expect(screen.getByText("600522.SH · paper.dataCoverage_ready")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "paper.dataUpdate" })).toBeDisabled();
  expect(onBusy).toHaveBeenLastCalledWith(false);
});

test("an incomplete source update retains the prior dates and never claims success", async () => {
  vi.mocked(paperApi.updateData).mockResolvedValue({ ...state, last_job: { ...completed.last_job!, status: "failed", code: "data_not_ready", updated_symbols: [], imported_snapshots: 0 } });
  render(<PaperDataUpdate disabled={false} onBusyChange={vi.fn()} />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "paper.dataUpdate" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("paper.dataJob_data_not_ready");
  expect(screen.getByText("2026-10-28")).toBeInTheDocument();
  expect(screen.queryByText(/paper.dataJob_updated/)).not.toBeInTheDocument();
  expect(paperApi.updateData).toHaveBeenCalledTimes(1);
});

test("labels an uncertain outcome without asserting that published data was rolled back", async () => {
  vi.mocked(paperApi.updateData).mockResolvedValue({ ...state,
    last_job: { ...completed.last_job!, status: "failed", code: "update_timeout", outcome_unknown: true },
  });
  render(<PaperDataUpdate disabled={false} onBusyChange={vi.fn()} />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "paper.dataUpdate" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("paper.dataOutcomeUnknown");
  expect(screen.queryByText(/paper.dataJob_updated/)).not.toBeInTheDocument();
  expect(screen.queryByText(/paper.dataJob_update_timeout/)).not.toBeInTheDocument();
  expect(paperApi.updateData).toHaveBeenCalledTimes(1);
});

test("lost update responses refresh durable state and keep a running job locked without retrying", async () => {
  const onBusy = vi.fn();
  vi.mocked(paperApi.dataStatus).mockResolvedValueOnce(state).mockResolvedValueOnce({ ...state,
    last_job: { ...completed.last_job!, status: "running", code: "running", finished_at: null, updated_symbols: [] },
  }).mockResolvedValue(completed);
  vi.mocked(paperApi.updateData).mockRejectedValue(new Error("Response lost"));
  render(<PaperDataUpdate disabled={false} onBusyChange={onBusy} />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "paper.dataUpdate" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Response lost");
  await waitFor(() => expect(paperApi.dataStatus).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.dataRefresh" })).toBeEnabled());
  expect(screen.getByRole("button", { name: "paper.dataUpdating" })).toBeDisabled();
  expect(onBusy).toHaveBeenLastCalledWith(true);
  expect(paperApi.updateData).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "paper.dataRefresh" }));
  await screen.findByText(/paper.dataJob_updated/);
  expect(onBusy).toHaveBeenLastCalledWith(false);
  expect(paperApi.updateData).toHaveBeenCalledTimes(1);
});

test("refreshes a lost save response while preserving the draft and never repeating the write", async () => {
  vi.mocked(paperApi.saveWatchlist).mockRejectedValue(new Error("Response lost"));
  vi.mocked(paperApi.dataStatus).mockResolvedValueOnce(state).mockResolvedValue({ ...state, watchlist: ["000001.SZ"] });
  render(<PaperDataUpdate disabled={false} onBusyChange={vi.fn()} />);
  await ready();
  fireEvent.change(screen.getByLabelText("paper.dataWatchlist"), { target: { value: "000001.sz" } });
  fireEvent.click(screen.getByRole("button", { name: "paper.dataSave" }));
  await screen.findByText("Response lost");
  await waitFor(() => expect(paperApi.dataStatus).toHaveBeenCalledTimes(2));
  expect(screen.getByLabelText("paper.dataWatchlist")).toHaveValue("000001.sz");
  expect(paperApi.saveWatchlist).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("paper.dataWatchlistSaved")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "paper.dataSave" })).toBeDisabled();
});

test("aborts an initial read on unmount and ignores a late response", async () => {
  const pending = deferred<PaperDataStatus>();
  vi.mocked(paperApi.dataStatus).mockReturnValue(pending.promise);
  const onBusy = vi.fn();
  const view = render(<PaperDataUpdate disabled={false} onBusyChange={onBusy} />);
  const signal = vi.mocked(paperApi.dataStatus).mock.calls[0][0]!;
  view.unmount();
  expect(signal.aborted).toBe(true);
  const calls = onBusy.mock.calls.length;
  await act(async () => { pending.resolve(completed); });
  expect(onBusy).toHaveBeenCalledTimes(calls);
  expect(paperApi.updateData).not.toHaveBeenCalled();
});

test("ignores a late update response after unmount without changing the next component", async () => {
  const pending = deferred<PaperDataStatus>();
  vi.mocked(paperApi.updateData).mockReturnValue(pending.promise);
  const onBusy = vi.fn();
  const view = render(<PaperDataUpdate disabled={false} onBusyChange={onBusy} />);
  await ready();
  fireEvent.click(screen.getByRole("button", { name: "paper.dataUpdate" }));
  view.unmount();
  const calls = onBusy.mock.calls.length;
  await act(async () => { pending.resolve(completed); });
  expect(onBusy).toHaveBeenCalledTimes(calls);
  expect(paperApi.dataStatus).toHaveBeenCalledTimes(1);
});
