"use client";

import { Check, ArrowRight, Sparkles } from "lucide-react";
import { useRouter } from "next/navigation";
import { useOnboarding } from "@/store/OnboardingContext";

export function WorkflowNextStep({ stepKey, nextHref, label }: { stepKey: string; nextHref: string; label: string }) {
  const { stepsCompleted, completeStep, dashboardOpened } = useOnboarding();
  const router = useRouter();
  if (dashboardOpened || stepsCompleted.includes(stepKey)) return null;
  return (
    <div className="mt-8 p-4 bg-gray-900 border border-emerald-800/40 rounded-xl">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm font-medium text-emerald-400">Step complete?</p>
          <p className="text-xs text-gray-500 mt-0.5">Mark this step as finished and proceed to the next one.</p>
        </div>
        <button
          onClick={() => { completeStep(stepKey); router.push(nextHref); }}
          className="flex items-center gap-2 px-5 py-2.5 bg-emerald-600 hover:bg-emerald-700 rounded-lg text-white text-sm transition-all"
        >
          <Check className="w-4 h-4" />
          {label}
          <ArrowRight className="w-4 h-4" />
        </button>
      </div>
    </div>
  );
}

export function OpenDashboardButton() {
  const { openDashboard } = useOnboarding();
  const router = useRouter();
  
  const handleOpen = () => {
    openDashboard();
    router.push("/dashboard");
  };

  return (
    <div className="mt-8 p-6 bg-gradient-to-r from-emerald-900/40 to-gray-900 border border-emerald-700/40 rounded-xl">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-lg font-semibold text-emerald-300">All setup steps complete!</p>
          <p className="text-sm text-gray-400 mt-1">Your digital cloud engineer is ready. Open the operational dashboard to begin monitoring.</p>
        </div>
        <button
          onClick={handleOpen}
          className="flex items-center gap-2 px-6 py-3 bg-emerald-600 hover:bg-emerald-700 rounded-xl text-white font-medium transition-all shadow-lg shadow-emerald-900/30"
        >
          <Sparkles className="w-5 h-5" />
          Open Dashboard
          <ArrowRight className="w-5 h-5" />
        </button>
      </div>
    </div>
  );
}