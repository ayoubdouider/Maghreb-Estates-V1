# Tools

Python scripts that do the actual work — API calls, data transformations, file operations. Each script follows a simple contract: accept inputs (via args or stdin), perform one deterministic task, write outputs to `.tmp/` or return them to stdout. No side effects outside `.tmp/`. Scripts should be runnable standalone so they're easy to test in isolation.
