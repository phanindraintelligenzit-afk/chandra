# Testing Guide

Chandra uses **pytest** for unit testing with **moto** for AWS mocking (no real AWS calls).
All unit tests run in ~1-2 seconds. Integration tests require Docker/Postgres and are marked with `@pytest.mark.integration`.

## Quick Start

Run the full quality gate (ruff + mypy + pytest):
```bash
make check
# or on Windows:
uv run ruff check src && uv run ruff format --check src && uv run mypy src --strict && uv run pytest -m "not integration"
```

## Unit Tests Only

### Run all unit tests
```bash
uv run pytest tests/unit/ -v
```

Output shows:
- Test file and function name
- Pass/fail status
- Execution time per test

### Run without integration tests
```bash
uv run pytest tests/unit/ -m "not integration" -v
```

Skips any tests marked with `@pytest.mark.integration`.

### Run quietly (minimal output)
```bash
uv run pytest tests/unit/ -q
```

Shows only summary: `14 passed in 0.66s`

### Run with short traceback on failure
```bash
uv run pytest tests/unit/ --tb=short
```

Prints relevant stack frames only (omits internals).

### Run with full traceback
```bash
uv run pytest tests/unit/ --tb=long
```

## Run Specific Tests

### Run a single test file
```bash
uv run pytest tests/unit/test_decision_router.py -v
```

### Run a specific test class
```bash
uv run pytest tests/unit/test_decision_router.py::TestDecisionRouter -v
```

### Run a specific test function
```bash
uv run pytest tests/unit/test_decision_router.py::TestDecisionRouter::test_critical_escalated -v
```

### Run multiple files
```bash
uv run pytest tests/unit/test_decision_router.py tests/unit/test_kra_context.py -v
```

### Run tests matching a pattern
```bash
uv run pytest tests/unit/ -k "decision_router" -v
```

Runs all tests with `decision_router` in the name.

### Run tests not matching a pattern
```bash
uv run pytest tests/unit/ -k "not integration" -v
```

## Coverage Reports

### Generate coverage report (terminal)
```bash
uv run pytest tests/unit/ -v --cov=src/chandra --cov-report=term-missing
```

Shows:
- Coverage percentage per module
- Lines not covered (missing)

### Generate HTML coverage report
```bash
uv run pytest tests/unit/ --cov=src/chandra --cov-report=html
```

Creates `htmlcov/index.html` — open in browser for interactive report.

### Coverage with specific module
```bash
uv run pytest tests/unit/ --cov=src/chandra/tools --cov-report=term-missing
```

Focus on one module's coverage.

## Integration Tests

### Run only integration tests
```bash
uv run pytest tests/unit/ -m "integration" -v
```

Requires:
- Docker (Postgres container)
- LocalStack (optional, for AWS service mocking)

### Run all tests (unit + integration)
```bash
uv run pytest tests/ -v
```

## By Component

### All observability tests
```bash
uv run pytest tests/unit/test_observability.py tests/unit/test_kra_context.py -v
```

### All KRA detector tests
```bash
uv run pytest tests/unit/test_cost_tools.py tests/unit/test_security_tools.py tests/unit/test_compliance_tools.py tests/unit/test_performance_tools.py tests/unit/test_reliability_tools.py -v
```

### All graph/node tests
```bash
uv run pytest tests/unit/test_decision_router.py tests/unit/test_kra_supervisor.py tests/unit/test_approval.py -v
```

### All briefing/composer tests
```bash
uv run pytest tests/unit/test_composer.py tests/unit/test_analyze_ranking.py -v
```

## Debugging & Inspection

### Stop on first failure
```bash
uv run pytest tests/unit/ -x -v
```

Useful for fixing one test at a time.

### Show print statements / logging
```bash
uv run pytest tests/unit/ -v -s
```

The `-s` flag disables output capturing; you'll see all `print()` and log statements.

### Run with pdb debugger
```bash
uv run pytest tests/unit/test_decision_router.py::TestDecisionRouter::test_critical_escalated -v --pdb
```

Drops into debugger on failure.

### Verbose with durations
```bash
uv run pytest tests/unit/ -v --durations=10
```

Shows 10 slowest tests.

## Markers

### Custom markers in this project

```bash
# Only integration tests
uv run pytest -m integration

# Exclude integration tests
uv run pytest -m "not integration"
```

Available markers:
- `integration` — requires external services (Postgres, AWS)

## Fixtures

Common fixtures available in `tests/conftest.py`:

- `aws` — moto `@mock_aws` context (all AWS calls mocked)
- `cloudwatch` — boto3 CloudWatch client (mocked)
- `s3` — boto3 S3 client (mocked)
- `iam` — boto3 IAM client (mocked)
- `ec2` — boto3 EC2 client (mocked)
- `rds` — boto3 RDS client (mocked)
- `detector_context` — `DetectorContext` instance for tool testing
- `client_factory` — mocked `ClientFactory` for testing AWS client creation

### Using fixtures in tests

```python
def test_s3_bucket_list(aws: None, s3: object) -> None:
    """Test with mocked S3."""
    s3.create_bucket(Bucket="test-bucket")  # type: ignore[union-attr]
    response = s3.list_buckets()  # type: ignore[union-attr]
    assert len(response["Buckets"]) == 1
```

## Quality Checks

### Linting (ruff)
```bash
uv run ruff check src
```

### Formatting check
```bash
uv run ruff format --check src
```

### Apply formatting
```bash
uv run ruff format src
```

### Type checking (mypy --strict)
```bash
uv run mypy src --strict
```

### All checks
```bash
make check
# or:
uv run ruff check src && uv run ruff format --check src && uv run mypy src --strict && uv run pytest -m "not integration"
```

## Common Patterns

### Test with setup and teardown
```python
class TestExample:
    def setup_method(self) -> None:
        """Run before each test."""
        self.data = []

    def teardown_method(self) -> None:
        """Run after each test."""
        self.data.clear()

    def test_example(self) -> None:
        self.data.append(1)
        assert len(self.data) == 1
```

### Parametrized tests (multiple inputs)
```python
@pytest.mark.parametrize("severity,expected", [
    ("critical", "high"),
    ("high", "high"),
    ("medium", "low"),
])
def test_decision_router_severity(severity: str, expected: str) -> None:
    # Test will run 3 times with different inputs
    pass
```

### Test for exceptions
```python
def test_invalid_log_level() -> None:
    with pytest.raises(ValueError, match="invalid LOG_LEVEL"):
        from chandra.config import Settings
        Settings(log_level="INVALID")
```

### Mark test as expected to fail
```python
@pytest.mark.xfail(reason="TODO: implement feature X")
def test_future_feature() -> None:
    pass
```

## Performance Tips

- **Fast feedback:** `uv run pytest tests/unit/ -x` — stop on first failure
- **Parallel:** `uv run pytest tests/unit/ -n auto` — run in parallel (requires `pytest-xdist`)
- **Selective:** `uv run pytest tests/unit/test_single_file.py` — test one file
- **Focused:** `uv run pytest tests/unit/ -k "keyword"` — test matching pattern

## CI/CD

GitHub Actions runs:
```bash
uv run ruff check src
uv run mypy src --strict
uv run pytest -m "not integration"
```

Every PR must pass all checks before merging.
