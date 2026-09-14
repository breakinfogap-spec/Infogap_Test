from __future__ import annotations

import argparse
import os
import sys

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

LOCAL_REQUIRED = [
    "NEWS_TIMEZONE",
    "NEWS_RETENTION_DAYS",
]

SMOKE_REQUIRED = [
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_CLOUD_PROJECT_ID",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_R2_BUCKET",
    "HEALTHCHECKS_PING_URL",
]


def main() -> int:
    if load_dotenv:
        load_dotenv()

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["local", "smoke", "daily"], default="local")
    args = parser.parse_args()

    os.environ.setdefault("NEWS_TIMEZONE", "America/Vancouver")
    os.environ.setdefault("NEWS_RETENTION_DAYS", "5")

    required = LOCAL_REQUIRED if args.mode == "local" else LOCAL_REQUIRED + SMOKE_REQUIRED
    missing = [name for name in required if not os.getenv(name)]

    if missing:
      print("Missing required environment variables:")
      for name in missing:
          print(f"- {name}")
      return 1

    print(f"Configuration check passed for mode={args.mode}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
