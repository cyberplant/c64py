## External VICE Test Assets

This directory is intentionally kept out of git because it is third-party code.

Fetch a pinned upstream snapshot before running VICE compatibility tests:

```bash
./scripts/fetch_vice_tests.sh
```

Defaults:
- source: `https://github.com/VICE-Team/svn-mirror.git`
- ref: `master`
- subdir: `testprogs`

You can override at runtime:

```bash
VICE_GIT_REF=master VICE_TESTPROGS_SUBDIR=testprogs ./scripts/fetch_vice_tests.sh
```
