import boto3
import json
import logging
from datetime import datetime, timedelta

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    """Lambda function to validate CloudWatch alarms and check for threshold issues."""
    cloudwatch = boto3.client('cloudwatch')
    
    # Get all alarms
    alarms = cloudwatch.describe_alarms()
    
    # Define service-specific threshold recommendations
    critical_thresholds = {
        'EC2-StatusCheckFailed': {'threshold': 1, 'period': 300},
        'EC2-CPUUtilizationHigh': {'threshold': 85, 'period': 300},
        'RDS-CPUUtilizationHigh': {'threshold': 80, 'period': 300},
        'RDS-FreeStorageSpace': {'threshold': 1000000000, 'period': 300}
    }
    
    issues = []
    
    for alarm in alarms['MetricAlarms']:
        alarm_name = alarm['AlarmName']
        
        # Check for misconfigured thresholds
        if alarm_name in critical_thresholds:
            expected_threshold = critical_thresholds[alarm_name]['threshold']
            if alarm['Threshold'] != expected_threshold:
                issues.append(f"Alarm {alarm_name}: Threshold is {alarm['Threshold']} but should be {expected_threshold}")
            
            # Check for evaluation period
            if alarm['EvaluationPeriods'] < 3:
                issues.append(f"Alarm {alarm_name}: EvaluationPeriods is {alarm['EvaluationPeriods']} but should be >= 3")
            
            # Check for period (should be 300 seconds for 5-min intervals)
            if alarm['Period'] != 300:
                issues.append(f"Alarm {alarm_name}: Period is {alarm['Period']} but should be 300 seconds")

        # Check for alarms without SNS action
        if not alarm.get('AlarmActions'):
            issues.append(f"Alarm {alarm_name} has no notification action configured")

    # If issues found, send alert
    if issues:
        sns = boto3.client('sns')
        topic_arn = "arn:aws:sns:us-east-1:${account_id}:monitoring-alerts"
        message = f"\\n--- MONTHLY MONITORING REVIEW ---\\n" \
                  f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\\n" \
                  f"Total Issues Found: {len(issues)}\\n" \
                  f"\\nIssues: \\n" \
                  f"\\n".join(issues)
        
        sns.publish(
            TopicArn=topic_arn,
            Subject="Monthly Monitoring Review: Threshold Issues Found",
            Message=message
        )
        logger.info(f"Published monthly review alert: {len(issues)} issues found")
    else:
        logger.info("No threshold issues found in monthly review")
    
    return {
        'statusCode': 200,
        'body': json.dumps('Monthly monitoring validation completed')
    }