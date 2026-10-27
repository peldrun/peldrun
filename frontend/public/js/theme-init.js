// frontend/public/theme-init.js
//
// Anti-FOUC & Anti-Shift Execution.
//
// Loaded via <Script src="/theme-init.js" strategy="beforeInteractive" />
// in app/layout.tsx so it runs before the browser's first paint,
// while remaining external to the React tree. This avoids React 19's
// "script tag inside a component" warning entirely, and is compatible
// with Next.js 13/14/15/16 including Turbopack.
//
// Fallback defaults ("system" and "en") mirror storage/schema.ts. On
// a first visit (no cookie, no localStorage) these apply. On every
// subsequent visit the cookie or localStorage wins, so updating the
// schema defaults only matters for first-time users.
(function () {
    try {
        function cleanVal(v) {
            if (!v) return "";
            var res = decodeURIComponent(v).trim();
            while (res.indexOf("%22") !== -1 || res.indexOf("%20") !== -1) {
                try {
                    res = decodeURIComponent(res).trim();
                } catch (e) {
                    break;
                }
            }
            return res.replace(/^["']+|["']+$/g, "");
        }

        function getCookie(name) {
            var m = document.cookie.match(
                new RegExp("(?:^|; )" + name + "=([^;]*)")
            );
            return m ? cleanVal(m[1]) : "";
        }

        var t =
            getCookie("theme") ||
            cleanVal(localStorage.getItem("theme")) ||
            "system";
        var isDark =
            t === "dark" ||
            (t === "system" &&
                window.matchMedia("(prefers-color-scheme: dark)").matches);
        if (isDark) {
            document.documentElement.classList.add("dark");
            document.documentElement.classList.remove("light");
        } else {
            document.documentElement.classList.remove("dark");
            document.documentElement.classList.add("light");
        }

        var l =
            getCookie("locale") ||
            cleanVal(localStorage.getItem("locale")) ||
            cleanVal(localStorage.getItem("language")) ||
            "en";
        if (l) {
            var isRtl = l.indexOf("ar") === 0;
            document.documentElement.lang = l;
            document.documentElement.dir = isRtl ? "rtl" : "ltr";
        }
    } catch (e) {
        // Never block rendering if storage access is restricted
        // (Safari ITP, private browsing, hardened browsers).
    }
})();
