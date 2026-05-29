filepath = "src/chandra/observability/__init__.py"
with open(filepath, 'r') as f:
    content = f.read()

# Fix logger.info calls
content = content.replace(
    'logger.info("observability.configured", endpoint="noop")',
    'logger.info("observability.configured: noop")'
)

content = content.replace(
    '''logger.info(
        "observability.configured",
        endpoint=otel_endpoint,
        log_level=log_level,
    )''',
    '''logger.info(f"observability.configured: endpoint={otel_endpoint}, level={log_level}")'''
)

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Fixed logger calls in observability/__init__.py")
