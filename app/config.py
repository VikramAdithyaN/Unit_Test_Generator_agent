from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str = ""

    github_token: str = ""
    github_webhook_secret: str = ""

    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    gitlab_webhook_secret: str = ""

    bitbucket_username: str = ""
    bitbucket_app_password: str = ""
    bitbucket_webhook_secret: str = ""

    workspace_dir: str = "/tmp/testgen-workspaces"

    model_name: str = Field(default="gpt-4o-mini", alias="MODEL_NAME")

    max_files_per_pr: int = 20
    max_file_loc: int = 2000
    max_attempts: int = 3


settings = Settings()
