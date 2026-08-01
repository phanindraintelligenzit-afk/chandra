"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useOnboarding } from "@/store/OnboardingContext";

export default function Home() {
  const router = useRouter();
  const { hydrated, onboardingCompleted, dashboardOpened } = useOnboarding();

  useEffect(() => {
    if (!hydrated) return;

    if (dashboardOpened) {
      // Dashboard is open — go to dashboard
      router.replace("/dashboard");
    } else if (!onboardingCompleted) {
      // Not onboarded yet — start onboarding (all steps in one flow)
      router.replace("/onboarding");
    } else {
      // Onboarding complete but dashboard not opened yet
      router.replace("/dashboard");
    }
  }, [hydrated, onboardingCompleted, dashboardOpened, router]);

  return null;
}