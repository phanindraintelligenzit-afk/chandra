"use client";

import { useState } from "react";
import { motion } from "framer-motion";
import { CheckCircle2, Plus, X, Save, ArrowLeft } from "lucide-react";

const AWS_SERVICES = [
  { value: "aws_s3_bucket", label: "S3 Bucket", icon: "🗄️" },
  { value: "aws_instance", label: "EC2 Instance", icon: "🖥️" },
  { value: "aws_vpc", label: "VPC Network", icon: "🌐" },
  { value: "aws_lambda_function", label: "Lambda Function", icon: "⚡" },
  { value: "aws_db_instance", label: "RDS Database", icon: "🗃️" },
  { value: "aws_iam_role", label: "IAM Role", icon: "🔑" },
  { value: "aws_iam_policy", label: "IAM Policy", icon: "📜" },
  { value: "aws_dynamodb_table", label: "DynamoDB Table", icon: "📊" },
  { value: "aws_sqs_queue", label: "SQS Queue", icon: "📨" },
  { value: "aws_sns_topic", label: "SNS Topic", icon: "🔔" },
  { value: "aws_ecs_service", label: "ECS Service", icon: "🐳" },
  { value: "aws_lb", label: "Load Balancer", icon: "⚖️" },
  { value: "aws_cloudfront_distribution", label: "CloudFront", icon: "🌍" },
  { value: "aws_elasticache_cluster", label: "ElastiCache", icon: "⚡" },
  { value: "aws_api_gateway_rest_api", label: "API Gateway", icon: "🔌" },
  { value: "aws_security_group", label: "Security Group", icon: "🛡️" },
];

const IAM_ACTIONS_BY_SERVICE: Record<string, string[]> = {
  aws_s3_bucket: ["s3:CreateBucket", "s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:PutBucketPolicy", "s3:PutEncryptionConfig", "s3:PutBucketVersioning", "s3:PutBucketPublicAccessBlock"],
  aws_instance: ["ec2:RunInstances", "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances", "ec2:DescribeInstances", "ec2:CreateTags"],
  aws_vpc: ["ec2:CreateVpc", "ec2:CreateSubnet", "ec2:CreateSecurityGroup", "ec2:CreateRouteTable", "ec2:AssociateRouteTable", "ec2:CreateInternetGateway"],
  aws_lambda_function: ["lambda:CreateFunction", "lambda:InvokeFunction", "lambda:UpdateFunctionCode", "lambda:DeleteFunction", "lambda:AddPermission"],
  aws_iam_role: ["iam:CreateRole", "iam:AttachRolePolicy", "iam:PassRole", "iam:GetRole", "iam:DeleteRole"],
  aws_iam_policy: ["iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:GetPolicy"],
  aws_db_instance: ["rds:CreateDBInstance", "rds:ModifyDBInstance", "rds:DeleteDBInstance", "rds:DescribeDBInstances"],
  aws_dynamodb_table: ["dynamodb:CreateTable", "dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:DeleteTable", "dynamodb:DescribeTable"],
  aws_sqs_queue: ["sqs:CreateQueue", "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteQueue", "sqs:GetQueueAttributes"],
  aws_sns_topic: ["sns:CreateTopic", "sns:Publish", "sns:Subscribe", "sns:DeleteTopic"],
  aws_lb: ["elasticloadbalancing:CreateLoadBalancer", "elasticloadbalancing:CreateTargetGroup", "elasticloadbalancing:CreateListener", "elasticloadbalancing:DescribeLoadBalancers"],
  aws_cloudfront_distribution: ["cloudfront:CreateDistribution", "cloudfront:GetDistribution", "cloudfront:UpdateDistribution", "cloudfront:DeleteDistribution"],
  aws_elasticache_cluster: ["elasticache:CreateCacheCluster", "elasticache:DescribeCacheClusters", "elasticache:ModifyCacheCluster", "elasticache:DeleteCacheCluster"],
  aws_api_gateway_rest_api: ["apigateway:POST", "apigateway:GET", "apigateway:PUT", "apigateway:DELETE"],
  aws_security_group: ["ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress", "ec2:RevokeSecurityGroupIngress", "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup"],
  aws_ecs_service: ["ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService", "ecs:DescribeServices", "ecs:RunTask"],
};

const REGIONS = [
  "us-east-1", "us-east-2", "us-west-1", "us-west-2",
  "eu-west-1", "eu-west-2", "eu-central-1", "eu-north-1",
  "ap-southeast-1", "ap-southeast-2", "ap-northeast-1", "ap-south-1",
  "sa-east-1", "ca-central-1",
];

interface ResourceConfig {
  type: string;
  config: Record<string, any>;
}

interface PermissionsPayload {
  kra_name: string;
  description: string;
  resources: ResourceConfig[];
  iam_permissions: string[];
  tags: Record<string, string>;
  region: string;
}

interface Props {
  onSave: (payload: PermissionsPayload) => void;
  onCancel: () => void;
  initialKraName?: string;
}

export function PermissionsPage({ onSave, onCancel, initialKraName = "" }: Props) {
  const [kraName, setKraName] = useState(initialKraName);
  const [description, setDescription] = useState("");
  const [selectedResources, setSelectedResources] = useState<ResourceConfig[]>([]);
  const [selectedPermissions, setSelectedPermissions] = useState<string[]>([]);
  const [region, setRegion] = useState("us-east-1");
  const [tags, setTags] = useState<Record<string, string>>({});
  const [newTagKey, setNewTagKey] = useState("");
  const [newTagValue, setNewTagValue] = useState("");

  const toggleResource = (svc: string) => {
    setSelectedResources((prev) => {
      const exists = prev.find((r) => r.type === svc);
      if (exists) return prev.filter((r) => r.type !== svc);
      return [...prev, { type: svc, config: {} }];
    });
    // Auto-add IAM permissions for this service
    const perms = IAM_ACTIONS_BY_SERVICE[svc] || [];
    setSelectedPermissions((prev) => {
      const existing = new Set(prev);
      perms.forEach((p) => existing.add(p));
      return Array.from(existing);
    });
  };

  const addTag = () => {
    if (newTagKey && newTagValue) {
      setTags({ ...tags, [newTagKey]: newTagValue });
      setNewTagKey("");
      setNewTagValue("");
    }
  };

  const removeTag = (key: string) => {
    const { [key]: _, ...rest } = tags;
    setTags(rest);
  };

  const togglePermission = (perm: string) => {
    setSelectedPermissions((prev) =>
      prev.includes(perm) ? prev.filter((p) => p !== perm) : [...prev, perm]
    );
  };

  const handleSave = () => {
    onSave({
      kra_name: kraName,
      description,
      resources: selectedResources,
      iam_permissions: selectedPermissions,
      tags,
      region,
    });
  };

  const isValid = kraName.trim().length > 0 && selectedResources.length > 0;

  return (
    <div className="min-h-screen bg-gray-950 text-gray-100 p-6">
      <div className="max-w-5xl mx-auto">
        <div className="flex items-center gap-4 mb-8">
          <button onClick={onCancel} className="text-gray-400 hover:text-white transition-colors">
            <ArrowLeft className="w-6 h-6" />
          </button>
          <h1 className="text-2xl font-bold text-white">Configure KRA Permissions</h1>
        </div>

        {/* KRA Name & Description */}
        <div className="grid grid-cols-2 gap-4 mb-6">
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">KRA Name</label>
            <input
              value={kraName}
              onChange={(e) => setKraName(e.target.value)}
              placeholder="e.g. S3 Bucket with Encryption"
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-400 mb-1">Target Region</label>
            <select
              value={region}
              onChange={(e) => setRegion(e.target.value)}
              className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {REGIONS.map((r) => (
                <option key={r} value={r}>{r}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-400 mb-1">Description</label>
          <textarea
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="What does this KRA do?"
            rows={2}
            className="w-full bg-gray-800 border border-gray-700 rounded-lg px-4 py-2.5 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {/* AWS Resources */}
        <div className="mb-6">
          <h2 className="text-lg font-semibold text-white mb-3">AWS Resources</h2>
          <div className="grid grid-cols-4 gap-2">
            {AWS_SERVICES.map((svc) => {
              const isSelected = selectedResources.some((r) => r.type === svc.value);
              return (
                <button
                  key={svc.value}
                  onClick={() => toggleResource(svc.value)}
                  className={`flex items-center gap-2 px-3 py-2 rounded-lg border text-sm transition-all ${
                    isSelected
                      ? "bg-blue-900/40 border-blue-500 text-blue-300"
                      : "bg-gray-800/50 border-gray-700 text-gray-400 hover:border-gray-500"
                  }`}
                >
                  <span>{svc.icon}</span>
                  <span>{svc.label}</span>
                  {isSelected && <CheckCircle2 className="w-4 h-4 ml-auto text-blue-400" />}
                </button>
              );
            })}
          </div>
        </div>

        {/* IAM Permissions */}
        {selectedPermissions.length > 0 && (
          <div className="mb-6">
            <h2 className="text-lg font-semibold text-white mb-3">IAM Permissions</h2>
            <div className="flex flex-wrap gap-2">
              {selectedPermissions.map((perm) => (
                <button
                  key={perm}
                  onClick={() => togglePermission(perm)}
                  className={`px-3 py-1.5 rounded-full text-xs font-mono border transition-all ${
                    selectedPermissions.includes(perm)
                      ? "bg-green-900/30 border-green-600 text-green-300"
                      : "bg-gray-800 border-gray-600 text-gray-500"
                  }`}
                >
                  {perm}
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Tags */}
        <div className="mb-6">
          <h2 className="text-lg font-semibold text-white mb-3">Tags</h2>
          <div className="flex gap-2 mb-2">
            <input
              value={newTagKey}
              onChange={(e) => setNewTagKey(e.target.value)}
              placeholder="Key"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <input
              value={newTagValue}
              onChange={(e) => setNewTagValue(e.target.value)}
              placeholder="Value"
              className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <button onClick={addTag} className="bg-blue-600 hover:bg-blue-700 px-3 py-2 rounded-lg text-white">
              <Plus className="w-5 h-5" />
            </button>
          </div>
          <div className="flex flex-wrap gap-2">
            {Object.entries(tags).map(([k, v]) => (
              <span key={k} className="flex items-center gap-1 bg-gray-800 border border-gray-700 rounded-full px-3 py-1 text-sm">
                <span className="text-blue-400">{k}:</span>
                <span className="text-gray-300">{v}</span>
                <button onClick={() => removeTag(k)} className="text-gray-500 hover:text-red-400 ml-1">
                  <X className="w-3.5 h-3.5" />
                </button>
              </span>
            ))}
          </div>
        </div>

        {/* Actions */}
        <div className="flex gap-3 justify-end pt-4 border-t border-gray-800">
          <button onClick={onCancel} className="px-5 py-2.5 rounded-lg bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors">
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={!isValid}
            className={`flex items-center gap-2 px-5 py-2.5 rounded-lg transition-all ${
              isValid ? "bg-blue-600 hover:bg-blue-700 text-white" : "bg-gray-800 text-gray-500 cursor-not-allowed"
            }`}
          >
            <Save className="w-4 h-4" />
            Save KRA Configuration
          </button>
        </div>
      </div>
    </div>
  );
}