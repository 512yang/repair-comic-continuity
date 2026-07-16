# Source baseline

- GitHub repository: `https://github.com/512yang/repair-comic-continuity.git`
- Source branch: `agent/continuity-v4-implementation`
- Source commit: `64c86409cf92e4e4f7833d3511b3bb0704665c97`
- Downloaded archive SHA-256: `b54d3e3cca02b30bf1650a9fba75b2c69e13ebaf682f8465c9fe197a14341f2c`
- Local feature branch: `agent/workbench-plugin-v1`

The public source snapshot omitted five files present in the signed V5.5 release.
Those exact signed files were restored in commit `4c010f9`; the packaged release
certificate then validated and all 596 Python tests passed with two skips.
