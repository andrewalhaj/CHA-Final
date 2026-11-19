"""Hermes cron entry point for the CHA Final host-health check."""

import sys

from hermes_cli.main import main

if __name__ == "__main__":
    sys.argv = ["hermes", "custom-build", "job-health"]
    main()
