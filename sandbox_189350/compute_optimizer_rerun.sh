#!/bin/bash

set -e

# Variables
REGION="us-east-1"

# Configure AWS CLI for the specified region
export AWS_DEFAULT_REGION=$REGION

# Validate AWS credentials
aws sts get-caller-identity > /dev/null 2>&1 || { echo "ERROR: AWS credentials not configured."; exit 1; }

# Trigger Compute Optimizer to re-analyze all supported resources
echo "Triggering Compute Optimizer re-analysis..."
aws compute-optimizer update-enrollment-status --status "Enrolled" --include-members

# Wait for analysis to start (up to 10 minutes)
echo "Waiting for Compute Optimizer to process data..."
sleep 600

# Get latest recommendations
echo "Fetching latest Compute Optimizer recommendations..."
aws compute-optimizer get-enrollment-status > compute_optimizer_status.json

# List recommendations for EC2 instances
aws compute-optimizer get-ec2-instance-recommendations --output json > ec2_recommendations.json

# List recommendations for RDS instances
aws compute-optimizer get-rds-instance-recommendations --output json > rds_recommendations.json

# Check if recommendations exist
if [ -s "ec2_recommendations.json" ] && jq -e '.instanceRecommendations | length > 0' ec2_recommendations.json > /dev/null; then
    echo "\n✅ EC2 Instance Recommendations Found:"
    jq -r '.instanceRecommendations[].instanceName + " - " + .currentPerformance + " -> " + .recommendationOptions[].optimizationOpportunity' ec2_recommendations.json
else
    echo "❌ No EC2 rightsizing opportunities found."
fi

if [ -s "rds_recommendations.json" ] && jq -e '.instanceRecommendations | length > 0' rds_recommendations.json > /dev/null; then
    echo "\n✅ RDS Instance Recommendations Found:"
    jq -r '.instanceRecommendations[].instanceName + " - " + .currentPerformance + " -> " + .recommendationOptions[].optimizationOpportunity' rds_recommendations.json
else
    echo "❌ No RDS rightsizing opportunities found."
fi

# Final summary
echo "\n📊 Summary: Compute Optimizer re-analysis completed. Check JSON outputs for details."
