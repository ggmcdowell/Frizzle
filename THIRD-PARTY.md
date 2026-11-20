# Third-party software

This repository holds no third-party source code. `LICENSE` covers the files here. The programs
and packages below are not in this repository. Each one keeps its own license or terms.

## Python packages

Setup builds the hub environment from `uv.lock`. The lock file pins the versions. It also lists
the packages that these packages need.

| Package | Used by | License |
|---|---|---|
| mcp | hub | MIT |
| anyio | hub | MIT |
| openai (Python client) | mem embeddings | Apache-2.0 |
| pytest (dev group) | the test runner | MIT |
| gdtoolkit (dev group) | gdformat in the Godot pack pre-commit hook | MIT |
| sqlite-vec | mem vector search | MIT or Apache-2.0 |
| httpx (through mcp) | hub | BSD-3-Clause |
| starlette (through mcp) | hub | BSD-3-Clause |
| uvicorn (through mcp) | hub | BSD-3-Clause |

## Programs and services

The hub starts or calls these as separate programs. It does not include or change them.

| Program or service | Use | License or terms |
|---|---|---|
| Homebrew | installs gitleaks and uv | BSD-2-Clause |
| trash (`/usr/bin/trash` on macOS) | every delete the hub makes | Apple terms |
| `/usr/bin/python3` (macOS) | the JSON reads of `Drawer/e2e.sh` | Apple terms |
| git | every repository step and hook | GPL-2.0 and other licenses |
| gitleaks (Homebrew) | the secret scan in the pre-commit hook | MIT |
| uv | the Python environments | Apache-2.0 or MIT |
| CPython 3.12 or newer | the hub interpreter | PSF-2.0 |
| Claude Code | sessions and workflow legs | Anthropic terms |
| OpenAI embeddings API | the mem card embeddings (optional) | OpenAI terms |
| Godot (Homebrew cask) | the Godot pack gate (optional) | MIT |
| GitHub CLI (`gh`) | the GitHub sign-in setup suggests (optional) | MIT |
| GitHub Actions `actions/checkout` | the CI workflow | MIT |

The hub also calls the macOS tool `getconf`. Apple terms cover it.
