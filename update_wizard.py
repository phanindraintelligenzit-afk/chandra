import re
import sys

with open("frontend/components/OnboardingWizard.tsx", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Insert import
content = content.replace(
    'import { useOnboarding } from "@/store/OnboardingContext";',
    'import ModelSelectionStep from "./ModelSelectionStep";\nimport { useOnboarding } from "@/store/OnboardingContext";'
)

# 2. Insert State
state_block = """  const [step, setStep] = useState(0);
  const [llmProvider, setLlmProvider] = useState<"bedrock" | "openai" | null>(null);
  const [bedrockModel, setBedrockModel] = useState("anthropic.claude-3-sonnet-20240229-v1:0");
  const [openaiKey, setOpenaiKey] = useState("dummy-not-needed");
  const [openaiBase, setOpenaiBase] = useState("http://52.2.42.146:8000/v1");
  const [openaiModel, setOpenaiModel] = useState("google/gemma-4-31B-it-qat-w4a16-ct");
  const [modelStatus, setModelStatus] = useState<"idle" | "loading" | "success" | "error">("idle");"""
content = content.replace('  const [step, setStep] = useState(0);', state_block)

# 3. Replace canNext
old_can_next = """  const canNext = useMemo(() => {
    if (step === 0) return normalizedName.length > 0 && !duplicateName && hasSelectedAvatar;
    if (step === 1) return role === "AWS Cloud Engineer";
    if (step === 2) return maturity === "L2";
    if (step === 3) return selectedKRAs.length > 0;
    return true;
  }, [step, normalizedName.length, duplicateName, hasSelectedAvatar, role, maturity, selectedKRAs.length]);"""

new_can_next = """  const canNext = useMemo(() => {
    if (step === 0) return normalizedName.length > 0 && !duplicateName && hasSelectedAvatar;
    if (step === 1) return llmProvider !== null && modelStatus !== "loading";
    if (step === 2) return role === "AWS Cloud Engineer";
    if (step === 3) return maturity === "L2";
    if (step === 4) return selectedKRAs.length > 0;
    return true;
  }, [step, normalizedName.length, duplicateName, hasSelectedAvatar, role, maturity, selectedKRAs.length, llmProvider, modelStatus]);"""

content = content.replace(old_can_next, new_can_next)

# 4. Replace next()
old_next = """  function next() {
    if (step === 0) {
      if (duplicateName) {
        setNotice("This agent name is already registered. Choose a different workforce identity.");
        return;
      }
      setAgentName(normalizedName);
      setEmployeeId(employeeIdPreview);
    }
    if (step === 3) {
      if (deploymentStartedRef.current) return;
      deploymentStartedRef.current = true;
      setStep(4);
      runDeploymentSequence();
      return;
    }
    setStep((s) => Math.min(s + 1, 4));
  }"""

new_next = """  async function next() {
    if (step === 0) {
      if (duplicateName) {
        setNotice("This agent name is already registered. Choose a different workforce identity.");
        return;
      }
      setAgentName(normalizedName);
      setEmployeeId(employeeIdPreview);
    }
    if (step === 1) {
      setModelStatus("loading");
      const payload = llmProvider === "bedrock" 
        ? { provider: "bedrock", model_name: bedrockModel }
        : { provider: "openai", model_name: openaiModel, api_key: openaiKey, api_base: openaiBase };
      
      try {
        const res = await fetch("http://localhost:6001/settings/model/test-and-save", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok || data.status === "error") {
          setModelStatus("error");
          setNotice(data.message || "Failed to connect to LLM");
          return;
        }
        setModelStatus("success");
      } catch (err: any) {
        setModelStatus("error");
        setNotice(err.message || "Network error");
        return;
      }
    }
    if (step === 4) {
      if (deploymentStartedRef.current) return;
      deploymentStartedRef.current = true;
      setStep(5);
      runDeploymentSequence();
      return;
    }
    setStep((s) => Math.min(s + 1, 5));
  }"""

content = content.replace(old_next, new_next)

# Shift existing step blocks: 
# We need to shift step === 4 to 5, step === 3 to 4, step === 2 to 3, step === 1 to 2
content = content.replace('step === 4', 'step === 5')
content = content.replace('step === 3', 'step === 4')
content = content.replace('step === 2', 'step === 3')
content = content.replace('step === 1', 'step === 2')

# Now insert step === 1 block right after step === 0 block ends.
# The step === 0 block ends with:
#                 <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE</button>
#               </div>
#             </motion.div>
#           )}
# We will use replace with count=1 so it ONLY replaces the FIRST occurrence (which is step 0).

end_of_step_0 = """                <button onClick={next} disabled={!canNext} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50">CONTINUE</button>
              </div>
            </motion.div>
          )}"""

step_1_block = """

          {step === 1 && (
            <motion.div key="model" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -16 }}>
              <ModelSelectionStep 
                provider={llmProvider}
                setProvider={setLlmProvider}
                bedrockModel={bedrockModel}
                setBedrockModel={setBedrockModel}
                openaiKey={openaiKey}
                setOpenaiKey={setOpenaiKey}
                openaiBase={openaiBase}
                setOpenaiBase={setOpenaiBase}
                openaiModel={openaiModel}
                setOpenaiModel={setOpenaiModel}
              />
              <div className="mt-8 flex items-center justify-between border-t border-signal/15 pt-5">
                <button onClick={prev} className="rounded-2xl px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-muted hover:text-frost">BACK</button>
                <div className="flex items-center gap-3">
                  {modelStatus === "loading" && <div className="text-sm text-amber flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-amber pulse-core"></span> Testing Connection...</div>}
                  {modelStatus === "success" && <div className="text-sm text-emerald-400">Connection Verified</div>}
                  <button onClick={next} disabled={!canNext || modelStatus === "loading"} className="ml-auto rounded-2xl bg-emerald-300/10 px-5 py-3 text-sm font-semibold uppercase tracking-[0.14em] text-emerald-200 disabled:opacity-50 flex items-center gap-2">
                    CONTINUE
                  </button>
                </div>
              </div>
            </motion.div>
          )}"""

if end_of_step_0 in content:
    content = content.replace(end_of_step_0, end_of_step_0 + step_1_block, 1) # <--- COUNT=1 IS THE FIX
else:
    print("Error: Could not find end of step 0 block")
    sys.exit(1)

with open("frontend/components/OnboardingWizard.tsx", "w", encoding="utf-8") as f:
    f.write(content)
print("Successfully updated OnboardingWizard.tsx")
