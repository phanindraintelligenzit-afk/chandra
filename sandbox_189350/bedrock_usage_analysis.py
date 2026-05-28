import boto3
import argparse
from datetime import datetime, timedelta
import json


def get_bedrock_requests(client, start_date, end_date):
    """
    Query CloudWatch Logs Insights to extract Bedrock usage by application and request path.
    """
    response = client.query_logs(
        logGroupNamePrefix='aws-bedrock',
        queryString='
fields @timestamp, @message
| filter @message like /\/v1\/generate|POST/ 
| parse @message "* * * * * * * * * * * " as method, path, status, _1, _2, _3, _4, _5, _6, _7, _8
| stats count() as requests, sum(requestSize) as totalSize by path, metadata.application
| sort requests desc
        ',
        startTime=int(start_date.timestamp() * 1000),
        endTime=int(end_date.timestamp() * 1000),
        limit=100
    )

    return response


def analyze_bedrock_cost(ce_client, start_date, end_date):
    """
    Get Bedrock cost by service dimension.
    """
    response = ce_client.get_cost_and_usage(
        TimePeriod={
            'Start': start_date.isoformat(),
            'End': end_date.isoformat()
        },
        Granularity='DAILY',
        Metrics=["UnblendedCost"],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "LINKED_ACCOUNT"},
            {"Type": "DIMENSION", "Key": "SERVICE"}
        ],
        Filters=[
            {
                "Dimensions": {
                    "Key": "SERVICE",
                    "Values": [
                        "Amazon Bedrock"
                    ]
                }
            }
        ]
    )

    return response


def generate_bedrock_report(log_results, cost_results, filename="bedrock_analysis.json"):
    """
    Generate a JSON report combining log insights and cost data.
    """
    report = {
        "analysis_timestamp": str(datetime.utcnow()),
        "usage_patterns": [],
        "cost_summary": {}
    }

    # Add usage patterns
    for result in log_results['results']:
        for record in result:
            if record['field'] == 'path':
                path = record['value']
            if record['field'] == 'application':
                app = record['value']
            if record['field'] == 'requests':
                req_count = int(record['value'])

        report["usage_patterns"].append({
            "request_path": path,
            "application": app,
            "request_count": req_count
        })

    # Add cost summary
    for group in cost_results['ResultsByTime']:
        for item in group['Groups']:
            cost = float(item['Metrics']['UnblendedCost']['Amount'])
            account = item['Keys'][0]
            report["cost_summary"][account] = cost

    with open(filename, 'w') as f:
        json.dump(report, f, indent=4)

    print(f"Bedrock usage report written to {filename}")


def main():
    parser = argparse.ArgumentParser(description='Analyze Bedrock usage by application and request path.')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--start-date', default=(datetime.today() - timedelta(days=7)).date(), type=lambda d: datetime.strptime(d, '%Y-%m-%d').date(), help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', default=datetime.today().date(), type=lambda d: datetime.strptime(d, '%Y-%m-%d').date(), help='End date (YYYY-MM-DD)')
    args = parser.parse_args()

    # Initialize clients
    logs = boto3.client('logs', region_name=args.region)
    ce = boto3.client('ce', region_name=args.region)

    # Fetch logs and cost
    log_results = get_bedrock_requests(logs, args.start_date, args.end_date)
    cost_results = analyze_bedrock_cost(ce, args.start_date, args.end_date)

    # Generate report
    generate_bedrock_report(log_results, cost_results)


if __name__ == '__main__':
    main()
