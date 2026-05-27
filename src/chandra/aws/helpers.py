from chandra.aws.client_factory import AwsClientFactory


def get_factory_for_state(state):

    factory = get_factory_for_state(state)

s3 = factory.client("s3")
cloudwatch = factory.client("cloudwatch")
iam = factory.client("iam")
sns = factory.client("sns")
sts = factory.client("sts")

    if state.assume_role_arn:
        return factory.assume_role(
            role_arn=state.assume_role_arn,
            session_name="chandra-cross-account"
        )

    return factory
