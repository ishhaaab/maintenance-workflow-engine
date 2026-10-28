# Third-party software

This repository's original code is MIT licensed. Dependencies retain their own licenses; installation does not relicense them. Exact Python package versions, distributions and hashes are recorded in `uv.lock`.

Principal runtime dependencies include FastAPI, Pydantic, PyMongo, redis-py, HTTPX and Uvicorn. SQLite is supplied by Python. Container images bundle additional upstream software under their respective terms. MongoDB Community Server is distributed under the SSPL; Redis 7.2 is distributed under its upstream BSD license. Python's image includes CPython and Debian packages with their upstream licenses.

No model weights are included. A locally served or remote model has separate provider/model terms. Review those before downloading weights or publishing evaluation artifacts.
