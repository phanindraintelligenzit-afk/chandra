"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ChandraExperience } from "@/components/ChandraExperience";
import { useOnboarding } from "@/store/OnboardingContext";

export default function DashboardPage() {
  const router = useRouter();
  const { hydrated, onboardingCompleted, dashboardOpened, agentName } = useOnboarding();

  useEffect(() => {
    if (!hydrated) return;
    // Strict guard: must have completed onboarding AND dashboard must have been explicitly opened
    // (dashboardOpened is set to true by openDashboard() only after deployment reaches 100%)
    if (!onboardingCompleted || !dashboardOpened || !agentName) {
      router.replace("/onboarding");
    }
  }, [hydrated, onboardingCompleted, dashboardOpened, agentName, router]);

  if (!hydrated || !onboardingCompleted || !dashboardOpened || !agentName) {
    return null;
  }

  return <ChandraExperience />;
}