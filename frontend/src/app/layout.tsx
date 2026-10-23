import type { Metadata, Viewport } from "next";
import { DM_Sans, Libre_Baskerville, Cairo } from "next/font/google";
import { getServerStorageSnapshot } from "@/lib/storage/server";
import { StorageProvider } from "@/components/providers/storage-provider";
import { ThemeProvider } from "@/components/layout/theme-provider";
import { SourceControlSettingsProvider } from "@/features/source-control/state/sourceControlSettingsProvider";
import { I18nProvider } from "@/components/providers/i18n-provider";
import { AppShell } from "@/components/layout/app-shell";
import { ToastContainer } from "@/components/ui/ToastNotification";

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
        {/* Anti-FOUC & Anti-Shift Execution (0ms before first paint) */}
        <script
          dangerouslySetInnerHTML={{
            __html: `
              (function() {
                try {
                  function cleanVal(v) {
                    if (!v) return '';
                    var res = decodeURIComponent(v).trim();
                    while (res.indexOf('%22') !== -1 || res.indexOf('%20') !== -1) {
                      try { res = decodeURIComponent(res).trim(); } catch(e) { break; }
                    }
                    return res.replace(/^["']+|["']+$/g, '');
                  }
                  function getCookie(name) {
                    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
                    return m ? cleanVal(m[1]) : '';
                  }

                  var t = getCookie('theme') || cleanVal(localStorage.getItem('theme')) || '${theme}';
                  var isDark = t === 'dark' || (t === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
                  if (isDark) {
                    document.documentElement.classList.add('dark');
                    document.documentElement.classList.remove('light');
                  } else {
                    document.documentElement.classList.remove('dark');
                    document.documentElement.classList.add('light');
                  }

                  var l = getCookie('locale') || cleanVal(localStorage.getItem('locale')) || cleanVal(localStorage.getItem('language')) || '${locale}';
                  if (l) {
                    var isRtl = l.indexOf('ar') === 0;
                    document.documentElement.lang = l;
                    document.documentElement.dir = isRtl ? 'rtl' : 'ltr';
                  }
                } catch(e) {}
              })();
            `,
          }}
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
