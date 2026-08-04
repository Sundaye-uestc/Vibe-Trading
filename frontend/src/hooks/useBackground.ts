const STORAGE_KEY = "qa-background";
const MAX_SIZE = 5 * 1024 * 1024; // 5MB

export function getStoredBackground(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

export function saveBackground(dataUrl: string | null) {
  if (dataUrl) {
    localStorage.setItem(STORAGE_KEY, dataUrl);
  } else {
    localStorage.removeItem(STORAGE_KEY);
  }
}

export { MAX_SIZE as BACKGROUND_MAX_SIZE };
