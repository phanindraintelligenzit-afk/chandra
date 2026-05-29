filepath = "src/chandra/observability/__init__.py"
with open(filepath, 'r') as f:
    content = f.read()

# Add _emit_metric function at the end
new_func = '''

def _emit_metric(metric_name: str, value: float, **tags: Any) -> None:
    """Emit a metric to CloudWatch."""
    logger.info(f"metric.emit: {metric_name}={value}", extra=tags)
'''

with open(filepath, 'w') as f:
    f.write(content + new_func)

print("✓ Added _emit_metric function")
