// scripts/generate-icons-manifest.mjs
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ICON_DIR = path.resolve(__dirname, '../public/icon');
const OUT_FILE = path.join(ICON_DIR, 'manifest.json');

// Defines the supported variant suffixes for icon grouping
const VARIANTS = ['dark', 'light', 'mono', 'color', 'wordmark'];

/**
 * Groups icon names by their base name and variant.
 * Example: 'anthropic-dark' and 'anthropic-light' both belong to the 'anthropic' group.
 */
function buildGroups(icons) {
    const groups = {};
    for (const name of icons) {
        const parts = name.split('-');
        const lastPart = parts[parts.length - 1];
        const baseName = VARIANTS.includes(lastPart) ? parts.slice(0, -1).join('-') : name;
        if (!groups[baseName]) {
            groups[baseName] = [];
        }
        groups[baseName].push(name);
    }
    return groups;
}

export async function generate() {
    let entries = [];
    try {
        entries = await fs.readdir(ICON_DIR);
    } catch (error) {
        console.warn(`⚠️  Icon directory not found: ${ICON_DIR}`);
        return;
    }

    // Filters for SVG files, removes the extension, and sorts alphabetically
    const icons = entries
        .filter((file) => file.toLowerCase().endsWith('.svg'))
        .map((file) => file.replace(/\.svg$/i, ''))
        .sort((a, b) => a.localeCompare(b));

    const manifest = {
        generatedAt: new Date().toISOString(),
        count: icons.length,
        icons: icons,
        groups: buildGroups(icons),
    };

    await fs.writeFile(OUT_FILE, JSON.stringify(manifest, null, 2), 'utf8');
    console.log(`✅ ${icons.length} icon(s) found and written to manifest.`);
}

// Executes the generation immediately when the script is run
generate();
