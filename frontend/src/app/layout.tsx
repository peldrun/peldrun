import type { Metadata, Viewport } from "next";
import { DM_Sans, Libre_Baskerville, Cairo } from "next/font/google";
import { getServerStorageSnapshot } from "@/lib/storage/server";
import { StorageProvider } from "@/components/providers/storage-provider";
import { ThemeProvider } from "@/components/layout/theme-provider";
import { SourceControlSettingsProvider } from "@/features/source-control/state/sourceControlSettingsProvider";
import { GenerativeUIBackendSync } from "@/components/generative-ui/backend-sync";
import { I18nProvider } from "@/components/providers/i18n-provider";
import { AppShell } from "@/components/layout/app-shell";
import { ToastContainer } from "@/components/ui/ToastNotification";
import Script from "next/script";

import "@/app/globals.css";

const dmSans = DM_Sans({
  subsets: ["latin"],
  variable: "--font-sans",
  display: "swap",
});

const libreBaskerville = Libre_Baskerville({
  subsets: ["latin"],
  weight: ["400", "700"],
  variable: "--font-serif",
  display: "swap",
});

const cairo = Cairo({
  subsets: ["arabic", "latin"],
  variable: "--font-arabic",
  display: "swap",
});

export const metadata: Metadata = {
  title: "peldrunWeb - Open Autonomous Agent",
  description: "Next-generation Web Interface for Autonomous AI Agents",
  icons: {
    icon: "/favicon.ico",
  },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#FFFFFF" },
    { media: "(prefers-color-scheme: dark)", color: "#0F0F0E" },
  ],
};

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const cookieSnapshot = await getServerStorageSnapshot();
  const sidebarState = cookieSnapshot.sidebar_state ?? "expanded";
  const theme = cookieSnapshot.theme ?? "system";
  const locale = cookieSnapshot.locale ?? "en";
  const isDark = theme === "dark";
  const isRtl = locale.startsWith("ar");

  return (
    <html
      lang={locale}
      dir={isRtl ? "rtl" : "ltr"}
      data-theme={theme}
      suppressHydrationWarning
      className={`${isDark ? "dark" : ""} ${dmSans.variable} ${libreBaskerville.variable}${cairo.variable}`}
    >
      <head>
        {/*
          Anti-FOUC & Anti-Shift Execution (0ms before first paint).

          The theme/locale initializer lives in /public/theme-init.js
          and is loaded as an EXTERNAL script via next/script. This is
          intentional:

            - React 19 warns about any <script> element rendered inside
              a React component (inline or dangerouslySetInnerHTML),
              and this warning is still emitted for inline
              beforeInteractive scripts in Next.js 16 + Turbopack.
            - Loading the same code from `/public` avoids the warning
              entirely while preserving the anti-FOUC guarantee: Next.js
              emits the <script src> tag in <head> BEFORE hydration, and
              the browser executes it synchronously before first paint.

          The fallback defaults ("system" and "en") match storage/schema.ts.
          Cookie and localStorage always take precedence when present,
          so the fallback only matters on a truly first visit.
        */}
        <Script
          id="peldrun-theme-init"
          src="/js/theme-init.js"
          strategy="beforeInteractive"
        />
      </head>
      <body
        data-sidebar={sidebarState}
        className="min-h-screen bg-background text-foreground font-sans antialiased selection:bg-peldrun-accent selection:text-white"
      >
        <StorageProvider cookieSnapshot={cookieSnapshot}>
          <ThemeProvider
            defaultTheme={theme}
            enableSystem
          >

            <SourceControlSettingsProvider />
            <GenerativeUIBackendSync />

            <I18nProvider initialLocale={locale}>
              <AppShell>{children}</AppShell>
            </I18nProvider>

                        {/*
              Toast container: a single instance mounted at the root
              so any component (including non-React code that calls
              `showToast.success/error/...`) can raise a toast. It
              renders into a fixed-position layer above everything
              else and stays mounted for the whole session.
            */}
            <ToastContainer />

          </ThemeProvider>
        </StorageProvider>
      </body>
    </html>
  );
}
