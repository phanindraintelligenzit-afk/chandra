import boto3
import argparse
import logging
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def send_test_alarm(region, topic_arn):
    """Send a test alarm to validate alert delivery."""
    cloudwatch = boto3.client('cloudwatch', region_name=region)
    
    try:
        # Create a temporary test alarm
        alarm_name = f"TEST-ALARM-{int(time.time())}"
        
        cloudwatch.put_metric_alarm(
            AlarmName=alarm_name,
            AlarmDescription='Test alarm to validate alert delivery',
            MetricName='CPUUtilization',
            Namespace='AWS/EC2',
            Statistic='Average',
            Threshold=0,  # This will trigger because we force it below
            ComparisonOperator='GreaterThanOrEqualToThreshold',
            EvaluationPeriods=1,
            Period=60,
            AlarmActions=[topic_arn],
            TreatMissingData='breaching'
        )
        
        # Force alarm state
        cloudwatch.set_alarm_state(
            AlarmName=alarm_name,
            StateValue='ALARM',
            StateReason='Manual test alarm for validation'
        )
        
        logger.info(f"Sent test alarm {alarm_name} to {topic_arn}")
        
        # Cleanup after a short delay
        time.sleep(10)
        cloudwatch.delete_alarms(AlarmNames=[alarm_name])
        logger.info(f"Cleaned up test alarm {alarm_name}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to send test alarm: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description='Validate alert delivery for CloudWatch alarms.')
    parser.add_argument('--region', required=True, help='AWS region to validate')
    parser.add_argument('--topic-arn', required=True, help='SNS topic ARN for alerts')
    args = parser.parse_args()
    
    success = send_test_alarm(args.region, args.topic_arn)
    
    if success:
        logger.info("Alert delivery validation completed successfully!")
    else:
        logger.error("Alert delivery validation failed!")
        exit(1)


if __name__ == '__main__':
    main()