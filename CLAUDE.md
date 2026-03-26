# edgar-gmr-etl conventions

See also: [/config/repos/CLAUDE.md](/config/repos/CLAUDE.md) for workspace-wide rules.

## Full gate (this repo)
```
pytest          # all unit tests must pass
pylint src tests  # must be clean (or pre-existing score must not regress)
```
Both must pass. Fix failures before committing.

## Clean repo
`git status` must show `nothing to commit, working tree clean` when done.
