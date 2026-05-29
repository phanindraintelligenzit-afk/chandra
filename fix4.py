filepath = "tests/unit/test_observation_ingestion.py"
with open(filepath, 'r') as f:
    content = f.read()

# Fix test_multiple_alarms_and_rules - add EventPattern
content = content.replace(
    '''        # Two rules
        for i in range(2):
            events.put_rule(  # type: ignore[union-attr]
                Name=f"rule-{i}",
                State="ENABLED",
            )''',
    '''        # Two rules
        for i in range(2):
            events.put_rule(  # type: ignore[union-attr]
                Name=f"rule-{i}",
                State="ENABLED",
                EventPattern='{"source": ["aws.ec2"]}',
            )'''
)

# Fix test_disabled_rule_included - add EventPattern
content = content.replace(
    '''    def test_disabled_rule_included(self, aws: None, events: object) -> None:
        """Disabled EventBridge rules are included."""
        events.put_rule(  # type: ignore[union-attr]
            Name="disabled-rule",
            State="DISABLED",
        )''',
    '''    def test_disabled_rule_included(self, aws: None, events: object) -> None:
        """Disabled EventBridge rules are included."""
        events.put_rule(  # type: ignore[union-attr]
            Name="disabled-rule",
            State="DISABLED",
            EventPattern='{"source": ["aws.ec2"]}',
        )'''
)

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Updated test_observation_ingestion.py")
