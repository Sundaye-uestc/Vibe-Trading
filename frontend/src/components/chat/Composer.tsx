import {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useImperativeHandle,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
  type DragEvent,
  type FormEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import {
  Minimize2,
  Download,
  Landmark,
  Loader2,
  Paperclip,
  Plus,
  ArrowUp,
  Square,
  Target,
  Users,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type { UploadedAttachment } from "@/lib/attachments";
import type { AgentActivity } from "@/stores/agent";
import {
  LiveRuntimeControl,
  LiveRuntimeStatus,
} from "@/components/chat/LiveRuntimePanel";

const CONNECTOR_CHECK_PROMPT =
  "List my trading connector profiles, show which one is selected, then check that selected connector. If it is not ready, tell me exactly what setup step is missing. Do not place or modify orders.";
const CONNECTOR_PORTFOLIO_PROMPT =
  "Use the selected trading connector profile to summarize my account, positions, concentration, cash, and portfolio risk. Do not place or modify orders.";

const ACCEPTED_FILE_TYPES =
  ".pdf,.docx,.xlsx,.xls,.pptx,.csv,.tsv,.txt,.md,.log,.json,.toml,.html,.xml,.rst,.png,.jpg,.jpeg,.gif,.bmp,.webp,.tiff";
const MAX_ATTACHMENT_COUNT = 5;
const MAX_FILE_SIZE = 50 * 1024 * 1024;
const BLOCKED_EXTENSIONS = [
  ".exe", ".msi", ".bat", ".cmd", ".com", ".scr", ".app", ".dmg",
  ".so", ".dll", ".dylib", ".py", ".pyw", ".sh", ".bash", ".zsh",
  ".fish", ".ps1", ".yaml", ".yml", ".j2", ".jinja", ".jinja2",
  ".template", ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2",
  ".xz",
];
const BLOCKED_FILENAMES = new Set(["dockerfile", "containerfile"]);

export type ComposerAttachment = UploadedAttachment;

export interface ComposerHandle {
  fill(prompt: string): void;
  focus(): void;
  submit(prompt: string): void;
}

interface Props {
  streaming: boolean;
  activityVerb?: AgentActivity["verb"];
  hasCompletedTurn: boolean;
  showExport: boolean;
  canExport: boolean;
  goalComposerActive: boolean;
  swarmPreset: { name: string; title: string } | null;
  panels?: ReactNode;
  onSubmit: (prompt: string, attachments: ComposerAttachment[]) => void;
  onCancel: () => void;
  onExport: () => void;
  onStartGoal: () => void;
  onCancelGoal: () => void;
  onStartSwarm: () => void;
  onCancelSwarm: () => void;
  onCompact: () => void;
  compacting: boolean;
}

export const Composer = memo(forwardRef<ComposerHandle, Props>(function Composer({
  streaming,
  activityVerb,
  hasCompletedTurn,
  showExport,
  canExport,
  goalComposerActive,
  swarmPreset,
  panels,
  onSubmit,
  onCancel,
  onExport,
  onStartGoal,
  onCancelGoal,
  onStartSwarm,
  onCancelSwarm,
  onCompact,
  compacting,
}, ref) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const isComposingRef = useRef(false);
  const lastCompositionEndRef = useRef(0);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const dragDepthRef = useRef(0);
  const [showUploadMenu, setShowUploadMenu] = useState(false);
  const uploadMenuRef = useRef<HTMLDivElement>(null);
  const uploadMenuTriggerRef = useRef<HTMLButtonElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const focus = useCallback(() => {
    inputRef.current?.focus({ preventScroll: true });
  }, []);

  const submitPrompt = useCallback((prompt: string) => {
    if ((!prompt.trim() && attachments.length === 0) || streaming || uploading) return;
    setInput("");
    if (inputRef.current) inputRef.current.style.height = "auto";
    const submittedAttachments = [...attachments];
    if (!goalComposerActive) setAttachments([]);
    onSubmit(prompt.trim(), submittedAttachments);
    inputRef.current?.focus();
  }, [attachments, goalComposerActive, onSubmit, streaming, uploading]);

  useImperativeHandle(ref, () => ({
    fill(prompt: string) {
      setInput(prompt);
      requestAnimationFrame(() => {
        const composer = inputRef.current;
        if (!composer) return;
        composer.focus({ preventScroll: true });
        composer.style.height = "auto";
        composer.style.height = `${composer.scrollHeight}px`;
      });
    },
    focus,
    submit: submitPrompt,
  }), [focus, submitPrompt]);

  const handleSubmit = useCallback((event: FormEvent) => {
    event.preventDefault();
    submitPrompt(input);
  }, [input, submitPrompt]);

  const uploadFiles = useCallback(async (files: File[]) => {
    if (files.length === 0 || streaming || uploading) return;
    const remainingSlots = MAX_ATTACHMENT_COUNT - attachments.length;
    if (remainingSlots <= 0) {
      toast.error(t("agent.attachmentLimit", { count: MAX_ATTACHMENT_COUNT }));
      return;
    }
    const validFiles = files.filter((file) => {
      const lowered = file.name.toLowerCase();
      if (
        BLOCKED_FILENAMES.has(lowered)
        || BLOCKED_EXTENSIONS.some((extension) => lowered.endsWith(extension))
      ) {
        toast.error(t("agent.executablesNotAllowed"));
        return false;
      }
      if (file.size > MAX_FILE_SIZE) {
        toast.error(t("agent.fileSizeExceeds"));
        return false;
      }
      return true;
    });
    if (validFiles.length > remainingSlots) {
      toast.error(t("agent.attachmentLimit", { count: MAX_ATTACHMENT_COUNT }));
    }
    const accepted = validFiles.slice(0, remainingSlots);
    if (accepted.length === 0) return;

    setUploading(true);
    setShowUploadMenu(false);
    try {
      for (const file of accepted) {
        try {
          const result = await api.uploadFile(file);
          setAttachments((current) => [
            ...current,
            { filename: result.filename, filePath: result.file_path },
          ]);
          toast.success(t("agent.uploaded", { filename: result.filename }));
        } catch (error) {
          toast.error(t("agent.uploadFailed", {
            error: error instanceof Error ? error.message : "Unknown error",
          }));
        }
      }
    } finally {
      setUploading(false);
    }
  }, [attachments.length, streaming, t, uploading]);

  const handleFileSelect = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    void uploadFiles(files);
  }, [uploadFiles]);

  const handlePaste = useCallback((event: ClipboardEvent<HTMLFormElement>) => {
    const files = Array.from(event.clipboardData.files ?? []);
    if (files.length === 0) return;
    event.preventDefault();
    void uploadFiles(files);
  }, [uploadFiles]);

  const handleDragEnter = useCallback((event: DragEvent<HTMLFormElement>) => {
    if (streaming || uploading) return;
    if (!Array.from(event.dataTransfer.types ?? []).includes("Files")) return;
    event.preventDefault();
    dragDepthRef.current += 1;
    setDraggingFiles(true);
  }, [streaming, uploading]);

  const handleDragLeave = useCallback((event: DragEvent<HTMLFormElement>) => {
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDraggingFiles(false);
  }, []);

  const handleDrop = useCallback((event: DragEvent<HTMLFormElement>) => {
    event.preventDefault();
    dragDepthRef.current = 0;
    setDraggingFiles(false);
    void uploadFiles(Array.from(event.dataTransfer.files ?? []));
  }, [uploadFiles]);

  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (uploadMenuRef.current && !uploadMenuRef.current.contains(event.target as Node)) {
        setShowUploadMenu(false);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setShowUploadMenu(false);
      uploadMenuTriggerRef.current?.focus();
    };
    if (showUploadMenu) {
      const focusFrame = requestAnimationFrame(() => {
        uploadMenuRef.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
      });
      document.addEventListener("mousedown", handleClickOutside);
      document.addEventListener("keydown", handleEscape);
      return () => {
        cancelAnimationFrame(focusFrame);
        document.removeEventListener("mousedown", handleClickOutside);
        document.removeEventListener("keydown", handleEscape);
      };
    }
  }, [showUploadMenu]);

  return (
    <form
      onSubmit={handleSubmit}
      onPaste={handlePaste}
      onDragEnter={handleDragEnter}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      className="studio-composer relative space-y-2"
    >
      {draggingFiles && (
        <div role="status" className="pointer-events-none absolute inset-0 z-50 flex items-center justify-center rounded-2xl border-2 border-dashed border-primary bg-background/90 text-sm font-medium text-primary">
          {t("agent.dropFiles")}
        </div>
      )}
      {swarmPreset && (
        <div className="flex items-center gap-1">
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary/10 text-primary text-xs font-medium">
            <Users className="h-3 w-3" />
            {swarmPreset.title}
            <button type="button" onClick={onCancelSwarm} className="hover:text-destructive transition-colors">
              <X className="h-3 w-3" />
            </button>
          </span>
        </div>
      )}
      {goalComposerActive && (
        <div className="flex items-center gap-1">
          <span className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary/10 text-primary text-xs font-medium">
            <Target className="h-3 w-3" />
            {t("agent.newResearchGoal")}
            <button type="button" onClick={onCancelGoal} className="hover:text-destructive transition-colors">
              <X className="h-3 w-3" />
            </button>
          </span>
        </div>
      )}
      {panels}
      <LiveRuntimeStatus />
      {attachments.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          {attachments.map((attachment, index) => (
            <span key={attachment.filePath} className="inline-flex max-w-full items-center gap-1.5 px-2.5 py-1 rounded-lg bg-primary/10 text-primary text-xs font-medium">
              <Paperclip className="h-3 w-3 shrink-0" />
              <span className="truncate">{attachment.filename}</span>
              <button
                type="button"
                aria-label={t("agent.removeAttachment", { filename: attachment.filename })}
                onClick={() => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))}
                className="hover:text-destructive transition-colors"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      {uploading && (
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Loader2 className="h-3 w-3 animate-spin" />
          {t("agent.uploading")}
        </div>
      )}
      <LiveRuntimeControl />
      <div className="studio-composer-surface transition-shadow">
        <div className="studio-composer-options relative" ref={uploadMenuRef}>
          <button
            ref={uploadMenuTriggerRef}
            type="button"
            onClick={() => setShowUploadMenu((previous) => !previous)}
            disabled={streaming || uploading}
            aria-haspopup="menu"
            aria-expanded={showUploadMenu}
            aria-controls="agent-more-options-menu"
            className="studio-composer-more flex items-center justify-center text-muted-foreground hover:text-foreground transition-colors disabled:opacity-40 shrink-0"
            title={t("agent.moreOptions")}
          >
            <Plus className="h-4 w-4" />
          </button>
          {showUploadMenu && (
            <div
              id="agent-more-options-menu"
              role="menu"
              aria-label={t("agent.moreOptions")}
              onKeyDown={(event) => {
                const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'));
                const index = items.indexOf(document.activeElement as HTMLButtonElement);
                if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Home" || event.key === "End") {
                  event.preventDefault();
                  const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
                    : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
                  items[next]?.focus();
                } else if (event.key === "Tab") setShowUploadMenu(false);
              }}
              className="studio-composer-menu absolute bottom-full start-0 mb-3 w-60 rounded-2xl border py-2 z-50"
            >
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  fileInputRef.current?.click();
                  setShowUploadMenu(false);
                }}
                className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
              >
                <Paperclip className="h-4 w-4" />
                {t("agent.uploadFile")}
              </button>
              <div className="border-t my-1" />
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setShowUploadMenu(false);
                  onStartGoal();
                  inputRef.current?.focus();
                }}
                className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
              >
                <Target className="h-4 w-4" />
                {t("agent.researchGoal")}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setShowUploadMenu(false);
                  onStartSwarm();
                  inputRef.current?.focus();
                }}
                className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
              >
                <Users className="h-4 w-4" />
                {t("agent.agentSwarm")}
              </button>
              <div className="border-t my-1" />
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setShowUploadMenu(false);
                  submitPrompt(CONNECTOR_CHECK_PROMPT);
                }}
                className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
              >
                <Landmark className="h-4 w-4" />
                {t("agent.checkConnector")}
              </button>
              <button
                type="button"
                role="menuitem"
                onClick={() => {
                  setShowUploadMenu(false);
                  submitPrompt(CONNECTOR_PORTFOLIO_PROMPT);
                }}
                className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
              >
                <Landmark className="h-4 w-4" />
                {t("agent.analyzePortfolio")}
              </button>
              <div className="border-t my-1" />
              <button
                type="button"
                role="menuitem"
                disabled={streaming || compacting}
                onClick={() => {
                  setShowUploadMenu(false);
                  onCompact();
                }}
                className="w-full px-3 py-2 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2 disabled:opacity-40 disabled:pointer-events-none"
              >
                <Minimize2 className="h-4 w-4" />
                {compacting ? t("agent.compactingContext") : t("agent.compactContext")}
              </button>
            </div>
          )}
        </div>
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={ACCEPTED_FILE_TYPES}
          onChange={handleFileSelect}
          className="hidden"
        />
        <textarea
          ref={inputRef}
          value={input}
          rows={1}
          onChange={(e) => setInput(e.target.value)}
          onCompositionStart={() => {
            isComposingRef.current = true;
          }}
          onCompositionEnd={() => {
            isComposingRef.current = false;
            lastCompositionEndRef.current = Date.now();
          }}
          onInput={(e) => {
            const el = e.target as HTMLTextAreaElement;
            el.style.height = "auto";
            el.style.height = el.scrollHeight + "px";
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              const nativeEvent = e.nativeEvent as KeyboardEvent & { isComposing?: boolean };
              const justFinishedComposing = Date.now() - lastCompositionEndRef.current < 80;
              if (isComposingRef.current || nativeEvent.isComposing || nativeEvent.keyCode === 229) {
                return;
              }
              if (justFinishedComposing) {
                e.preventDefault();
                return;
              }
              e.preventDefault();
              submitPrompt(input);
            }
          }}
          placeholder={
            streaming
              ? t(`agent.activity.verbs.${activityVerb ?? "working"}` as never)
              : goalComposerActive
              ? t("agent.describeGoal")
              : hasCompletedTurn
              ? t("agent.followUpPlaceholder" as never)
              : t("agent.placeholder")
          }
          aria-label={t("agent.messageInputLabel")}
          aria-readonly={streaming}
          className={[
            "studio-composer-input min-w-0 resize-none overflow-y-auto bg-transparent text-sm outline-none",
            streaming ? "cursor-not-allowed text-muted-foreground/70" : "",
          ].join(" ")}
          readOnly={streaming}
        />
        {showExport && (
          <button
            type="button"
            onClick={onExport}
            disabled={!canExport}
            className="studio-composer-export text-muted-foreground hover:text-foreground transition-colors disabled:opacity-30 disabled:pointer-events-none"
            title={t("agent.exportChat")}
          >
            <Download className="h-4 w-4" />
          </button>
        )}
        {streaming ? (
          <button
            type="button"
            onClick={onCancel}
            className="studio-composer-submit studio-composer-stop bg-destructive text-destructive-foreground text-sm font-medium"
            title={t("agent.stopGeneration")}
          >
            <Square className="h-4 w-4" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={uploading || (goalComposerActive ? !input.trim() : (!input.trim() && attachments.length === 0))}
            className="studio-composer-submit bg-primary text-primary-foreground text-sm font-medium disabled:opacity-40"
            title={t("agent.send")}
            aria-label={t("agent.send")}
          >
            <ArrowUp className="h-[18px] w-[18px]" />
          </button>
        )}
      </div>
      <p className="studio-composer-hint text-[11px] text-muted-foreground">{t("agent.inputHint")}</p>
    </form>
  );
}));
