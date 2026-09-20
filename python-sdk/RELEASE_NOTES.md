# 0.1.0rc10

This release publishes the workspace command surface previously available in newer source checkouts and Desktop bundles but absent from the PyPI rc9 wheel.

- Adds pinned target resolution with `tangram-app context resolve` and installed-app discovery and invocation with `tangram-app app search`, `inspect`, and `call`.
- Adds durable invocation status and resume, Desktop-mediated approvals, workspace knowledge commands, and installed-app skill projection.
- Adds the unified `tangram` entry point and packaged SQL skill, alongside lifecycle, endpoint parsing, and credential matching fixes.
- Keeps the local app development workflow. Workspace operations require a compatible Tangram server and native CLI; interactive approvals require Desktop. Installing this wheel does not update those components.

Install with `python -m pip install --upgrade tangram-app-sdk==0.1.0rc10`.

The release workflow checks the workspace command help from a clean wheel installation before publishing, preventing source-only availability from being mistaken for released capabilities.
