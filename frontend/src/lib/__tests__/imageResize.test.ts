import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AVATAR_RESIZE_TARGET,
  backgroundResizeTarget,
  fitWithinSize,
  IMAGE_MAX_BYTES,
  ImageResizeError,
  resizeImageFileToDataUrl,
} from "../imageResize";

/** jsdom never decodes an image, so ``onload`` has to be driven by hand. */
class FakeImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = 512;
  naturalHeight = 256;
  width = 512;
  height = 256;

  set src(_value: string) {
    queueMicrotask(() => this.onload?.());
  }
}

function imageFile(bytes = 16, type = "image/png"): File {
  return new File([new Uint8Array(bytes)], "art", { type });
}

describe("fitWithinSize", () => {
  it("downscales a landscape image onto the cap's width", () => {
    expect(fitWithinSize(1024, 512, 256, 256)).toEqual({ width: 256, height: 128 });
  });

  it("downscales a portrait image onto the cap's height", () => {
    expect(fitWithinSize(512, 1024, 256, 256)).toEqual({ width: 128, height: 256 });
  });

  it("never upscales an image smaller than the cap", () => {
    expect(fitWithinSize(100, 50, 256, 256)).toEqual({ width: 100, height: 50 });
  });

  it("fits a 4K background inside the clamped viewport cap", () => {
    expect(fitWithinSize(4000, 3000, 1920, 1080)).toEqual({
      width: 1440,
      height: 1080,
    });
  });

  it("reports zero for a source with no dimensions", () => {
    expect(fitWithinSize(0, 0, 256, 256)).toEqual({ width: 0, height: 0 });
  });
});

describe("backgroundResizeTarget", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("uses the viewport size", () => {
    vi.stubGlobal("innerWidth", 1024);
    vi.stubGlobal("innerHeight", 768);
    expect(backgroundResizeTarget()).toEqual({ maxWidth: 1024, maxHeight: 768 });
  });

  it("clamps a 4K viewport so the stored data URL stays small", () => {
    vi.stubGlobal("innerWidth", 3840);
    vi.stubGlobal("innerHeight", 2160);
    expect(backgroundResizeTarget()).toEqual({ maxWidth: 1920, maxHeight: 1080 });
  });
});

describe("resizeImageFileToDataUrl", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("rejects a non-image file before reading it", async () => {
    await expect(
      resizeImageFileToDataUrl(imageFile(16, "text/plain"), AVATAR_RESIZE_TARGET),
    ).rejects.toMatchObject({ code: "invalid-type" });
  });

  it("rejects a file over the size limit", async () => {
    const file = imageFile();
    Object.defineProperty(file, "size", { value: IMAGE_MAX_BYTES + 1 });

    await expect(
      resizeImageFileToDataUrl(file, AVATAR_RESIZE_TARGET),
    ).rejects.toMatchObject({ code: "too-large" });
  });

  it("reports a browser without canvas support instead of storing the original", async () => {
    vi.stubGlobal("Image", FakeImage);
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);

    const error = await resizeImageFileToDataUrl(
      imageFile(),
      AVATAR_RESIZE_TARGET,
    ).catch((err: unknown) => err);

    expect(error).toBeInstanceOf(ImageResizeError);
    expect((error as ImageResizeError).code).toBe("canvas-unavailable");
  });

  it("draws onto the fitted box and exports a JPEG", async () => {
    vi.stubGlobal("Image", FakeImage);
    const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      drawImage,
    } as unknown as CanvasRenderingContext2D);
    const toDataUrl = vi
      .spyOn(HTMLCanvasElement.prototype, "toDataURL")
      .mockReturnValue("data:image/jpeg;base64,STUB");

    const dataUrl = await resizeImageFileToDataUrl(
      imageFile(),
      AVATAR_RESIZE_TARGET,
    );

    expect(dataUrl).toBe("data:image/jpeg;base64,STUB");
    // 512x256 fitted into 256x256 — the stored frame keeps the aspect ratio.
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 256, 128);
    expect(toDataUrl).toHaveBeenCalledWith("image/jpeg", 0.85);
  });
});
