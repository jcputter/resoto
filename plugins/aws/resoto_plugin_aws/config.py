from dataclasses import dataclass, field
from typing import List, ClassVar, Optional


@dataclass
class AwsConfig:
    kind: ClassVar[str] = "aws"
    access_key_id: Optional[str] = field(
        default=None,
        metadata={
            "description": "AWS Access Key ID (null to load from env - recommended)"
        },
    )
    secret_access_key: Optional[str] = field(
        default=None,
        metadata={
            "description": "AWS Secret Access Key (null to load from env - recommended)"
        },
    )
    role: Optional[str] = field(
        default=None, metadata={"description": "IAM role name to assume"}
    )
    role_override: bool = field(
        default=False,
        metadata={
            "description": "Override any stored role names (e.g. from remote graphs)"
        },
    )
    account: Optional[List[str]] = field(
        default=None,
        metadata={
            "description": "List of AWS Account ID(s) to collect (null for all if scrape_org is true)"
        },
    )
    region: Optional[List[str]] = field(
        default=None,
        metadata={"description": "List of AWS Regions to collect (null for all)"},
    )
    scrape_org: bool = field(
        default=False, metadata={"description": "Scrape the entire AWS organization"}
    )
    fork: bool = field(
        default=True,
        metadata={"description": "Forked collector process instead of threads"},
    )
    scrape_exclude_account: Optional[List[str]] = field(
        default=None,
        metadata={"description": "List of accounts to exclude when scraping the org"},
    )
    assume_current: bool = field(
        default=False, metadata={"description": "Assume given role in current account"}
    )
    do_not_scrape_current: bool = field(
        default=False, metadata={"description": "Do not scrape current account"}
    )
    account_pool_size: int = field(
        default=5, metadata={"description": "Account thread/process pool size"}
    )
    region_pool_size: int = field(
        default=20, metadata={"description": "Region thread pool size"}
    )
    collect: List[str] = field(
        default_factory=list,
        metadata={"description": "List of AWS services to collect (default: all)"},
    )
    no_collect: List[str] = field(
        default_factory=list,
        metadata={"description": "List of AWS services to exclude (default: none)"},
    )
