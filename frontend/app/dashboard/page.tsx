"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { ChandraExperience } from "@/components/ChandraExperience";
import { useOnboarding } from "@/store/OnboardingContext";

export default function DashboardPage() {
  const router = useRouter();
  const { hydrated, onboardingCompleted, agentName } = useOnboarding();

  useEffect(() => {
    if (!hydrated) return;
    if (!onboardingCompleted || !agentName) {
      router.replace("/onboarding");
    }
  }, [hydrated, onboardingCompleted, agentName, router]);

  if (!hydrated || !onboardingCompleted || !agentName) {
    return null;
  }

  return <ChandraExperience />;
}
