import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { PaperDataCheck } from "../PaperDataCheck";
import { paperApi, type PaperPreflight } from "@/lib/api";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("@/lib/api", () => ({ paperApi: { preflight: vi.fn() } }));
const report: PaperPreflight = {
  market: "cn_a", from_date: "2026-10-26", to_date: "2026-10-28", symbols: ["600522.SH"], ready: false, status: "blocked",
  checks: [{ key: "calendar", status: "blocked", message: "Calendar is incomplete", missing_dates: ["2026-10-27"] }],
  bars: [{ symbol: "600522.SH", as_of: "2026-10-28", missing_dates: [], invalid_dates: [] }],
  provenance: { source: "tencent", bars_as_of: "2026-10-28" }, warnings: ["No ledger change"],
};
beforeEach(() => vi.resetAllMocks());
function fill() {
  fireEvent.click(screen.getByText("paper.preflightTitle"));
  fireEvent.change(screen.getByLabelText("paper.preflightSymbols"), { target: { value: "600522.sh，600522.SH" } });
  fireEvent.change(screen.getByLabelText("paper.preflightFrom"), { target: { value: "2026-10-26" } });
  fireEvent.change(screen.getByLabelText("paper.preflightTo"), { target: { value: "2026-10-28" } });
}
test("checks normalized symbols only on request and displays blockers with missing dates", async () => {
  vi.mocked(paperApi.preflight).mockResolvedValue(report);
  render(<PaperDataCheck market="cn_a" disabled={false} />);
  fill();
  expect(paperApi.preflight).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "paper.preflightRun" }));
  await waitFor(() => expect(paperApi.preflight).toHaveBeenCalledWith({ market: "cn_a", from_date: "2026-10-26", to_date: "2026-10-28", symbols: ["600522.SH"] }));
  expect(await screen.findByRole("status")).toHaveTextContent("paper.preflightStatus_blocked");
  expect(screen.getByText(/2026-10-27/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("paper.preflightSymbols"), { target: { value: "000001.SZ" } });
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
test("locks fields during a check and discards a result from a previous market/account", async () => {
  let resolve!: (value: PaperPreflight) => void;
  vi.mocked(paperApi.preflight).mockReturnValue(new Promise((done) => { resolve = done; }));
  const view = render(<PaperDataCheck key="first" market="cn_a" disabled={false} />);
  fill();
  fireEvent.click(screen.getByRole("button", { name: "paper.preflightRun" }));
  expect(screen.getByLabelText("paper.preflightSymbols")).toBeDisabled();
  view.rerender(<PaperDataCheck key="second" market="us" disabled={false} />);
  resolve(report);
  await waitFor(() => expect(screen.queryByText("Calendar is incomplete")).not.toBeInTheDocument());
});
test("shows a failed check without retrying or claiming readiness", async () => {
  vi.mocked(paperApi.preflight).mockRejectedValue(new Error("Check timed out"));
  render(<PaperDataCheck market="cn_a" disabled={false} />);
  fill();
  fireEvent.click(screen.getByRole("button", { name: "paper.preflightRun" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Check timed out");
  expect(paperApi.preflight).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
