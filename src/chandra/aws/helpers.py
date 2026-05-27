from chandra.aws.client_factory import AwsClientFactory


def get_factory_for_state(state):

    factory = AwsClientFactory()

    if state.assume_role_arn:
        return factory.assume_role(
            role_arn=state.assume_role_arn,
            session_name="chandra-cross-account"
        )

    return factory
