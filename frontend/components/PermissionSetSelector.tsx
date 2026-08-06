import { useEffect, useState } from "react";
import { Loader2, ShieldCheck, AlertCircle } from "lucide-react";
import { getApiUrl } from "../lib/utils";

export default function PermissionSetSelector({
  requiredPermissions,
  onAttach
}: {
  requiredPermissions: any[];
  onAttach: (permissionSetId: string) => void;
}) {
  const [loading, setLoading] = useState(true);
  const [recommendation, setRecommendation] = useState<any>(null);
  const [availableSets, setAvailableSets] = useState<any[]>([]);
  const [selectedSet, setSelectedSet] = useState<string>("");

  useEffect(() => {
    let mounted = true;
    const fetchRecommendation = async () => {
      try {
        // Fetch all sets
        const setsRes = await fetch(getApiUrl("/api/permission-sets"));
        if (setsRes.ok) {
          const setsData = await setsRes.json();
          if (mounted && setsData.permissions) setAvailableSets(setsData.permissions);
        }

        // Fetch recommendation
        const recRes = await fetch(getApiUrl("/api/permission-sets/recommend"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ required_permissions: requiredPermissions })
        });
        if (recRes.ok) {
          const data = await recRes.json();
          if (mounted && data.recommendation) {
            setRecommendation(data.recommendation);
            if (data.recommendation.recommendation_type === "existing" && data.recommendation.permission_set_id) {
              setSelectedSet(data.recommendation.permission_set_id);
            } else {
              // If new is recommended, default to empty or the first available
              if (setsData.permissions && setsData.permissions.length > 0) {
                setSelectedSet(setsData.permissions[0].id);
              }
            }
          }
        }
      } catch (err) {
        console.error("Failed to fetch recommendation", err);
      } finally {
        if (mounted) setLoading(false);
      }
    };
    fetchRecommendation();
    return () => { mounted = false; };
  }, [requiredPermissions]);

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-cyan-300 text-xs py-2">
        <Loader2 size={12} className="animate-spin" />
        Copilot analyzing permission sets...
      </div>
    );
  }

  return (
    <div className="mt-4 border border-cyan-400/20 bg-cyan-400/5 rounded p-3">
      <div className="text-[0.6rem] uppercase tracking-widest text-cyan-300 mb-2 font-bold flex items-center gap-2">
        <ShieldCheck size={12} />
        Attach Permission Set
      </div>
      
      {recommendation && (
        <div className="mb-3 bg-black/30 p-2 rounded text-xs border border-white/5">
          <div className="text-frost font-semibold mb-1">
            Copilot Recommendation: {recommendation.recommendation_type === "existing" ? "Use Existing Set" : "Create New Set"}
          </div>
          <div className="text-muted italic">{recommendation.reason}</div>
          {recommendation.recommendation_type === "new" && recommendation.suggested_new_set && (
            <div className="mt-2 text-orange-300">
              <AlertCircle size={10} className="inline mr-1" />
              Suggested New Set Name: {recommendation.suggested_new_set.name}
            </div>
          )}
        </div>
      )}

      <div className="mb-3">
        <label className="block text-[0.6rem] text-muted mb-1 uppercase tracking-wider">Select Permission Set</label>
        <select
          value={selectedSet}
          onChange={(e) => setSelectedSet(e.target.value)}
          className="w-full bg-black/50 border border-cyan-400/30 rounded px-2 py-1.5 text-xs text-frost outline-none focus:border-cyan-400/60"
        >
          {availableSets.map(set => (
            <option key={set.id} value={set.id}>{set.name} (v{set.version})</option>
          ))}
        </select>
      </div>

      <button
        onClick={() => {
          if (selectedSet) onAttach(selectedSet);
        }}
        disabled={!selectedSet}
        className="w-full bg-cyan-400/20 hover:bg-cyan-400/30 text-cyan-300 border border-cyan-400/40 rounded py-1.5 text-[0.65rem] uppercase tracking-widest font-bold transition disabled:opacity-50"
      >
        Attach & Approve
      </button>
    </div>
  );
}
