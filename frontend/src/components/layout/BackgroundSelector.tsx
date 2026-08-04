import { useState, useRef } from "react";
import { useTranslation } from "react-i18next";
import { X, Upload, Trash2 } from "lucide-react";
import { getStoredBackground, BACKGROUND_MAX_SIZE } from "@/hooks/useBackground";

interface Props {
  open: boolean;
  onClose: () => void;
  onSave: (dataUrl: string | null) => void;
}

export function BackgroundSelector({ open, onClose, onSave }: Props) {
  const { t } = useTranslation();
  const [imageDataUrl, setImageDataUrl] = useState<string | null>(getStoredBackground);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setError("");

    if (file.size > BACKGROUND_MAX_SIZE) {
      setError(t("layout.imageTooLarge"));
      return;
    }

    if (!file.type.startsWith("image/")) {
      setError(t("layout.invalidImageType"));
      return;
    }

    const reader = new FileReader();
    reader.onload = () => {
      setImageDataUrl(reader.result as string);
    };
    reader.onerror = () => {
      setError(t("layout.imageLoadFailed"));
    };
    reader.readAsDataURL(file);
  };

  const handleApply = () => {
    if (!imageDataUrl) return;
    try {
      onSave(imageDataUrl);
    } catch (e) {
      console.error("BackgroundSelector: onSave failed", e);
    }
    onClose();
  };

  const handleRemove = () => {
    setImageDataUrl(null);
    onSave(null);
    onClose();
  };

  if (!open) return null;

  const hasExisting = !!(getStoredBackground() || imageDataUrl);

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
            className="w-full flex items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/30 px-4 py-6 text-sm text-muted-foreground hover:border-primary/50 hover:text-primary transition-colors"
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
            disabled={!imageDataUrl}
            className="flex-1 rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
          >
            {t("layout.applyBackground")}
          </button>
        </div>
      </div>
    </div>
  );
}
