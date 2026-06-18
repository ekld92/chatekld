"""Allow ``python -m audit`` to run the diagnostic CLI."""
from audit.scan import main


if __name__ == "__main__":
    raise SystemExit(main())
