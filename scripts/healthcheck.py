"""Container HEALTHCHECK probe.

Checks that the FastAPI service inside this container answers its
liveness endpoint. Deliberately does NOT call AWS or Postgres: a
container healthcheck must reflect the health of *this* container, and
external-dependency status is reported separately by /health/ready.

Exit code 0 = healthy, 1 = unhealthy (Docker restarts the container
after the configured retries).
"""

import sys
import urllib.error
import urllib.request

HEALTH_URL = "http://127.0.0.1:6001/health"


def main() -> int:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=5) as response:
            if response.status == 200:
                print("healthy")
                return 0
            print(f"unhealthy: HTTP {response.status}")
            return 1
    except (urllib.error.URLError, OSError) as exc:
        print(f"unhealthy: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
