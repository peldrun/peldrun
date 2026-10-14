// scripts/generate-icons-manifest.mjs
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// --- Paths -------------------------------------------------------------------
const ICON_DIR = path.resolve(__dirname, '../public/icon');
const OUT_FILE = path.join(ICON_DIR, 'manifest.json');

// Adjust this if the docs folder lives elsewhere.
// Current: D:\AI\peldrun\frontend\scripts  →  ../../docs  →  D:\AI\peldrun\docs
const DOCS_DIR = path.resolve(__dirname, '../../docs');
const DOCS_FILE = path.join(DOCS_DIR, 'IconName.md');

// --- Variants ----------------------------------------------------------------
// Suffixes used to group icon variants under a shared base name.
const VARIANTS = ['dark', 'light', 'mono', 'color', 'wordmark'];

/**
 * Groups icon names by their base name and variant.
 * Example: 'anthropic-dark' and 'anthropic-light' → group 'anthropic'.
 */
function buildGroups(icons) {
    const groups = {};
    for (const name of icons) {
        const parts = name.split('-');
        const lastPart = parts[parts.length - 1];
        const baseName = VARIANTS.includes(lastPart)
            ? parts.slice(0, -1).join('-')
            : name;
        if (!groups[baseName]) groups[baseName] = [];
        groups[baseName].push(name);
    }
    return groups;
}

/**
 * Reads the icon directory and builds the manifest object.
 * Returns { manifest, icons } or null on failure.
 */
async function buildManifest() {
    let entries = [];
    try {
        entries = await fs.readdir(ICON_DIR);
    } catch (error) {
        console.warn(`⚠️  Icon directory not found: ${ICON_DIR}`);
        return null;
    }

    const icons = entries
        .filter((file) => file.toLowerCase().endsWith('.svg'))
        .map((file) => file.replace(/\.svg$/i, ''))
        .sort((a, b) => a.localeCompare(b));

    const manifest = {
        generatedAt: new Date().toISOString(),
        count: icons.length,
        icons,
        groups: buildGroups(icons),
    };

    return { manifest, icons };
}

/**
 * Writes manifest.json to the icon directory.
 */
export async function generate() {
    const result = await buildManifest();
    if (!result) return null;

    const { manifest, icons } = result;

    await fs.writeFile(OUT_FILE, JSON.stringify(manifest, null, 2), 'utf8');
    console.log(`✅ ${icons.length} icon(s) written to manifest.json`);

    return manifest;
}

/**
 * Generates (or fully regenerates) the markdown documentation
 * listing every icon, grouped by base name.
 */
export async function generateMarkdown() {
    const result = await buildManifest();
    if (!result) return;

    const { manifest, icons } = result;
    const { groups, count, generatedAt } = manifest;

    const groupedNames = Object.keys(groups).sort((a, b) => a.localeCompare(b));
    const now = new Date(generatedAt);

    const lines = [];

    // --- Header ---
    lines.push('# 🎨 Icon Inventory');
    lines.push('');
    lines.push('Auto-generated list of all SVG icons in `public/icon/`.');
    lines.push('');
    lines.push(`- **Total icons:** ${count}`);
    lines.push(`- **Generated at:** ${now.toISOString()}`);
    lines.push('- **Source:** `public/icon/manifest.json`');
    lines.push('');
    lines.push('> ⚠️ This file is generated automatically. Do not edit it by hand.');
    lines.push('> Run `npm run icons` to regenerate.');
    lines.push('');
    lines.push('---');
    lines.push('');

    // --- Grouped by base name ---
    lines.push('## 📦 Icons by Base Name');
    lines.push('');
    lines.push('| Base Name | Variants | Files |');
    lines.push('|---|---|---|');

    for (const base of groupedNames) {
        const variants = groups[base];
        // Detect which variant each file maps to (default when it equals base)
        const labeled = variants.map((v) => {
            if (v === base) return 'default';
            const suffix = v.slice(base.length + 1); // remove "base-"
            return suffix;
        });
        lines.push(
            `| \`${base}\` | ${labeled.join(', ')} | ${variants.length} |`
        );
    }

    lines.push('');
    lines.push('---');
    lines.push('');

    // --- Full alphabetical list ---
    lines.push('## 📋 Full Icon List');
    lines.push('');
    lines.push('| # | Icon Name | File |');
    lines.push('|---|---|---|');

    icons.forEach((name, i) => {
        lines.push(`| ${i + 1} | \`${name}\` | ${name}.svg |`);
    });

    lines.push('');
    lines.push('---');
    lines.push('');

    // --- Usage examples ---
    lines.push('## 🧪 Usage Examples');
    lines.push('');
    lines.push('```tsx');
    lines.push('// Default variant');
    lines.push(`<Icon name="${icons[0] ?? 'deepseek'}" size={32} />`);
    lines.push('');
    lines.push('// Resolve a variant automatically');
    lines.push('<Icon name="anthropic" variant="dark" size={32} />');
    lines.push('// → tries "anthropic-dark.svg", then falls back to "anthropic.svg"');
    lines.push('');
    lines.push('// Inline icon (inherits currentColor)');
    lines.push('<InlineIcon name="nvidia-mono" forceCurrentColor className="text-blue-600" />');
    lines.push('');
    lines.push('// Icon picker dropdown');
    lines.push('<IconPicker value={icon} onChange={setIcon} />');
    lines.push('```');
    lines.push('');

    // --- Ensure docs dir exists, then write ---
    await fs.mkdir(DOCS_DIR, { recursive: true });
    await fs.writeFile(DOCS_FILE, lines.join('\n'), 'utf8');

    console.log(
        `📝 ${icons.length} icon(s) written to ${path.relative(process.cwd(), DOCS_FILE)}`
    );
}

// --- Entry point -------------------------------------------------------------
// Runs both generators when the script is executed directly.
async function main() {
    await generate();
    await generateMarkdown();
}

main().catch((err) => {
    console.error('❌ Generation failed:', err);
    process.exit(1);
});
