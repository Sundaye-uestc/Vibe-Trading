import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PaperTrading } from "../PaperTrading";
import { paperApi } from "@/lib/api";
import userEvent from "@testing-library/user-event";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("@/lib/api", () => ({ paperApi: { list: vi.fn(), create: vi.fn(), advance: vi.fn(), history: vi.fn(), preflight: vi.fn() } }));
const account = { id: "paper-1", label: "Example", market: "us", currency: "USD", start_on: "2026-10-20", last_date: null, revision: 7, account_state: { cash: 1000, positions: [] }, last_result: null } as const;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(paperApi.list).mockResolvedValue([{ ...account }]);
  vi.mocked(paperApi.history).mockResolvedValue([]);
});

test("reuses an uncertain create request and reconciles an already-created account without duplicates", async () => {
  vi.mocked(paperApi.list).mockResolvedValueOnce([]).mockResolvedValue([{ ...account }]);
  vi.mocked(paperApi.create).mockRejectedValueOnce(new Error("response lost")).mockResolvedValueOnce({ ...account });
  render(<PaperTrading />);
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.create" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText("paper.label"), { target: { value: "Example" } });
  fireEvent.click(screen.getByRole("button", { name: "paper.create" }));
  await screen.findByText("response lost");
  await waitFor(() => expect(paperApi.list).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.create" })).toBeEnabled());
  const first = vi.mocked(paperApi.create).mock.calls[0][0];
  expect(first.idempotency_key).toBeTruthy();
  expect(paperApi.create).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole("button", { name: "paper.create" }));
  await waitFor(() => expect(paperApi.create).toHaveBeenCalledTimes(2));
  expect(vi.mocked(paperApi.create).mock.calls[1][0]).toEqual(first);
  await screen.findByRole("button", { name: "paper.advance" });
  expect(screen.getAllByRole("option", { name: "Example · USD" })).toHaveLength(1);
});

test("assigns a new create key for a changed draft but preserves the uncertain original draft key", async () => {
  vi.mocked(paperApi.list).mockResolvedValue([]);
  vi.mocked(paperApi.create).mockRejectedValue(new Error("response lost"));
  render(<PaperTrading />);
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.create" })).toBeEnabled());
  for (const [index, name] of ["Original", "Edited", "Original"].entries()) {
    fireEvent.change(screen.getByLabelText("paper.label"), { target: { value: name } });
    fireEvent.click(screen.getByRole("button", { name: "paper.create" }));
    await waitFor(() => expect(paperApi.create).toHaveBeenCalledTimes(index + 1));
    await waitFor(() => expect(screen.getByRole("button", { name: "paper.create" })).toBeEnabled());
  }
  const keys = vi.mocked(paperApi.create).mock.calls.map(([body]) => body.idempotency_key);
  expect(keys[0]).not.toBe(keys[1]);
  expect(keys[0]).toBe(keys[2]);
});

test("sends the loaded revision and shows an in-band failure without claiming a saved ledger", async () => {
  vi.mocked(paperApi.advance).mockResolvedValue({ account: { ...account }, committed: false, result: { code: "PAPER_DATA_NOT_READY", message: "Await daily data" } });
  render(<PaperTrading />);
  await screen.findByRole("option", { name: "Example · USD" });
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.create" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText("paper.accounts"), { target: { value: "paper-1" } });
  fireEvent.change(await screen.findByLabelText("paper.date"), { target: { value: "2026-10-20" } });
  fireEvent.click(screen.getByRole("button", { name: "paper.advance" }));
  await waitFor(() => expect(paperApi.advance).toHaveBeenCalledWith("paper-1", { expected_revision: 7, to_date: "2026-10-20", orders: [] }));
  expect(await screen.findByRole("alert")).toHaveTextContent("PAPER_DATA_NOT_READY");
  expect(screen.getByRole("status")).toHaveTextContent("paper.notSaved");
  expect(screen.queryByText("paper.saved")).not.toBeInTheDocument();
});

test("preserves configurable initial ledger and next-session selection", async () => {
  vi.mocked(paperApi.create).mockResolvedValue({ ...account });
  render(<PaperTrading />);
  await screen.findByRole("option", { name: "Example · USD" });
  await waitFor(() => expect(screen.getByRole("button", { name: "paper.create" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText("paper.label"), { target: { value: "My simulated account" } });
  fireEvent.change(screen.getByLabelText("paper.start"), { target: { value: "next_session" } });
  fireEvent.change(screen.getByLabelText(/paper.cash/), { target: { value: "2500" } });
  fireEvent.click(screen.getByRole("button", { name: "paper.addPosition" }));
  fireEvent.change(screen.getByLabelText("paper.symbol"), { target: { value: "AAPL.US" } });
  fireEvent.change(screen.getByLabelText("paper.quantity"), { target: { value: "5" } });
  fireEvent.change(screen.getByLabelText("paper.cost"), { target: { value: "100" } });
  fireEvent.click(screen.getByRole("button", { name: "paper.create" }));
  await waitFor(() => expect(paperApi.create).toHaveBeenCalledWith({ idempotency_key: expect.any(String), label: "My simulated account", market: "us", start: "next_session", account_state: { cash: 2500, positions: [{ symbol: "AAPL.US", qty: 5, avg_cost: 100 }] } }));
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

test("locks account switching while settling and uses the new revision on the next request", async () => {
  const user = userEvent.setup();
  const pending = deferred<Awaited<ReturnType<typeof paperApi.advance>>>();
  vi.mocked(paperApi.advance).mockReturnValueOnce(pending.promise);
  render(<PaperTrading />);
  await screen.findByRole("option", { name: "Example · USD" });
  await user.selectOptions(screen.getByLabelText("paper.accounts"), "paper-1");
  fireEvent.change(await screen.findByLabelText("paper.date"), { target: { value: "2026-10-20" } });
  await user.click(screen.getByRole("button", { name: "paper.advance" }));
  expect(screen.getByLabelText("paper.accounts")).toBeDisabled();
  expect(screen.getByLabelText("paper.date")).toBeDisabled();
  pending.resolve({ account: { ...account, revision: 8, last_date: "2026-10-20" }, result: {}, committed: true });
  await screen.findByText("paper.saved");
  await waitFor(() => expect(screen.getByLabelText("paper.date")).toBeEnabled());
  vi.mocked(paperApi.advance).mockResolvedValueOnce({ account: { ...account, revision: 9 }, result: {}, committed: true });
  fireEvent.change(screen.getByLabelText("paper.date"), { target: { value: "2026-10-21" } });
  await user.click(screen.getByRole("button", { name: "paper.advance" }));
  await waitFor(() => expect(paperApi.advance).toHaveBeenLastCalledWith("paper-1", {
    expected_revision: 8, to_date: "2026-10-21", orders: [],
  }));
});

test("reloads ledger and history after an uncertain mutation without automatically repeating it", async () => {
  const user = userEvent.setup();
  render(<PaperTrading />);
  await screen.findByRole("option", { name: "Example · USD" });
  await user.selectOptions(screen.getByLabelText("paper.accounts"), "paper-1");
  const date = await screen.findByLabelText("paper.date");
  vi.mocked(paperApi.list).mockResolvedValue([{ ...account, revision: 8, account_state: { cash: 2222, positions: [] } }]);
  vi.mocked(paperApi.advance).mockRejectedValueOnce(new Error("request timed out"));
  fireEvent.change(date, { target: { value: "2026-10-20" } });
  await user.click(screen.getByRole("button", { name: "paper.advance" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("request timed out");
  await waitFor(() => expect(paperApi.list).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.getByLabelText("paper.date")).toBeEnabled());
  expect(paperApi.advance).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("paper.saved")).not.toBeInTheDocument();
  vi.mocked(paperApi.advance).mockResolvedValueOnce({ account: { ...account, revision: 9 }, result: {}, committed: true });
  await user.click(screen.getByRole("button", { name: "paper.advance" }));
  await waitFor(() => expect(paperApi.advance).toHaveBeenLastCalledWith("paper-1", {
    expected_revision: 8, to_date: "2026-10-20", orders: [],
  }));
});

test("shows the A-share data prerequisite and clears strategy drafts when changing accounts", async () => {
  const user = userEvent.setup();
  vi.mocked(paperApi.list).mockResolvedValue([{ ...account }, { ...account, id: "paper-2", label: "A-share", market: "cn_a", currency: "CNY" }]);
  render(<PaperTrading />);
  await screen.findByRole("option", { name: "Example · USD" });
  await user.selectOptions(screen.getByLabelText("paper.accounts"), "paper-1");
  await user.selectOptions(await screen.findByLabelText("paper.mode"), "strategy");
  await user.type(screen.getByLabelText("paper.strategyPath"), "old_strategy.py");
  await user.selectOptions(screen.getByLabelText("paper.accounts"), "paper-2");
  expect(screen.getByText("paper.dataRequirements")).toBeInTheDocument();
  expect(screen.getByLabelText("paper.mode")).toHaveValue("orders");
  await user.selectOptions(screen.getByLabelText("paper.mode"), "strategy");
  expect(screen.getByLabelText("paper.strategyPath")).toHaveValue("");
});
