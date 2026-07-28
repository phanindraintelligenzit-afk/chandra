"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Server, ShieldCheck, PlayCircle, Activity, History, LayoutDashboard, Menu, X } from "lucide-react";
import { useState } from "react";

const NAV_ITEMS = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/aws-tasks", label: "AWS Tasks", icon: Server },
  { href: "/aws-permissions", label: "AWS Permissions", icon: ShieldCheck },
  { href: "/execution-review", label: "Execution Review", icon: PlayCircle },
  { href: "/deployment", label: "Deployment", icon: Activity },
  { href: "/executions", label: "Execution History", icon: History },
];

export default function AppNav() {
  const pathname = usePathname();
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <>
      {/* Mobile hamburger */}
      <button
        onClick={() => setMobileOpen(!mobileOpen)}
        className="fixed top-4 left-4 z-50 p-2 bg-gray-900 border border-gray-800 rounded-lg text-gray-400 hover:text-white lg:hidden"
      >
        {mobileOpen ? <X className="w-5 h-5" /> : <Menu className="w-5 h-5" />}
      </button>

      {/* Sidebar */}
      <nav className={`
        fixed top-0 left-0 h-full w-56 bg-gray-950 border-r border-gray-800 z-40
        transform transition-transform duration-200
        ${mobileOpen ? "translate-x-0" : "-translate-x-full"}
        lg:translate-x-0
      `}>
        <div className="p-4 border-b border-gray-800">
          <h2 className="text-lg font-bold text-white">Chandra</h2>
          <p className="text-xs text-gray-500 mt-1">Digital Cloud Engineer</p>
        </div>

        <div className="p-3 space-y-1">
          {NAV_ITEMS.map((item) => {
            const isActive = pathname === item.href;
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={() => setMobileOpen(false)}
                className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${
                  isActive
                    ? "bg-blue-600/20 text-blue-400 border border-blue-600/30"
                    : "text-gray-400 hover:text-white hover:bg-gray-800/50 border border-transparent"
                }`}
              >
                <Icon className="w-4 h-4 shrink-0" />
                <span>{item.label}</span>
              </Link>
            );
          })}
        </div>

        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-gray-800">
          <p className="text-[0.6rem] uppercase tracking-wider text-gray-600">v1.0 — feature/local-llm</p>
        </div>
      </nav>

      {/* Spacer for desktop sidebar */}
      <div className="hidden lg:block w-56 shrink-0" />
    </>
  );
}