import { render } from "@testing-library/react";
import { AgentAvatar, getAgentAvatarConfig } from "../AgentAvatar";

// This avatar is user-configurable (Settings → 更换头像): a letter over a
// gradient by default, or an uploaded image cropped by drag position. The
// upstream project replaced it with the static BrandMark logo, so these
// assertions describe this checkout's behaviour instead.
describe("AgentAvatar", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("renders the default letter avatar as a 32px rounded square", () => {
    const { container } = render(<AgentAvatar />);
    const wrapper = container.firstElementChild;

    expect(wrapper).toHaveClass("h-8", "w-8", "rounded-lg", "shrink-0");
    expect(wrapper).toHaveTextContent("P");
    expect(container.querySelector("svg")).toBeNull();
  });

  it("renders a stored letter and gradient", () => {
    localStorage.setItem(
      "qa-agent-avatar",
      JSON.stringify({ type: "letter", letter: "A", gradient: "from-red-500 to-blue-500" }),
    );
    const { container } = render(<AgentAvatar />);
    const wrapper = container.firstElementChild;

    expect(wrapper).toHaveTextContent("A");
    expect(wrapper?.className).toContain("from-red-500");
  });

  it("renders a stored image with its cropped offset", () => {
    localStorage.setItem(
      "qa-agent-avatar",
      JSON.stringify({
        type: "image",
        imageDataUrl: "data:image/png;base64,AAAA",
        imagePosition: { x: 30, y: 70 },
      }),
    );
    const { container } = render(<AgentAvatar />);
    const wrapper = container.firstElementChild as HTMLElement;

    expect(wrapper.style.backgroundImage).toContain("data:image/png;base64,AAAA");
    expect(wrapper.style.backgroundPosition).toBe("30% 70%");
  });

  it("falls back to the default when the stored config is unreadable", () => {
    localStorage.setItem("qa-agent-avatar", "{not json");

    expect(getAgentAvatarConfig().letter).toBe("P");
  });
});
