"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, ReactNode } from "react";
import { generateEmployeeId, type AgentGender } from "./agentProfile";
import {
  buildKraAgentPayload,
  buildSelectedKras,
  normalizeCustomKra,
  normalizeKraName,
  predefinedKraIds,
  type CustomKra,
  type KraAgentPayload
} from "./kraCatalog";
import {
  fetchCustomKras,
  saveCustomKras,
  type StoredCustomKra,
  type AgentObservation,
  type CostMetricsOutput
} from "@/services/api";


export type OnboardingState = {
  agentName: string;
  employeeId: string;
  gender: AgentGender;
  avatarId: string;
  role: string;
  maturity: string;
  permissions: string[];
  predefinedKras: string[];
  customKras: CustomKra[];
  selectedKRAs: string[];
  kraPayload: KraAgentPayload;
  onboardingCompleted: boolean;
  hydrated: boolean;
  stepsCompleted: string[];
  dashboardOpened: boolean;
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
  addCustomKRA: (name: string, description?: string) => void;
  removeCustomKRA: (name: string) => void;
  toggleCustomKRA: (name: string) => void;
  setAllCustomKRAsSelected: (selected: boolean) => void;
  setObservations: (data: AgentObservation | null, error?: string | null) => void;
  setCostMetrics: (data: CostMetricsOutput | null, error?: string | null) => void;
  completeOnboarding: () => void;
    completeStep: (step: string) => void;
    openDashboard: () => void;
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
    stepsCompleted: [],
    dashboardOpened: false,
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
  toggleCustomKRA: () => {},
  setAllCustomKRAsSelected: () => {},

  setObservations: () => {},
  setCostMetrics: () => {},
  completeOnboarding: () => {},
    completeStep: () => {},
    openDashboard: () => {},
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

/**
 * Normalize a list of custom KRAs from sessionStorage.
 * Handles both the new { name, description } format and the legacy string format.
 */
function normalizeCustomKraList(value: unknown): CustomKra[] {
  if (!Array.isArray(value)) return [];
  const result: CustomKra[] = [];
  const seen = new Set<string>();
  for (const entry of value) {
    const normalized = normalizeCustomKra(entry);
    if (!normalized) continue;
    const key = normalized.name.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(normalized);
  }
  return result;
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
  const [customKras, setCustomKras] = useState<CustomKra[]>([]);
  const [onboardingCompleted, setOnboardingCompleted] = useState<boolean>(false);
  const [stepsCompleted, setStepsCompleted] = useState<string[]>([]);
  const [dashboardOpened, setDashboardOpened] = useState<boolean>(false);
  const [observations, setObservationsState] = useState<AgentObservation | null>(null);
  const [observationsError, setObservationsError] = useState<string | null>(null);
  const [costMetrics, setCostMetricsState] = useState<CostMetricsOutput | null>(null);
  const [costMetricsError, setCostMetricsError] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(false);
  const selectedKRAs = useMemo(() => buildSelectedKras(predefinedKras, customKras), [predefinedKras, customKras]);
  const kraPayload = useMemo(() => buildKraAgentPayload(predefinedKras, customKras), [predefinedKras, customKras]);

  const predefinedKrasRef = useRef(predefinedKras);
  const customKrasRef = useRef(customKras);
  useEffect(() => {
    predefinedKrasRef.current = predefinedKras;
  }, [predefinedKras]);
  useEffect(() => {
    customKrasRef.current = customKras;
  }, [customKras]);

  const hydratedRef = useRef(false);
  // Guard so we only sync to customKras.json once hydration is done — otherwise
  // a fast user click on "Add" would race the file fetch and overwrite it.
  const customKrasHydratedRef = useRef(false);
  // Debounce handle for the PUT /customKras call.
  const customKrasSaveTimerRef = useRef<number | null>(null);

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
      // Hydrate custom KRAs — supports both new { name, description } and legacy string formats
      if (Array.isArray(parsed.customKras)) {
        setCustomKras(normalizeCustomKraList(parsed.customKras));
      } else if (Array.isArray(parsed.selectedKRAs)) {
        const legacyCustoms = parsed.selectedKRAs
          .filter((kra: string) => !predefinedKraIds.includes(kra))
          .map((name: string) => normalizeCustomKra(name))
          .filter((kra: CustomKra | null): kra is CustomKra => Boolean(kra));
        setCustomKras(legacyCustoms);
      }
      if (typeof parsed.onboardingCompleted === "boolean") setOnboardingCompleted(parsed.onboardingCompleted);
            if (Array.isArray(parsed.stepsCompleted)) setStepsCompleted(parsed.stepsCompleted);
            if (typeof parsed.dashboardOpened === "boolean") setDashboardOpened(parsed.dashboardOpened);
            if (parsed.observations) setObservationsState(parsed.observations as AgentObservation);
      if (typeof parsed.observationsError === "string") setObservationsError(parsed.observationsError);
      if (parsed.costMetrics) setCostMetricsState(parsed.costMetrics as CostMetricsOutput);
      if (typeof parsed.costMetricsError === "string") setCostMetricsError(parsed.costMetricsError);
    }
    setHydrated(true);
  }, []);

  // Fetch the persisted custom KRAs from the backend JSON file (customKras.json)
  // on first mount, then merge with anything in localStorage so the user sees
  // their full library the next time they open the onboarding page.
  useEffect(() => {
    if (customKrasHydratedRef.current) return;
    customKrasHydratedRef.current = true;
    let cancelled = false;
    (async () => {
      try {
        const stored = await fetchCustomKras();
        if (cancelled || !Array.isArray(stored) || stored.length === 0) return;
        const normalized = normalizeCustomKraList(stored);
        if (normalized.length === 0) return;
        setCustomKras((current) => {
          const merged: CustomKra[] = [...current];
          const seen = new Set(merged.map((k) => k.name.toLowerCase()));
          for (const entry of normalized) {
            const key = entry.name.toLowerCase();
            if (seen.has(key)) continue;
            seen.add(key);
            merged.push(entry);
          }
          return merged;
        });
      } catch (error) {
        console.warn("Failed to hydrate customKras from backend:", error);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Debounced persistence — every time the customKras list changes we save it
  // back to customKras.json on the backend so the library survives a hard refresh.
  useEffect(() => {
    if (!hydrated || !customKrasHydratedRef.current) return;
    if (customKrasSaveTimerRef.current !== null) {
      window.clearTimeout(customKrasSaveTimerRef.current);
    }
    const snapshot: StoredCustomKra[] = customKras.map((k) => ({
      name: k.name,
      description: k.description,
      selected: k.selected !== false
    }));
    customKrasSaveTimerRef.current = window.setTimeout(() => {
      customKrasSaveTimerRef.current = null;
      saveCustomKras(snapshot).catch((error) => {
        console.warn("Failed to save customKras to backend:", error);
      });
    }, 350);
    return () => {
      if (customKrasSaveTimerRef.current !== null) {
        window.clearTimeout(customKrasSaveTimerRef.current);
        customKrasSaveTimerRef.current = null;
      }
    };
  }, [customKras, hydrated]);


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
                stepsCompleted,
                dashboardOpened,
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
  }, [agentName, employeeId, gender, avatarId, role, maturity, permissions, predefinedKras, customKras, selectedKRAs, onboardingCompleted, stepsCompleted, dashboardOpened, observations, observationsError, costMetrics, costMetricsError, hydrated]);

  const toggleKRA = useCallback((kra: string) => {
    setPredefinedKras((current) => (current.includes(kra) ? current.filter((k) => k !== kra) : [...current, kra]));
  }, []);

  const togglePermission = useCallback((permission: string) => {
    setPermissions((current) => (current.includes(permission) ? current.filter((item) => item !== permission) : [...current, permission]));
  }, []);

  const addCustomKRA = useCallback((name: string, description?: string) => {
    const normalizedName = normalizeKraName(name);
    if (!normalizedName) return;
    const normalizedDescription = (description ?? "").replace(/\s+/g, " ").trim();
    setCustomKras((current) => {
      const exists = current.some((item) => item.name.toLowerCase() === normalizedName.toLowerCase());
      if (exists) return current;
      const entry: CustomKra = {
        name: normalizedName,
        description: normalizedDescription || normalizedName,
        selected: true
      };
      return [...current, entry];
    });
  }, []);

  const removeCustomKRA = useCallback((name: string) => {
    setCustomKras((current) => current.filter((item) => item.name !== name));
  }, []);

  const toggleCustomKRA = useCallback((name: string) => {
    setCustomKras((current) => current.map((item) => item.name === name ? { ...item, selected: item.selected === false ? true : false } : item));
  }, []);

  const setAllCustomKRAsSelected = useCallback((selected: boolean) => {
    setCustomKras((current) => current.map((item) => ({ ...item, selected })));
  }, []);


  const completeOnboarding = useCallback(() => {
      setOnboardingCompleted(true);
    }, []);

    const completeStep = useCallback((step: string) => {
      setStepsCompleted((prev) => prev.includes(step) ? prev : [...prev, step]);
    }, []);

    const openDashboard = useCallback(() => {
      setDashboardOpened(true);
    }, []);

  const setObservations = useCallback((data: AgentObservation | null, error: string | null = null) => {
    if (typeof window !== "undefined") {
      console.log("📝 SET OBSERVATIONS CALLED - data:", data ? `health=${data.health}` : "null", "error:", error);
    }
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
        setStepsCompleted([]);
        setDashboardOpened(false);
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
            stepsCompleted,
            dashboardOpened,
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
      toggleCustomKRA,
      setAllCustomKRAsSelected,
      setObservations,
            setCostMetrics,
            completeOnboarding,
            completeStep,
            openDashboard,
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
            stepsCompleted,
            dashboardOpened,
            observations,
            observationsError,
            costMetrics,
            costMetricsError,
            togglePermission,
            toggleKRA,
            addCustomKRA,
            removeCustomKRA,
            toggleCustomKRA,
            setAllCustomKRAsSelected,
            setObservations,
            setCostMetrics,
            completeOnboarding,
            completeStep,
            openDashboard,
            reset
    ]
  );


  return <OnboardingCtx.Provider value={contextValue}>{children}</OnboardingCtx.Provider>;
}

export function useOnboarding() {
  return useContext(OnboardingCtx);
}