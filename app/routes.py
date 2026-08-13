from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.pipeline import run_pipeline
from app.scm.bitbucket import BitbucketClient
from app.scm.github import GitHubClient
from app.scm.gitlab import GitLabClient

log = logging.getLogger(__name__)

router = APIRouter()


def _headers_lc(request: Request) -> dict:
    return {k.lower(): v for k, v in request.headers.items()}


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.post("/webhook/github")
async def github_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    headers = _headers_lc(request)
    client = GitHubClient()
    if not client.verify_signature(headers, body):
        raise HTTPException(status_code=401, detail="invalid signature")
    payload = client.parse_webhook(headers, json.loads(body))
    if not payload:
        return {"status": "ignored"}
    log.info("accepted github PR #%s on %s", payload.pr_number, payload.repo_full_name)
    background.add_task(_safe_run, payload_dict=payload.__dict__)
    return {"status": "accepted", "pr_number": payload.pr_number}


@router.post("/webhook/gitlab")
async def gitlab_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    headers = _headers_lc(request)
    client = GitLabClient()
    if not client.verify_signature(headers, body):
        raise HTTPException(status_code=401, detail="invalid token")
    payload = client.parse_webhook(headers, json.loads(body))
    if not payload:
        return {"status": "ignored"}
    log.info("accepted gitlab MR !%s on %s", payload.pr_number, payload.repo_full_name)
    background.add_task(_safe_run, payload_dict=payload.__dict__)
    return {"status": "accepted", "mr_number": payload.pr_number}


@router.post("/webhook/bitbucket")
async def bitbucket_webhook(request: Request, background: BackgroundTasks):
    body = await request.body()
    headers = _headers_lc(request)
    client = BitbucketClient()
    if not client.verify_signature(headers, body):
        raise HTTPException(status_code=401, detail="invalid secret")
    payload = client.parse_webhook(headers, json.loads(body))
    if not payload:
        return {"status": "ignored"}
    log.info("accepted bitbucket PR #%s on %s", payload.pr_number, payload.repo_full_name)
    background.add_task(_safe_run, payload_dict=payload.__dict__)
    return {"status": "accepted", "pr_number": payload.pr_number}


def _safe_run(payload_dict: dict) -> None:
    from app.models import PRPayload

    payload = PRPayload(**payload_dict)
    try:
        run_pipeline(payload)
    except Exception:
        log.exception("pipeline failed for %s PR #%s", payload.scm, payload.pr_number)
