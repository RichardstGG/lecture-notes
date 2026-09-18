"""Run the local-only UI backend with `python -m ui.backend`."""
import argparse

import uvicorn


def main(argv=None):
    parser = argparse.ArgumentParser(description="Lecture Notes local UI backend")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    uvicorn.run("ui.backend.app:app", host="127.0.0.1", port=args.port, reload=False)


if __name__ == "__main__":
    main()
