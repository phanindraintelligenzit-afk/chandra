import json
from chandra.aws.organizations import Account
from chandra.briefing.org_summary import (
    aggregate_org_scorecard,
    build_org_briefing,
)

print("=" * 70)
print("AWS ORGANIZATIONS FAN-OUT DEMO")
print("=" * 70)
print()

accounts = [
    Account(
        id="111111111111",
        arn="arn:aws:organizations::111111111111:account/o-abc/111111111111",
        name="Production",
        email="prod@company.com",
        status="ACTIVE",
    ),
    Account(
        id="222222222222",
        arn="arn:aws:organizations::222222222222:account/o-abc/222222222222",
        name="Development",
        email="dev@company.com",
        status="ACTIVE",
    ),
    Account(
        id="333333333333",
        arn="arn:aws:organizations::333333333333:account/o-abc/333333333333",
        name="Security",
        email="sec@company.com",
        status="ACTIVE",
    ),
    Account(
        id="444444444444",
        arn="arn:aws:organizations::444444444444:account/o-abc/444444444444",
        name="Finance",
        email="fin@company.com",
        status="ACTIVE",
    ),
    Account(
        id="555555555555",
        arn="arn:aws:organizations::555555555555:account/o-abc/555555555555",
        name="Analytics",
        email="ana@company.com",
        status="ACTIVE",
    ),
]

print("ACCOUNTS IN OU:")
print("-" * 70)
for account in accounts:
    print(f"{account.id} | {account.name} | {account.email}")

print()
print("=" * 70)
print("RUNNING CHANDRA ACROSS ALL ACCOUNTS")
print("=" * 70)
print()

account_scorecards = {
    "111111111111": {
        "cost": 75,
        "security": 85,
        "compliance": 80,
        "performance": 88,
        "reliability": 82,
        "overall": 82,
    },
    "222222222222": {
        "cost": 72,
        "security": 78,
        "compliance": 75,
        "performance": 80,
        "reliability": 76,
        "overall": 76,
    },
    "333333333333": {
        "cost": 80,
        "security": 92,
        "compliance": 90,
        "performance": 85,
        "reliability": 88,
        "overall": 87,
    },
    "444444444444": {
        "cost": 68,
        "security": 72,
        "compliance": 70,
        "performance": 75,
        "reliability": 71,
        "overall": 71,
    },
    "555555555555": {
        "cost": 76,
        "security": 84,
        "compliance": 82,
        "performance": 86,
        "reliability": 85,
        "overall": 82,
    },
}

account_briefings = {
    "111111111111": "Production: 5 critical, 12 high, 25 medium findings. Cost: $1,245.50",
    "222222222222": "Dev: 1 critical, 8 high, 18 medium findings. Cost: $456.75",
    "333333333333": "Security: 0 critical, 2 high, 8 medium findings. Cost: $789.25",
    "444444444444": "Finance: 2 critical, 15 high, 30 medium findings. Cost: $2,105.00",
    "555555555555": "Analytics: 1 critical, 10 high, 22 medium findings. Cost: $876.50",
}

org_scorecard = aggregate_org_scorecard(
    account_scorecards
)

org_briefing = build_org_briefing(
    account_briefings,
    org_scorecard,
)

print("ORGANIZATION SCORECARD:")
print("-" * 70)
for kra, score in org_scorecard.items():
    print(f"{kra:12} | {score:3}")

print()
print("=" * 70)
print("ORG BRIEFING (MARKDOWN):")
print("=" * 70)
print(org_briefing)

print()
print("=" * 70)
print("JSON OUTPUT:")
print("=" * 70)
json_output = {
    "organization_scorecard": org_scorecard,
    "account_count": len(accounts),
    "accounts": [
        {
            "id": a.id,
            "name": a.name,
            "scorecard": account_scorecards[a.id],
        }
        for a in accounts
    ],
}
print(json.dumps(json_output, indent=2))
