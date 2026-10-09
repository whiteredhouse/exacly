# Model-diff pilot: how the stimuli were built

This records how the two pilot changes for the [developer test kit](model-diff-developer-test-kit.md) were chosen and turned into a code view and a model-level view. It applies the kit's fixed recipe to a real Python codebase and notes every judgement call the recipe needed.

The stimuli come from a client codebase, so they are kept out of this repository. Here the client codebase is called "the source repo", and the two changes are A and B.

## Choosing the changes

1. **Agent-made evidence.** Only squash-merged commits whose message carries a Claude `Co-authored-by` trailer and an agent-usage line were considered. Most commits in the source repo qualify.
2. **Size and spread.** A script classified every agent-made commit from the last three months by the files it changed under the backend source root. Candidates had 4 to 10 changed source files, 150 to 500 changed source lines, at least one changed test file, and either 3 or more components touched or a new dependency between components.
3. **Pair.** From the shortlist, two changes from the same fortnight were picked. Both have one clear user goal, a real edge case, and a size within 2× of the other.
4. **Swap.** The first A was replaced because its main tests needed a private container image that could not be pulled, so half of its behaviour could not be traced. The replacement is from the same shortlist.

## Applying the recipe

1. **Components.** The source repo keeps an architecture file that maps every module path to a domain by glob rules (first match wins). The domains are the components. The file is read at the change's own commit; for a change older than the file, its first version is used. A module no rule matches becomes its own component, named `unmapped:<path>`.
2. **Structure diagram.**
   - Import edges come from `import` statements in the changed files, parsed with Python's `ast`, at the parent and at the commit.
   - Nodes are the components of the changed files, plus any component reached by an added or removed edge.
   - Unchanged edges are drawn only between those nodes. Other neighbours are left out to keep it to one page.
   - Added edges are bold, removed edges would be dashed, and new modules get a dashed border.
3. **Interface diff.** Public (no leading underscore) top-level functions, classes, methods and class fields whose signature was added, removed or changed, shown as before and after. Bodies and private helpers are left out.
4. **Agent summary.** The commit message body (the squashed PR description), cut before the "Generated with Claude Code", agent-usage and co-author lines. It is shown in both views.
5. **Code view.** Every diff under the backend source root. Migrations, tests, docs, frontend and infrastructure are left out of both views.

## Tracing behaviour

1. **Where.** On the developer's machine, in `git archive` copies of the parent and the commit, so the checkout is never touched. Dependencies come from the lock file, and Postgres and Redis come from the repo's own test containers.
2. **Which tests.** After: every test file the commit touched, run at the commit. Before: those of the same files that already existed, run at the parent. Run serially, since the profiler collected no tests under `pytest-xdist`.
3. **What is recorded.** A pytest plugin outside the copies sets `sys.setprofile` and `threading.setprofile` and records (caller, callee) for each call where both frames are in the backend package and their components differ. Calls into Redis, Temporal and the model client library are recorded as `external:<lib>`. Calls from test code are not counted.
4. **Marking.** A call is marked **(new)** only when its call site is on an added line of the diff. A call first reached by the new tests, from code that already existed, is drawn plain.
5. **Noise.** Calls the Temporal workflow sandbox makes while importing modules (`<module>`, `_ThreadLocalCallable`, `_RestrictedProxy`) are dropped and counted.
6. **Drawing.** The trace gives call pairs, not order, so the diagram orders them along the traced call chain. The main path is the chain with the most new calls; where chains tie, they are shown side by side. Other new or newly reached calls are listed under the diagram. Anything the tests did not reach is stated as untraced, never drawn from reading the code.

## What the structure step cannot see

- Imports re-exported through a package `__init__` do not show as edges to the real module.
- Calls made by name, such as Temporal activities started as `"activity_name"`, have no import at all.
- A new dependency between components that already depended on each other does not show at component level.

In change B, the main new path ran through activities called by name, so only the behaviour trace showed it. In change A, the one new component dependency was a lookup the unit tests stub out, so neither step showed it well.

## Imbalances in the pair

Record these on the result sheet:

- A's code view is longer (749 against 513 diff lines), mostly prompt wording. B's is mostly new logic.
- A adds one dependency between components and no new module. B adds a new module and three dependencies, so A's structure view says less.
- A's main new dependency (a locale lookup) was not traced: unit tests stub it, and the integration test could not start.
- B involves Temporal and Redis concurrency and includes a second, related fix, so it is likely the harder change.

## Lessons for Exacly

- A domain map the codebase already maintains gives components for free and is better than package boundaries. Most codebases will not have one.
- Import-level structure diffs are thin for typical changes. In both changes, the behaviour trace carried most of what the model view showed.
- Test-driven tracing depends on the tests reaching the changed code with real services. Stubs and unavailable containers leave gaps that the view has to state openly.
- Tracing needs the repo's own environment, so the extraction has to run where the developer's tests run.
