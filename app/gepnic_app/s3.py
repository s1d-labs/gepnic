"""Object storage clients for GePNIC's own buckets: "r2" (bundles, older pages) and "b2" (documents), keys from the environment."""
from . import config

_clients = {}


def ready(store: str) -> bool:
    if store == "b2":
        return bool(config.B2_BUCKET and config.B2_ENDPOINT and config.B2_ACCESS_KEY_ID)
    return bool(config.R2_BUCKET and config.R2_ENDPOINT and config.R2_ACCESS_KEY_ID)


def client(store: str = "r2"):
    """(boto3 client, bucket), created once per process."""
    if store not in _clients:
        import boto3
        from botocore.config import Config
        cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"})
        if store == "b2":
            _clients[store] = (boto3.client("s3", endpoint_url=config.B2_ENDPOINT, aws_access_key_id=config.B2_ACCESS_KEY_ID,
                                            aws_secret_access_key=config.B2_SECRET_ACCESS_KEY, region_name=config.B2_REGION or None, config=cfg),
                               config.B2_BUCKET)
        else:
            _clients[store] = (boto3.client("s3", endpoint_url=config.R2_ENDPOINT, aws_access_key_id=config.R2_ACCESS_KEY_ID,
                                            aws_secret_access_key=config.R2_SECRET_ACCESS_KEY, region_name="auto", config=cfg), config.R2_BUCKET)
    return _clients[store]
