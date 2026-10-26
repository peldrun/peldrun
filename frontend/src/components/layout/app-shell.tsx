"use client";

import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  MessageSquare,
  Folder,
  FolderOpen,
  Settings,
  Store,
  History,
  PanelLeftClose,
  PanelLeft,
} from "lucide-react";
import "@/lib/vault";

import { GlobalTooltip } from "@/components/ui/global-tooltip";

import { Header } from "@/components/layout/header";
import { Sidebar } from "@/components/layout/sidebar";
import { ThemeToggle } from "@/components/ui/theme-toggle";
import { LanguageToggle } from "@/components/ui/language-toggle";
import { useAppStorage } from "@/hooks/use-app-storage";
import { SystemHealthPill } from '@/components/layout/SystemHealthPill';


const navItems = [
  { href: "/chat", icon: MessageSquare, label: "Chat" },
  { href: "/projects", icon: FolderOpen, label: "Projects" },
  { href: "/files", icon: Folder, label: "Files" },
  { href: "/history", icon: History, label: "History" },


];

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [sidebarState, setSidebarState] = useAppStorage("sidebar_state");
  const isSidebarOpen = sidebarState === "expanded";

  const toggleSidebar = () => {
    setSidebarState(isSidebarOpen ? "collapsed" : "expanded");
  };

  if (pathname === "/setup") {
    return (
      <div className="min-h-screen w-screen bg-background text-foreground font-sans overflow-x-hidden selection:bg-primary/25">
        <GlobalTooltip />
        {children}
      </div>


    );
  }

  const isSettingsActive = pathname.startsWith("/settings");

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-background text-foreground font-sans">
      <GlobalTooltip />
      <aside className="w-16 h-full flex flex-col items-center justify-between py-3 border-r border-border bg-custom backdrop-blur-md z-30 shrink-0 select-none">
        <div className="flex flex-col items-center gap-3">
          <Link
            href="/chat"
            className="w-10 h-10 rounded-sm bg-transparent flex items-center justify-center p-1.5 transition-all group"
            title="peldrun Web"
          >
            <img
                src="/peldrun-logo.svg"
                alt="peldrun Web Logo"
                className="w-full h-full object-contain group-hover:scale-105 transition-transform bg-transparent
                          [[data-theme='dark']_&]:hidden"
              />
            <img
                src="/peldrun-logo-light.svg"
                alt="peldrun Web Logo"
                className="w-full h-full object-contain group-hover:scale-105 transition-transform bg-transparent
                          hidden [[data-theme='dark']_&]:block"
              />

          </Link>

          <button
            type="button"
            onClick={toggleSidebar}
            title={isSidebarOpen ? "Collapse Sidebar" : "Expand Sidebar"}
            className="w-10 h-10 flex items-center justify-center rounded-xs text-black hover:bg-muted hover:text-foreground transition-all cursor-pointer"
          >
            {isSidebarOpen ? <PanelLeftClose size={18} /> : <PanelLeft size={18} />}
          </button>

          <nav className="flex flex-col gap-1.5">
            {navItems.map((item) => {
              const Icon = item.icon;
              const active =
                pathname === item.href ||
                (item.href !== "/chat" && pathname.startsWith(item.href)) ||
                (item.href === "/chat" && pathname.startsWith("/chat"));

              return (
                <Link
                  key={item.href}
                  href={item.href}
                  title={item.label}
                  className={`w-10 h-10 rounded-xs flex items-center justify-center transition-all ${
                    active
                      ? "bg-muted text-black shadow-peldrun-xs"
                      : "text-black hover:bg-muted hover:text-foreground"
                  }`}
                >
                  <Icon size={18} />
                </Link>
              );
            })}
          </nav>
        </div>

        <div className="flex flex-col items-center gap-2">

          <Link
            href="/stores"
            title="Stores"
            className={`w-10 h-10 mb-1 flex items-center justify-center rounded-xs transition-all ${
              isSettingsActive
                ? "bg-transparent text-black shadow-peldrun-xs"
                : "text-black hover:bg-muted hover:text-foreground"
            }`}
          >
            <Store size={18} />
          </Link>


          <Link
            href="/settings"
            title="Settings"
            className={`w-10 h-10 mb-1 flex items-center justify-center rounded-xs transition-all ${
              isSettingsActive
                ? "bg-transparent text-black shadow-peldrun-xs"
                : "text-black hover:bg-muted hover:text-foreground"
            }`}
          >
            <Settings size={18} />
          </Link>

          <div className="flex flex-col items-center gap-2 pt-2 border-t border-border/60">
            <ThemeToggle />
            <LanguageToggle compact={true} />
          </div>

          <div className="flex flex-col items-center gap-2 pt-2 border-t border-border/60">

             <div className="SystemHealthPill">
               <SystemHealthPill />
              </div>

          </div>

        </div>
      </aside>

      {isSidebarOpen && <Sidebar />}

      <div className="flex-1 flex flex-col min-w-0 overflow-hidden bg-background">
        <Header />
        <main className="flex-1 min-h-0 overflow-hidden relative flex">
          {children}
        </main>
      </div>
    </div>
  );
}

export default AppShell;
