#!/usr/bin/env python3
"""Run the configured canonical Mongo bootstrap."""
import subprocess
from seed_mongo import main


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            raise SystemExit('Mongo bootstrap/check failed; private output withheld. Inspect the owned deployment before retrying.') from None
        raise SystemExit(str(error)) from None
