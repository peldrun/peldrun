// frontend/src/lib/modelVendor.ts

/**
 * Model Vendor Resolution
 *
 * Extracts the ORIGIN vendor of a model from its raw identifier.
 * A model running on LM Studio might actually be "DeepSeek R1" —
 * this module tells you that so the UI can show a small vendor badge
 * on top of the provider icon.
 *
 * The lookup is purely name-based (no network). It handles:
 *   - Plain names:        "deepseek-r1-distill-qwen-7b"
 *   - Namespaced IDs:     "models/qwen3-vl-8b-instruct"
 *   - Cloudflare format:  "@cf/google/gemma-4-26b-a4b-it"
 *   - HuggingFace paths:  "meta-llama/Llama-3.1-8B-Instruct"
 *   - Versioned suffixes: "claude-3-5-sonnet-20241022"
 *
 * Unknown vendors fall back to the first "word" of the identifier,
 * title-cased, with `iconName: null` so the caller can hide the badge.
 */

export interface ModelVendorInfo {
  /** Lowercase stable identifier, e.g. "deepseek", "qwen", "google". */
  vendorId: string | null;
  /** Human-readable vendor name for tooltips, e.g. "DeepSeek". */
  vendorName: string;
  /**
   * Icon registry name. `null` when the vendor has no known icon —
   * the caller should NOT attempt to render an `Icon` in that case.
   */
  iconName: string | null;
  /** Confidence in the match. */
  confidence: "exact" | "inferred" | "unknown";
}

interface VendorRule {
  id: string;
  name: string;
  icon: string | null;
  /** Substrings to search for in the normalized model name (lowercase). */
  keywords: string[];
}

/**
 * Vendor rules, ordered by specificity.
 *
 * Keep this list SHORT and PRECISE — a false positive is worse than
 * falling back to `unknown` because the UI will show a wrong vendor
 * badge that the user cannot dismiss.
 */
const VENDOR_RULES: VendorRule[] = [
  // ── LLM API providers ─────────────────────────────────────────
  {
    id: "deepseek",
    name: "DeepSeek",
    icon: "deepseek",
    keywords: ["deepseek"],
  },
  {
    id: "anthropic",
    name: "Anthropic (Claude)",
    icon: "anthropic",
    keywords: ["claude"],
  },
  {
    id: "openai",
    name: "OpenAI",
    icon: "openai",
    keywords: [
      "gpt-",
      "gpt_",
      "gpt4",
      "gpt3",
      "chatgpt",
      "davinci",
      "o1-",
      "o3-",
      "o4-",
      "text-embedding",
    ],
  },
  {
    id: "gemini",
    name: "gemini",
    icon: "gemini",
    keywords: ["gemma", "gemini", "palm-", "palm_", "bison"],
  },
  {
    id: "meta",
    name: "Meta (Llama)",
    icon: "metaai",
    keywords: ["llama", "meta-llama", "code-llama", "codellama"],
  },
  {
    id: "mistral",
    name: "Mistral AI",
    icon: "mistral",
    keywords: ["mistral", "mixtral", "codestral", "pixtral", "ministral"],
  },
  {
    id: "microsoft",
    name: "Microsoft (Phi)",
    icon: "microsoft-365-copilot",
    keywords: ["phi-", "phi_", "phi3", "phi4", "phi2"],
  },
  {
    id: "cohere",
    name: "Cohere",
    icon: "cohere",
    keywords: ["command-r", "command_", "command-", "c4ai-command", "cohere"],
  },
  {
    id: "xai",
    name: "xAI (Grok)",
    icon: "xai",
    keywords: ["grok"],
  },
  {
    id: "nvidia",
    name: "NVIDIA (Nemotron)",
    icon: "nvidia",
    keywords: ["nemotron", "nvidia"],
  },
  {
    id: "gigachat",
    name: "GigaChat (Sber)",
    icon: "gigachat",
    keywords: ["gigachat", "giga-chat"],
  },
  {
    id: "cloudflare",
    name: "Cloudflare AI",
    icon: "cloudflare",
    keywords: ["@cf/", "cloudflare"],
  },
  {
    id: "moonshot",
    name: "Moonshot (Kimi)",
    icon: null,
    keywords: ["moonshot", "kimi"],
  },
  {
    id: "zhipu",
    name: "Zhipu (GLM)",
    icon: "zhipu",
    keywords: ["glm-", "glm_", "chatglm"],
  },
  {
    id: "01-ai",
    name: "01.AI (Yi)",
    icon: null,
    keywords: ["yi-", "yi_", "yi-"],
  },
  {
    id: "tii",
    name: "TII (Falcon)",
    icon: null,
    keywords: ["falcon"],
  },
  {
    id: "lmsys",
    name: "LMSYS (Vicuna)",
    icon: null,
    keywords: ["vicuna"],
  },
  {
    id: "huggingface",
    name: "HuggingFace (SmolLM)",
    icon: "huggingface",
    keywords: ["smollm"],
  },
  {
    id: "amazon",
    name: "Amazon (Nova / Titan)",
    icon: "amazon-q",
    keywords: ["nova-", "nova_", "titan-", "titan_", "amazon."],
  },
  {
    id: "perplexity",
    name: "Perplexity",
    icon: "perplexity",
    keywords: ["pplx", "perplexity"],
  },
  {
    id: "nous",
    name: "Nous Research (Hermes)",
    icon: null,
    keywords: ["hermes", "nous-"],
  },
  {
    id: "allenai",
    name: "Allen Institute (OLMo)",
    icon: null,
    keywords: ["olmo"],
  },
  {
    id: "stability",
    name: "Stability AI",
    icon: null,
    keywords: ["stablelm", "stable-lm", "stable-code"],
  },
  {
    id: "databricks",
    name: "Databricks (DBRX)",
    icon: null,
    keywords: ["dbrx"],
  },
  {
    id: "ai21",
    name: "AI21 (Jamba)",
    icon: null,
    keywords: ["jamba"],
  },
  {
    id: "baidu",
    name: "Baidu (ERNIE)",
    icon: null,
    keywords: ["ernie"],
  },
  {
    id: "tencent",
    name: "Tencent (Hunyuan)",
    icon: null,
    keywords: ["hunyuan"],
  },
  // ── Qwen after most others because "qwen" appears as a distill
  //     suffix in DeepSeek variants (e.g. deepseek-r1-distill-qwen-7b).
  //     Matching "deepseek" first prevents this misattribution.
  {
    id: "qwen",
    name: "Alibaba (Qwen)",
    icon: "qwen",
    keywords: ["qwen"],
  },
];

/**
 * Normalize a raw model ID to a lowercase plain string with the
 * namespace prefixes ("models/", "@cf/", "huggingface/") stripped.
 *
 * We keep the original string as-is for substring matching — stripping
 * prefixes would break the `@cf/` Cloudflare rule, and the rule list
 * is written to tolerate prefixes naturally.
 */
function normalize(raw: string): string {
  return raw.trim().toLowerCase();
}

/**
 * Extract the first "word" from an identifier, using `-`, `_`, `/`, and
 * `.` as word separators. Used as a last-resort vendor name.
 */
function firstWord(raw: string): string | null {
  const cleaned = raw.replace(/^[@/]+/, "").trim();
  if (!cleaned) return null;
  const match = cleaned.match(/^[A-Za-z][A-Za-z0-9]*/);
  if (!match) return null;
  const word = match[0];
  // Reject two-letter noise like "is", "of", "to" — they are almost
  // certainly part of a longer identifier, not a vendor name.
  if (word.length < 3) return null;
  return word;
}

/**
 * Resolve the origin vendor of a model.
 *
 * @param modelName  The full model identifier, e.g.
 *                   "deepseek-r1-distill-qwen-7b" or
 *                   "@cf/google/gemma-4-26b-a4b-it".
 * @returns Info about the vendor, or a generic "unknown" record when
 *          nothing matched.
 */
export function inferModelVendor(
  modelName: string | null | undefined
): ModelVendorInfo {
  if (!modelName || typeof modelName !== "string" || !modelName.trim()) {
    return {
      vendorId: null,
      vendorName: "Unknown",
      iconName: null,
      confidence: "unknown",
    };
  }

  const normalized = normalize(modelName);

  // Pass 1: substring match against the rule list (ordered).
  for (const rule of VENDOR_RULES) {
    for (const kw of rule.keywords) {
      if (normalized.includes(kw)) {
        return {
          vendorId: rule.id,
          vendorName: rule.name,
          iconName: rule.icon,
          confidence: "exact",
        };
      }
    }
  }

  // Pass 2: use the first identifier segment as a best-effort guess.
  // This yields a readable "vendor name" even when no icon is known.
  const fallback = firstWord(normalized);
  if (fallback) {
    const titleCase =
      fallback.charAt(0).toUpperCase() + fallback.slice(1);
    return {
      vendorId: fallback,
      vendorName: titleCase,
      iconName: null,
      confidence: "inferred",
    };
  }

  return {
    vendorId: null,
    vendorName: "Unknown",
    iconName: null,
    confidence: "unknown",
  };
}
