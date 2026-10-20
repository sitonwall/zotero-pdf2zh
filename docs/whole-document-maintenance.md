# Whole-document fork maintenance

This is an AGPL-3.0 downstream fork of guaguastandup/zotero-pdf2zh.
The original plugin, server entry point, license, and upstream authorship remain
intact. New opt-in code lives in `server/whole_document/`.

## Branches and public review

- `main`: preserved upstream-based branch. Do not force-push over prior work.
- `feat/whole-document-translation`: reviewable feature diff based on upstream
  `c2fecf45496b51f774c5c811a8bafb57f1f36bcd`.
- `whole-document`: maintained user-facing branch, including this fork's README.
- [Draft PR #1](https://github.com/sitonwall/zotero-pdf2zh/pull/1) holds the feature
  review. [Upstream issue #369](https://github.com/guaguastandup/zotero-pdf2zh/issues/369)
  records the proposal. Upstream's `collaborators_only` PR creation policy blocks
  external PR submission; an upstream collaborator can import the branch if wanted.
- [BabelDOC #629](https://github.com/funstory-ai/BabelDOC/pull/629) contains the
  isolated line-wrap fix. The callback stays in the pinned downstream engine fork.

## Validated checks

The dedicated [Whole-document worker run](https://github.com/sitonwall/zotero-pdf2zh/actions/runs/39461657150)
passed on both Windows and Ubuntu, including locked installation, 34 regressions,
lint, real PDF rendering with synthetic responses, and actual hidden HTTP workers
with a local fake model. No paid credentials are present in CI.

The inherited generic CI currently fails in unchanged plugin/environment-manager
checks. The [upstream base run](https://github.com/guaguastandup/zotero-pdf2zh/actions/runs/35908188155)
and [feature-branch run](https://github.com/sitonwall/zotero-pdf2zh/actions/runs/39461657019)
both show those failures. They are recorded separately and are not hidden or
counted as passing. Existing Windows Environment CI also passed on the feature.

## Updating

Fetch upstream and open an integration branch from `whole-document`. Merge or
cherry-pick deliberately; resolve conflicts before updating the maintained branch.
Run the commands in the worker README and review dedicated CI on Windows/Linux.
Keep upstream adaptation separate from behavior changes.

The engine is pinned by full Git commit in `pyproject.toml` and `uv.lock`.
Update both together, rerun the tests, and document changes to the callback or
protected-token policy. Checkpoint context includes channel/planning settings;
never reset dispatch reservations or copy checkpoints blindly to bypass budgets.

For each prerelease, tag a reviewed commit, include source/lock/license and an
artifact hash manifest, and link validation records. Preserve old tags and
releases. Keep private configuration, PDFs, source IR, and usage logs outside Git.
Real-model testing always requires an explicit monetary limit; CI is offline/local.

The current version is experimental. Author translation approval, actual Zotero
attachment acceptance, whole/chapter comparison, and production activation remain
open gates. Repository maintenance does not imply that these gates passed.
