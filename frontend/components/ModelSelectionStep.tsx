import { Sparkles } from "lucide-react";

type ModelSelectionStepProps = {
  provider: "bedrock" | "openai" | null;
  setProvider: (p: "bedrock" | "openai" | null) => void;
  bedrockModel: string;
  setBedrockModel: (v: string) => void;
  openaiKey: string;
  setOpenaiKey: (v: string) => void;
  openaiBase: string;
  setOpenaiBase: (v: string) => void;
  openaiModel: string;
  setOpenaiModel: (v: string) => void;
};

export default function ModelSelectionStep({
  provider,
  setProvider,
  bedrockModel,
  setBedrockModel,
  openaiKey,
  setOpenaiKey,
  openaiBase,
  setOpenaiBase,
  openaiModel,
  setOpenaiModel
}: ModelSelectionStepProps) {
  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-3xl font-semibold tracking-[0.02em]">Select Model Provider</h2>
        <p className="mt-1 text-sm text-frost/60">Configure the AI engine for your digital worker.</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <button 
          onClick={() => setProvider("bedrock")}
          className={`p-6 rounded-2xl border transition-all duration-300 flex flex-col items-center gap-4 ${provider === "bedrock" ? "border-amber/60 bg-amber/10 shadow-[0_0_20px_rgba(255,160,59,0.1)]" : "border-signal/25 bg-black/40 hover:border-signal/50"}`}
        >
          <div className="w-16 h-16 rounded-full bg-black flex items-center justify-center border border-signal/20">
             <img src="/icons/aws.svg" alt="AWS" className="w-8 h-8 opacity-90" onError={(e) => (e.currentTarget.style.display='none')} />
          </div>
          <span className="font-semibold tracking-[0.08em] text-sm uppercase">AWS Bedrock</span>
        </button>

        <button 
          onClick={() => setProvider("openai")}
          className={`p-6 rounded-2xl border transition-all duration-300 flex flex-col items-center gap-4 ${provider === "openai" ? "border-amber/60 bg-amber/10 shadow-[0_0_20px_rgba(255,160,59,0.1)]" : "border-signal/25 bg-black/40 hover:border-signal/50"}`}
        >
          <div className="w-16 h-16 rounded-full bg-black flex items-center justify-center border border-signal/20">
            <Sparkles className="w-8 h-8 text-amber/80" />
          </div>
          <span className="font-semibold tracking-[0.08em] text-sm uppercase">OpenAI Compatible</span>
        </button>
      </div>

      <div className="min-h-[220px]">
        {provider === "bedrock" && (
          <div className="animate-in fade-in slide-in-from-bottom-4 duration-500 flex flex-col gap-5 p-5 rounded-xl border border-signal/20 bg-black/30">
            <h3 className="text-xs font-bold tracking-[0.15em] text-amber uppercase">Bedrock Configuration</h3>
            <div className="flex flex-col gap-1.5">
              <label className="text-[0.65rem] font-semibold tracking-[0.15em] text-muted uppercase">Model Name</label>
              <input 
                type="text" 
                value={bedrockModel}
                onChange={(e) => setBedrockModel(e.target.value)}
                className="w-full bg-black/60 border border-signal/30 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-amber/60 focus:bg-black transition-colors text-frost placeholder:text-muted"
              />
            </div>
            <p className="text-[0.7rem] text-muted italic">AWS keys will be securely loaded from the backend environment.</p>
          </div>
        )}

        {provider === "openai" && (
          <div className="animate-in fade-in slide-in-from-bottom-4 duration-500 flex flex-col gap-5 p-5 rounded-xl border border-signal/20 bg-black/30">
             <h3 className="text-xs font-bold tracking-[0.15em] text-amber uppercase">OpenAI Endpoint Configuration</h3>
             <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <div className="flex flex-col gap-1.5">
                  <label className="text-[0.65rem] font-semibold tracking-[0.15em] text-muted uppercase">API Key</label>
                  <input 
                    type="text" 
                    value={openaiKey}
                    onChange={(e) => setOpenaiKey(e.target.value)}
                    className="w-full bg-black/60 border border-signal/30 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-amber/60 focus:bg-black transition-colors text-frost placeholder:text-muted"
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <label className="text-[0.65rem] font-semibold tracking-[0.15em] text-muted uppercase">API Base URL</label>
                  <input 
                    type="text" 
                    value={openaiBase}
                    onChange={(e) => setOpenaiBase(e.target.value)}
                    className="w-full bg-black/60 border border-signal/30 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-amber/60 focus:bg-black transition-colors text-frost placeholder:text-muted"
                  />
                </div>
             </div>
             <div className="flex flex-col gap-1.5">
                <label className="text-[0.65rem] font-semibold tracking-[0.15em] text-muted uppercase">Model Name</label>
                <input 
                  type="text" 
                  value={openaiModel}
                  onChange={(e) => setOpenaiModel(e.target.value)}
                  className="w-full bg-black/60 border border-signal/30 rounded-lg px-4 py-2.5 text-sm focus:outline-none focus:border-amber/60 focus:bg-black transition-colors text-frost placeholder:text-muted"
                />
              </div>
          </div>
        )}
      </div>
    </div>
  );
}
