# Unit Test Generator Agent

LangChain + LangGraph agent that, when a Pull/Merge Request is opened on
**GitHub, GitLab, or Bitbucket**, generates regression unit tests for the
change and delivers them as a **follow-up PR** targeting the source branch.
Suspicious behavior changes are flagged as review comments on the original PR.

## Why this design is different

Naive LLM test generation is a tautology: if the LLM reads the PR's new code
and writes tests for it, buggy code gets buggy tests that pass. This agent:

1. **Runs the repo's existing suite first** against the PR head.
   If existing tests fail, the diff broke something a human explicitly tested.
2. **Generates from the base branch, not head.** Regression tests derived from
   the *pre-change* code are run against the *post-change* code. If they fail,
   behavior changed.
3. **Classifies each failure.** An LLM decides whether the change matches the
   PR title/body (**intentional**) or is unjustified (**suspicious**).
   Suspicious ones become inline review comments.
4. **Only fills coverage gaps.** Doesn't regenerate what human tests already
   cover.

## Architecture

```
┌──────────────────┐    webhook    ┌──────────────┐   invoke   ┌───────────────────┐
│ GitHub / GitLab  ├──────────────►│ FastAPI      ├───────────►│  LangGraph agent  │
│ Bitbucket        │  (verified)   │ (app/)       │ background │  (agent/)         │
└──────────────────┘               └───────┬──────┘            └─────────┬─────────┘
                                           │                             │
                                           ▼                             ▼
                                  ┌────────────────┐             ┌───────────────┐
                                  │ Clone repo     │             │ 9-node graph: │
                                  │ (gitpython)    │             │ ingest → run  │
                                  └────────┬───────┘             │ existing suite│
                                           │                     │ → gaps → plan │
                                           ▼                     │ → generate    │
                                  ┌────────────────┐             │ → execute     │
                                  │ Push tests     │             │ → evaluate    │
                                  │ branch + PR    │             │ → classify    │
                                  │ + review flags │             │ → deliver     │
                                  └────────────────┘             └───────────────┘
```

## Setup

```bash
uv sync
cp .env.example .env
# fill in OPENAI_API_KEY, LANGSMITH_*, and the SCM tokens you plan to use
```

## Demo flow — offline (no ngrok, no webhook)

Use the `simulate` command. It clones a repo you provide, runs the full
pipeline, and opens a real PR back to the SCM.

```bash
# GitHub example
uv run testgen simulate \
  --scm github \
  --repo-full-name your-org/your-repo \
  --pr-number 42 \
  --clone-url https://github.com/your-org/your-repo.git \
  --base-ref main \
  --head-ref feature/my-branch \
  --head-sha $(git -C /path/to/local ls-remote origin feature/my-branch | awk '{print $1}') \
  --pr-title "cleanup: tidy is_even" \
  --pr-body "Small refactor."
```

For local-only testing (no push to any SCM), use `generate`:

```bash
uv run testgen generate \
  --repo /tmp/sample_repo \
  --base main \
  --head feature/my-branch \
  --pr-title "cleanup: tidy is_even"
# Tests land at /tmp/sample_repo/generated_tests/
```

## Demo flow — real webhook

1. Start the FastAPI server:
   ```bash
   uv run testgen-server
   # or: uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
2. Expose to the internet:
   ```bash
   ngrok http 8000
   ```
3. Register a webhook in your SCM:
   | SCM | URL | Events |
   |---|---|---|
   | GitHub | `https://<ngrok>/webhook/github` | Pull requests |
   | GitLab | `https://<ngrok>/webhook/gitlab` | Merge request events |
   | Bitbucket | `https://<ngrok>/webhook/bitbucket` | Pull request created/updated |
4. Set the webhook secret to the same value you put in `.env`
   (`GITHUB_WEBHOOK_SECRET` / `GITLAB_WEBHOOK_SECRET` / `BITBUCKET_WEBHOOK_SECRET`).
5. Open a PR. Watch the trace live at https://smith.langchain.com in the
   `unit_test_generator_agent` project.

## What the reviewer sees

| Scenario | Delivery |
|---|---|
| Coverage gaps found, all generated tests pass | Follow-up PR with tests |
| Some generated tests fail + change looks **intentional** (matches PR title/body) | Follow-up PR includes them tagged "documents pre-change behavior" |
| Some generated tests fail + change looks **suspicious** | Follow-up PR + review comment on the ORIGINAL PR flagging the concern |
| No coverage gaps (existing tests already cover the diff) | Comment: "no gaps; nothing to add" |
| Existing suite fails against HEAD | Suite failure is reported prominently in the PR body |

## Layout

```
agent/
  cli.py           # Typer CLI (generate + simulate commands)
  graph.py         # LangGraph StateGraph
  nodes.py         # 9 nodes: ingest, run_existing_suite,
                   #          compute_coverage_gaps, plan, generate,
                   #          execute, evaluate, classify_failure, deliver
  prompts.py       # Versioned prompts
  state.py         # AgentState + related TypedDicts
  sandbox.py       # Subprocess pytest runners
  git_utils.py     # git ref/diff helpers
  tools.py         # Language detection, test-path conventions

app/
  main.py          # FastAPI entry
  routes.py        # /webhook/{github,gitlab,bitbucket}
  pipeline.py      # end-to-end: clone → graph → push → PR → comments
  config.py        # pydantic-settings from .env
  models.py        # PRPayload, DeliveryReport
  git_ops.py       # clone + push (gitpython)
  scm/
    base.py        # SCMClient interface
    github.py      # PyGithub
    gitlab.py      # python-gitlab
    bitbucket.py   # REST via httpx
    factory.py     # get_client(scm)
```

## Milestones remaining

| # | What | Status |
|---|---|---|
| M1a | LangGraph skeleton + CLI | ✅ |
| M1b | Base-vs-head, existing-suite-first, gap-only, classifier | ✅ |
| M2  | FastAPI webhook + SCM adapters + follow-up PR | ✅ |
| M3  | Docker sandbox for isolated test execution | ⏳ |
| M4  | JS/TS + Java tiers (jest, mvn) | ⏳ |
| M5  | GitLab + Bitbucket production hardening | ⏳ (adapters exist; needs field testing) |
| M6  | Real coverage instrumentation (pytest-cov), `.testgen.yml` per-repo config, rate limiting | ⏳ |

## Troubleshooting

- **`git diff` fails** in the pipeline: base ref not fetched. Ensure the
  webhook payload includes a valid `base.ref` and the token has clone access.
- **PyGithub 401**: check `GITHUB_TOKEN` scope includes `repo` (private) or
  `public_repo` (public).
- **Bitbucket 403 on comments**: app password missing `pullrequest:write`.
- **pytest not found**: the target repo doesn't have pytest installed; the
  M1 sandbox uses your system Python. M3 (Docker sandbox) fixes this.
