"""Cloud Run entrypoint.

Kept to a single line of behaviour so the buildpack's `uvicorn main:app` target
stays stable while everything real lives in the `nano` package.
"""

from nano.app import create_app

app = create_app()
