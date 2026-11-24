import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ArrowUpRight, ChevronDown, TrendingUp, Globe, Sparkles, Users, UserCircle2, NotebookPen, Landmark, Gem } from "lucide-react";
import { BrandMark } from "@/components/common/BrandMark";

interface Example {
  titleKey: string;
  descKey: string;
  promptKey: string;
}

interface Category {
  labelKey: string;
  icon: React.ReactNode;
  examples: Example[];
}

const CATEGORIES: Category[] = [
  {
    labelKey: "welcome.categories.multiMarketBacktest",
    icon: <TrendingUp className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.crossMarketPortfolio",
        descKey: "welcome.examples.crossMarketPortfolioDesc",
        promptKey: "welcome.examples.crossMarketPortfolioPrompt",
      },
      {
        titleKey: "welcome.examples.btcMacd",
        descKey: "welcome.examples.btcMacdDesc",
        promptKey: "welcome.examples.btcMacdPrompt",
      },
      {
        titleKey: "welcome.examples.usTechMaxDiv",
        descKey: "welcome.examples.usTechMaxDivDesc",
        promptKey: "welcome.examples.usTechMaxDivPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.researchAnalysis",
    icon: <Sparkles className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.multiFactorAlpha",
        descKey: "welcome.examples.multiFactorAlphaDesc",
        promptKey: "welcome.examples.multiFactorAlphaPrompt",
      },
      {
        titleKey: "welcome.examples.optionsGreeks",
        descKey: "welcome.examples.optionsGreeksDesc",
        promptKey: "welcome.examples.optionsGreeksPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.valueInvesting",
    icon: <Gem className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.valueCommittee",
        descKey: "welcome.examples.valueCommitteeDesc",
        promptKey: "welcome.examples.valueCommitteePrompt",
      },
      {
        titleKey: "welcome.examples.bottleneckHunter",
        descKey: "welcome.examples.bottleneckHunterDesc",
        promptKey: "welcome.examples.bottleneckHunterPrompt",
      },
      {
        titleKey: "welcome.examples.thesisTracker",
        descKey: "welcome.examples.thesisTrackerDesc",
        promptKey: "welcome.examples.thesisTrackerPrompt",
      },
      {
        titleKey: "welcome.examples.valuationCheck",
        descKey: "welcome.examples.valuationCheckDesc",
        promptKey: "welcome.examples.valuationCheckPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.swarmTeams",
    icon: <Users className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.investmentCommittee",
        descKey: "welcome.examples.investmentCommitteeDesc",
        promptKey: "welcome.examples.investmentCommitteePrompt",
      },
      {
        titleKey: "welcome.examples.quantStrategyDesk",
        descKey: "welcome.examples.quantStrategyDeskDesc",
        promptKey: "welcome.examples.quantStrategyDeskPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.docWebResearch",
    icon: <Globe className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.earningsReport",
        descKey: "welcome.examples.earningsReportDesc",
        promptKey: "welcome.examples.earningsReportPrompt",
      },
      {
        titleKey: "welcome.examples.macroResearch",
        descKey: "welcome.examples.macroResearchDesc",
        promptKey: "welcome.examples.macroResearchPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.tradeJournal",
    icon: <NotebookPen className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.analyzeBrokerExport",
        descKey: "welcome.examples.analyzeBrokerExportDesc",
        promptKey: "welcome.examples.analyzeBrokerExportPrompt",
      },
      {
        titleKey: "welcome.examples.diagnoseBehavior",
        descKey: "welcome.examples.diagnoseBehaviorDesc",
        promptKey: "welcome.examples.diagnoseBehaviorPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.tradingConnectors",
    icon: <Landmark className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.checkConnector",
        descKey: "welcome.examples.checkConnectorDesc",
        promptKey: "welcome.examples.checkConnectorPrompt",
      },
      {
        titleKey: "welcome.examples.analyzePortfolio",
        descKey: "welcome.examples.analyzePortfolioDesc",
        promptKey: "welcome.examples.analyzePortfolioPrompt",
      },
      {
        titleKey: "welcome.examples.quoteTrend",
        descKey: "welcome.examples.quoteTrendDesc",
        promptKey: "welcome.examples.quoteTrendPrompt",
      },
    ],
  },
  {
    labelKey: "welcome.categories.shadowAccount",
    icon: <UserCircle2 className="h-4 w-4" />,
    examples: [
      {
        titleKey: "welcome.examples.trainShadow",
        descKey: "welcome.examples.trainShadowDesc",
        promptKey: "welcome.examples.trainShadowPrompt",
      },
      {
        titleKey: "welcome.examples.shadowDelta",
        descKey: "welcome.examples.shadowDeltaDesc",
        promptKey: "welcome.examples.shadowDeltaPrompt",
      },
      {
        titleKey: "welcome.examples.shadowReport",
        descKey: "welcome.examples.shadowReportDesc",
        promptKey: "welcome.examples.shadowReportPrompt",
      },
    ],
  },
];

const QUICK_ACTIONS = [
  {
    titleKey: "welcome.examples.valuationCheck",
    promptKey: "welcome.examples.valuationCheckPrompt",
    icon: <Gem className="h-4 w-4" aria-hidden="true" />,
  },
  {
    titleKey: "welcome.examples.optionsGreeks",
    promptKey: "welcome.examples.optionsGreeksPrompt",
    icon: <Sparkles className="h-4 w-4" aria-hidden="true" />,
  },
  {
    titleKey: "welcome.examples.crossMarketPortfolio",
    promptKey: "welcome.examples.crossMarketPortfolioPrompt",
    icon: <TrendingUp className="h-4 w-4" aria-hidden="true" />,
  },
  {
    titleKey: "welcome.examples.investmentCommittee",
    promptKey: "welcome.examples.investmentCommitteePrompt",
    icon: <Users className="h-4 w-4" aria-hidden="true" />,
  },
] as const;

interface Props {
  onExample: (s: string) => void;
}

export function WelcomeScreen({ onExample }: Props) {
  const { t } = useTranslation();
  const [isExamplesOpen, setIsExamplesOpen] = useState(false);
  const [activeCategory, setActiveCategory] = useState(0);
  const examplesTriggerRef = useRef<HTMLButtonElement>(null);

  return (
    <div className="studio-welcome">
      <div className="studio-welcome-inner">
        <div className="studio-welcome-intro">
          <div className="studio-welcome-copy">
            <div className="studio-eyebrow">
              <BrandMark className="h-5 w-5 shrink-0" />
              <span>{t("welcome.studioEyebrow" as never, { defaultValue: "ROOM FOR A CLEARER VIEW" })}</span>
            </div>
            <h1 className="studio-welcome-title">
              <span>{t("welcome.studioTitleFirst" as never, { defaultValue: "Every idea deserves" })}</span>{" "}
              <span>{t("welcome.studioTitleSecond" as never, { defaultValue: "a clearer view." })}</span>
            </h1>
            <p className="studio-welcome-description">{t("welcome.taskSubtitle" as any)}</p>
          </div>
          <div className="studio-research-orbit" aria-hidden="true">
            <svg viewBox="0 0 240 240" fill="none" role="presentation">
              <circle cx="120" cy="120" r="101" className="orbit-ring" />
              <circle cx="120" cy="120" r="73" className="orbit-ring orbit-ring-inner" />
              <path d="M19 120H221M120 19V221" className="orbit-axis" />
              <path d="M43 159C62 171 67 118 87 130S110 154 127 109S159 124 170 85S192 71 207 63" className="orbit-trace" />
              <path d="M36 181C66 173 78 181 101 162S148 168 168 144S196 154 214 136" className="orbit-secondary" />
              <circle cx="127" cy="109" r="16" className="orbit-halo" />
              <circle cx="127" cy="109" r="4" className="orbit-dot" />
              <circle cx="207" cy="63" r="3" className="orbit-dot orbit-dot-small" />
              <path d="M115 24H125M216 115V125M115 216H125M24 115V125" className="orbit-tick" />
            </svg>
            <span>{t("welcome.studioTraceCaption" as never, { defaultValue: "Question · Research · Verify" })}</span>
          </div>
        </div>

        <div className="studio-start-heading">
          <span>{t("welcome.studioPromptLabel" as never, { defaultValue: "A place to begin" })}</span>
          <span className="studio-start-rule" aria-hidden="true" />
        </div>
        <div
          className="studio-quick-actions"
          role="group"
          aria-label={t("welcome.quickActions" as any)}
        >
          {QUICK_ACTIONS.map((action, index) => (
            <button
              key={action.titleKey}
              type="button"
              onClick={() => onExample(t(action.promptKey as any))}
              className="studio-quick-action focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
            >
              <span className="studio-quick-number" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
              <span className="studio-quick-icon" aria-hidden="true">{action.icon}</span>
              <span className="studio-quick-label">{t(action.titleKey as any)}</span>
              <ArrowUpRight className="studio-quick-arrow h-4 w-4" aria-hidden="true" />
            </button>
          ))}
        </div>

        <button
          ref={examplesTriggerRef}
          type="button"
          aria-expanded={isExamplesOpen}
          aria-controls="welcome-example-library"
          onClick={() => setIsExamplesOpen((open) => !open)}
          className="studio-examples-trigger inline-flex items-center gap-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
        >
          <span>{t("welcome.browseAllExamples" as any)}</span>
          <ChevronDown
            className={`h-4 w-4 transition-transform duration-300 ${isExamplesOpen ? "rotate-180" : ""}`}
            aria-hidden="true"
          />
        </button>

        <div
          id="welcome-example-library"
          aria-hidden={!isExamplesOpen}
          onKeyDown={(event) => {
            if (event.key !== "Escape" || !isExamplesOpen) return;
            event.preventDefault();
            event.stopPropagation();
            setIsExamplesOpen(false);
            examplesTriggerRef.current?.focus();
          }}
          className={`studio-example-library grid text-left transition-[grid-template-rows,opacity] duration-300 ease-out ${
            isExamplesOpen
              ? "grid-rows-[1fr] opacity-100"
              : "grid-rows-[0fr] opacity-0"
          }`}
        >
          <div className="min-h-0 overflow-hidden">
            {/* One category at a time: a chip tab bar + that category's cards.
                Rendering all 8 categories at once (grid or masonry) reads as
                an unstructured wall — categories have uneven card counts. */}
            <div className="pt-6">
              <div
                className="studio-example-tabs flex flex-wrap gap-1.5"
                role="tablist"
                aria-label={t("welcome.browseAllExamples" as any)}
              >
                {CATEGORIES.map((cat, index) => (
                  <button
                    key={cat.labelKey}
                    type="button"
                    role="tab"
                    aria-selected={index === activeCategory}
                    tabIndex={isExamplesOpen ? 0 : -1}
                    onClick={() => setActiveCategory(index)}
                    className={
                      index === activeCategory
                        ? "studio-example-tab studio-example-tab-selected inline-flex items-center gap-1.5 border-b border-primary/70 px-3 py-2 text-xs font-medium text-primary"
                        : "studio-example-tab inline-flex items-center gap-1.5 border-b border-transparent px-3 py-2 text-xs text-muted-foreground transition-colors hover:border-border/60 hover:text-foreground"
                    }
                  >
                    {cat.icon}
                    <span>{t(cat.labelKey as any)}</span>
                  </button>
                ))}
              </div>
              <div role="tabpanel" className="mt-4 grid gap-2 text-left sm:grid-cols-2">
                {CATEGORIES[activeCategory].examples.map((ex) => (
                  <button
                    key={ex.titleKey}
                    type="button"
                    tabIndex={isExamplesOpen ? 0 : -1}
                    onClick={() => onExample(t(ex.promptKey as any))}
                    className="studio-example-entry block w-full border-b border-border/60 px-3 py-3 text-left transition-colors hover:border-primary/40 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
                  >
                    <span className="text-sm font-medium leading-snug text-foreground">
                      {t(ex.titleKey as any)}
                    </span>
                    <span className="mt-0.5 block text-xs leading-snug text-muted-foreground">
                      {t(ex.descKey as any)}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
