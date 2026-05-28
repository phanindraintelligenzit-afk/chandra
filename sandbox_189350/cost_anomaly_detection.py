import boto3
import argparse
from datetime import datetime, timedelta


def create_anomaly_monitor(client, metric_name, namespace, dimension_name=None, dimension_value=None, monitor_name=None):
    """
    Create a CloudWatch anomaly detection monitor for a given cost metric.
    """
    if not monitor_name:
        monitor_name = f"{namespace}-{metric_name}-anomaly-monitor"

    # Create anomaly detection model
    response = client.put_anomaly_detection_model(
        Namespace=namespace,
        MetricName=metric_name,
        DimensionName=dimension_name,
        DimensionValue=dimension_value,
        ModelName=monitor_name,
        ModelDescription=f"Anomaly detection for {metric_name} in {namespace}",
        ModelType='SINGLE_VARIATE',
        ModelStatus='ENABLED'
    )
    print(f"Created anomaly monitor: {monitor_name}")
    return response


def main():
    parser = argparse.ArgumentParser(description='Create CloudWatch Anomaly Detection Monitors for Cost Metrics.')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    args = parser.parse_args()

    # Initialize CloudWatch client
    cloudwatch = boto3.client('cloudwatch', region_name=args.region)

    # Define cost metrics to monitor
    cost_metrics = [
        {
            'namespace': 'AWS/Billing',
            'metric_name': 'EstimatedCharges',
            'dimension_name': 'ServiceName',
            'dimension_value': 'RDS',
            'monitor_name': 'RDS-Cost-Anomaly-Monitor'
        },
        {
            'namespace': 'AWS/Billing',
            'metric_name': 'EstimatedCharges',
            'dimension_name': 'ServiceName',
            'dimension_value': 'EC2',
            'monitor_name': 'EC2-Cost-Anomaly-Monitor'
        },
        {
            'namespace': 'AWS/Billing',
            'metric_name': 'EstimatedCharges',
            'dimension_name': 'ServiceName',
            'dimension_value': 'CloudWatch',
            'monitor_name': 'CloudWatch-Cost-Anomaly-Monitor'
        },
        {
            'namespace': 'AWS/Billing',
            'metric_name': 'EstimatedCharges',
            'dimension_name': 'ServiceName',
            'dimension_value': 'Bedrock',
            'monitor_name': 'Bedrock-Cost-Anomaly-Monitor'
        }
    ]

    # Create monitors for each cost metric
    for metric in cost_metrics:
        create_anomaly_monitor(
            client=cloudwatch,
            metric_name=metric['metric_name'],
            namespace=metric['namespace'],
            dimension_name=metric['dimension_name'],
            dimension_value=metric['dimension_value'],
            monitor_name=metric['monitor_name']
        )

    print("All anomaly monitors created successfully.")


if __name__ == '__main__':
    main()
