"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import {
  Server, ShieldCheck, PlayCircle, Activity, History, LayoutDashboard,
  Menu, X, CheckCircle, User, Sparkles, ArrowRight
} from "lucide-react";
import { useState } from "react";
import { useOnboarding } from "@/store/OnboardingContext";
import { Check } from "lucide-react";

const WORKFLOW_STEPS = [
  { key: "worker-details", href: "/onboarding", label: "Worker Details", icon: User },
  { key: "aws-tasks", href: "/aws-tasks", label: "AWS Tasks", icon: Server },
  { key: "aws-permissions", href: "/aws-permissions", label: "AWS Permissions", icon: ShieldCheck },
  { key: "execution-review", href: "/execution-review", label: "Execution Review", icon: PlayCircle },
  { key: "deployment", href: "/deployment", label: "Deployment", icon: Activity },
  { key: "execution-history", href: "/executions", label: "Execution History", icon: History },
];

const OPERATIONAL_NAV = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/aws-tasks", label: "AWS Tasks", icon: Server },
  { href: "/aws-permissions", label: "AWS Permissions", icon: ShieldCheck },
  { href: "/execution-review", label: "Execution Review", icon: PlayCircle },
  { href: "/deployment", label: "Deployment", icon: Activity },
  { href: "/executions", label: "Execution History", icon: History },
];

export default function AppNav() {
  const pathname = usePathname();
  const router = useRouter();
  const { onboardingCompleted, dashboardOpened, openDashboard, hydrated } = useOnboarding();
  const [mobileOpen, setMobileOpen] = useState(false);

  // Hide nav on onboarding page when dashboard is not yet opened
  if (pathname.startsWith("/onboarding") && !dashboardOpened) return null;

  const isOnboarding = pathname.startsWith("/onboarding");
  const workerDetailsDone = onboardingCompleted;

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
      <nav className={`fixed top-0 left-0 h-full w-56 bg-gray-950 border-r border-gray-800 z-40 transform transition-transform duration-200 ${mobileOpen ? "translate-x-0" : "-translate-x-full"} lg:translate-x-0`}>
        <div className="p-4 border-b border-gray-800">
          <h2 className="text-lg font-bold text-white">Chandra</h2>
          <p className="text-xs text-gray-500 mt-1">
            {dashboardOpened ? "Operational Dashboard" : "Setup Workflow"}
          </p>
        </div>

        <div className="p-3 space-y-1">
          {!dashboardOpened ? (
            /* ── Workflow progress nav ── */
            <>
              {WORKFLOW_STEPS.map((item) => {
                const isActive = pathname === item.href;
                const Icon = item.icon;
                const isCompleted = false;
                const isEnabled = false;

                return (
                  <div key={item.href}>
                    <Link
                      href={isEnabled ? item.href : "#"}
                      onClick={(e) => {
                        if (!isEnabled) e.preventDefault();
                        setMobileOpen(false);
                      }}
                      className={`flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm transition-all ${
                        isActive
                          ? "bg-blue-600/20 text-blue-400 border border-blue-600/30"
                          : isEnabled
                            ? "text-gray-400 hover:text-white hover:bg-gray-800/50 border border-transparent"
                            : "text-gray-600 cursor-not-allowed border border-transparent"
                      }`}
                    >
                      <Icon className="w-4 h-4 shrink-0" />
                      <span className="flex-1">{item.label}</span>
                      {isCompleted && <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0" />}
                      {!isEnabled && !isCompleted && <span className="w-3.5 h-3.5" />}
                    </Link>
                  </div>
                );
              })}

              {/* (Old onboarding steps logic removed) */}
            </>
          ) : (
            /* ── Operational Dashboard nav ── */
            <>
              {OPERATIONAL_NAV.map((item) => {
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
            </>
          )}
        </div>

        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-gray-800">
          <p className="text-[0.6rem] uppercase tracking-wider text-gray-600">
            {dashboardOpened ? "v1.0 — Operational" : "v1.0 — Setup"}
          </p>
        </div>
      </nav>

      {/* Spacer for desktop sidebar */}
      <div className="hidden lg:block w-56 shrink-0" />
    </>
  );
}