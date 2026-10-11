import { afterEach, describe, expect, it, vi } from "vitest";
import {
  compactStoredImage,
  MAX_IMAGE_STORAGE_CHARS,
  safeGet,
  writeImageValue,
} from "../storage";

const AVATAR_CAP = { maxWidth: 256, maxHeight: 256 };

/** jsdom never decodes an image, so ``onload`` has to be driven by hand. */
class FakeImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = 512;
  naturalHeight = 512;
  width = 512;
  height = 512;

  set src(_value: string) {
    queueMicrotask(() => this.onload?.());
  }
}

function quotaError(): DOMException {
  return new DOMException("quota exceeded", "QuotaExceededError");
}

describe("writeImageValue", () => {
  afterEach(() => {
    localStorage.clear();
  });

  it("writes directly when there is room", () => {
    const onEvict = vi.fn();

    expect(writeImageValue("qa-agent-avatar", "small", onEvict)).toBe(true);

    expect(safeGet("qa-agent-avatar")).toBe("small");
    expect(onEvict).not.toHaveBeenCalled();
  });

  it("evicts the sibling image blobs and retries when the origin is full", () => {
    localStorage.setItem("qa-user-avatar", "legacy-blob");
    localStorage.setItem("qa-background", "legacy-background");
    localStorage.setItem("i18nextLng", "zh-CN");
    const realSetItem = Storage.prototype.setItem;
    let rejectFirst = true;
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (
      this: Storage,
      key: string,
      value: string,
    ) {
      if (rejectFirst && key === "qa-agent-avatar") {
        rejectFirst = false;
        throw quotaError();
      }
      return realSetItem.call(this, key, value);
    });
    const onEvict = vi.fn();

    expect(writeImageValue("qa-agent-avatar", "small", onEvict)).toBe(true);

    expect(safeGet("qa-agent-avatar")).toBe("small");
    // Only the re-creatable image blobs are sacrificed...
    expect(safeGet("qa-user-avatar")).toBeNull();
    expect(safeGet("qa-background")).toBeNull();
    // ...and preferences are never touched.
    expect(safeGet("i18nextLng")).toBe("zh-CN");
    expect(onEvict).toHaveBeenCalledWith(["qa-user-avatar", "qa-background"]);
  });

  it("reports failure when the retry also fails", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw quotaError();
    });

    expect(writeImageValue("qa-agent-avatar", "x")).toBe(false);
  });
});

describe("compactStoredImage", () => {
  afterEach(() => {
    localStorage.clear();
    vi.unstubAllGlobals();
  });

  it("leaves a value that is already small alone", async () => {
    localStorage.setItem("qa-agent-avatar", "small");

    expect(await compactStoredImage("qa-agent-avatar", AVATAR_CAP)).toBe(false);

    expect(safeGet("qa-agent-avatar")).toBe("small");
  });

  it("drops an oversized value that holds no re-encodable image", async () => {
    localStorage.setItem("qa-user-avatar", "x".repeat(MAX_IMAGE_STORAGE_CHARS + 1));

    expect(await compactStoredImage("qa-user-avatar", AVATAR_CAP)).toBe(true);

    expect(safeGet("qa-user-avatar")).toBeNull();
  });

  it("re-encodes an oversized stored image down to the cap", async () => {
    const bigBlob = "data:image/jpeg;base64," + "A".repeat(MAX_IMAGE_STORAGE_CHARS);
    localStorage.setItem(
      "qa-user-avatar",
      JSON.stringify({ type: "image", imageDataUrl: bigBlob }),
    );
    vi.stubGlobal("Image", FakeImage);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage: vi.fn(),
    } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toDataURL").mockReturnValue(
      "data:image/jpeg;base64,SMALL",
    );

    expect(await compactStoredImage("qa-user-avatar", AVATAR_CAP)).toBe(true);

    const stored = safeGet("qa-user-avatar") ?? "";
    expect(stored).toContain("data:image/jpeg;base64,SMALL");
    expect(stored.length).toBeLessThan(bigBlob.length);
  });
});
