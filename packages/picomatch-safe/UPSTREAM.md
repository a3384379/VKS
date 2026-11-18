# Local build-time picomatch repair

This private package contains the MIT-licensed runtime files from upstream
`picomatch@4.0.7`. The original license and copyright are retained.

Upstream archive: `https://registry.npmjs.org/picomatch/-/picomatch-4.0.7.tgz`

Archive integrity:
`sha512-qcJu88Q2IWqJsDD529JKMdwGm/dvInW4HvQnRwiH9JtihJvzGOscDtHE3x1pBKeUOTysQ8kVmLnJ2kJu7yhcGA==`

## Why it is present

[GHSA-fc5q-39xc-vjpf](https://github.com/advisories/GHSA-fc5q-39xc-vjpf)
reports nested unbounded quantifiers in single-branch repeated extglobs. At
integration time upstream had no published patched version. The suggested Vite
downgrade also produced independent high-severity advisories, so Vite stays at
its existing version.

The only source change is in `lib/parse.js`: extend the existing default literal
fallback to single branches that contain a wildcard or variable-length group.
Plain repeated literals, fixed-width character classes, and the existing safe
character-only star-sequence flattening keep their behavior. Conservatively
rejected repeated patterns are treated as literal strings, as in upstream's
existing recursion safeguard. The explicit `maxExtglobRecursion: false` opt-out
is retained and must not be used for untrusted patterns.

Root npm overrides direct Vite, fdir and tinyglobby consumers to this same local
package. There are no install scripts or additional dependencies. The package
is private and is never published under the upstream name.

`apps/web/tests/picomatch-safety.test.mjs` checks consumer resolution, normal
build globs, and known failing repeated forms in a child process with a hard
deadline. `npm audit` does not assess source changes in this local derivative;
its audit result alone is not evidence that this patch is correct. Keep the
independent regression and full Vite browser/build checks. Replace this package
with an audited upstream release once one is available.
