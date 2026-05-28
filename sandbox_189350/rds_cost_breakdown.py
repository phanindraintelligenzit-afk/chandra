import boto3
import argparse
from datetime import datetime, timedelta
import csv


def get_rds_cost_breakdown(client, start_date, end_date):
    """
    Fetch RDS cost breakdown by instance class, storage, I/O, and backup retention using Cost Explorer API.
    """
    response = client.get_cost_and_usage(
        TimePeriod={
            'Start': start_date.isoformat(),
            'End': end_date.isoformat()
        },
        Granularity='DAILY',
        Metrics=["UnblendedCost"],
        GroupBy=[
            {"Type": "DIMENSION", "Key": "SERVICE"},
            {"Type": "DIMENSION", "Key": "INSTANCE_TYPE"},
            {"Type": "DIMENSION", "Key": "STORAGE_TYPE"},
            {"Type": "DIMENSION", "Key": "IOPORT"},
            {"Type": "DIMENSION", "Key": "BACKUP_RETENTION"}
        ],
        Filters=[
            {
                "Dimensions": {
                    "Key": "SERVICE",
                    "Values": [
                        "Amazon Relational Database Service"
                    ]
                }
            }
        ]
    )

    return response


def write_rds_report(results, filename="rds_cost_breakdown.csv"):
    """
    Write RDS cost breakdown to CSV file.
    """
    with open(filename, 'w', newline='') as csvfile:
        fieldnames = ['Date', 'InstanceType', 'StorageType', 'IOPort', 'BackupRetention', 'Cost']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()

        for group in results['ResultsByTime']:
            date = group['TimePeriod']['Start']
            for item in group['Groups']:
                cost = item['Metrics']['UnblendedCost']['Amount']
                keys = item['Keys']
                # Extract dimension values
                instance_type = keys[0] if len(keys) > 0 else "N/A"
                storage_type = keys[1] if len(keys) > 1 else "N/A"
                io_port = keys[2] if len(keys) > 2 else "N/A"
                backup_retention = keys[3] if len(keys) > 3 else "N/A"

                writer.writerow({
                    'Date': date,
                    'InstanceType': instance_type,
                    'StorageType': storage_type,
                    'IOPort': io_port,
                    'BackupRetention': backup_retention,
                    'Cost': cost
                })

    print(f"RDS cost breakdown written to {filename}")


def main():
    parser = argparse.ArgumentParser(description='Fetch RDS cost breakdown by metric categories.')
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    parser.add_argument('--start-date', default=(datetime.today() - timedelta(days=7)).date(), type=lambda d: datetime.strptime(d, '%Y-%m-%d').date(), help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end-date', default=datetime.today().date(), type=lambda d: datetime.strptime(d, '%Y-%m-%d').date(), help='End date (YYYY-MM-DD)')
    args = parser.parse_args()

    # Initialize Cost Explorer client
    ce = boto3.client('ce', region_name=args.region)

    # Fetch cost data
    results = get_rds_cost_breakdown(ce, args.start_date, args.end_date)

    # Write report
    write_rds_report(results)


if __name__ == '__main__':
    main()
