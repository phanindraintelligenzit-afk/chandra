"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useRouter } from "next/navigation";
import { KeyRound, ShieldCheck, Sparkles } from "lucide-react";
import { useOnboarding } from "@/store/OnboardingContext";
import { fetchAgentObservations } from "@/services/api";
import { buildKraPayload } from "@/services/mapping";
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
import { normalizeKraName, predefinedKraCatalog } from "@/store/kraCatalog";

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
        <span className="text-[0.55rem] uppercase tracking-[0.2em] text-muted">AGENT</span>
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
    completeOnboarding,
    setObservations
  } = useOnboarding();
  const [step, setStep] = useState(0);
  const [localName, setLocalName] = useState(agentName || "");
  const [deployStage, setDeployStage] = useState<number>(0);
  const [deployProgress, setDeployProgress] = useState<number>(0);
  const [customKraInput, setCustomKraInput] = useState("");
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

  const canNext = useMemo(() => {
    if (step === 0) return normalizedName.length > 0 && !duplicateName && hasSelectedAvatar;
    if (step === 1) return role === "AWS Cloud Engineer";
    if (step === 2) return maturity === "L2";
    if (step === 3) return selectedKRAs.length > 0;
    if (step === 4) return permissions.length > 0;
    return true;
  }, [step, normalizedName.length, duplicateName, hasSelectedAvatar, role, maturity, selectedKRAs.length, permissions.length]);

  function next() {
    if (step === 0) {
      if (duplicateName) {
        setNotice("This agent name is already registered. Choose a different workforce identity.");
        return;
      }
      setAgentName(normalizedName);
      setEmployeeId(employeeIdPreview);
    }
    if (step === 4) {
      if (deploymentStartedRef.current) return;
      deploymentStartedRef.current = true;
      setStep(5);
      runDeploymentSequence();
      return;
    }
    setStep((s) => Math.min(s + 1, 5));
  }

  function prev() {
    setStep((s) => Math.max(s - 1, 0));
  }

  function addCustomKrasFromInput() {
    const entries = customKraInput
      .split(/[\n,]+/)
      .map((entry) => normalizeKraName(entry))
      .filter(Boolean);

    entries.forEach((entry) => addCustomKRA(entry));
    if (entries.length) setCustomKraInput("");
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

    const kraPayloadEntries = buildKraPayload(predefinedKras, customKras);
    const payload = {
      region: "us-east-1",
      kras: kraPayloadEntries,
      selected_kras: selectedKRAs,
      custom_kras: customKras,
      maturity_level: maturity,
      deployment: {
        role,
        permissions,
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

      let data = null;
      let attempt = 0;
      const maxAttempts = 5;
      
      while (!data && !controller.signal.aborted && attempt < maxAttempts) {
        attempt += 1;
        console.log(`DEPLOYMENT ATTEMPT ${attempt}/${maxAttempts}`, payload);
        setObservationsStatus("loading");
        setObservationsErrorMessage(attempt > 1 ? `Live backend still initializing. Retrying telemetry request ${attempt}/${maxAttempts}.` : "");
        try {
          data = await fetchAgentObservations(payload, { signal: controller.signal });
          console.log("OBS RESPONSE SUCCESS", data);
        } catch (error) {
          if (controller.signal.aborted) return;
          const message = error instanceof Error ? error.message : "Backend request failed";
          console.error(`OBS RESPONSE ERROR (Attempt ${attempt})`, message);
          setObservationsErrorMessage(`${message}. Waiting for live operational response.`);
          if (attempt < maxAttempts) {
            await wait(Math.min(10_000, 1_500 * attempt));
          }
        }
      }
      
      if (controller.signal.aborted) return;

      if (!data) {
        console.warn("MAX ATTEMPTS REACHED - PROCEEDING TO DASHBOARD WITH FALLBACK");
        setObservationsErrorMessage("Maximum attempts reached. Dashboard will continue trying in background.");
      }

      setDeployStage(4);
      await animateProgressTo(deploymentTargets[4], controller.signal);
      await wait(220);

      if (!data) {
        setObservations(null, "Live operational intelligence timeout. Dashboard will continue retrying.");
      } else {
        setObservations(data);
      }

      setDeployStage(5);
      await animateProgressTo(deploymentTargets[5], controller.signal);
      await wait(220);

      if (!controller.signal.aborted) {
        setObservationsStatus("success");
        setDeployStage(6);
        await animateProgressTo(100, controller.signal);
        await wait(420);
        completeOnboarding();
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
            <div className="text-[0.58rem] uppercase tracking-[0.2em] text-amber">IDENTITY REGISTRY</div>
            <div className="mt-1 text-frost/85">{notice}</div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <div className="onboarding-shell relative z-10 w-full max-w-5xl rounded-2xl border border-signal/25 bg-gradient-to-b from-black/70 via-black/50 to-black/40 p-6 shadow-[0_30px_80px_rgba(255,59,59,0.18),0_0_0_1px_rgba(255,59,59,0.08)] backdrop-blur">
        <div className="mb-5 flex min-h-[60px] items-start justify-between gap-4">
          <div className="flex items-center gap-2 text-[0.6rem] uppercase tracking-[0.24em] text-signal">
            <span className="h-1.5 w-1.5 rounded-full bg-signal pulse-core" />
            CHANDRA OPERATIONAL ONBOARDING
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
                  <div className="text-[0.65rem] uppercase tracking-[0.24em] text-signal">CHANDRA IDENTITY REGISTRY</div>
                  <h2 className="mt-3 text-3xl font-semibold uppercase tracking-[0.02em]">DEFINE YOUR DIGITAL OPERATOR IDENTITY</h2>
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
              <div className="mt-4 rounded-3xl border border-white/10 bg-black/30 p-5">
                <div className="flex flex-col gap-3 sm:flex-row">
                  <input
                    value={customKraInput}
                    onChange={(event) => setCustomKraInput(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        addCustomKrasFromInput();
                      }
                    }}
                    placeholder="ADD CUSTOM KRA..."
                    className="min-w-0 flex-1 rounded-2xl border border-white/10 bg-black/20 px-4 py-3 text-sm text-frost outline-none transition placeholder:text-muted focus:border-emerald-300/40 focus:ring-2 focus:ring-emerald-300/15"
                  />
                  <button
                    type="button"
                    onClick={addCustomKrasFromInput}
                    disabled={!customKraInput.trim()}
                    className="rounded-2xl border border-white/10 bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 transition hover:border-emerald-300/30 disabled:opacity-50"
                  >
                    ADD
                  </button>
                </div>
                {customKras.length > 0 ? (
                  <div className="mt-4 grid gap-4 md:grid-cols-2">
                    {customKras.map((kra) => (
                      <label key={kra} className="flex cursor-pointer items-start gap-4 rounded-3xl border border-white/10 bg-black/30 p-5 transition hover:border-emerald-300/20">
                        <input
                          type="checkbox"
                          checked
                          onChange={() => removeCustomKRA(kra)}
                          className="mt-1 h-4 w-4 accent-emerald-300"
                        />
                        <div>
                          <div className="font-semibold uppercase tracking-[0.04em] text-frost">{kra}</div>
                          <div className="mt-2 text-sm text-frost/70">Custom operational KRA included in agent evaluation.</div>
                        </div>
                      </label>
                    ))}
                  </div>
                ) : null}
              </div>
              <div className="mt-6 flex items-center gap-3">
                <button onClick={prev} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted">BACK</button>
                <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE</button>
              </div>
            </motion.div>
          )}

          {step === 4 && (
            <motion.div key="permissions" initial={{ opacity: 0, x: 30 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -20 }}>
              <h3 className="text-2xl font-semibold uppercase tracking-[0.02em]">GIVE ME PERMISSIONS.</h3>
              <p className="text-muted mt-2">
                Configure the operational systems {agentName || normalizedName || "this agent"} can access under governance controls.
              </p>
              <div className="mt-6 grid gap-3 md:grid-cols-2">
                {permissionCatalog.map((permission) => {
                  const active = permissions.includes(permission.id);
                  return (
                    <button
                      key={permission.id}
                      type="button"
                      onClick={() => togglePermission(permission.id)}
                      className={`flex items-start gap-4 rounded-3xl border p-4 text-left transition hover:-translate-y-0.5 ${
                        active ? "border-emerald-300/50 bg-emerald-300/10" : "border-white/10 bg-black/30 hover:border-emerald-300/20"
                      }`}
                    >
                      <span className={`mt-0.5 flex h-9 w-9 items-center justify-center rounded-full border ${active ? "border-emerald-300/50 bg-emerald-300/15 text-emerald-200" : "border-white/10 bg-black/40 text-muted"}`}>
                        {active ? <ShieldCheck size={16} /> : <KeyRound size={16} />}
                      </span>
                      <span>
                        <span className="block font-semibold uppercase tracking-[0.04em] text-frost">{permission.label}</span>
                        <span className="mt-1 block text-sm leading-5 text-frost/65">{permission.desc}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
              <div className="mt-6 flex items-center gap-3">
                <button onClick={prev} className="rounded-2xl border border-white/10 px-4 py-3 text-sm uppercase tracking-[0.14em] text-muted">BACK</button>
                <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-signal px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-black disabled:opacity-50">DEPLOY DIGITAL EMPLOYEE</button>
              </div>
            </motion.div>
          )}

          {step === 5 && (
            <motion.div key="deploy" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
              <div className="mx-auto max-w-xl text-center">
                <div className="mx-auto flex h-52 w-52 items-center justify-center rounded-full border border-signal/30 bg-black/30 p-3 shadow-[0_0_60px_rgba(255,59,59,0.18)]">
                  <div
                    className="flex h-full w-full items-center justify-center rounded-full"
                    style={{ background: `conic-gradient(rgba(255,59,59,0.85) ${progress * 3.6}deg, rgba(255,255,255,0.08) 0deg)` }}
                  >
                    <div className="flex h-36 w-36 flex-col items-center justify-center rounded-full border border-signal/30 bg-black/85">
                      <div className="text-4xl font-semibold text-frost">{progress}%</div>
                      <div className="mt-1 text-[0.6rem] uppercase tracking-[0.2em] text-signal">DEPLOYING</div>
                    </div>
                  </div>
                </div>
                <h3 className="mt-8 text-2xl font-semibold uppercase tracking-[0.02em]">
                  DEPLOYING {(agentName || localName).toUpperCase()}.
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
