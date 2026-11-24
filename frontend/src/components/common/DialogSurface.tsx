import { useEffect, useId, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

/** A focused local workspace layer with keyboard containment and return focus. */
export function DialogSurface({ open, title, closeLabel, onClose, children, className = "" }: {
  open: boolean;
  title: string;
  closeLabel: string;
  onClose: () => void;
  children: ReactNode;
  className?: string;
}) {
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const app = document.getElementById("root");
    const wasInert = app?.inert ?? false;
    if (app) app.inert = true;
    const initial = panel.current?.querySelector<HTMLElement>("[data-autofocus]");
    (initial ?? close.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      } else if (event.key === "Tab") {
        const targets = Array.from(panel.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
        ) ?? []).filter((element) => !element.hidden && element.getAttribute("type") !== "hidden");
        const first = targets[0];
        const last = targets[targets.length - 1];
        if (event.shiftKey && (document.activeElement === first || !panel.current?.contains(document.activeElement))) {
          event.preventDefault(); last?.focus();
        } else if (!event.shiftKey && (document.activeElement === last || !panel.current?.contains(document.activeElement))) {
          event.preventDefault(); first?.focus();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (app) app.inert = wasInert;
      if (previous?.isConnected) previous.focus();
    };
  }, [open]);
  if (!open) return null;
  return createPortal(
    <div className="studio-modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <div ref={panel} role="dialog" aria-modal="true" aria-labelledby={id} className={`studio-modal ${className}`}>
        <div className="studio-modal-heading">
          <h2 id={id}>{title}</h2>
          <button ref={close} type="button" className="studio-icon-button" aria-label={closeLabel} onClick={onClose}><X size={18} /></button>
        </div>
        {children}
      </div>
    </div>, document.body,
  );
}
