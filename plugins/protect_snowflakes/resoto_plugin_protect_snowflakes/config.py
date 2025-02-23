from dataclasses import dataclass, field
from typing import ClassVar, Dict, List


default_config = {
    "aws": {
        "110465657741": {
            "us-east-1": {"aws_ec2_instance": ["i-0fcbe8974615bfd37"]},
        },
    },
}


@dataclass
class ProtectSnowflakesConfig:
    kind: ClassVar[str] = "plugin_protect_snowflakes"
    enabled: bool = field(
        default=False,
        metadata={"description": "Enable plugin?"},
    )
    config: Dict[str, Dict[str, Dict[str, Dict[str, List[str]]]]] = field(
        default_factory=lambda: default_config,
        metadata={
            "description": (
                "Configuration for the plugin\n"
                "See https://github.com/someengineering/resoto/tree/main/plugins/protect_snowflakes for syntax details"
            )
        },
    )

    @staticmethod
    def validate(cfg: "ProtectSnowflakesConfig") -> bool:
        config = cfg.config
        if not isinstance(config, dict):
            raise ValueError("Config is no dict")

        for cloud_id, account_data in config.items():
            if not isinstance(cloud_id, str):
                raise ValueError(f"Cloud ID {cloud_id} is no string")
            if not isinstance(account_data, dict):
                raise ValueError(f"Account Data {account_data} is no dict")

            for account_id, region_data in account_data.items():
                if not isinstance(account_id, str):
                    raise ValueError(f"Account ID {account_id} is no string")
                if not isinstance(region_data, dict):
                    raise ValueError(f"Region Data {region_data} is no dict")

                for region_id, resource_data in region_data.items():
                    if not isinstance(region_id, str):
                        raise ValueError(f"Region ID {region_id} is no string")
                    if not isinstance(resource_data, dict):
                        raise ValueError(f"Resource Data {resource_data} is no dict")

                    for kind, resource_list in resource_data.items():
                        if not isinstance(kind, str):
                            raise ValueError(f"Resource Kind {kind} is no string")
                        if not isinstance(resource_list, list):
                            raise ValueError(
                                f"Resource List {resource_list} is no list"
                            )

                        for resource_id in resource_list:
                            if not isinstance(resource_id, str):
                                raise ValueError(
                                    f"Resource ID {resource_id} is no string"
                                )
        return True
