"""Container-local readiness probe; no credentials or provider calls."""
import os
import urllib.request


def main():
    port = int(os.environ.get('JARVIS_PORT', '8000'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f'http://127.0.0.1:{port}/health', timeout=3) as response:
        if response.status != 200:
            raise SystemExit(1)


if __name__ == '__main__':
    main()
