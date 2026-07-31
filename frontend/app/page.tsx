"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useOnboarding } from "@/store/OnboardingContext";

export default function Home() {
  const router = useRouter();
  const { hydrated, onboardingCompleted, stepsCompleted, dashboardOpened } = useOnboarding();

  useEffect(() => {
    if (!hydrated) return;

    if (dashboardOpened) {
      // Dashboard is open — go to dashboard
      router.replace("/dashboard");
    } else if (!onboardingCompleted) {
      // Not onboarded yet — start onboarding
      router.replace("/onboarding");
    } else if (!stepsCompleted.includes("aws-tasks")) {
      // Onboarding done — next step: AWS Tasks
      router.replace("/aws-tasks");
    } else if (!stepsCompleted.includes("aws-permissions")) {
      router.replace("/aws-permissions");
    } else if (!stepsCompleted.includes("execution-review")) {
      router.replace("/execution-review");
    } else if (!stepsCompleted.includes("deployment")) {
      router.replace("/deployment");
    } else if (!stepsCompleted.includes("execution-history")) {
      router.replace("/executions");
    } else {
      // All steps done — show dashboard
      router.replace("/dashboard");
    }
  }, [hydrated, onboardingCompleted, stepsCompleted, dashboardOpened, router]);

  return null;
}