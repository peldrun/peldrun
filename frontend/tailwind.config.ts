import type { Config } from "tailwindcss";

const config: Config = {
  darkMode: ["class"],
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    container: {
      center: true,
      padding: "2rem",
      screens: {
        "2xl": "1400px",
      },
    },
    extend: {
      fontFamily: { 
        sans: ["var(--font-sans)", "system-ui", "sans-serif"],
        serif: ["var(--font-serif)", "Georgia", "serif"],
        arabic: ["var(--font-arabic)", "sans-serif"],
        mono: ["var(--font-mono)", "monospace"],
      },
     fontSize: {
        'xxs':   ['0.775rem',  { lineHeight: '1.5' }],   // 14px
        'xs':   ['0.875rem',  { lineHeight: '1.5' }],   // 14px
        'sm':   ['1rem',      { lineHeight: '1.6' }],   // 16px
        'base': ['1.125rem',  { lineHeight: '1.7' }],   // 18px
        'lg':   ['1.25rem',   { lineHeight: '1.7' }],   // 20px
        'xl':   ['1.5rem',    { lineHeight: '1.7' }],   // 24px
        '2xl':  ['1.75rem',   { lineHeight: '1.6' }],   // 28px
        '3xl':  ['2rem',      { lineHeight: '1.5' }],   // 32px
        '4xl':  ['2.5rem',    { lineHeight: '1.4' }],   // 40px
        '5xl':  ['3rem',      { lineHeight: '1.2' }],   // 48px
        '6xl':  ['3.75rem',   { lineHeight: '1.1' }],   // 60px
        '7xl':  ['4.5rem',    { lineHeight: '1.1' }],   // 72px
        '8xl':  ['6rem',      { lineHeight: '1' }],     // 96px
        '9xl':  ['8rem',      { lineHeight: '1' }],     // 128px
      },
      colors: {
        border: "hsl(var(--border))",
        input: "hsl(var(--input))",
        ring: "hsl(var(--ring))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        primary: {
          DEFAULT: "hsl(var(--primary))",
          foreground: "hsl(var(--primary-foreground))",
        },
        secondary: {
          DEFAULT: "hsl(var(--secondary))",
          foreground: "hsl(var(--secondary-foreground))",
        },
        destructive: {
          DEFAULT: "hsl(var(--destructive))",
          foreground: "hsl(var(--destructive-foreground))",
        },
        muted: {
          DEFAULT: "hsl(var(--muted))",
          foreground: "hsl(var(--muted-foreground))",
        },
        accent: {
          DEFAULT: "hsl(var(--accent))",
          foreground: "hsl(var(--accent-foreground))",
        },
        popover: {
          DEFAULT: "hsl(var(--popover))",
          foreground: "hsl(var(--popover-foreground))",
        },
        card: {
          DEFAULT: "hsl(var(--card))",
          foreground: "hsl(var(--card-foreground))",
        },

        black:{
          DEFAULT: "hsl(var(--black))",
          foreground: "hsl(var(--black-foreground))",
        },
        
        

        

        custom: "var(--custom-color)",
        sidebg: "var(--sidebg)",
        
        peldrun: {
          black: "#34322D",
          gray: "#F8F8F8",
          white: "#FFFFFF",
          darkBg: "#0F0F0E",
          darkSurface: "#1A1918",
          darkElevated: "#242321",
          darkBorder: "#2E2D2B",
          darkText: "#F5F5F4",
          darkMuted: "#A8A7A3",
          accent: "#305CDE",
          secondary: "#59B5F7",
          success: "#10B981",
          warning: "#F59E0B",
          error: "#EF4444",
          info: "#3B82F6",
        },
      },
      borderRadius: {
        none: "0px",
        xs: "2px",
        sm: "4px",
        md: "8px",
        lg: "12px",
        xl: "16px",
        full: "9999px",
      },
      boxShadow: {
        "peldrun-xs": "0 1px 2px rgba(0, 0, 0, 0.02)",
        "peldrun-sm": "0 1px 3px rgba(0, 0, 0, 0.04)",
        "peldrun-md": "0 4px 6px rgba(0, 0, 0, 0.04)",
        "peldrun-lg": "0 4px 20px rgba(0, 0, 0, 0.06)",
      },
    },
  },
  plugins: [],
};

export default config;
