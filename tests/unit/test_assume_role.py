from moto import mock_sts

from chandra.aws.client_factory import AwsClientFactory


@mock_sts
def test_assume_role():

    factory = AwsClientFactory()

    role_arn = "arn:aws:iam::123456789012:role/TestRole"

    assumed = factory.assume_role(
        role_arn=role_arn,
        session_name="test-session"
    )

    sts = assumed.client("sts")

    result = sts.get_caller_identity()

    assert result is not None
