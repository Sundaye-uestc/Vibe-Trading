import { useState, useEffect } from "react";

export interface ImagePosition {
  x: number; // percentage 0-100
  y: number; // percentage 0-100
}

export interface AvatarConfig {
  type: "letter" | "image";
  letter: string;
  gradient: string;
  imageDataUrl?: string;
  imagePosition?: ImagePosition;
}

const DEFAULT_AGENT: AvatarConfig = {
  type: "letter",
  letter: "P",
  gradient: "from-[#1a365d] to-[#0891b2]",
};

const DEFAULT_USER: AvatarConfig = {
  type: "letter",
  letter: "U",
  gradient: "from-[#6b7280] to-[#9ca3af]",
};

function parseStored(raw: string | null): AvatarConfig | null {
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    if (parsed.type === "image" && typeof parsed.imageDataUrl === "string") {
      const pos = parsed.imagePosition;
      return {
        type: "image",
        letter: "",
        gradient: "",
        imageDataUrl: parsed.imageDataUrl,
        imagePosition: (pos && typeof pos.x === "number" && typeof pos.y === "number")
          ? { x: pos.x, y: pos.y }
          : { x: 50, y: 50 },
      };
    }
    if (parsed.type === "letter" && typeof parsed.letter === "string" && typeof parsed.gradient === "string") {
      return { type: "letter", letter: parsed.letter.slice(0, 2) || "?", gradient: parsed.gradient };
    }
  } catch { /* ignore */ }
  return null;
}

export function getAgentAvatarConfig(): AvatarConfig {
  return parseStored(localStorage.getItem("qa-agent-avatar")) ?? DEFAULT_AGENT;
}

export function getUserAvatarConfig(): AvatarConfig {
  return parseStored(localStorage.getItem("qa-user-avatar")) ?? DEFAULT_USER;
}

function avatarBgStyle(config: AvatarConfig): React.CSSProperties | undefined {
  if (config.type !== "image" || !config.imageDataUrl) return undefined;
  const pos = config.imagePosition || { x: 50, y: 50 };
  return {
    backgroundImage: `url(${config.imageDataUrl})`,
    backgroundSize: "cover",
    backgroundPosition: `${pos.x}% ${pos.y}%`,
  };
}

export function AgentAvatar() {
  const [config, setConfig] = useState<AvatarConfig>(getAgentAvatarConfig);

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail;
      if (detail?.tab === "agent") {
        setConfig(getAgentAvatarConfig());
      }
    };
    window.addEventListener("avatar-changed", handler);
    return () => window.removeEventListener("avatar-changed", handler);
  }, []);

  const bgStyle = avatarBgStyle(config);
  if (bgStyle) {
    return (
      <div
        className="h-8 w-8 rounded-lg shrink-0 mt-0.5 select-none overflow-hidden"
        style={bgStyle}
      />
    );
  }

  return (
    <div
      className={`h-8 w-8 rounded-lg bg-gradient-to-br ${config.gradient} flex items-center justify-center text-white text-xs font-bold shrink-0 mt-0.5 select-none`}
    >
      {config.letter}
    </div>
  );
}

export { avatarBgStyle };
