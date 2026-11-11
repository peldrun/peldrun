// frontend/src/lib/file-icon-mapper.ts

/**
 * Maps file extensions and well-known filenames to icon names understood
 * by the application's <Icon name="..." /> component.
 *
 * The mapping is intentionally kept data-only and side-effect free so it
 * can be reused by any consumer (workspace tree, diff viewer, artifacts
 * panel, etc.). Consumers receive `null` for unrecognized files and are
 * expected to fall back to a generic file icon.
 *
 * NOTE: The icon *values* (right-hand side) must match the names supported
 * by `@/components/Icon/Icon`. If your icon set uses different identifiers
 * (e.g. "js" instead of "javascript"), only the values below need to be
 * adjusted — the public API of this module stays the same.
 */

/** Extension (without the leading dot) -> icon name. */
const EXTENSION_MAP: Record<string, string> = {
  // ---- Markup & styling -------------------------------------------------
  html: "html",
  htm: "html",
  xhtml: "html",
  css: "css",
  scss: "sass",
  sass: "sass",
  less: "less",
  styl: "stylus",
  xml: "xml",
  svg: "svg",
  vue: "vue",
  svelte: "svelte",
  astro: "astro",
  hbs: "handlebars",
  handlebars: "handlebars",
  ejs: "ejs",
  pug: "pug",
  jade: "pug",
  twig: "twig",
  liquid: "liquid",
  njk: "nunjucks",

  // ---- JavaScript / TypeScript family -----------------------------------
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "react",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "react",
  json: "json",
  json5: "json",
  jsonc: "json",
  map: "json",
  webmanifest: "json",

  // ---- Data / config ----------------------------------------------------
  yaml: "yaml",
  yml: "yaml",
  toml: "toml",
  ini: "ini",
  cfg: "ini",
  conf: "ini",
  env: "env",
  csv: "csv",
  tsv: "csv",
  sql: "sql",
  graphql: "graphql",
  gql: "graphql",
  prisma: "prisma",
  proto: "protobuf",
  avsc: "json",

  // ---- Documentation ----------------------------------------------------
  md: "markdown",
  markdown: "markdown",
  mdx: "markdown",
  rst: "rst",
  adoc: "asciidoc",
  asciidoc: "asciidoc",
  txt: "text",
  pdf: "pdf",

  // ---- Python -----------------------------------------------------------
  py: "python",
  pyi: "python",
  pyw: "python",
  pyc: "python",
  pyo: "python",
  ipynb: "jupyter",
  whl: "python",
  egg: "python",

  // ---- Ruby -------------------------------------------------------------
  rb: "ruby",
  erb: "ruby",
  rake: "ruby",
  gemspec: "ruby",
  rbi: "ruby",

  // ---- Go ---------------------------------------------------------------
  go: "go",

  // ---- Rust -------------------------------------------------------------
  rs: "rust",

  // ---- JVM --------------------------------------------------------------
  java: "java",
  class: "java",
  jar: "java",
  kt: "kotlin",
  kts: "kotlin",
  scala: "scala",
  sc: "scala",
  groovy: "groovy",
  gradle: "gradle",
  clj: "clojure",
  cljs: "clojure",
  cljc: "clojure",
  edn: "clojure",

  // ---- C family ---------------------------------------------------------
  c: "c",
  h: "c",
  cc: "c++",
  cpp: "c++",
  cxx: "c++",
  "c++": "c++",
  hpp: "c++",
  hh: "c++",
  hxx: "c++",
  hxxpp: "c++",
  cs: "csharp",
  csx: "csharp",
  fs: "fsharp",
  fsx: "fsharp",
  fsi: "fsharp",
  vb: "visualbasic",
  vbs: "visualbasic",

  // ---- Objective-C / Swift ---------------------------------------------
  m: "objectivec",
  mm: "objectivec",
  swift: "swift",

  // ---- PHP --------------------------------------------------------------
  php: "php",
  phtml: "php",

  // ---- Shell / scripting ------------------------------------------------
  sh: "powershell",
  bash: "powershell",
  zsh: "azure-powershell",
  fish: "azure-powershell",
  ksh: "powershell",
  csh: "azure-powershell",
  ps1: "powershell",
  psm1: "powershell",
  psd1: "powershell",
  bat: "batch",
  cmd: "batch",

  // ---- Other languages --------------------------------------------------
  lua: "lua",
  pl: "perl",
  pm: "perl",
  t: "perl",
  r: "r",
  rmd: "r",
  jl: "julia",
  dart: "dart",
  ex: "elixir",
  exs: "elixir",
  eex: "elixir",
  erl: "erlang",
  hrl: "erlang",
  hs: "haskell",
  lhs: "haskell",
  ml: "ocaml",
  mli: "ocaml",
  nim: "nim",
  zig: "zig",
  sol: "solidity",
  vy: "vyper",
  cairo: "cairo",

  // ---- Assembly / hardware ---------------------------------------------
  asm: "assembly",
  s: "assembly",
  vhd: "vhdl",
  vhdl: "vhdl",
  v: "verilog",
  sv: "verilog",

  // ---- Infra / DevOps ---------------------------------------------------
  tf: "terraform",
  tfvars: "terraform",
  hcl: "hcl",
  nix: "nix",
  dockerfile: "docker",

  // ---- Archives ---------------------------------------------------------
  zip: "archive",
  tar: "archive",
  gz: "archive",
  tgz: "archive",
  bz2: "archive",
  xz: "archive",
  rar: "archive",
  "7z": "archive",

  // ---- Images -----------------------------------------------------------
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  ico: "image",
  bmp: "image",
  tiff: "image",
  tif: "image",
  avif: "image",

  // ---- Video / audio ----------------------------------------------------
  mp4: "video",
  mov: "video",
  webm: "video",
  mkv: "video",
  avi: "video",
  mp3: "audio",
  wav: "audio",
  ogg: "audio",
  flac: "audio",

  // ---- Fonts ------------------------------------------------------------
  ttf: "font",
  otf: "font",
  woff: "font",
  woff2: "font",
  eot: "font",

  // ---- Misc text --------------------------------------------------------
  log: "log",
  lock: "lock",
};

/** Exact filename (lowercased) -> icon name. Checked before extension map. */
const FILENAME_MAP: Record<string, string> = {
  // Git
  ".gitignore": "git",
  ".gitattributes": "git",
  ".gitmodules": "git",
  ".gitkeep": "git",
  ".gitconfig": "git",

  // Node / JS ecosystem
  "package.json": "npm",
  "package-lock.json": "npm",
  "yarn.lock": "yarn",
  "pnpm-lock.yaml": "pnpm",
  ".npmrc": "npm",
  ".nvmrc": "node",
  ".yarnrc": "yarn",
  ".yarnrc.yml": "yarn",
  "tsconfig.json": "typescript",
  "tsconfig.base.json": "typescript",
  "jsconfig.json": "javascript",

  // Tooling configs
  "vite.config.js": "vite",
  "vite.config.ts": "vite",
  "vite.config.mjs": "vite",
  "next.config.js": "next",
  "next.config.mjs": "next",
  "next.config.ts": "next",
  "nuxt.config.js": "nuxt",
  "nuxt.config.ts": "nuxt",
  "svelte.config.js": "svelte",
  "astro.config.mjs": "astro",
  "tailwind.config.js": "tailwind",
  "tailwind.config.ts": "tailwind",
  "postcss.config.js": "postcss",
  "postcss.config.cjs": "postcss",
  "babel.config.js": "babel",
  ".babelrc": "babel",
  ".browserslistrc": "browserslist",
  ".eslintrc": "eslint",
  ".eslintrc.js": "eslint",
  ".eslintrc.cjs": "eslint",
  ".eslintrc.json": "eslint",
  ".eslintrc.yml": "eslint",
  "eslint.config.js": "eslint",
  ".prettierrc": "prettier",
  ".prettierrc.js": "prettier",
  ".prettierrc.json": "prettier",
  ".prettierignore": "prettier",
  ".editorconfig": "editorconfig",

  // Env
  ".env": "env",
  ".env.local": "env",
  ".env.development": "env",
  ".env.development.local": "env",
  ".env.production": "env",
  ".env.production.local": "env",
  ".env.test": "env",
  ".env.test.local": "env",

  // Build tools
  makefile: "make",
  "gnumakefile": "make",
  cmakelists: "cmake",
  "cmakelists.txt": "cmake",
  "cmakecache.txt": "cmake",
  "meson.build": "meson",
  "build.gradle": "gradle",
  "build.gradle.kts": "gradle",
  "settings.gradle": "gradle",
  "settings.gradle.kts": "gradle",
  "pom.xml": "maven",
  "build.sbt": "scala",
  "build.zig": "zig",

  // Docker / K8s / Infra
  dockerfile: "docker",
  ".dockerignore": "docker",
  "docker-compose.yml": "docker",
  "docker-compose.yaml": "docker",
  "compose.yml": "docker",
  "compose.yaml": "docker",
  "chart.yaml": "helm",
  "values.yaml": "helm",
  "kustomization.yaml": "kubernetes",
  "kustomization.yml": "kubernetes",

  // Language-specific project files
  "cargo.toml": "rust",
  "cargo.lock": "rust",
  "go.mod": "go",
  "go.sum": "go",
  "go.work": "go",
  gemfile: "ruby",
  "gemfile.lock": "ruby",
  "rakefile": "ruby",
  "requirements.txt": "python",
  "setup.py": "python",
  "setup.cfg": "python",
  "pyproject.toml": "python",
  "pipfile": "python",
  "pipfile.lock": "python",
  "poetry.lock": "python",
  "tox.ini": "python",
  "pytest.ini": "python",
  "manage.py": "django",
  "composer.json": "composer",
  "composer.lock": "composer",
  "mix.exs": "elixir",
  "mix.lock": "elixir",
  "rebar.config": "erlang",
  "stack.yaml": "haskell",
  "package.swift": "swift",
  "podfile": "cocoapods",
  "podfile.lock": "cocoapods",
  "pubspec.yaml": "dart",
  "pubspec.lock": "dart",
  "deno.json": "deno",
  "deno.jsonc": "deno",
  "bun.lockb": "bun",

  // Docs & meta
  readme: "markdown",
  "readme.md": "markdown",
  "readme.mdx": "markdown",
  "readme.txt": "text",
  license: "license",
  "license.md": "license",
  "license.txt": "license",
  licence: "license",
  "licence.md": "license",
  changelog: "markdown",
  "changelog.md": "markdown",
  "changes.md": "markdown",
  contributing: "markdown",
  "contributing.md": "markdown",
  "code_of_conduct.md": "markdown",
  "security.md": "markdown",
};

/** Extracts the basename from a path using both separators. */
function basename(path: string): string {
  const normalized = path.replace(/\\/g, "/");
  const idx = normalized.lastIndexOf("/");
  return idx === -1 ? normalized : normalized.slice(idx + 1);
}

/**
 * Resolves the icon name (as understood by `<Icon name="..." />`) for a
 * given file path or filename.
 *
 * Returns `null` when:
 *   - the input is empty, OR
 *   - the file is not recognized as a programming language, markup,
 *     config, data, media or well-known tool file.
 *
 * Callers should render a generic file icon in the `null` case.
 */
export function getFileIconName(pathOrName: string): string | null {
  if (!pathOrName) return null;
  const base = basename(pathOrName).toLowerCase();
  if (!base) return null;

  // Exact filename match first (covers dotfiles and extensionless files
  // like "Dockerfile", "Makefile", ".gitignore", "package.json" ...).
  if (Object.prototype.hasOwnProperty.call(FILENAME_MAP, base)) {
    return FILENAME_MAP[base];
  }

  // Some files carry compound extensions (e.g. "x.test.ts", "a.d.ts",
  // "b.stories.tsx"). The final segment decides the language.
  const dotIdx = base.lastIndexOf(".");
  if (dotIdx <= 0 || dotIdx === base.length - 1) return null;

  const ext = base.slice(dotIdx + 1);
  return Object.prototype.hasOwnProperty.call(EXTENSION_MAP, ext)
    ? EXTENSION_MAP[ext]
    : null;
}

/** True when the file has a known icon mapping. */
export function hasKnownFileIcon(pathOrName: string): boolean {
  return getFileIconName(pathOrName) !== null;
}
