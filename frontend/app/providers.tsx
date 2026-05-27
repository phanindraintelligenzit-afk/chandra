"use client";

import type { ReactNode } from "react";
import { OnboardingProvider } from "@/store/OnboardingContext";

export default function Providers({ children }: { children: ReactNode }) {
  return <OnboardingProvider>{children}</OnboardingProvider>;
}
