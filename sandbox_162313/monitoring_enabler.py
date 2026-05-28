import boto3
import argparse
import logging
from botocore.exceptions import ClientError

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_active_regions():
    """Retrieve all regions with active resources."""
    ec2 = boto3.client('ec2')
    regions = [region['RegionName'] for region in ec2.describe_regions()['Regions']]
    active_regions = []
    
    for region in regions:
        try:
            ec2_in_region = boto3.client('ec2', region_name=region)
            # Check if any EC2 instances exist
            response = ec2_in_region.describe_instances(MaxResults=1)
            if len(response['Reservations']) > 0:
                active_regions.append(region)
                continue
            
            # Check if any RDS instances exist
            rds = boto3.client('rds', region_name=region)
            rds_response = rds.describe_db_instances(MaxRecords=1)
            if len(rds_response['DBInstances']) > 0:
                active_regions.append(region)
                continue
                
            # Check if any EBS volumes exist
            ebs_response = ec2_in_region.describe_volumes(MaxResults=1)
            if len(ebs_response['Volumes']) > 0:
                active_regions.append(region)
                continue
                
        except ClientError as e:
            logger.warning(f"Error checking region {region}: {e}")
            continue

    return active_regions


def enable_aws_config_in_region(region):
    """Enable AWS Config Recorder and Delivery Channel in given region."""
    config = boto3.client('config', region_name=region)
    s3 = boto3.client('s3', region_name=region)
    
    # Create S3 bucket for Config delivery
    bucket_name = f"aws-config-bucket-{region}"
    try:
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={'LocationConstraint': region}
        )
        logger.info(f"Created S3 bucket {bucket_name} for Config in {region}")
    except ClientError as e:
        if e.response['Error']['Code'] != 'BucketAlreadyExists':
            logger.error(f"Failed to create S3 bucket in {region}: {e}")
            return False
        else:
            logger.info(f"S3 bucket {bucket_name} already exists in {region}")
    
    # Check if Config recorder exists
    try:
        recorder = config.describe_configuration_recorders()
        if len(recorder['ConfigurationRecorders']) == 0:
            config.put_configuration_recorder(
                ConfigurationRecorder={
                    'name': 'default-recorder',
                    'roleArn': 'arn:aws:iam::${account_id}:role/aws-service-role/config.amazonaws.com/AWSServiceRoleForConfig'
                }
            )
            logger.info(f"Created Config recorder in {region}")
        else:
            logger.info(f"Config recorder already exists in {region}")
    except ClientError as e:
        logger.error(f"Failed to create Config recorder in {region}: {e}")
        return False
    
    # Create delivery channel
    try:
        config.put_delivery_channel(
            DeliveryChannel={
                'name': 'default-channel',
                's3BucketName': bucket_name,
                'snapshotDryRun': False
            }
        )
        logger.info(f"Created Config delivery channel in {region}")
    except ClientError as e:
        logger.error(f"Failed to create Config delivery channel in {region}: {e}")
        return False
    
    # Start recording
    try:
        config.start_configuration_recorder(ConfigurationRecorderName='default-recorder')
        logger.info(f"Started Config recorder in {region}")
        return True
    except ClientError as e:
        logger.error(f"Failed to start Config recorder in {region}: {e}")
        return False


def create_cloudwatch_alarms(region):
    """Create baseline CloudWatch alarms for EC2 and RDS in given region."""
    cloudwatch = boto3.client('cloudwatch', region_name=region)
    
    # EC2 alarms
    ec2_alarms = [
        {
            'Name': 'EC2-StatusCheckFailed',
            'MetricName': 'StatusCheckFailed',
            'Namespace': 'AWS/EC2',
            'Threshold': 1,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 2,
            'Period': 300
        },
        {
            'Name': 'EC2-CPUUtilizationHigh',
            'MetricName': 'CPUUtilization',
            'Namespace': 'AWS/EC2',
            'Threshold': 85,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        },
        {
            'Name': 'EC2-DiskReadOps',
            'MetricName': 'DiskReadOps',
            'Namespace': 'AWS/EC2',
            'Threshold': 5000,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        },
        {
            'Name': 'EC2-DiskWriteOps',
            'MetricName': 'DiskWriteOps',
            'Namespace': 'AWS/EC2',
            'Threshold': 5000,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        },
        {
            'Name': 'EC2-NetworkIn',
            'MetricName': 'NetworkIn',
            'Namespace': 'AWS/EC2',
            'Threshold': 10000000,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        },
        {
            'Name': 'EC2-NetworkOut',
            'MetricName': 'NetworkOut',
            'Namespace': 'AWS/EC2',
            'Threshold': 10000000,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        }
    ]
    
    for alarm in ec2_alarms:
        try:
            cloudwatch.put_metric_alarm(
                AlarmName=alarm['Name'],
                AlarmDescription=f'Alarm when {alarm['Name']} exceeds threshold',
                AlarmActions=['arn:aws:sns:us-east-1:${account_id}:monitoring-alerts'],
                MetricName=alarm['MetricName'],
                Namespace=alarm['Namespace'],
                Statistic='Average',
                Threshold=alarm['Threshold'],
                ComparisonOperator=alarm['ComparisonOperator'],
                EvaluationPeriods=alarm['EvaluationPeriods'],
                Period=alarm['Period'],
                Unit='Count',
                TreatMissingData='breaching'
            )
            logger.info(f"Created alarm {alarm['Name']} in {region}")
        except ClientError as e:
            logger.error(f"Failed to create {alarm['Name']} in {region}: {e}")

    # RDS alarms
    rds_alarms = [
        {
            'Name': 'RDS-CPUUtilizationHigh',
            'MetricName': 'CPUUtilization',
            'Namespace': 'AWS/RDS',
            'Threshold': 80,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        },
        {
            'Name': 'RDS-DatabaseConnections',
            'MetricName': 'DatabaseConnections',
            'Namespace': 'AWS/RDS',
            'Threshold': 100,
            'ComparisonOperator': 'GreaterThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        },
        {
            'Name': 'RDS-FreeStorageSpace',
            'MetricName': 'FreeStorageSpace',
            'Namespace': 'AWS/RDS',
            'Threshold': 1000000000,  # 1GB
            'ComparisonOperator': 'LessThanOrEqualToThreshold',
            'EvaluationPeriods': 3,
            'Period': 300
        }
    ]
    
    for alarm in rds_alarms:
        try:
            cloudwatch.put_metric_alarm(
                AlarmName=alarm['Name'],
                AlarmDescription=f'Alarm when {alarm['Name']} exceeds threshold',
                AlarmActions=['arn:aws:sns:us-east-1:${account_id}:monitoring-alerts'],
                MetricName=alarm['MetricName'],
                Namespace=alarm['Namespace'],
                Statistic='Average',
                Threshold=alarm['Threshold'],
                ComparisonOperator=alarm['ComparisonOperator'],
                EvaluationPeriods=alarm['EvaluationPeriods'],
                Period=alarm['Period'],
                Unit='Count',
                TreatMissingData='breaching'
            )
            logger.info(f"Created alarm {alarm['Name']} in {region}")
        except ClientError as e:
            logger.error(f"Failed to create {alarm['Name']} in {region}: {e}")


def main():
    parser = argparse.ArgumentParser(description='Enable comprehensive monitoring across AWS regions.')
    parser.add_argument('--account-id', required=True, help='AWS Account ID')
    args = parser.parse_args()
    
    active_regions = get_active_regions()
    logger.info(f"Found {len(active_regions)} active regions: {active_regions}")
    
    for region in active_regions:
        logger.info(f"Processing region {region}...")
        
        # Enable AWS Config
        if enable_aws_config_in_region(region):
            logger.info(f"Successfully enabled AWS Config in {region}")
        
        # Create CloudWatch alarms
        create_cloudwatch_alarms(region)
        
    logger.info("Monitoring coverage expansion completed.")


if __name__ == '__main__':
    main()