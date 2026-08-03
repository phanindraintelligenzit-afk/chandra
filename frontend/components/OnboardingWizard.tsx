"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { usePathname, useRouter } from "next/navigation";
import { KeyRound, Search, ShieldCheck, Sparkles, X } from "lucide-react";

import { useOnboarding } from "@/store/OnboardingContext";
import { fetchAgentObservations, fetchCostMetrics } from "@/services/api";
import { buildKraPayload } from "@/services/mapping";
import AwsTasksStep from "./AwsTasksStep";
import AwsPermissionsStep from "./AwsPermissionsStep";
import {
  agentAvatars,
  agentGenders,
  generateEmployeeId,
  getAvatarById,
  getAvatarImageSrc,
  getRoleIconSrc,
  isDuplicateAgentName,
  normalizeAgentName,
  permissionCatalog,
  type AgentAvatar
} from "@/store/agentProfile";
import {
  normalizeKraDescription,
  normalizeKraName,
  predefinedKraCatalog,
  type CustomKra
} from "@/store/kraCatalog";

type RoleCard = {
  name: string;
  icon: string;
  signal: string;
};

const roles: RoleCard[] = [
  { name: "AWS Cloud Engineer", icon: "aws.svg", signal: "AWS" },
  { name: "Java Developer", icon: "java.svg", signal: "JVM" },
  { name: "Azure Cloud Engineer", icon: "azure.svg", signal: "AZR" },
  { name: "DevOps Engineer", icon: "devops.svg", signal: "OPS" },
  { name: "Security Analyst", icon: "security.svg", signal: "SEC" },
  { name: "Kubernetes Administrator", icon: "kubernetes.svg", signal: "K8S" }
];

const maturities = [
  { id: "L1", label: "Observe", desc: "Beginner Digital Worker" },
  { id: "L2", label: "Operate", desc: "Intermediate Cloud Engineer" },
  { id: "L3", label: "Govern", desc: "Senior Autonomous Engineer" },
  { id: "L4", label: "Architect", desc: "Enterprise Operations Architect" }
];

const deploymentStages = [
  "INITIALIZING AGENT",
  "CONFIGURING KRAS",
  "VALIDATING ACCESS LAYERS",
  "RETRIEVING CLOUD TELEMETRY",
  "ANALYZING OPERATIONAL INTELLIGENCE",
  "SYNCHRONIZING SECURITY POSTURE",
  "FINALIZING DEPLOYMENT"
];

const deploymentTargets = [10, 24, 38, 55, 80, 92, 100];

const STEP_PATHS = [
  "/onboarding",
  "/onboarding/role",
  "/onboarding/maturity",
  "/onboarding/monitoring",
  "/onboarding/aws-tasks",
  "/onboarding/aws-permissions",
  "/onboarding/deploying"
];

function wait(ms: number) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function AvatarPortrait({
  avatar,
  selected = false,
  size = 80
}: {
  avatar: AgentAvatar;
  selected?: boolean;
  size?: number;
}) {
  return (
    <div
      className="avatar-portrait shrink-0"
      style={{ width: size, height: size, borderColor: selected ? "rgba(142,217,168,0.6)" : undefined }}
    >
      <img src={getAvatarImageSrc(avatar)} alt={avatar.label} draggable={false} />
    </div>
  );
}

function MaturityRings({ level, active }: { level: number; active: boolean }) {
  const rings = Array.from({ length: level }, (_, index) => index);
  return (
    <div className="relative mx-auto h-16 w-16">
      {rings.map((index) => {
        const inset = index * 6;
        return (
          <div
            key={index}
            className={`maturity-ring ${active ? "active" : ""}`}
            style={{
              inset,
              animationDelay: `${index * 0.32}s`
            }}
          />
        );
      })}
      <div
        className={`absolute inset-0 m-auto flex h-7 w-7 items-center justify-center rounded-full border text-[0.7rem] font-bold ${
          active ? "border-emerald-300/60 bg-emerald-300/15 text-emerald-200" : "border-white/15 bg-black/60 text-frost/80"
        }`}
        style={{ top: "50%", left: "50%", transform: "translate(-50%, -50%)" }}
      >
        {level}
      </div>
    </div>
  );
}

function TopRightProfile({
  avatar,
  displayName,
  agentId
}: {
  avatar: AgentAvatar;
  displayName: string;
  agentId: string;
}) {
  return (
    <div className="flex items-center gap-3 rounded-2xl border border-signal/35 bg-black/50 px-3 py-2 shadow-[0_0_24px_rgba(255,59,59,0.18)] backdrop-blur">
      <AvatarPortrait avatar={avatar} size={42} selected />
      <div className="flex flex-col text-right">
        <span className="text-[0.55rem] uppercase tracking-[0.2em] text-muted">WORKER</span>
        <span className="text-sm font-semibold uppercase tracking-[0.08em] text-frost">
          {displayName || "PENDING"}
        </span>
        <span className="text-[0.58rem] uppercase tracking-[0.18em] text-signal">{agentId}</span>
      </div>
    </div>
  );
}

export default function OnboardingWizard() {
  const router = useRouter();
  const pathname = usePathname();
  const {
    agentName,
    employeeId,
    setEmployeeId,
    gender,
    setGender,
    avatarId,
    setAvatarId,
    setAgentName,
    role,
    setRole,
    maturity,
    setMaturity,
    permissions,
    togglePermission,
    selectedKRAs,
    predefinedKras,
    customKras,
    toggleKRA,
    addCustomKRA,
    removeCustomKRA,
    toggleCustomKRA,
    setAllCustomKRAsSelected,
    selectedAwsTasks,
    selectedAwsPermissions,
    completeOnboarding,
    openDashboard,

    setObservations,
    setCostMetrics,
    hydrated
  } = useOnboarding();
  
  const initialStepIndex = STEP_PATHS.indexOf(pathname);
  const [step, setStep] = useState(initialStepIndex !== -1 ? initialStepIndex : 0);
  const [localName, setLocalName] = useState(agentName || "");
  const [deployStage, setDeployStage] = useState<number>(0);
  const [deployProgress, setDeployProgress] = useState<number>(0);
  const [customKraName, setCustomKraName] = useState("");
  const [customKraDescription, setCustomKraDescription] = useState("");
  const [customKraSearch, setCustomKraSearch] = useState("");
  const [notice, setNotice] = useState("");

  const [observationsStatus, setObservationsStatus] = useState<"idle" | "loading" | "success" | "error">("idle");
  const [observationsErrorMessage, setObservationsErrorMessage] = useState<string>("");
  const submissionRef = useRef<AbortController | null>(null);
  const deploymentStartedRef = useRef(false);

  const normalizedName = normalizeAgentName(localName);
  const duplicateName = normalizedName.length > 0 && isDuplicateAgentName(normalizedName);
  const employeeIdPreview = useMemo(() => (normalizedName ? generateEmployeeId(normalizedName) : ""), [normalizedName]);
  const hasSelectedAvatar = Boolean(avatarId);
  const hasName = Boolean(normalizedName || agentName);
  const selectedAvatar = hasSelectedAvatar ? getAvatarById(avatarId) : null;
  const progress = deployProgress;
  const displayName = (agentName || normalizedName || "").toUpperCase();
  const currentAgentId = agentName ? employeeId || employeeIdPreview : employeeIdPreview;
  const showProfilePill = hasSelectedAvatar && hasName;

  useEffect(() => {
    setLocalName((current) => (current === agentName ? current : agentName));
  }, [agentName]);

  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 2600);
    return () => window.clearTimeout(timer);
  }, [notice]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!hydrated) return;

    const idx = STEP_PATHS.indexOf(pathname);
    if (idx === -1) {
      router.replace(STEP_PATHS[0]);
      return;
    }

    if (idx !== step) {
      const nameToCheck = agentName || normalizedName;
      if (idx > 0 && !(nameToCheck.length > 0 && hasSelectedAvatar)) {
        router.replace(STEP_PATHS[0]);
      } else if (idx > 1 && role !== "AWS Cloud Engineer") {
        router.replace(STEP_PATHS[1]);
      } else if (idx > 2 && maturity !== "L2") {
        router.replace(STEP_PATHS[2]);
      } else if (idx > 3 && selectedKRAs.length === 0) {
        router.replace(STEP_PATHS[3]);
      } else if (idx > 4 && selectedAwsTasks.length === 0) {
        router.replace(STEP_PATHS[4]);
      } else {
        setStep(idx);
        if (idx === 6 && !deploymentStartedRef.current) {
          deploymentStartedRef.current = true;
          runDeploymentSequence();
        } else if (idx !== 6) {
          submissionRef.current?.abort();
          deploymentStartedRef.current = false;
        }
      }
    }
  }, [pathname, hydrated, step, agentName, normalizedName, hasSelectedAvatar, role, maturity, selectedKRAs, selectedAwsTasks, router]);

  const canNext = useMemo(() => {
    if (step === 0) return normalizedName.length > 0 && !duplicateName && hasSelectedAvatar;
    if (step === 1) return role === "AWS Cloud Engineer";
    if (step === 2) return maturity === "L2";
    if (step === 3) return selectedKRAs.length > 0;
    if (step === 4) return selectedAwsTasks.length > 0;
    if (step === 5) return true; // permissions can proceed anytime
    return true;
  }, [step, normalizedName.length, duplicateName, hasSelectedAvatar, role, maturity, selectedKRAs.length, selectedAwsTasks.length]);

  function next() {
    if (step === 0) {
      if (duplicateName) {
        setNotice("This agent name is already registered. Choose a different workforce identity.");
        return;
      }
      setAgentName(normalizedName);
      setEmployeeId(employeeIdPreview);
    }
    if (step === 5) {
      if (deploymentStartedRef.current) return;
      router.push(STEP_PATHS[6]);
      return;
    }
    router.push(STEP_PATHS[Math.min(step + 1, 6)]);
  }

  function prev() {
    router.push(STEP_PATHS[Math.max(step - 1, 0)]);
  }

  function addCustomKraFromInput() {
    const name = normalizeKraName(customKraName);
    if (!name) return;
    const description = normalizeKraDescription(customKraDescription);
    addCustomKRA(name, description);
    setCustomKraName("");
    setCustomKraDescription("");
  }

  async function animateProgressTo(target: number, signal: AbortSignal) {
    setDeployProgress((current) => Math.min(current, target));
    while (!signal.aborted) {
      let finished = false;
      setDeployProgress((current) => {
        if (current >= target) {
          finished = true;
          return current;
        }
        const distance = target - current;
        return Math.min(target, current + Math.max(1, Math.ceil(distance / 8)));
      });
      if (finished) return;
      await wait(130);
    }
  }

  async function runDeploymentSequence() {
    setDeployStage(0);
    setDeployProgress(0);
    setObservationsStatus("loading");
    setObservationsErrorMessage("");
    setObservations(null, null);

    const activeCustomKras = customKras.filter(k => k.selected !== false);
    const kraPayloadEntries = buildKraPayload(predefinedKras, activeCustomKras);
    const payload = {
      region: process.env.NEXT_PUBLIC_AWS_REGION || "us-east-1",
      kras: kraPayloadEntries,
      selected_kras: selectedKRAs,
      custom_kras: activeCustomKras.map((k) => ({ name: k.name, description: k.description })),
      maturity_level: maturity,
      deployment: {
        role,
        permissions,
        aws_tasks: selectedAwsTasks,
        aws_permissions: selectedAwsPermissions,
        agent_name: agentName || normalizedName,
        employee_id: employeeId || employeeIdPreview
      }
    };

    submissionRef.current?.abort();
    const controller = new AbortController();
    submissionRef.current = controller;

    try {
      for (let stageIndex = 0; stageIndex < 3; stageIndex += 1) {
        if (controller.signal.aborted) return;
        setDeployStage(stageIndex);
        await animateProgressTo(deploymentTargets[stageIndex], controller.signal);
        await wait(220);
      }

      setDeployStage(3);
      await animateProgressTo(deploymentTargets[3], controller.signal);

      setDeployStage(4);
      await animateProgressTo(deploymentTargets[4], controller.signal);
      await wait(220);

      setDeployStage(5);
      await animateProgressTo(deploymentTargets[5], controller.signal);
      await wait(220);

      if (!controller.signal.aborted) {
        setObservationsStatus("success");
        setDeployStage(6);
        await animateProgressTo(100, controller.signal);
        await wait(420);
        completeOnboarding();

        // Fire cost metrics fetch in background (don't await) so it loads first
        const costController = new AbortController();
        fetchCostMetrics(7, { signal: costController.signal })
          .then((data) => setCostMetrics(data))
          .catch((error) => {
            const message = error instanceof Error ? error.message : "Cost metrics request failed";
            setCostMetrics(null, message);
          });

        // Fire observations fetch in background (don't await) with longer timeout
        const obsController = new AbortController();
        const obsTimeout = setTimeout(() => obsController.abort(), 600_000); // 10 minute timeout

        fetchAgentObservations(payload, { signal: obsController.signal })
          .then((data) => {
            clearTimeout(obsTimeout);
            console.log("OBS RESPONSE SUCCESS", data);
            setObservations(data);
          })
          .catch((error) => {
            clearTimeout(obsTimeout);
            const message = error instanceof Error ? error.message : "Backend request failed";
            console.error("OBS RESPONSE ERROR", message);
            setObservations(null, message);
          });

        openDashboard();
        router.push("/dashboard");
      }
    } catch (error: unknown) {
      if (!controller.signal.aborted) {
        const message = error instanceof Error ? error.message : "Backend request failed";
        setObservations(null, message);
        setObservationsErrorMessage(message);
        setObservationsStatus("error");
      }
    }
  }

  useEffect(() => {
    return () => {
      submissionRef.current?.abort();
    };
  }, []);

  return (
    <div className="relative min-h-screen bg-obsidian text-frost flex items-center justify-center p-6 overflow-hidden">
      <div className="onboarding-ambient" />
      <AnimatePresence>
        {notice ? (
          <motion.div
            initial={{ opacity: 0, y: -12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -12 }}
            className="fixed right-5 top-5 z-[80] max-w-sm rounded-2xl border border-amber/30 bg-black/85 px-4 py-3 text-sm text-frost shadow-amber backdrop-blur"
          >
            <div className="text-[0.58rem] uppercase tracking-[0.2em] text-amber">SYSTEM NOTIFICATION</div>
            <div className="mt-1 text-frost/85">{notice}</div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <div className="onboarding-shell relative z-10 w-full max-w-5xl rounded-2xl border border-signal/25 bg-gradient-to-b from-black/70 via-black/50 to-black/40 p-6 shadow-[0_30px_80px_rgba(255,59,59,0.18),0_0_0_1px_rgba(255,59,59,0.08)] backdrop-blur">
        <div className="mb-5 flex min-h-[60px] items-start justify-between gap-4">
          <div className="flex items-center gap-3">
            <img
              src="/intelligenz-it-logo.png.png"
              alt="Intelligenz IT Logo"
              className="w-64 sm:w-80 h-10 sm:h-12 object-fill drop-shadow-[0_0_12px_rgba(255,255,255,0.15)]"
            />
          </div>
          {showProfilePill && selectedAvatar ? (
            <TopRightProfile avatar={selectedAvatar} displayName={displayName} agentId={currentAgentId} />
          ) : null}
        </div>

        <AnimatePresence mode="wait">
          {step === 0 && (
            <motion.div key="name" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -16 }}>
              <div className="space-y-6">
                <div>
                  <h2 className="text-3xl font-semibold tracking-[0.02em]">Define your Digital FTE Worker</h2>
                </div>

                <label className="block">
                  <input
                    value={localName}
                    onChange={(e) => setLocalName(e.target.value)}
                    placeholder="ENTER AGENT NAME..."
                    className={`w-full rounded-2xl border bg-black/20 px-4 py-3 text-xl text-frost outline-none transition focus:ring-2 ${
                      duplicateName ? "border-amber/50 focus:border-amber/60 focus:ring-amber/15" : "border-white/10 focus:border-emerald-300/40 focus:ring-emerald-300/15"
                    }`}
                  />
                </label>

                <div className="grid gap-4 md:grid-cols-3">
                  {agentGenders.map((option) => (
                    <button
                      key={option}
                      type="button"
                      onClick={() => setGender(option)}
                      className={`rounded-2xl border px-4 py-3 text-left text-sm transition ${
                        gender === option ? "border-emerald-300/50 bg-emerald-300/10 text-emerald-200" : "border-white/10 bg-black/30 text-frost/75 hover:border-emerald-300/20"
                      }`}
                    >
                      <div className="text-[0.6rem] uppercase tracking-[0.18em] text-muted">IDENTITY MODE</div>
                      <div className="mt-1 font-semibold uppercase tracking-[0.04em]">{option}</div>
                    </button>
                  ))}
                </div>

                <div>
                  <div className="mb-3 text-[0.62rem] uppercase tracking-[0.2em] text-amber">SELECT AVATAR</div>
                  <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-6">
                    {agentAvatars.map((avatar) => {
                      const selected = avatarId === avatar.id;
                      return (
                        <button
                          key={avatar.id}
                          type="button"
                          onClick={() => setAvatarId(avatar.id)}
                          className={`avatar-card rounded-3xl border bg-black/30 p-4 text-left ${
                            selected ? "selected border-emerald-300/60 bg-emerald-300/8" : "border-white/10"
                          }`}
                        >
                          <div className="flex flex-col items-center gap-3">
                            <AvatarPortrait avatar={avatar} selected={selected} size={88} />
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>

                {normalizedName.length > 0 ? (
                  <div className="rounded-2xl border border-white/10 bg-white/5 p-4 text-sm text-frost/75">
                    Initializing identity for <span className="font-semibold text-frost">{normalizedName.toUpperCase()}</span> as employee ID{" "}
                    <span className="font-semibold text-amber">{employeeIdPreview}</span>.
                  </div>
                ) : null}
                <div className="flex items-center gap-3">
                  <button onClick={() => router.back()} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted">CANCEL</button>
                  <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE</button>
                </div>
              </div>
            </motion.div>
          )}

          {step === 1 && (
            <motion.div key="role" initial={{ opacity: 0, x: 30 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
              <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">
                WHAT ROLE SHOULD {(normalizedName || agentName || "THIS AGENT").toUpperCase()} PERFORM?
              </h3>
              <p className="text-muted mt-2">Choose the operating role for your digital employee.</p>
              <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {roles.map((item) => {
                  const disabled = item.name !== "AWS Cloud Engineer";
                  return (
                    <button
                      key={item.name}
                      type="button"
                      onClick={() => !disabled && setRole(item.name)}
                      disabled={disabled}
                      className={`role-card group text-left rounded-3xl border px-4 py-5 transition ${disabled ? "cursor-not-allowed opacity-60 border-white/10 bg-black/20" : role === item.name ? "border-emerald-300/60 bg-emerald-300/10" : "border-white/10 bg-black/30 hover:border-emerald-300/20 hover:bg-black/40"}`}
                    >
                      <div className="mb-4 flex items-center justify-between">
                        <div className="role-icon flex h-12 w-12 items-center justify-center rounded-2xl border border-white/30 bg-gradient-to-br from-white/90 via-white/75 to-white/60 shadow-[0_0_18px_rgba(255,59,59,0.25),inset_0_0_0_1px_rgba(255,255,255,0.6)]">
                          <img src={getRoleIconSrc(item.icon)} alt={item.name} width={26} height={26} draggable={false} className="role-icon-img" />
                        </div>
                        <span className="rounded-full border border-signal/30 bg-signal/10 px-2.5 py-1 text-[0.58rem] uppercase tracking-[0.16em] text-signal">{item.signal}</span>
                      </div>
                      <div className="font-semibold uppercase tracking-[0.04em] text-frost">{item.name}</div>
                      <div className="mt-2 text-sm leading-6 text-frost/75">{disabled ? "Coming Soon" : "Available now"}</div>
                    </button>
                  );
                })}
              </div>
              <div className="mt-6 flex items-center gap-3">
                <button onClick={prev} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted">BACK</button>
                <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE</button>
              </div>
            </motion.div>
          )}

          {step === 2 && (
            <motion.div key="maturity" initial={{ opacity: 0, x: 30 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
              <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">SELECT MATURITY PATHWAY</h3>
              <p className="text-muted mt-2">Choose the governance level for this AI workforce deployment.</p>
              <div className="relative mt-8">
                <div className="absolute left-6 right-6 top-11 hidden h-px bg-gradient-to-r from-white/10 via-amber/50 to-white/10 md:block" />
                <div className="grid gap-4 md:grid-cols-4">
                  {maturities.map((item, index) => {
                    const disabled = item.id !== "L2";
                    const active = maturity === item.id;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => !disabled && setMaturity(item.id)}
                        disabled={disabled}
                        className={`relative rounded-3xl border p-4 text-left transition ${disabled ? "cursor-not-allowed opacity-60 border-white/10 bg-black/20" : active ? "border-emerald-300/60 bg-emerald-300/10" : "border-white/10 bg-black/30 hover:border-emerald-300/20 hover:bg-black/40"}`}
                      >
                        <MaturityRings level={index + 1} active={active} />
                        <div className="mt-4 text-center">
                          <div className="text-lg font-bold uppercase">{item.id}</div>
                          <div className="mt-1 text-[0.62rem] uppercase tracking-[0.18em] text-amber">{item.label.toUpperCase()}</div>
                          <div className="text-sm text-frost/70 mt-2">{item.desc}</div>
                          <div className="mt-2 text-[11px] uppercase tracking-[0.08em] text-muted">{disabled ? "FUTURE RELEASE" : "AVAILABLE"}</div>
                        </div>
                      </button>
                    );
                  })}
                </div>
              </div>
              <div className="mt-6 flex items-center gap-3">
                <button onClick={prev} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted">BACK</button>
                <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE</button>
              </div>
            </motion.div>
          )}

          {step === 3 && (
            <motion.div key="kras" initial={{ opacity: 0, x: 30 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
              <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">
                WHAT SHOULD {(agentName || normalizedName || "THIS AGENT").toUpperCase()} HANDLE?
              </h3>
              <p className="text-muted mt-2">Choose the responsibilities that will shape the operational dashboard.</p>

              {/* Predefined KRAs grid */}
              <div className="mt-6 grid gap-4 md:grid-cols-2">
                {predefinedKraCatalog.map((kra) => (
                  <label key={kra.id} className="flex cursor-pointer items-start gap-4 rounded-3xl border border-white/10 bg-black/30 p-5 transition hover:-translate-y-0.5 hover:border-emerald-300/20">
                    <input
                      type="checkbox"
                      checked={selectedKRAs.includes(kra.id)}
                      onChange={() => toggleKRA(kra.id)}
                      className="mt-1 h-4 w-4 accent-emerald-300"
                    />
                    <div>
                      <div className="font-semibold uppercase tracking-[0.04em] text-frost">{kra.id}</div>
                      <div className="mt-2 text-sm text-frost/70">{kra.desc}</div>
                    </div>
                  </label>
                ))}
              </div>

              {/* Custom KRAs list — shown ABOVE the inputs */}
              {customKras.length > 0 && (() => {
                const search = customKraSearch.trim().toLowerCase();
                const filtered = search
                  ? customKras.filter((k) =>
                      k.name.toLowerCase().includes(search) ||
                      (k.description || "").toLowerCase().includes(search)
                    )
                  : customKras;
                const selectedCount = customKras.filter((k) => k.selected !== false).length;
                const allSelected = selectedCount === customKras.length;
                const noneSelected = selectedCount === 0;
                const visibleSelectedCount = filtered.filter((k) => k.selected !== false).length;
                return (
                  <div className="mt-4 rounded-3xl border border-white/10 bg-black/30 p-5">
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                      <div className="flex flex-wrap items-center gap-3">
                        <label className="flex cursor-pointer items-center gap-2 rounded-full border border-white/10 bg-black/40 px-3 py-1.5 transition hover:border-emerald-300/30">
                          <input
                            type="checkbox"
                            checked={allSelected}
                            ref={(el) => {
                              if (el) el.indeterminate = !allSelected && !noneSelected;
                            }}
                            onChange={(event) => setAllCustomKRAsSelected(event.target.checked)}
                            aria-label="Toggle all custom KRAs"
                            className="h-3.5 w-3.5 accent-emerald-300 cursor-pointer"
                          />
                          <span className="text-[0.58rem] uppercase tracking-[0.18em] text-frost/80">
                            {`${selectedCount}/${customKras.length} Selected`}
                          </span>

                        </label>
                        <button
                          type="button"
                          onClick={() => setAllCustomKRAsSelected(true)}
                          disabled={allSelected}
                          className="normal-case rounded-full border border-emerald-300/30 bg-emerald-300/10 px-3 py-1.5 text-[0.65rem] font-semibold tracking-[0.04em] text-emerald-200 transition hover:border-emerald-300/60 hover:bg-emerald-300/15 disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          ✓&nbsp;Tick All
                        </button>
                        <button
                          type="button"
                          onClick={() => setAllCustomKRAsSelected(false)}
                          disabled={noneSelected}
                          className="normal-case rounded-full border border-white/10 bg-white/[0.04] px-3 py-1.5 text-[0.65rem] font-semibold tracking-[0.04em] text-frost/80 transition hover:border-white/30 hover:bg-white/[0.08] disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          ✕&nbsp;Untick All
                        </button>


                      </div>
                      <div className="relative w-full sm:w-72">
                        <Search
                          size={14}
                          className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted"
                        />
                        <input
                          value={customKraSearch}
                          onChange={(event) => setCustomKraSearch(event.target.value)}
                          placeholder="Search custom KRAs..."
                          className="w-full rounded-full border border-white/10 bg-black/30 py-2 pl-9 pr-9 text-xs text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                        />
                        {customKraSearch ? (
                          <button
                            type="button"
                            onClick={() => setCustomKraSearch("")}
                            aria-label="Clear search"
                            className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full p-1 text-muted transition hover:bg-white/10 hover:text-frost"
                          >
                            <X size={12} />
                          </button>
                        ) : null}
                      </div>
                    </div>
                    {search && filtered.length > 0 ? (
                      <div className="mb-2 text-[0.58rem] uppercase tracking-[0.18em] text-muted">
                        {visibleSelectedCount} of {filtered.length} visible selected
                      </div>
                    ) : null}

                    <div
                      className="custom-kra-scroll space-y-3 overflow-y-auto pr-1"
                      style={{ maxHeight: "320px" }}
                    >
                      {filtered.length === 0 ? (
                        <div className="rounded-2xl border border-dashed border-white/10 bg-black/20 px-4 py-6 text-center text-xs text-muted">
                          No custom KRAs match “{customKraSearch}”.
                        </div>
                      ) : (
                        filtered.map((kra: CustomKra) => (
                          <div
                            key={kra.name}
                            className={`flex items-start gap-4 rounded-2xl border p-4 transition ${
                              kra.selected !== false
                                ? "border-emerald-300/25 bg-emerald-300/[0.04] hover:border-emerald-300/40"
                                : "border-white/5 bg-white/[0.02] opacity-50 hover:opacity-80"
                            }`}
                          >
                            <input
                              type="checkbox"
                              checked={kra.selected !== false}
                              onChange={() => toggleCustomKRA(kra.name)}
                              aria-label={`Toggle Custom KRA ${kra.name}`}
                              className="mt-1 h-4 w-4 shrink-0 accent-emerald-300 cursor-pointer"
                            />
                            <div className="min-w-0 flex-1">
                              <div className={`font-semibold uppercase tracking-[0.04em] break-words ${kra.selected !== false ? "text-frost" : "text-muted"}`}>
                                {kra.name}
                              </div>
                              {kra.description ? (
                                <div className={`mt-1.5 text-sm break-words ${kra.selected !== false ? "text-frost/75" : "text-muted/70"}`}>
                                  {kra.description}
                                </div>
                              ) : (
                                <div className="mt-1.5 text-[0.7rem] italic text-muted">
                                  No description provided
                                </div>
                              )}
                            </div>
                            <button
                              type="button"
                              onClick={() => removeCustomKRA(kra.name)}
                              className="shrink-0 rounded-lg border border-signal/30 bg-signal/10 px-2.5 py-1 text-[0.6rem] font-semibold uppercase tracking-[0.12em] text-signal transition hover:bg-signal/20"
                              aria-label={`Remove ${kra.name}`}
                            >
                              REMOVE
                            </button>
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                );
              })()}


              {/* ADD CUSTOM KRA — placed AT THE BOTTOM of step 3 */}
              <div className="mt-4 rounded-3xl border border-white/10 bg-black/30 p-5">
                <div className="mb-3 text-[0.62rem] uppercase tracking-[0.2em] text-amber">ADD CUSTOM KRA</div>
                <div className="flex flex-col gap-3">
                  <input
                    value={customKraName}
                    onChange={(event) => setCustomKraName(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addCustomKraFromInput();
                      }
                    }}
                    placeholder="KRA NAME (e.g. Disaster Recovery Drills)"
                    className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                  />
                  <textarea
                    value={customKraDescription}
                    onChange={(event) => setCustomKraDescription(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                        event.preventDefault();
                        addCustomKraFromInput();
                      }
                    }}
                    placeholder="KRA DESCRIPTION (e.g. Run quarterly failover tests for production RDS and document RTO/RPO attainment)"
                    rows={3}
                    className="min-w-0 flex-1 resize-none rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                  />
                  <div className="flex justify-end">
                    <button
                      type="button"
                      onClick={addCustomKraFromInput}
                      disabled={!customKraName.trim()}
                      className="rounded-2xl border border-white/10 bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:border-emerald-300/30 disabled:opacity-50"
                    >
                      ADD
                    </button>
                  </div>
                </div>
              </div>

              <div className="mt-6 flex items-center gap-3">
                <button onClick={prev} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted">BACK</button>
                <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE TO AWS TASKS</button>
              </div>
            </motion.div>
          )}

          {step === 4 && <AwsTasksStep onNext={next} onPrev={prev} />}

          {step === 5 && <AwsPermissionsStep onNext={next} onPrev={prev} />}

          {step === 6 && (
            <motion.div key="deploy" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <div className="mx-auto max-w-xl text-center">
                <div className="mx-auto flex h-52 w-52 items-center justify-center rounded-full border border-signal/30 bg-black/30 p-3 shadow-[0_0_60px_rgba(255,59,59,0.18)]">
                  <div
                    className="flex h-full w-full items-center justify-center rounded-full"
                    style={{ background: `conic-gradient(rgba(255,59,59,0.85) ${progress * 3.6}deg, rgba(255,255,255,0.08) 0deg)` }}
                  >
                    <div className="flex h-36 w-36 flex-col items-center justify-center rounded-full border border-signal/30 bg-black/85">
                      <div className="text-4xl font-semibold text-frost">{progress}%</div>
                      <div className="mt-1 text-[0.6rem] uppercase tracking-[0.2em] text-signal">STARTING</div>
                    </div>
                  </div>
                </div>
                <h3 className="mt-8 text-2xl font-semibold uppercase tracking-[0.02em]">
                  STARTING {(agentName || localName).toUpperCase()} DIGITAL WORKER.
                </h3>
                <p className="text-muted mt-2">Provisioning intelligence and establishing governed operational access.</p>
                <div className="mt-4 inline-flex items-center gap-2 rounded-full border border-signal/30 bg-black/40 px-3 py-1 text-[0.6rem] uppercase tracking-[0.18em] text-signal">
                  <span className={`h-1.5 w-1.5 rounded-full ${observationsStatus === "loading" ? "bg-signal pulse-core" : observationsStatus === "success" ? "bg-emerald-300" : observationsStatus === "error" ? "bg-amber" : "bg-muted"}`} />
                  {observationsStatus === "loading"
                    ? "RETRIEVING LIVE OPERATIONAL INTELLIGENCE..."
                    : observationsStatus === "success"
                    ? "LIVE OPERATIONAL INTELLIGENCE RECEIVED"
                    : observationsStatus === "error"
                    ? "LIVE DEPLOYMENT SYNC PAUSED"
                    : "AWAITING BACKEND SYNC"}
                </div>
                {observationsStatus === "error" && observationsErrorMessage ? (
                  <div className="mt-3">
                    <p className="text-[0.7rem] text-amber/80">{observationsErrorMessage}</p>
                  </div>
                ) : null}
                <div className="mt-6 rounded-3xl border border-signal/20 bg-black/30 p-5 text-left shadow-[0_0_24px_rgba(255,59,59,0.08)]">
                  <div className="flex items-center gap-3">
                    <Sparkles size={16} className="text-signal" />
                    <div>
                      <div className="text-sm font-semibold uppercase tracking-[0.04em] text-frost">{deploymentStages[Math.min(deployStage, deploymentStages.length - 1)]}</div>
                      <div className="mt-1 text-[0.65rem] uppercase tracking-[0.16em] text-muted">EMPLOYEE ID {employeeId || employeeIdPreview} - {permissions.length} ACCESS SCOPES</div>
                    </div>
                  </div>
                  <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-white/10">
                    <motion.div className="h-full rounded-full bg-signal" animate={{ width: `${progress}%` }} transition={{ duration: 0.6 }} />
                  </div>
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}