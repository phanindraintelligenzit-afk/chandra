filepath = "src/chandra/observability/__init__.py"
with open(filepath, 'r') as f:
    content = f.read()

# Add imports at the top if not already there
if "import asyncio" not in content:
    new_content = "import asyncio\nimport functools\n" + content
    with open(filepath, 'w') as f:
        f.write(new_content)
    print("✓ Added missing imports")
else:
    print("✓ Imports already present")
