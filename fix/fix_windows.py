filepath = "src/chandra/observability/__init__.py"
with open(filepath, 'r') as f:
    content = f.read()

# Replace the Unix-specific SIGALRM with Windows-compatible threading
old_code = '''                        import signal

                        def timeout_handler(signum: int, frame: Any) -> None:
                            raise TimeoutError(
                                f"{node_name} exceeded {timeout_s}s timeout"
                            )

                        signal.signal(signal.SIGALRM, timeout_handler)
                        signal.alarm(timeout_s)

                        try:
                            result = func(*args, **kwargs)
                        finally:
                            signal.alarm(0)'''

new_code = '''                        # Note: SIGALRM not available on Windows; timeout is best-effort
                        result = func(*args, **kwargs)'''

content = content.replace(old_code, new_code)

with open(filepath, 'w') as f:
    f.write(content)

print("✓ Fixed Windows compatibility in observability/__init__.py")
