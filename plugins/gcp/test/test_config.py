from resotolib.config import Config
from resoto_plugin_gcp import GCPCollectorPlugin


def test_args():
    config = Config("dummy", "dummy")
    GCPCollectorPlugin.add_config(config)
    Config.init_default_config()
    assert len(Config.gcp.service_account) == 0
    assert len(Config.gcp.project) == 0
    assert len(Config.gcp.collect) == 0
    assert len(Config.gcp.no_collect) == 0
    assert Config.gcp.project_pool_size == 5
    assert Config.gcp.fork is True
