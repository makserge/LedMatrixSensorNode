"""python -m radar_zone_learner: starts the app (shell.service.main)."""
import sys

from radar_zone_learner.shell.service import main

if __name__ == "__main__":
    sys.exit(main())
