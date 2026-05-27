"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, ReactNode } from "react";
import { generateEmployeeId, type AgentGender } from "./agentProfile";
import { buildKraAgentPayload, buildSelectedKras, normalizeKraName, predefinedKraIds, type KraAgentPayload } from "./kraCatalog";
import type { AgentObservation, CostMetricsOutput } from "@/services/api";

export type OnboardingState = {
  agentName: string;
  employeeId: string;
  gender: AgentGender;
  avatarId: string;
  role: string;
  maturity: string;
  permissions: string[];
  predefinedKras: string[];
  customKras: string[];
  selectedKRAs: string[];
  kraPayload: KraAgentPayload;
  onboardingCompleted: boolean;
  hydrated: boolean;
  observations: AgentObservation | null;
  observationsError: string | null;
  costMetrics: CostMetricsOutput | null;
  costMetricsError: string | null;
  setAgentName: (name: string) => void;
  setEmployeeId: (id: string) => void;
  setGender: (gender: AgentGender) => void;
  setAvatarId: (avatarId: string) => void;
  setRole: (role: string) => void;
  setMaturity: (m: string) => void;
  togglePermission: (permission: string) => void;
  toggleKRA: (kra: string) => void;
  addCustomKRA: (kra: string) => void;
  removeCustomKRA: (kra: string) => void;
  setObservations: (data: AgentObservation | null, error?: string | null) => void;
  setCostMetrics: (data: CostMetricsOutput | null, error?: string | null) => void;
  completeOnboarding: () => void;
  reset: () => void;
};

const defaultState: OnboardingState = {
  agentName: "",
  employeeId: "",
  gender: "Neutral / Synthetic AI",
  avatarId: "",
  role: "AWS Cloud Engineer",
  maturity: "L2",
  permissions: [],
  predefinedKras: [],
  customKras: [],
  selectedKRAs: [],
  kraPayload: { predefinedKras: [], customKras: [], selectedKras: [] },
  onboardingCompleted: false,
  hydrated: false,
  observations: null,
  observationsError: null,
  costMetrics: null,
  costMetricsError: null,
  setAgentName: () => {},
  setEmployeeId: () => {},
  setGender: () => {},
  setAvatarId: () => {},
  setRole: () => {},
  setMaturity: () => {},
  togglePermission: () => {},
  toggleKRA: () => {},
  addCustomKRA: () => {},
  removeCustomKRA: () => {},
  setObservations: () => {},
  setCostMetrics: () => {},
  completeOnboarding: () => {},
  reset: () => {}
};

const OnboardingCtx = createContext<OnboardingState>(defaultState);

const STORAGE_KEY = "digital-employee-onboarding";

function readSessionStorage(): Partial<OnboardingState> | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY) ?? window.sessionStorage.getItem(STORAGE_KEY);
    if (!stored) return null;
    return JSON.parse(stored) as Partial<OnboardingState>;
  } catch {
    return null;
  }
}

export function OnboardingProvider({ children }: { children: ReactNode }) {
  const [agentName, setAgentName] = useState<string>("");
  const [employeeId, setEmployeeId] = useState<string>("");
  const [gender, setGender] = useState<AgentGender>("Neutral / Synthetic AI");
  const [avatarId, setAvatarId] = useState<string>("");
  const [role, setRole] = useState<string>("AWS Cloud Engineer");
  const [maturity, setMaturity] = useState<string>("L2");
  const [permissions, setPermissions] = useState<string[]>([]);
  const [predefinedKras, setPredefinedKras] = useState<string[]>([]);
  const [customKras, setCustomKras] = useState<string[]>([]);
  const [onboardingCompleted, setOnboardingCompleted] = useState<boolean>(false);
  const [observations, setObservationsState] = useState<AgentObservation | null>(null);
  const [observationsError, setObservationsError] = useState<string | null>(null);
  const [costMetrics, setCostMetricsState] = useState<CostMetricsOutput | null>(null);
  const [costMetricsError, setCostMetricsError] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const selectedKRAs = useMemo(() => buildSelectedKras(predefinedKras, customKras), [predefinedKras, customKras]);
  const kraPayload = useMemo(() => buildKraAgentPayload(predefinedKras, customKras), [predefinedKras, customKras]);

  const predefinedKrasRef = useRef(predefinedKras);
  useEffect(() => {
    predefinedKrasRef.current = predefinedKras;
  }, [predefinedKras]);

  const hydratedRef = useRef(false);

  useEffect(() => {
    if (hydratedRef.current) return;
    hydratedRef.current = true;
    const parsed = readSessionStorage();
    if (parsed) {
      if (parsed.agentName) {
        setAgentName(parsed.agentName);
        setEmployeeId(parsed.employeeId || generateEmployeeId(parsed.agentName));
      }
      if (parsed.employeeId) setEmployeeId(parsed.employeeId);
      if (parsed.gender) setGender(parsed.gender);
      if (parsed.avatarId) setAvatarId(parsed.avatarId);
      if (parsed.role) setRole(parsed.role);
      if (parsed.maturity) setMaturity(parsed.maturity);
      if (Array.isArray(parsed.permissions)) setPermissions(parsed.permissions);
      if (Array.isArray(parsed.predefinedKras)) {
        setPredefinedKras(parsed.predefinedKras);
      } else if (Array.isArray(parsed.selectedKRAs)) {
        setPredefinedKras(parsed.selectedKRAs.filter((kra: string) => predefinedKraIds.includes(kra)));
      }
      if (Array.isArray(parsed.customKras)) {
        setCustomKras(parsed.customKras);
      } else if (Array.isArray(parsed.selectedKRAs)) {
        setCustomKras(parsed.selectedKRAs.filter((kra: string) => !predefinedKraIds.includes(kra)));
      }
      if (typeof parsed.onboardingCompleted === "boolean") setOnboardingCompleted(parsed.onboardingCompleted);
      if (parsed.observations) setObservationsState(parsed.observations as AgentObservation);
      if (typeof parsed.observationsError === "string") setObservationsError(parsed.observationsError);
      if (parsed.costMetrics) setCostMetricsState(parsed.costMetrics as CostMetricsOutput);
      if (typeof parsed.costMetricsError === "string") setCostMetricsError(parsed.costMetricsError);
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      const serialized = JSON.stringify({
        agentName,
        employeeId,
        gender,
        avatarId,
        role,
        maturity,
        permissions,
        predefinedKras,
        customKras,
        selectedKRAs,
        onboardingCompleted,
        observations,
        observationsError,
        costMetrics,
        costMetricsError
      });
      window.localStorage.setItem(STORAGE_KEY, serialized);
      window.sessionStorage.setItem(STORAGE_KEY, serialized);
    } catch {
      // ignore storage errors
    }
  }, [agentName, employeeId, gender, avatarId, role, maturity, permissions, predefinedKras, customKras, selectedKRAs, onboardingCompleted, observations, observationsError, costMetrics, costMetricsError, hydrated]);

  const toggleKRA = useCallback((kra: string) => {
    setPredefinedKras((current) => (current.includes(kra) ? current.filter((k) => k !== kra) : [...current, kra]));
  }, []);

  const togglePermission = useCallback((permission: string) => {
    setPermissions((current) => (current.includes(permission) ? current.filter((item) => item !== permission) : [...current, permission]));
  }, []);

  const addCustomKRA = useCallback((kra: string) => {
    const normalized = normalizeKraName(kra);
    if (!normalized) return;
    setCustomKras((current) => {
      const exists = buildSelectedKras(predefinedKrasRef.current, current).some((item) => item.toLowerCase() === normalized.toLowerCase());
      return exists ? current : [...current, normalized];
    });
  }, []);

  const removeCustomKRA = useCallback((kra: string) => {
    setCustomKras((current) => current.filter((item) => item !== kra));
  }, []);

  const completeOnboarding = useCallback(() => {
    setOnboardingCompleted(true);
  }, []);

  const setObservations = useCallback((data: AgentObservation | null, error: string | null = null) => {
    setObservationsState(data);
    setObservationsError(error);
  }, []);

  const setCostMetrics = useCallback((data: CostMetricsOutput | null, error: string | null = null) => {
    setCostMetricsState(data);
    setCostMetricsError(error);
  }, []);

  const reset = useCallback(() => {
    setAgentName("");
    setEmployeeId("");
    setGender("Neutral / Synthetic AI");
    setAvatarId("");
    setRole("AWS Cloud Engineer");
    setMaturity("L2");
    setPermissions([]);
    setPredefinedKras([]);
    setCustomKras([]);
    setOnboardingCompleted(false);
    setObservationsState(null);
    setObservationsError(null);
    setCostMetricsState(null);
    setCostMetricsError(null);
    try {
      window.localStorage.removeItem(STORAGE_KEY);
      window.sessionStorage.removeItem(STORAGE_KEY);
    } catch {
      // ignore
    }
  }, []);

  const contextValue = useMemo<OnboardingState>(
    () => ({
      agentName,
      employeeId,
      gender,
      avatarId,
      role,
      maturity,
      permissions,
      predefinedKras,
      customKras,
      selectedKRAs,
      kraPayload,
      onboardingCompleted,
      hydrated,
      observations,
      observationsError,
      costMetrics,
      costMetricsError,
      setAgentName,
      setEmployeeId,
      setGender,
      setAvatarId,
      setRole,
      setMaturity,
      togglePermission,
      toggleKRA,
      addCustomKRA,
      removeCustomKRA,
      setObservations,
      setCostMetrics,
      completeOnboarding,
      reset
    }),
    [
      agentName,
      employeeId,
      gender,
      avatarId,
      role,
      maturity,
      permissions,
      predefinedKras,
      customKras,
      selectedKRAs,
      kraPayload,
      onboardingCompleted,
      hydrated,
      observations,
      observationsError,
      costMetrics,
      costMetricsError,
      togglePermission,
      toggleKRA,
      addCustomKRA,
      removeCustomKRA,
      setObservations,
      setCostMetrics,
      completeOnboarding,
      reset
    ]
  );

  return <OnboardingCtx.Provider value={contextValue}>{children}</OnboardingCtx.Provider>;
}

export function useOnboarding() {
  return useContext(OnboardingCtx);
}
