# Third-Party Notices

Version 0.4.0 has no required third-party runtime dependency and contains no copied third-party source code. It uses Python's standard-library `sqlite3`, `json`, `argparse`, and related modules.

The optional `parsing` extra can install `pypdf` for PDF text extraction and `pywin32` on Windows for local document automation. These packages are not imported by the dependency-free core and remain governed by their own licenses and installed versions.

The schema retains scheduler fields so a future implementation may integrate a separately licensed spaced-repetition library. The current `simple-v1` scheduler is project code and does not claim to implement FSRS.

Conceptual references for future evaluation (not bundled, imported, or copied):

- Free Spaced Repetition Scheduler: <https://github.com/open-spaced-repetition/free-spaced-repetition-scheduler> (MIT license; no version pinned because it is not a dependency).
- Experience API (xAPI) specification: <https://github.com/adlnet/xAPI-Spec> (Apache-2.0 license; not implemented as a compatibility claim).

Before adding any dependency, record its repository, exact version or commit, license, copied/modified files, and transitive-license impact here. GPL/AGPL code requires an explicit project-level license decision before use.
