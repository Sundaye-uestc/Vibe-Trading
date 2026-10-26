import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChannelConfigPanel } from "../ChannelConfigPanel";
import { type ChannelConfigEntry, type ChannelConfigResponse } from "@/lib/api";
import i18n from "@/i18n";

const apiMock = vi.hoisted(() => ({
  getChannelConfig: vi.fn(),
  updateChannelConfig: vi.fn(),
  testChannelConfig: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ api: apiMock }));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

function channel(): ChannelConfigEntry {
  return {
    display_name: "QQ", available: true, configured: true, enabled: false,
    loaded: false, running: false, supports_test: true,
    fields: [
      { key: "app_id", type: "text", secret: false, required: true },
      { key: "secret", type: "password", secret: true, required: true },
      { key: "download_chunk_size", type: "number", secret: false, required: false },
      { key: "allow_from", type: "list", secret: false, required: false },
    ],
    values: { enabled: false, app_id: "saved-id", download_chunk_size: 1024, allow_from: [] },
    secrets: { secret: { set: true, masked: "****1234" } },
  };
}

describe("channel configuration drafts", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    await i18n.changeLanguage("en");
    const response: ChannelConfigResponse = {
      config_path: "agent.json", writable: true, runtime_running: false,
      channels: { qq: channel() },
    };
    apiMock.getChannelConfig.mockResolvedValue(response);
    apiMock.testChannelConfig.mockResolvedValue({ ok: true });
    apiMock.updateChannelConfig.mockImplementation(async (_name, payload) => {
      const { secret: _secret, ...values } = payload;
      return { channel: { ...channel(), values }, applied: "saved" };
    });
  });

  it("keeps every typed credential character for testing and saving, then clears the draft", async () => {
    const user = userEvent.setup();
    render(<ChannelConfigPanel />);
    const input = await screen.findByLabelText(/^Qq · secret/);

    await user.type(input, "demo-secret-123");
    expect(input).toHaveValue("demo-secret-123");
    await user.click(screen.getByRole("button", { name: "Test connection" }));
    expect(apiMock.testChannelConfig).toHaveBeenCalledWith("qq", expect.objectContaining({ secret: "demo-secret-123" }));
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.updateChannelConfig).toHaveBeenCalledWith("qq", expect.objectContaining({ secret: "demo-secret-123" }));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(input).toHaveAttribute("placeholder", "•••••••• (unchanged)");
  });

  it("omits an untouched or cleared credential instead of overwriting the saved value", async () => {
    const user = userEvent.setup();
    render(<ChannelConfigPanel />);
    const input = await screen.findByLabelText(/^Qq · secret/);

    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.updateChannelConfig.mock.calls[0][1]).not.toHaveProperty("secret");
    await user.type(input, "discard-this-draft");
    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.updateChannelConfig.mock.calls[1][1]).not.toHaveProperty("secret");
  });

  it("preserves leading zeros in identifiers and converts only numeric fields", async () => {
    const user = userEvent.setup();
    render(<ChannelConfigPanel />);
    const identifier = await screen.findByLabelText(/^Qq · app id/);
    const chunkSize = screen.getByRole("spinbutton", { name: "Qq · download chunk size" });

    await user.clear(identifier);
    await user.type(identifier, "00123456789");
    await user.clear(chunkSize);
    await user.type(chunkSize, "4096");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.updateChannelConfig).toHaveBeenCalledWith("qq", expect.objectContaining({
      app_id: "00123456789", download_chunk_size: 4096,
    }));
  });

  it("keeps list separators while typing and sends complete list entries", async () => {
    const user = userEvent.setup();
    render(<ChannelConfigPanel />);
    const input = await screen.findByLabelText("Qq · allow from");

    await user.type(input, "00123, 00456");
    expect(input).toHaveValue("00123, 00456");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.updateChannelConfig).toHaveBeenCalledWith("qq", expect.objectContaining({
      allow_from: ["00123", "00456"],
    }));
  });

  it("locks channel switching, refresh and edits until an in-flight save finishes", async () => {
    const user = userEvent.setup();
    const email: ChannelConfigEntry = {
      ...channel(), display_name: "Email",
      fields: [{ key: "smtp_host", type: "text", secret: false, required: true }],
      values: { enabled: false, smtp_host: "smtp.example" }, secrets: {},
    };
    apiMock.getChannelConfig.mockResolvedValueOnce({
      config_path: "agent.json", writable: true, runtime_running: false,
      channels: { qq: channel(), email },
    });
    let finishSave!: (result: { channel: ChannelConfigEntry; applied: string }) => void;
    apiMock.updateChannelConfig.mockReturnValueOnce(new Promise((resolve) => { finishSave = resolve; }));
    render(<ChannelConfigPanel />);
    const identifier = await screen.findByLabelText(/^Qq · app id/);

    await user.click(screen.getByRole("button", { name: "Save" }));
    const switchChannel = screen.getByRole("button", { name: /^Email\s*email$/ });
    expect(switchChannel).toBeDisabled();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
    expect(identifier).toBeDisabled();
    await user.click(switchChannel);
    await user.type(identifier, "should-not-be-saved");
    expect(identifier).toHaveValue("saved-id");

    await act(async () => { finishSave({ channel: channel(), applied: "saved" }); });
    expect(switchChannel).toBeEnabled();
    await user.click(switchChannel);
    expect(screen.getByLabelText(/^Email · smtp host/)).toHaveValue("smtp.example");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(apiMock.updateChannelConfig.mock.calls[1]).toEqual(["email", email.values]);
  });
});
