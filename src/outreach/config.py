from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_LINKEDIN_USER_DATA_DIR = Path("playwright/chrome-data")


class ScoringWeights(BaseModel):
    existing_connection: int = 100
    usc_marshall: int = 55
    usc_alumni: int = 40
    shared_history: int = 40
    second_degree: int = 30
    indian_background: int = 20
    university_recruiter: int = 18
    product_role: int = 18
    engineering_role: int = 12
    strong_mutuals: int = 10
    adjacent_role: int = -5
    recruiter_penalty: int = -10
    unrelated_role_penalty: int = -15
    third_degree_penalty: int = -10


class SearchStrategy(BaseModel):
    default_limit: int = 10
    final_company_limit: int = 25
    note_generation_limit: int = 15
    hard_company_limit: int = 40
    broad_fallback_min_pool_size: int = 18
    max_pages_high_value: int = 2
    max_pages_default: int = 1
    action_delay_min_ms: int = 1200
    action_delay_max_ms: int = 2600
    ex_companies: list[str] = Field(
        default_factory=lambda: ["Gojek", "Hevo", "Hevo Data", "Intuit", "Optum"]
    )
    shared_history_keywords: list[str] = Field(
        default_factory=lambda: ["thapar", "thapar institute", "thaparian"]
    )
    role_keywords_product: list[str] = Field(
        default_factory=lambda: [
            "product manager",
            "technical product manager",
            "tpm",
            "product owner",
            "product strategy",
            "product lead",
            "group product",
            "director of product",
            "apm",
            "pm ",
            " pm",
        ]
    )
    role_keywords_engineering: list[str] = Field(
        default_factory=lambda: [
            "data engineer",
            "software engineer",
            "senior software engineer",
            "staff engineer",
            "engineering",
            "developer",
            "sde",
            "swe",
            "tech lead",
            "architect",
            "infra",
            "platform",
        ]
    )
    deprioritize_titles: list[str] = Field(
        default_factory=lambda: [
            "recruiter",
            "sourcer",
            "talent",
            "sales",
            "account executive",
            "gtm",
        ]
    )
    adjacent_titles: list[str] = Field(
        default_factory=lambda: [
            "sales",
            "go to market",
            "gtm",
            "business operations",
            "strategy",
            "solutions consultant",
            "solution architect",
            "customer success",
        ]
    )
    pass_definitions: dict[str, dict[str, str | int | bool]] = Field(
        default_factory=lambda: {
            "existing_connections": {"query": "", "limit": 20, "priority": 1, "use_us_location": False, "connection_degree": "1st", "enabled": True},
            "product_usc_marshall": {"query": "product manager", "limit": 10, "priority": 2, "use_us_location": True, "school": "USC Marshall School of Business", "enabled": False},
            "product_usc": {"query": "product manager", "limit": 15, "priority": 3, "use_us_location": True, "school": "University of Southern California", "enabled": True},
            "product_network": {"query": "product manager", "limit": 20, "priority": 4, "use_us_location": True, "connection_degree": "2nd", "enabled": True},
            "engineering_usc_marshall": {"query": "software engineer", "limit": 8, "priority": 5, "use_us_location": True, "school": "USC Marshall School of Business", "enabled": False},
            "engineering_usc": {"query": "software engineer", "limit": 15, "priority": 6, "use_us_location": True, "school": "University of Southern California", "enabled": True},
            "engineering_network": {"query": "software engineer", "limit": 15, "priority": 7, "use_us_location": True, "connection_degree": "2nd", "enabled": True},
            "broad_fallback": {"query": "", "limit": 6, "priority": 8, "use_us_location": True, "connection_degree": "2nd", "enabled": True, "run_if_below_pool_size": 18},
        }
    )


class OutreachSettings(BaseSettings):
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    notion_api_token: str | None = Field(default=None, alias="NOTION_API_TOKEN")
    notion_database_id: str | None = Field(default=None, alias="NOTION_DATABASE_ID")
    linkedin_chrome_user_data_dir: Path = Field(
        default=DEFAULT_LINKEDIN_USER_DATA_DIR,
        alias="LINKEDIN_CHROME_USER_DATA_DIR",
    )
    linkedin_profile_name: str = Field(default="Default", alias="LINKEDIN_PROFILE_NAME")
    linkedin_debug_port: int = Field(default=9222, alias="LINKEDIN_DEBUG_PORT")
    timezone: str = Field(default="America/Los_Angeles", alias="TIMEZONE")
    tracking_workspace_dir: Path = Field(
        default=Path("workspace"),
        alias="TRACKING_WORKSPACE_DIR",
    )
    max_candidates_per_company: int = 40
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    search: SearchStrategy = Field(default_factory=SearchStrategy)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        populate_by_name=True,
        extra="ignore",
    )

    @property
    def artifacts_dir(self) -> Path:
        return Path("artifacts")

    @property
    def fallback_linkedin_user_data_dir(self) -> Path:
        return Path.cwd() / "playwright" / "chrome-data"

    @property
    def resolved_linkedin_user_data_dir(self) -> Path:
        path = self.linkedin_chrome_user_data_dir
        return path if path.is_absolute() else Path.cwd() / path

    @property
    def resolved_tracking_workspace_dir(self) -> Path:
        path = self.tracking_workspace_dir
        return path if path.is_absolute() else Path.cwd() / path

    def using_fallback_linkedin_profile(self) -> bool:
        return (
            not self.linkedin_chrome_user_data_dir.is_absolute()
            and self.linkedin_chrome_user_data_dir == DEFAULT_LINKEDIN_USER_DATA_DIR
        )

    def validate_explicit_linkedin_profile(self) -> None:
        if self.using_fallback_linkedin_profile():
            raise ValueError(
                "LINKEDIN_CHROME_USER_DATA_DIR must point to an explicit absolute Chrome profile path. "
                f"Refusing repo-relative fallback profile: {DEFAULT_LINKEDIN_USER_DATA_DIR}"
            )
