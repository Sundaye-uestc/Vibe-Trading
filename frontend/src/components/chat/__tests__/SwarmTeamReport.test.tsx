import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { api } from "@/lib/api";
import i18n from "@/i18n";
import { SwarmStatusCard } from "../SwarmStatusCard";
import type { SwarmRunStatus } from "@/types/agent";

const team: SwarmRunStatus = {
  runId: "swarm-example", preset: "investment_committee", status: "completed",
  startedAt: 1, totalLayers: 0, currentLayer: 0, agents: [],
};

afterEach(() => vi.restoreAllMocks());

it("reads the saved report for this run on demand and reuses it when reopened", async () => {
  await i18n.changeLanguage("en");
  const getReport = vi.spyOn(api, "getSwarmRun").mockResolvedValue({ final_report: "# Original research" });
  const user = userEvent.setup();
  render(<SwarmStatusCard status={team} />);
  const toggle = screen.getByText("View team draft");
  expect(getReport).not.toHaveBeenCalled();
  await user.click(toggle);
  expect(await screen.findByRole("heading", { name: "Original research" })).toBeVisible();
  expect(getReport).toHaveBeenCalledWith(team.runId);
  await user.click(toggle);
  await user.click(toggle);
  expect(getReport).toHaveBeenCalledTimes(1);
});

it("retries a failed read and distinguishes an empty report", async () => {
  await i18n.changeLanguage("en");
  const getReport = vi.spyOn(api, "getSwarmRun").mockRejectedValueOnce(new Error("network"))
    .mockResolvedValueOnce({ final_report: "" });
  const user = userEvent.setup();
  render(<SwarmStatusCard status={team} />);
  await user.click(screen.getByText("View team draft"));
  await user.click(await screen.findByRole("button", { name: "Couldn’t load the draft. Retry" }));
  await waitFor(() => expect(screen.getByText("The team did not save a draft.")).toBeVisible());
  expect(getReport).toHaveBeenCalledTimes(2);
});
