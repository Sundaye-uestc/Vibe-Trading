import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { Layout } from "../Layout";
import { api } from "@/lib/api";

const sessions = [
  {
    session_id: "session-1",
    title: "A very long session title that must truncate",
  },
];

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => ({
      "app.version": "v0.1.15",
      "layout.agent": "Agent",
      "layout.alphaZoo": "Alpha Zoo",
      "layout.cancel": "Cancel",
      "layout.collapse": "Collapse",
      "layout.confirm": "Confirm",
      "layout.correlation": "Correlation Matrix",
      "layout.dark": "Dark",
      "layout.delete": "Delete",
      "layout.expand": "Expand",
      "layout.home": "Home",
      "layout.language": "Language",
      "layout.light": "Light",
      "layout.mainNavigation": "Main navigation",
      "layout.newChat": "New Chat",
      "layout.openNavigation": "Open navigation",
      "layout.closeNavigation": "Close navigation",
      "layout.noSessions": "No sessions yet",
      "layout.rename": "Rename",
      "layout.reports": "Reports",
      "layout.runtime": "Runtime",
      "layout.sessions": "Sessions",
      "layout.settings": "Settings",
      "layout.sidebar": "Vibe-Trading sidebar",
      "layout.skipToMain": "Skip to main content",
    })[key] ?? key,
    i18n: {
      language: "en",
      languages: ["en"],
      changeLanguage: vi.fn().mockResolvedValue(undefined),
    },
  }),
}));

vi.mock("@/hooks/useDarkMode", () => ({
  useDarkMode: () => ({ dark: false, toggle: vi.fn() }),
}));

vi.mock("@/lib/api", () => ({
  api: {
    listSessions: vi.fn().mockResolvedValue([
      {
        session_id: "session-1",
        title: "A very long session title that must truncate",
      },
    ]),
    deleteSession: vi.fn().mockResolvedValue(undefined),
    renameSession: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock("@/stores/agent", () => ({
  useAgentStore: (selector: (state: {
    sseStatus: string;
    sseRetryAttempt: number;
    streamingSessionId: null;
  }) => unknown) => selector({
    sseStatus: "connected",
    sseRetryAttempt: 0,
    streamingSessionId: null,
  }),
}));

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/agent"]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/agent" element={<div>Agent content</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout accessibility", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    document.documentElement.dir = "ltr";
  });
  it("labels landmarks, brand, main content, and the new-chat affordance", () => {
    renderLayout();

    expect(screen.getByRole("complementary", { name: "Vibe-Trading sidebar" })).toHaveStyle({ width: "232px" });
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Vibe-Trading" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New Chat" })).toHaveAttribute("title", "New Chat");
    expect(screen.getByText("Skip to main content")).toHaveAttribute("href", "#main");
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
    expect(screen.getByRole("main").parentElement).toHaveClass("relative");
    // Regression: <main> is a flex item inside a flex-col/overflow-hidden
    // parent. Without min-h-0, a flex item's default min-height:auto lets it
    // grow past its allotted space to fit tall content (e.g. a long
    // generated report) instead of respecting its own overflow-auto -- the
    // excess then gets hard-clipped by the parent's overflow-hidden with no
    // scrollbar at all, rather than scrolling into view.
    expect(screen.getByRole("main")).toHaveClass("flex-1", "min-h-0", "overflow-auto");
  });

  it("exposes session actions on keyboard focus and labels the rename input", async () => {
    renderLayout();

    const title = await screen.findByText(sessions[0].title);
    expect(title).toHaveClass("min-w-0", "truncate");

    const renameButton = screen.getByRole("button", { name: "Rename" });
    expect(renameButton.parentElement).toHaveClass("group-focus-within:opacity-100");
    fireEvent.click(renameButton);

    expect(screen.getByRole("textbox", { name: `Rename: ${sessions[0].title}` })).toHaveClass(
      "focus:ring-2",
      "focus:ring-primary/40",
    );
  });

  it("does not crash when localStorage access is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("Blocked", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("Blocked", "SecurityError");
    });

    expect(() => renderLayout()).not.toThrow();
  });

  it("uses button disclosure semantics for the language switcher", () => {
    renderLayout();

    const languageButton = screen.getByRole("button", { name: "Language" });
    expect(languageButton).toHaveAttribute("aria-expanded", "false");
    expect(languageButton).not.toHaveAttribute("aria-haspopup");
  });

  it("synchronizes the sidebar width from another tab", () => {
    window.localStorage.setItem("qa-sidebar-width", "320");
    renderLayout();
    const sidebar = screen.getByRole("complementary", { name: "Vibe-Trading sidebar" });
    expect(sidebar).toHaveStyle({ width: "320px" });

    window.localStorage.setItem("qa-sidebar-width", "200");
    fireEvent(window, new StorageEvent("storage", { key: "qa-sidebar-width" }));

    expect(sidebar).toHaveStyle({ width: "200px" });
  });

  it("ignores a stored width outside the draggable range", () => {
    window.localStorage.setItem("qa-sidebar-width", "9999");
    renderLayout();

    expect(screen.getByRole("complementary", { name: "Vibe-Trading sidebar" }))
      .toHaveStyle({ width: "232px" });
  });

  it("uses a mobile drawer without inheriting a saved desktop width and restores focus on Escape", async () => {
    const original = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({ ...original(query), matches: query === "(max-width: 767px)" }));
    localStorage.setItem("qa-sidebar-width", "400");
    renderLayout();
    const open = screen.getByRole("button", { name: "Open navigation" });
    expect(screen.queryByRole("navigation", { name: "Main navigation" })).not.toBeInTheDocument();
    fireEvent.click(open);
    const drawer = screen.getByRole("dialog", { name: "Vibe-Trading sidebar" });
    expect(drawer.style.width).toBe("");
    expect(screen.getByRole("main").parentElement?.inert).toBe(true);
    const close = within(drawer).getByRole("button", { name: "Close navigation" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByRole("main").parentElement?.inert).toBe(false);
    expect(open).toHaveFocus();
    await act(async () => {});
  });

  it("keeps deletion behind a confirmation dialog and returns focus when cancelled", async () => {
    renderLayout();
    const remove = await screen.findByRole("button", { name: "Delete" });
    remove.focus();
    fireEvent.click(remove);
    const dialog = screen.getByRole("alertdialog", { name: "Delete" });
    expect(dialog).toHaveTextContent(sessions[0].title);
    expect(api.deleteSession).not.toHaveBeenCalled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(remove).toHaveFocus();
    expect(api.deleteSession).not.toHaveBeenCalled();
  });

  it.each(["ltr", "rtl"])("anchors the %s language disclosure to its trigger and closes it before the drawer", async (direction) => {
    document.documentElement.dir = direction;
    const original = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({ ...original(query), matches: query === "(max-width: 767px)" }));
    renderLayout();
    fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
    const language = screen.getByRole("button", { name: "Language" });
    fireEvent.click(language);
    expect(language).toHaveAttribute("aria-expanded", "true");
    // A transformed glass drawer is a containing block: viewport-derived
    // coordinates must not be reapplied relative to its nonzero RTL origin.
    const menu = screen.getByRole("list", { name: "Select language" });
    expect(menu.parentElement).toHaveStyle({ position: "relative" });
    expect(menu).toHaveStyle({ position: "absolute", insetInlineStart: "0", width: "160px", maxHeight: "60dvh", overflowY: "auto" });
    expect(menu.style.left).toBe("");
    fireEvent.keyDown(language, { key: "Escape" });
    expect(language).toHaveAttribute("aria-expanded", "false");
    expect(language).toHaveFocus();
    expect(screen.getByRole("dialog", { name: "Vibe-Trading sidebar" })).toBeInTheDocument();
    fireEvent.keyDown(language, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => {});
  });

  it("cancels a rename draft on Escape without saving it", async () => {
    renderLayout();
    const rename = await screen.findByRole("button", { name: "Rename" });
    rename.focus();
    fireEvent.click(rename);
    const input = screen.getByRole("textbox", { name: `Rename: ${sessions[0].title}` });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: "Uncommitted draft" } });
    fireEvent.keyDown(input, { key: "Escape" });
    expect(api.renameSession).not.toHaveBeenCalled();
    expect(rename).toHaveFocus();
  });

  it("saves a rename only after submitting the dialog", async () => {
    renderLayout();
    fireEvent.click(await screen.findByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: `Rename: ${sessions[0].title}` });
    fireEvent.change(input, { target: { value: "New research" } });
    fireEvent.click(within(screen.getByRole("dialog", { name: "Rename" })).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(api.renameSession).toHaveBeenCalledWith("session-1", "New research"));
    expect(await screen.findByText("New research")).toBeInTheDocument();
  });
});
