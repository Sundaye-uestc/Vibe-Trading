import { useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { X, Upload, Trash2 } from "lucide-react";
import { getStoredBackground } from "@/hooks/useBackground";
import {
  backgroundResizeTarget,
  ImageResizeError,
  resizeImageFileToDataUrl,
  type ImageResizeErrorCode,
} from "@/lib/imageResize";

interface Props {
  open: boolean;
  onClose: () => void;
  /** Returns false when the value could not be persisted. */
  onSave: (dataUrl: string | null) => boolean;
}

export function BackgroundSelector({ open, onClose, onSave }: Props) {
  const { t } = useTranslation();
  const [imageDataUrl, setImageDataUrl] = useState<string | null>(getStoredBackground);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const messageFor = (code: ImageResizeErrorCode): string => {
    switch (code) {
      case "too-large":
        return t("layout.imageTooLarge");
      case "invalid-type":
        return t("layout.invalidImageType");
      case "canvas-unavailable":
        return t("layout.imageResizeUnsupported");
      default:
        return t("layout.imageLoadFailed");
    }
  };

  const handleFileSelect = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setError("");
    setBusy(true);
    try {
      // Shrink before previewing: what the preview shows is exactly what gets
      // persisted, so a "successful" apply can never turn out to be unstorable.
      setImageDataUrl(
        await resizeImageFileToDataUrl(file, {
          ...backgroundResizeTarget(),
          quality: 0.85,
        }),
      );
    } catch (err) {
      setError(
        err instanceof ImageResizeError
          ? messageFor(err.code)
          : t("layout.imageLoadFailed"),
      );
    } finally {
      setBusy(false);
      // Allow re-picking the same file after a failure.
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const handleApply = () => {
    if (!imageDataUrl || busy) return;
    if (!onSave(imageDataUrl)) {
      // Storage refused the write (quota, or DOM storage disabled). Keep the
      // dialog open so the failure is visible instead of a background that
      // vanishes on the next reload.
      setError(t("layout.imageSaveFailed"));
      return;
    }
    onClose();
  };

  const handleRemove = () => {
    setImageDataUrl(null);
    if (!onSave(null)) {
      setError(t("layout.imageSaveFailed"));
      return;
    }
    onClose();
  };

  if (!open) return null;

  const hasExisting = !!imageDataUrl;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div
        className="relative z-10 bg-card border rounded-2xl shadow-xl w-[360px] max-w-[95vw] p-6 space-y-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">{t("layout.changeBackground")}</h3>
          <button onClick={onClose} className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors">
            <X className="h-4 w-4" />
          </button>
        </div>

        {imageDataUrl && (
          <div
            className="w-full h-32 rounded-lg bg-cover bg-center border"
            style={{ backgroundImage: `url(${imageDataUrl})` }}
          />
        )}

        <input ref={fileInputRef} type="file" accept="image/*" onChange={handleFileSelect} className="hidden" />
        <div className="space-y-3">
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
            className="w-full flex items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/30 px-4 py-6 text-sm text-muted-foreground hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
          >
            <Upload className="h-4 w-4" />
            {imageDataUrl ? t("layout.changeImage") : t("layout.chooseImage")}
          </button>
          <p className="text-[10px] text-muted-foreground text-center">{t("layout.backgroundSizeHint")}</p>
          {error && <p className="text-xs text-destructive text-center">{error}</p>}
        </div>

        <div className="flex gap-2">
          {hasExisting && (
            <button
              onClick={handleRemove}
              className="flex items-center gap-1.5 rounded-lg border border-destructive/30 px-3 py-2 text-xs font-medium text-destructive hover:bg-destructive/5 transition-colors"
            >
              <Trash2 className="h-3.5 w-3.5" />
              {t("layout.removeBackground")}
            </button>
          )}
          <button
            type="button"
            onClick={handleApply}
            disabled={!imageDataUrl || busy}
            className="flex-1 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {t("layout.applyBackground")}
          </button>
        </div>
      </div>
    </div>
  );
}
