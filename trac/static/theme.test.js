import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const stylesheet = readFileSync(resolve(process.cwd(), "trac/static/dashboard.css"), "utf8");

function readTokens(selector) {
    const selectorStart = stylesheet.indexOf(selector);
    const blockStart = stylesheet.indexOf("{", selectorStart);
    const blockEnd = stylesheet.indexOf("}", blockStart);
    const declarations = stylesheet.slice(blockStart + 1, blockEnd);
    return Object.fromEntries(
        [...declarations.matchAll(/--([\w-]+):\s*([^;]+);/g)]
            .map(([, name, value]) => [name, value.trim()])
    );
}

describe("dashboard theme tokens", () => {
    it("uses the requested neutral palette in dark mode", () => {
        expect(readTokens(':root[data-theme="dark"]')).toMatchObject({
            bg: "#000000",
            surface: "#111111",
            "surface-subtle": "#181818",
            "surface-elevated": "#181818",
            "surface-hover": "#1C1C1C",
            border: "#262626",
            text: "#FFFFFF",
            "text-muted": "#A1A1AA",
            "text-subtle": "#71717A",
            "sidebar-bg": "#080808",
            accent: "#22D3EE",
            "accent-hover": "#06B6D4",
            success: "#34D399",
            high: "#FBBF24",
            critical: "#F87171"
        });
    });

    it("retains the existing light theme colors", () => {
        expect(readTokens(":root")).toMatchObject({
            bg: "#F6F7F9",
            surface: "#FFFFFF",
            text: "#101828",
            "text-muted": "#475467",
            accent: "#4F46E5"
        });
    });
});
