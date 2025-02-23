from resotolib.args import ArgumentParser
from resotoworker.__main__ import add_args
from resotolib.core import add_args as core_add_args, resotocore


def test_args():
    arg_parser = ArgumentParser(
        description="resoto worker",
        env_args_prefix="RESOTOWORKER_",
    )
    add_args(arg_parser)
    core_add_args(arg_parser)
    arg_parser.parse_args()
    assert resotocore.http_uri == "http://localhost:8900"
