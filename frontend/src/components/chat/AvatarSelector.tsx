import { useState, useEffect, useRef, useCallback } from "react";
import { useTranslation } from "react-i18next";
import { X, Upload, ImageIcon, Type, Move } from "lucide-react";
import { toast } from "sonner";
import { getAgentAvatarConfig, getUserAvatarConfig, type AvatarConfig, type ImagePosition } from "./AgentAvatar";
import {
  AVATAR_RESIZE_TARGET,
  ImageResizeError,
  resizeImageFileToDataUrl,
  type ImageResizeErrorCode,
} from "@/lib/imageResize";

const GRADIENTS = [
  { label: "Ocean", value: "from-[#1a365d] to-[#0891b2]" },
  { label: "Purple", value: "from-[#7c3aed] to-[#a78bfa]" },
  { label: "Emerald", value: "from-[#059669] to-[#34d399]" },
  { label: "Red", value: "from-[#dc2626] to-[#f87171]" },
  { label: "Amber", value: "from-[#d97706] to-[#fbbf24]" },
  { label: "Blue", value: "from-[#2563eb] to-[#60a5fa]" },
  { label: "Pink", value: "from-[#be185d] to-[#f472b6]" },
  { label: "Zinc", value: "from-[#52525b] to-[#a1a1aa]" },
];

type AvatarTab = "agent" | "user";
type AvatarMode = "letter" | "image";

interface Props {
  open: boolean;
  onClose: () => void;
}

const PREVIEW_SIZE = 128; // px — larger preview for easier drag

export function AvatarSelector({ open, onClose }: Props) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<AvatarTab>("agent");
  const [mode, setMode] = useState<AvatarMode>("letter");
  const [letter, setLetter] = useState("P");
  const [gradient, setGradient] = useState(GRADIENTS[0].value);
  const [imageDataUrl, setImageDataUrl] = useState<string | null>(null);
  const [imagePos, setImagePos] = useState<ImagePosition>({ x: 50, y: 50 });
  const [imageError, setImageError] = useState("");
  const [saveError, setSaveError] = useState("");
  const [busy, setBusy] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Drag state for image crop
  const [dragging, setDragging] = useState(false);
  const dragStartRef = useRef<{ x: number; y: number; startPos: ImagePosition }>({ x: 0, y: 0, startPos: { x: 50, y: 50 } });

  // Load current config when opening
  useEffect(() => {
    if (!open) return;
    const config = tab === "agent" ? getAgentAvatarConfig() : getUserAvatarConfig();
    setMode(config.type);
    if (config.type === "image") {
      setImageDataUrl(config.imageDataUrl || null);
      setImagePos(config.imagePosition || { x: 50, y: 50 });
      setLetter("P");
      setGradient(GRADIENTS[0].value);
    } else {
      setImageDataUrl(null);
      setImagePos({ x: 50, y: 50 });
      setLetter(config.letter);
      setGradient(config.gradient);
    }
    setImageError("");
    setSaveError("");
    setDragging(false);
  }, [open, tab]);

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
    setImageError("");
    setBusy(true);
    try {
      // Downscale before previewing. The avatar is drawn at 32px through CSS
      // `background-size: cover`, so a full-resolution data URL buys nothing
      // and overflows the localStorage quota — which made the save below throw
      // QuotaExceededError and the dialog look unresponsive.
      setImageDataUrl(
        await resizeImageFileToDataUrl(file, AVATAR_RESIZE_TARGET),
      );
      setImagePos({ x: 50, y: 50 }); // Reset position on new image
    } catch (err) {
      setImageError(
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

  /* ---- Image drag-to-crop ---- */
  const onDragStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    setDragging(true);
    dragStartRef.current = {
      x: e.clientX,
      y: e.clientY,
      startPos: { ...imagePos },
    };
  }, [imagePos]);

  useEffect(() => {
    if (!dragging) return;

    const onMouseMove = (e: MouseEvent) => {
      const dx = e.clientX - dragStartRef.current.x;
      const dy = e.clientY - dragStartRef.current.y;
      // Scale: 1px drag = ~0.5% position change (sensitivity)
      const sensitivity = 0.4;
      setImagePos({
        x: Math.max(0, Math.min(100, dragStartRef.current.startPos.x + dx * sensitivity)),
        y: Math.max(0, Math.min(100, dragStartRef.current.startPos.y + dy * sensitivity)),
      });
    };

    const onMouseUp = () => {
      setDragging(false);
    };

    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    return () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };
  }, [dragging]);

  const handleSave = () => {
    let config: AvatarConfig;
    if (mode === "image" && imageDataUrl) {
      config = { type: "image", letter: "", gradient: "", imageDataUrl, imagePosition: imagePos };
    } else {
      config = { type: "letter", letter: letter.slice(0, 2) || "?", gradient };
    }
    setSaveError("");
    const key = tab === "agent" ? "qa-agent-avatar" : "qa-user-avatar";
    try {
      localStorage.setItem(key, JSON.stringify(config));
    } catch {
      // Storage refused the write. Report it and keep the dialog open: the
      // exception used to escape here, so the avatar-changed event and
      // onClose() never ran and the save button looked dead.
      const message = t("layout.imageSaveFailed");
      setSaveError(message);
      toast.error(message);
      return;
    }
    window.dispatchEvent(new CustomEvent("avatar-changed", { detail: { tab } }));
    onClose();
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      {/* Dialog */}
      <div className="relative bg-card border rounded-2xl shadow-xl w-[380px] max-w-[95vw] p-6 space-y-5 max-h-[90vh] overflow-y-auto">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold">{t("layout.changeAvatar")}</h3>
          <button onClick={onClose} className="p-1 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors">
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Tabs: Agent / User */}
        <div className="flex rounded-lg bg-muted p-0.5">
          {(["agent", "user"] as AvatarTab[]).map((tabOpt) => (
            <button
              key={tabOpt}
              onClick={() => setTab(tabOpt)}
              className={`flex-1 rounded-md py-1.5 text-xs font-medium transition-colors ${
                tab === tabOpt
                  ? "bg-background text-foreground shadow-sm"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {tabOpt === "agent" ? t("layout.agentAvatar") : t("layout.userAvatar")}
            </button>
          ))}
        </div>

        {/* Mode toggle */}
        <div className="flex rounded-lg bg-muted p-0.5">
          <button
            onClick={() => setMode("letter")}
            className={`flex-1 flex items-center justify-center gap-1.5 rounded-md py-1.5 text-xs font-medium transition-colors ${
              mode === "letter" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <Type className="h-3.5 w-3.5" />
            {t("layout.avatarLetterMode")}
          </button>
          <button
            onClick={() => setMode("image")}
            className={`flex-1 flex items-center justify-center gap-1.5 rounded-md py-1.5 text-xs font-medium transition-colors ${
              mode === "image" ? "bg-background text-foreground shadow-sm" : "text-muted-foreground hover:text-foreground"
            }`}
          >
            <ImageIcon className="h-3.5 w-3.5" />
            {t("layout.avatarImageMode")}
          </button>
        </div>

        {/* Preview */}
        <div className="flex flex-col items-center gap-2">
          {mode === "image" && imageDataUrl ? (
            <>
              {/* Large draggable preview */}
              <div
                className={`rounded-lg overflow-hidden border-2 select-none shadow-lg ${
                  dragging ? "border-primary cursor-grabbing" : "border-border cursor-grab"
                }`}
                style={{ width: PREVIEW_SIZE, height: PREVIEW_SIZE }}
                onMouseDown={onDragStart}
              >
                <div
                  className="w-full h-full"
                  style={{
                    backgroundImage: `url(${imageDataUrl})`,
                    backgroundSize: "cover",
                    backgroundPosition: `${imagePos.x}% ${imagePos.y}%`,
                  }}
                />
              </div>
              <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
                <Move className="h-3 w-3" />
                {t("layout.dragToCrop")}
              </div>
              {/* Small preview: how it looks as actual avatar */}
              <div className="flex items-center gap-2">
                <span className="text-[10px] text-muted-foreground">{t("layout.avatarPreview")}</span>
                <div
                  className="h-8 w-8 rounded-lg overflow-hidden border"
                  style={{
                    backgroundImage: `url(${imageDataUrl})`,
                    backgroundSize: "cover",
                    backgroundPosition: `${imagePos.x}% ${imagePos.y}%`,
                  }}
                />
              </div>
            </>
          ) : (
            <div
              className={`h-16 w-16 rounded-lg bg-gradient-to-br ${gradient} flex items-center justify-center text-white text-xl font-bold select-none shadow-lg`}
            >
              {letter.slice(0, 2) || "?"}
            </div>
          )}
        </div>

        {/* Letter mode controls */}
        {mode === "letter" && (
          <>
            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">{t("layout.avatarLetter")}</label>
              <input
                type="text"
                maxLength={2}
                value={letter}
                onChange={(e) => setLetter(e.target.value || "")}
                className="w-full rounded-lg border bg-background px-3 py-2 text-sm text-foreground outline-none focus:ring-2 focus:ring-primary/30"
                placeholder="P"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-xs font-medium text-muted-foreground">{t("layout.avatarPresets")}</label>
              <div className="grid grid-cols-4 gap-2">
                {GRADIENTS.map((g) => (
                  <button
                    key={g.value}
                    onClick={() => setGradient(g.value)}
                    className={`h-10 rounded-lg bg-gradient-to-br ${g.value} transition-all ${
                      gradient === g.value
                        ? "ring-2 ring-primary ring-offset-2 ring-offset-card scale-105"
                        : "hover:scale-105"
                    }`}
                    title={g.label}
                  />
                ))}
              </div>
            </div>
          </>
        )}

        {/* Image mode controls */}
        {mode === "image" && (
          <div className="space-y-3">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/*"
              onChange={handleFileSelect}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy}
              className="w-full flex items-center justify-center gap-2 rounded-lg border-2 border-dashed border-muted-foreground/30 px-4 py-2.5 text-sm text-muted-foreground hover:border-primary/50 hover:text-primary transition-colors disabled:opacity-50"
            >
              <Upload className="h-4 w-4" />
              {imageDataUrl ? t("layout.changeImage") : t("layout.chooseImage")}
            </button>
            <p className="text-[10px] text-muted-foreground text-center">{t("layout.imageSizeHint")}</p>
            {imageError && (
              <p className="text-xs text-destructive text-center">{imageError}</p>
            )}
          </div>
        )}

        {saveError && (
          <p className="text-xs text-destructive text-center">{saveError}</p>
        )}

        {/* Save */}
        <button
          onClick={handleSave}
          disabled={busy || (mode === "letter" ? !letter.trim() : !imageDataUrl)}
          className="w-full rounded-lg bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
        >
          {t("layout.saveAvatar")}
        </button>
      </div>
    </div>
  );
}
