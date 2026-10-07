export type SettingsTab =
  | "llm"
  | "usage"
  | "browser"
  | "search"
  | "sandbox"
  | "mcp"
  | "system";

export type HubSubTab = "overview" | "budgets" | "lmstudio" | "cloud" | "custom";

export type ApiModeType =
  | "Auto-detect"
  | "Chat Completions"
  | "Responses API"
  | "Anthropic Messages";

export type ProviderConnectionStatus = "online" | "offline" | "untested";

export interface CustomEndpoint {
  id: string;
  name: string;
  providerId: string;
  endpointUrl: string;
  apiMode: ApiModeType;
  defaultModel: string;
  contextWindow: number | string;
  maxOutputTokens?: number;
  apiKey: string;
  status: ProviderConnectionStatus;
  latency?: number;
  lastError?: string;
  useForNewChats?: boolean;
  savedModels?: string[];
}

export interface CloudProviderVaultItem {
  id: string;
  name: string;
  type: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  popularModels: string[];
  badge: "Cloud" | "Enterprise" | "Local";
  keyPrefixHint: string;
  status: ProviderConnectionStatus;
  latency?: number;
  lastError?: string;
  savedModels?: string[];
  contextWindow?: number;
  maxOutputTokens?: number;
}

export interface LMStudioSettings {
  savedModels?: string[];
  provider: string;
  baseUrl: string;
  apiKey: string;
  model: string;
  status: ProviderConnectionStatus;
  latency?: number;
  lastError?: string;
}

export interface OllamaSettings {
  baseUrl: string;
  model: string;
  status: ProviderConnectionStatus;
  latency?: number;
  lastError?: string;
  savedModels: string[];
}

export const INITIAL_OLLAMA_SETTINGS: OllamaSettings = {
  baseUrl: "http://127.0.0.1:11434",
  model: "",
  status: "untested",
  savedModels: [],
};

export const INITIAL_CLOUD_PROVIDERS: CloudProviderVaultItem[] = [
  {
    id: "google",
    name: "Google Gemini",
    type: "",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
    apiKey: "",
    model: "gemini-2.0-flash",
    popularModels: [
      "gemini-2.0-flash",
      "gemini-1.5-pro",
      "gemini-1.5-flash",
      "gemini-2.5-pro-preview-05-06",
    ],
    badge: "Cloud",
    keyPrefixHint: "Google Gemini keys start with 'AIzaSy...'",
    status: "untested",
    savedModels: [],
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    type: "",
    baseUrl: "https://api.deepseek.com",
    apiKey: "",
    model: "deepseek-chat",
    popularModels: ["deepseek-chat", "deepseek-reasoner"],
    badge: "Cloud",
    keyPrefixHint: "DeepSeek keys start with 'sk-'",
    status: "untested",
    savedModels: [],
  },
  {
    id: "openai",
    name: "OpenAI",
    type: "",
    baseUrl: "https://api.openai.com/v1",
    apiKey: "",
    model: "gpt-4o",
    popularModels: ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"],
    badge: "Cloud",
    keyPrefixHint: "OpenAI keys start with 'sk-proj-'",
    status: "untested",
    savedModels: [],
  },
  {
    id: "anthropic",
    name: "Anthropic Claude",
    type: "anthropic",
    baseUrl: "https://api.anthropic.com/v1",
    apiKey: "",
    model: "claude-3-5-sonnet-20241022",
    popularModels: [
      "claude-3-5-sonnet-20241022",
      "claude-3-5-haiku-20241022",
      "claude-3-opus-20240229",
    ],
    badge: "Cloud",
    keyPrefixHint: "Anthropic keys start with 'sk-ant-'",
    status: "untested",
    savedModels: [],
  },
];

export const INITIAL_LMSTUDIO_SETTINGS: LMStudioSettings = {
  provider: "lmstudio",
  baseUrl: "http://127.0.0.1:1234/v1",
  apiKey: "",
  model: "qwen3-vl-8b-instruct",
  status: "untested",
  savedModels: ["qwen3-vl-8b-instruct"],
};

export interface FullAppConfig {
  llm: {
    provider: string;
    provider_name: string;
    model: string;
    base_url: string;
    api_key: string;
    max_tokens: number;
    context_window?: number;
    max_output_tokens?: number;
    temperature: number;
    api_type: string;
  };
  llm_vision: {
    provider: string;
    provider_name: string;
    model: string;
    base_url: string;
    api_key: string;
    max_tokens: number;
    context_window?: number;
    max_output_tokens?: number;
    temperature: number;
  };
  browser: {
    headless: boolean;
    disable_security: boolean;
    chrome_instance_path: string;
    cdp_url: string;
    wss_url: string;
    max_content_length: number;
    proxy: { server: string; username: string; password: string };
  };
  search: {
    engine: string;
    fallback_engines: string[];
    retry_delay: number;
    max_retries: number;
    lang: string;
    country: string;
  };
  sandbox: {
    use_sandbox: boolean;
    image: string;
    work_dir: string;
    memory_limit: string;
    cpu_limit: number;
    timeout: number;
    network_enabled: boolean;
  };
  daytona: {
    daytona_api_key: string;
    daytona_server_url: string;
    daytona_target: string;
    sandbox_image_name: string;
    VNC_password: string;
  };
  mcp: { server_reference: string };
  runflow: { use_data_analysis_agent: boolean };
}


export interface ModelBudgetItemConfig {
  context_window: number;
  max_output_tokens: number;
  provider?: string;
  provider_name?: string;
}

export type ModelBudgetsMap = Record<string, ModelBudgetItemConfig>;