// Client-side downscaling for user-supplied artwork (avatars, chat background).
//
// Both are persisted as data URLs in localStorage, whose per-origin quota is
// ~5MB counted in UTF-16 code units: a 5MB upload becomes ~6.7M base64
// characters and overflows it, so the write throws QuotaExceededError and the
// preference silently fails to persist. Every stored copy is therefore a
// downscaled JPEG.
//
// Both surfaces render the stored image with CSS `background-size: cover`, so
// the full frame is kept rather than pre-cropped — the user's drag-to-crop
// offsets keep meaning exactly what they meant before the resize.

export const IMAGE_MAX_BYTES = 5 * 1024 * 1024;

export type ImageResizeErrorCode =
  | "invalid-type"
  | "too-large"
  | "decode-failed"
  | "canvas-unavailable";

export class ImageResizeError extends Error {
  readonly code: ImageResizeErrorCode;

  constructor(code: ImageResizeErrorCode, message?: string) {
    super(message ?? code);
    this.name = "ImageResizeError";
    this.code = code;
  }
}

export interface ResizeOptions {
  maxWidth: number;
  maxHeight: number;
  /** JPEG quality in 0..1. Defaults to 0.85. */
  quality?: number;
}

export interface ResizeTarget {
  maxWidth: number;
  maxHeight: number;
}

/** Avatars render at 32px and are cropped by CSS, so 256px is ample. */
export const AVATAR_RESIZE_TARGET: ResizeTarget = {
  maxWidth: 256,
  maxHeight: 256,
};

/** The chat background cap: viewport-sized, clamped so a 4K display cannot
 * produce a multi-megabyte data URL. */
export function backgroundResizeTarget(): ResizeTarget {
  const width = typeof window === "undefined" ? 1920 : window.innerWidth;
  const height = typeof window === "undefined" ? 1080 : window.innerHeight;
  return {
    maxWidth: Math.min(Math.max(width, 1), 1920),
    maxHeight: Math.min(Math.max(height, 1), 1080),
  };
}

/** Largest box with the source's aspect ratio that fits inside the cap.
 *
 * Never upscales: an image smaller than the cap keeps its own dimensions, so a
 * small upload is not inflated into a blurry one.
 */
export function fitWithinSize(
  srcWidth: number,
  srcHeight: number,
  maxWidth: number,
  maxHeight: number,
): { width: number; height: number } {
  if (srcWidth <= 0 || srcHeight <= 0) return { width: 0, height: 0 };
  const scale = Math.min(maxWidth / srcWidth, maxHeight / srcHeight, 1);
  return {
    width: Math.max(1, Math.round(srcWidth * scale)),
    height: Math.max(1, Math.round(srcHeight * scale)),
  };
}

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new ImageResizeError("decode-failed"));
    reader.readAsDataURL(file);
  });
}

function loadImage(dataUrl: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new ImageResizeError("decode-failed"));
    image.src = dataUrl;
  });
}

/** Downscale a picked file into a JPEG data URL small enough to persist.
 *
 * Throws :class:`ImageResizeError` — the caller maps ``code`` onto a user-facing
 * message rather than storing an oversized value that would fail the quota.
 */
export async function resizeImageFileToDataUrl(
  file: File,
  options: ResizeOptions,
): Promise<string> {
  if (!file.type.startsWith("image/")) {
    throw new ImageResizeError("invalid-type");
  }
  if (file.size > IMAGE_MAX_BYTES) {
    throw new ImageResizeError("too-large");
  }

  const image = await loadImage(await readAsDataUrl(file));
  const target = fitWithinSize(
    image.naturalWidth || image.width,
    image.naturalHeight || image.height,
    options.maxWidth,
    options.maxHeight,
  );
  if (target.width === 0 || target.height === 0) {
    throw new ImageResizeError("decode-failed");
  }

  const canvas = document.createElement("canvas");
  canvas.width = target.width;
  canvas.height = target.height;
  const context = canvas.getContext("2d");
  if (!context) {
    throw new ImageResizeError("canvas-unavailable");
  }
  context.drawImage(image, 0, 0, target.width, target.height);
  return canvas.toDataURL("image/jpeg", options.quality ?? 0.85);
}
