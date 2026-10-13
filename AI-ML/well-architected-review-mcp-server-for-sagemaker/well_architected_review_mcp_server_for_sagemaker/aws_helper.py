# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""AWS helper for the Well-Architected Review MCP Server for Amazon SageMaker."""

import boto3
import os
import time
from well_architected_review_mcp_server_for_sagemaker import __version__
from well_architected_review_mcp_server_for_sagemaker.consts import SUPPORTED_REGIONS
from botocore.config import Config
from loguru import logger
from pydantic import validate_call
from typing import Any, Dict, Optional, cast, get_args


class AwsHelper:
    """Helper class for AWS operations.

    Provides utility methods for interacting with AWS services,
    including region and profile management and client creation.

    Implements a singleton pattern with a client cache to avoid
    creating multiple clients for the same service.
    """

    _instance = None
    _client_cache: Dict[str, Any] = {}
    _cache_metadata: Dict[str, float] = {}
    _cache_ttl: int = 1800  # 30 minutes
    _cache_max_size: int = 100

    @staticmethod
    def get_aws_region() -> Optional[SUPPORTED_REGIONS]:
        """Get the AWS region from the environment if set."""
        region = os.environ.get('AWS_REGION')
        return cast(SUPPORTED_REGIONS, region) if region in get_args(SUPPORTED_REGIONS) else None

    @staticmethod
    def get_aws_profile() -> Optional[str]:
        """Get the AWS profile from the environment if set."""
        return os.environ.get('AWS_PROFILE')

    @classmethod
    @validate_call
    def create_boto3_client(
        cls, service_name: str, region_name: Optional[SUPPORTED_REGIONS] = None
    ) -> Any:
        """Create or retrieve a cached boto3 client.

        Configured with a custom user agent suffix to identify API calls
        made by this MCP server. Clients are cached for performance.

        Args:
            service_name: The AWS service name (e.g., 'sagemaker')
            region_name: Optional region name override

        Returns:
            A boto3 client for the specified service
        """
        try:
            region: Optional[SUPPORTED_REGIONS] = (
                region_name if region_name is not None else cls.get_aws_region()
            )
            profile = cls.get_aws_profile()
            cache_key = f'{service_name}+{region_name}'

            current_time = time.time()
            if cache_key in cls._client_cache:
                if cache_key in cls._cache_metadata:
                    cache_time = cls._cache_metadata[cache_key]
                    if current_time - cache_time < cls._cache_ttl:
                        logger.info(
                            f'Using cached boto3 client for {service_name} in {region_name}'
                        )
                        return cls._client_cache[cache_key]
                    else:
                        logger.info(
                            f'Cache expired for {service_name} in {region_name}, creating new client'
                        )
                        del cls._client_cache[cache_key]
                        del cls._cache_metadata[cache_key]
                else:
                    del cls._client_cache[cache_key]

            config = Config(
                user_agent_extra=f'md/well-architected-review-mcp-server-for-sagemaker#{__version__}'
            )

            if profile:
                session = boto3.Session(profile_name=profile)
                if region is not None:
                    client = session.client(service_name, region_name=region, config=config)
                else:
                    client = session.client(service_name, config=config)
            else:
                if region is not None:
                    client = boto3.client(service_name, region_name=region, config=config)
                else:
                    client = boto3.client(service_name, config=config)

            if len(cls._client_cache) >= cls._cache_max_size:
                oldest_key = min(cls._cache_metadata.keys(), key=lambda k: cls._cache_metadata[k])
                logger.info(f'Cache size limit reached, evicting oldest entry: {oldest_key}')
                del cls._client_cache[oldest_key]
                del cls._cache_metadata[oldest_key]

            cls._client_cache[cache_key] = client
            cls._cache_metadata[cache_key] = current_time

            logger.info(f'Created and cached new boto3 client for {service_name} in {region_name}')
            return client
        except Exception as e:
            raise Exception(f'Failed to create boto3 client for {service_name}: {str(e)}')
