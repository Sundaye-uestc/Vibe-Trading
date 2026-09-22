const STORAGE_KEY = "qa-background";

export function getStoredBackground(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** Persist the chat background.
 *
 * Returns false when storage refused the write — quota exceeded, or DOM storage
 * disabled outright. Callers must surface that: the background is rendered from
 * this same value, so swallowing the failure shows a background that silently
 * disappears on the next reload.
 */
export function saveBackground(dataUrl: string | null): boolean {
  try {
    if (dataUrl) {
      localStorage.setItem(STORAGE_KEY, dataUrl);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
    return true;
  } catch {
    return false;
  }
}
