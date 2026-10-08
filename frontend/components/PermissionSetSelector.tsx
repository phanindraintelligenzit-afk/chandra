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
        let fetchedSets: any[] = [];
        // Fetch all sets
        const setsRes = await fetch(getApiUrl("/api/permission-sets"));
        if (setsRes.ok) {
          const setsData = await setsRes.json();
          if (setsData.permissions && Array.isArray(setsData.permissions) && setsData.permissions.length > 0) {
            fetchedSets = setsData.permissions;
          }
        }

        if (fetchedSets.length === 0) {
          fetchedSets = [
            { id: "eab39a74-a48a-4f19-9803-e71e37cc4d62", name: "S3 Bucket Operator", version: 1, aws_service: "S3" },
            { id: "4bbb47a9-d7f7-4921-81e4-1f3d5f215579", name: "EC2 Operator", version: 1, aws_service: "EC2" },
            { id: "ps_1786690414403", name: "Lambda Deployer Access", version: 1, aws_service: "Lambda" },
            { id: "c1f7cdb2-0551-4cd7-be8c-4f508dc4e37f", name: "VPC Admin", version: 1, aws_service: "VPC" }
          ];
        }

        // Always prioritize S3 Bucket Operator and EC2 Operator at top of dropdown
        const priorityOrder = ["s3 bucket operator", "ec2 operator"];
        fetchedSets.sort((a, b) => {
          const aIdx = priorityOrder.indexOf((a.name || "").toLowerCase().trim());
          const bIdx = priorityOrder.indexOf((b.name || "").toLowerCase().trim());
          if (aIdx !== -1 && bIdx !== -1) return aIdx - bIdx;
          if (aIdx !== -1) return -1;
          if (bIdx !== -1) return 1;
          return (a.name || "").localeCompare(b.name || "");
        });

        if (mounted) setAvailableSets(fetchedSets);

        // Auto-select based on required permissions / request title
        const reqStr = JSON.stringify(requiredPermissions || []).toLowerCase();
        let defaultMatch = fetchedSets[0]?.id || "";
        const isVpc = reqStr.includes("vpc") || reqStr.includes("subnet") || reqStr.includes("network") || reqStr.includes("cidr") || reqStr.includes("route_table");
        const isLambda = reqStr.includes("lambda") || reqStr.includes("function") || reqStr.includes("serverless");
        const isEc2 = !isVpc && (reqStr.includes("ec2") || reqStr.includes("instance") || reqStr.includes("intance") || reqStr.includes("server") || reqStr.includes("vm"));
        const isS3 = reqStr.includes("s3") || reqStr.includes("bucket");
        const isDynamo = reqStr.includes("dynamo") || reqStr.includes("table");
        const isRds = reqStr.includes("rds") || reqStr.includes("database");

        if (isVpc) {
          const vpcSet = fetchedSets.find(s => s.name?.toLowerCase().includes("vpc") || s.aws_service === "VPC");
          if (vpcSet) defaultMatch = vpcSet.id;
        } else if (isLambda) {
          const lamSet = fetchedSets.find(s => s.name?.toLowerCase().includes("lambda") || s.aws_service === "Lambda");
          if (lamSet) defaultMatch = lamSet.id;
        } else if (isEc2) {
          const ec2Set = fetchedSets.find(s => s.name?.toLowerCase().includes("ec2") || s.aws_service === "EC2");
          if (ec2Set) defaultMatch = ec2Set.id;
        } else if (isS3) {
          const s3Set = fetchedSets.find(s => s.name?.toLowerCase().includes("s3") || s.aws_service === "S3");
          if (s3Set) defaultMatch = s3Set.id;
        } else if (isDynamo) {
          const dynamoSet = fetchedSets.find(s => s.name?.toLowerCase().includes("dynamo") || s.aws_service === "DynamoDB");
          if (dynamoSet) defaultMatch = dynamoSet.id;
        } else if (isRds) {
          const rdsSet = fetchedSets.find(s => s.name?.toLowerCase().includes("rds") || s.aws_service === "RDS");
          if (rdsSet) defaultMatch = rdsSet.id;
        }
        if (mounted && defaultMatch) setSelectedSet(defaultMatch);

        // Fetch recommendation only if not already explicitly matching
        const recRes = await fetch(getApiUrl("/api/permission-sets/recommend"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ required_permissions: requiredPermissions })
        });
        if (recRes.ok) {
          const data = await recRes.json();
          if (mounted && data.recommendation) {
            setRecommendation(data.recommendation);
            // Only override if the recommendation is valid and matches the requested service
            if (data.recommendation.recommendation_type === "existing" && data.recommendation.permission_set_id) {
              const recSet = fetchedSets.find(s => s.id === data.recommendation.permission_set_id);
              if (isVpc && recSet && (recSet.aws_service === "VPC" || recSet.name?.toLowerCase().includes("vpc"))) {
                setSelectedSet(data.recommendation.permission_set_id);
              } else if (isLambda && recSet && (recSet.aws_service === "Lambda" || recSet.name?.toLowerCase().includes("lambda"))) {
                setSelectedSet(data.recommendation.permission_set_id);
              } else if (isEc2 && recSet && (recSet.aws_service === "EC2" || recSet.name?.toLowerCase().includes("ec2"))) {
                setSelectedSet(data.recommendation.permission_set_id);
              } else if (isS3 && recSet && (recSet.aws_service === "S3" || recSet.name?.toLowerCase().includes("s3"))) {
                setSelectedSet(data.recommendation.permission_set_id);
              } else if (!isEc2 && !isS3 && !isVpc && !isLambda) {
                setSelectedSet(data.recommendation.permission_set_id);
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
        Set & Approve
      </button>
    </div>
  );
}
