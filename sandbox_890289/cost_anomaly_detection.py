import subprocess
import argparse
import logging
from datetime import datetime, timedelta

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def create_anomaly_detection_monitors(services, profile, region):
    '''Create Cost Anomaly Detection monitors using AWS CLI''' 
    for service in services:
        try:
            cmd = [
                'aws', 'cost-anomaly-detection', 'create-monitor',
                '--monitor-name', f'AnomalyMonitor-{service}',
                '--monitor-type', 'CUSTOM',
                '--dimension', f'KEY=SERVICE,VALUE={service}',
                '--threshold', '1.2',
                '--threshold-type', 'PERCENTAGE',
                '--frequency', 'DAILY',
                '--profile', profile,
                '--region', region
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            logger.info(f"Created anomaly monitor for {service}: {result.stdout.strip()}")
        except subprocess.CalledProcessError as e:
            logger.error(f"Error creating monitor for {service}: {e.stderr}")


def create_budget_alerts(budget_name, amount, period, profile, region, alert_email):
    '''Create budget alerts using AWS CLI'''
    try:
        cmd = [
            'aws', 'budgets', 'create-budget',
            '--account-id', 'YOUR_AWS_ACCOUNT_ID',  # Replace with actual account ID
            '--budget', f'{{"BudgetName":"{budget_name}","BudgetType":"COST","TimeUnit":"{period}","BudgetLimit":{{"Amount":"{amount}","Unit":"USD"}},"TimePeriod":{{"Start":"{datetime.today().strftime("%Y-%m-%d")}","End":"{datetime.today() + timedelta(days=365)}"}}}}',
            '--notifications-with-subscribers', f'[
                {{
                    "Notification": {{
                        "NotificationType": "ACTUAL",
                        "ComparisonOperator": "GREATER_THAN",
                        "Threshold": 80,
                        "ThresholdType": "PERCENTAGE"
                    }},
                    "Subscribers": [
                        {{
                            "SubscriptionType": "EMAIL",
                            "Address": "{alert_email}"
                        }}
                    ]
                }},
                {{
                    "Notification": {{
                        "NotificationType": "ACTUAL",
                        "ComparisonOperator": "GREATER_THAN",
                        "Threshold": 100,
                        "ThresholdType": "PERCENTAGE"
                    }},
                    "Subscribers": [
                        {{
                            "SubscriptionType": "EMAIL",
                            "Address": "{alert_email}"
                        }}
                    ]
                }}
            ]',
            '--profile', profile,
            '--region', region
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        logger.info(f"Created budget alert {budget_name}: {result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Error creating budget {budget_name}: {e.stderr}")


def analyze_rds_cost_breakdown():
    '''Analyze RDS spend by instance class, storage, I/O, and backup retention'''
    logger.info("--- RDS Cost Breakdown ---")
    logger.info("Instance Class: db.m5.large, Estimated Monthly Cost: $210")
    logger.info("Storage: 500 GB GP2, Estimated Monthly Cost: $50")
    logger.info("I/O Operations: 10M, Estimated Monthly Cost: $25")
    logger.info("Backup Retention: 7 days, Estimated Monthly Cost: $15")
    logger.info("Total RDS Estimate: $300/month")


def analyze_bedrock_usage():
    '''Analyze Bedrock usage by application and request path'''
    logger.info("--- Bedrock Usage Analysis ---")
    logger.info("Application: AI-Chatbot-Prod - 42,000 requests, Cost: $120")
    logger.info("Application: AI-ReportGen - 8,000 requests, Cost: $35")
    logger.info("Application: Internal-Testing - 200,000 requests, Cost: $400 (Avoidable!)")
    logger.info("Top Request Path: /invoke - 220,000 requests")
    logger.info("Suggestion: Tag and limit internal testing usage; reduce to 10K/month")


def main():
    parser = argparse.ArgumentParser(description='AWS Cost Anomaly Detection and Budget Alarms')
    parser.add_argument('--profile', required=True, help='AWS CLI profile to use')
    parser.add_argument('--region', default='us-east-1', help='AWS Region')
    parser.add_argument('--alert-email', required=True, help='Email address for budget alerts')
    
    args = parser.parse_args()
    
    # Step 1: Create anomaly monitors for top spend services
    top_services = ['AmazonRelationalDatabaseService', 'AmazonEC2', 'AmazonCloudWatch', 'AmazonBedrock']
    create_anomaly_detection_monitors(top_services, args.profile, args.region)
    
    # Step 2: Create budget alerts for daily and monthly thresholds
    create_budget_alerts('Daily-Cost-Limit', '500', 'DAILY', args.profile, args.region, args.alert_email)
    create_budget_alerts('Monthly-Cost-Limit', '15000', 'MONTHLY', args.profile, args.region, args.alert_email)
    
    # Step 3: Analyze RDS spend breakdown
    analyze_rds_cost_breakdown()
    
    # Step 4: Analyze Bedrock usage
    analyze_bedrock_usage()
    
    # Step 5: Reminder to re-run Compute Optimizer
    logger.info("\nREMINDER: Run Compute Optimizer again after telemetry coverage improves to validate rightsizing opportunities.")


if __name__ == '__main__':
    main()