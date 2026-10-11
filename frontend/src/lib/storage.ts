// Safe localStorage access. Storage APIs THROW (SecurityError/QuotaExceededError)
// in restricted contexts — blocked third-party iframes, WebViews with DOM storage
// off, zero-quota private modes. Only the index.html boot script guards itself;
// every in-app access must go through these wrappers so a locked-down browser
// degrades to in-memory defaults instead of white-screening the app.

export function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function safeSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* storage unavailable — preference simply won't persist */
  }
}

export function safeRemove(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/** Keys holding re-creatable image blobs — the only large values this app writes.
 *
 * Each one is something the user can set again in two clicks, which is what
 * makes them the right thing to sacrifice when the origin runs out of room.
 */
export const IMAGE_STORAGE_KEYS = [
  "qa-agent-avatar",
  "qa-user-avatar",
  "qa-background",
] as const;

/** A downscaled image is tens of KB. Anything past this came from a code path
 * that stored the user's original file, and such a blob can hold the entire
 * origin quota by itself. */
export const MAX_IMAGE_STORAGE_CHARS = 512 * 1024;

/** Write an image value, evicting the other image blobs if the origin is full.
 *
 * A single legacy blob can consume the whole quota, after which *every* write
 * fails however small it is — the state where changing an avatar reports
 * "storage full" for a 30KB payload. Evicting only the sibling image keys and
 * retrying once breaks that deadlock; the caller is told what was dropped so it
 * can say so instead of silently discarding a picture.
 *
 * @returns true when the value landed.
 */
export function writeImageValue(
  key: string,
  value: string,
  onEvict?: (evicted: readonly string[]) => void,
): boolean {
  try {
    localStorage.setItem(key, value);
    return true;
  } catch {
    /* fall through: almost certainly QuotaExceededError */
  }

  const evicted: string[] = [];
  for (const other of IMAGE_STORAGE_KEYS) {
    if (other === key || safeGet(other) === null) continue;
    safeRemove(other);
    evicted.push(other);
  }
  try {
    localStorage.setItem(key, value);
  } catch {
    return false;
  }
  if (evicted.length > 0) onEvict?.(evicted);
  return true;
}

/** Re-encode a stored image that predates downscaling.
 *
 * Self-heals a wedged origin on load rather than leaving the app permanently
 * unable to save. Returns true when the stored value was changed or dropped.
 */
export async function compactStoredImage(
  key: string,
  target: { maxWidth: number; maxHeight: number },
): Promise<boolean> {
  const value = safeGet(key);
  if (value === null || value.length <= MAX_IMAGE_STORAGE_CHARS) return false;

  // The blob sits inside a JSON avatar config as often as it is the raw value.
  const match = value.match(/data:image\/[^;]+;base64,[A-Za-z0-9+/=]+/);
  if (!match) {
    // Nothing re-encodable: such a value can only wedge storage, so drop it.
    safeRemove(key);
    return true;
  }
  try {
    const { resizeDataUrlToDataUrl } = await import("./imageResize");
    const compacted = await resizeDataUrlToDataUrl(match[0], target);
    safeSet(key, value.replace(match[0], compacted));
  } catch {
    // Undecodable legacy blob: same reasoning as above.
    safeRemove(key);
  }
  return true;
}
