"""
Every SQL statement in the application (MA-1).

A route used to be the only place a query existed, which is what made "routes
mix HTTP, SQL, filesystem and subprocess management" a maintainability finding
rather than a style complaint: nothing below HTTP could be read, reused or
tested on its own, and the same statement was written twice in three places.

**Every function here takes a cursor. None of them opens one.** That is the
load-bearing rule of this package, and it is not a style preference:

* `infra.db.db_cursor()` is **one transaction per `with` block**. A repository
  that opened its own cursor would put each statement in its own transaction,
  so a caller that needs two writes to commit together - the absent register
  (FS-4), the correction and its audit row (FS-10) - could not express that at
  all, and the atomicity those findings turn on would be gone with no visible
  change at any call site.
* The caller decides the boundary because the caller is the only thing that
  knows what "together" means.

`tests/test_db_access.py` enforces this at the source level over every
first-party module, so a repository that grows a `db_cursor()` fails the build.
"""
