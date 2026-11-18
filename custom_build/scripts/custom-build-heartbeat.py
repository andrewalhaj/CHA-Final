"""Hermes cron entry point for the CHA Final heartbeat."""

import sys

from hermes_cli.main import main

if __name__ == "__main__":
    sys.argv = ["hermes", "custom-build", "heartbeat"]
    main()
